import asyncio
import json
import logging
import os
import re
import time
import uuid

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agents.contact_update import run_contact_update_agent
from app.agents.conversational import run_agent, CLOSING_FOLLOWUP_MARKER
from app.agents.funnel import run_funnel_agent
from app.core.config import get_settings
from app.core.error_webhook import send_error_to_webhook
from app.core.kapso_debug import (
    add_kapso_debug_event,
    get_kapso_debug_events,
    mask_secret,
    subscribe_sse,
    unsubscribe_sse,
)
from app.core.kapso_prompt import build_kapso_context_payload, build_kapso_system_prompt
from app.db import queries as db
from app.db.client import get_supabase
from app.schemas.chat import AgentRunTrace, ChatRequest, MCPServerConfig, TimingInfo, ToolCall
from app.schemas.channel import ChannelInboundMessage
from app.schemas.contact_update import ContactUpdateAgentRequest, ContactUpdateAgentResponse
from app.schemas.funnel import FunnelAgentRequest, FunnelAgentResponse
from app.schemas.kapso import KapsoInboundRequest, KapsoInboundResponse, KapsoReactionPayload
from app.services.channel_adapter import normalize_kapso_inbound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kapso", tags=["kapso"])
FUNNEL_TIMEOUT_SECONDS = 25
CONTACT_UPDATE_TIMEOUT_SECONDS = 20
DEFAULT_EMPTY_REPLY_TEXT = "Hola, te leo. ¿En qué puedo ayudarte?"
FUNNEL_SKIP_TEXTS = {
    "hola",
    "hola!",
    "hi",
    "hello",
    "buenos dias",
    "buen día",
    "buen dia",
    "buenas tardes",
    "buenas noches",
    "ok",
    "oki",
    "dale",
    "gracias",
    "muchas gracias",
}
MULTIMEDIA_EXTENSIONS = (
    ".ogg",
    ".mp3",
    ".wav",
    ".mp4",
    ".avi",
    ".mov",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
)
MULTIMEDIA_URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Regex to parse Kapso media enrichment text
_MEDIA_URL_RE = re.compile(r"URL:\s*(https?://[^\s]+)", re.IGNORECASE)
_AUDIO_TRANSCRIPT_RE = re.compile(r"Transcript:\s*(.+)", re.IGNORECASE | re.DOTALL)
_MEDIA_FILENAME_RE = re.compile(r"\(([^)]+\.[a-z0-9]{2,5})\)", re.IGNORECASE)
SUPABASE_STORAGE_BUCKET = "multimedia"
SUPABASE_EDGE_CREAR_MULTIMEDIA = "crear-multimedia-inicial-v1"
SUPABASE_EDGE_GUARDAR_MULTIMEDIA = "guardar-multimedia-v4"
VISION_MODEL = "google/gemini-2.5-flash"
VISION_MAX_CONCURRENT = 3  # Max parallel vision calls to avoid rate limits
VISION_MAX_RETRIES = 3
VISION_BASE_BACKOFF = 2  # seconds
_vision_semaphore = asyncio.Semaphore(VISION_MAX_CONCURRENT)
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"}


def _normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\D+", "", str(value))
    if normalized.startswith("00"):
        normalized = normalized[2:]
    return normalized or None


def _extract_multimedia_urls(message: str | None) -> list[str]:
    if not message:
        return []
    text = str(message)
    if "http" not in text.lower():
        return []
    urls = MULTIMEDIA_URL_REGEX.findall(text)
    return [url for url in urls if any(ext in url.lower() for ext in MULTIMEDIA_EXTENSIONS)]


def _parse_audio_content(text: str) -> tuple[str | None, str | None, str | None]:
    """Parse Kapso-enriched audio text into (audio_url, transcript, filename)."""
    audio_url = None
    transcript = None
    filename = None

    url_match = _MEDIA_URL_RE.search(text)
    if url_match:
        audio_url = url_match.group(1).strip()

    transcript_match = _AUDIO_TRANSCRIPT_RE.search(text)
    if transcript_match:
        transcript = transcript_match.group(1).strip()

    filename_match = _MEDIA_FILENAME_RE.search(text)
    if filename_match:
        filename = filename_match.group(1).strip()

    return audio_url, transcript, filename


def _parse_media_content(text: str) -> tuple[str | None, str | None]:
    """Parse Kapso-enriched image/document text into (media_url, filename)."""
    media_url = None
    filename = None

    url_match = _MEDIA_URL_RE.search(text)
    if url_match:
        media_url = url_match.group(1).strip()

    filename_match = _MEDIA_FILENAME_RE.search(text)
    if filename_match:
        filename = filename_match.group(1).strip()

    return media_url, filename


def _detect_media_type(filename: str | None) -> str | None:
    """Return 'image', 'document', or None based on filename extension."""
    if not filename:
        return None
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    if ext in _DOCUMENT_EXTENSIONS:
        return "document"
    return None


async def _describe_image_with_vision(image_url: str, instructions: str | None = None) -> str | None:
    """Call OpenRouter vision model to describe an image.

    Uses Gemini Flash via OpenRouter.  Returns the description text or None.
    Includes: semaphore (max concurrent), retry with exponential backoff, fallback.
    """
    settings = get_settings()
    prompt = instructions or "Describe la imagen de forma detallada y útil."
    payload = {
        "model": VISION_MODEL,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    last_exc: Exception | None = None
    async with _vision_semaphore:
        for attempt in range(1, VISION_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=60.0) as http:
                    resp = await http.post(
                        f"{settings.OPENROUTER_BASE_URL}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("retry-after", VISION_BASE_BACKOFF * attempt))
                        logger.warning("Vision rate limited (429), retry %d/%d in %.1fs", attempt, VISION_MAX_RETRIES, retry_after)
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    description = data["choices"][0]["message"]["content"]
                    logger.info("Vision description obtained: %d chars (attempt %d)", len(description), attempt)
                    return description
            except Exception as exc:
                last_exc = exc
                if attempt < VISION_MAX_RETRIES:
                    wait = VISION_BASE_BACKOFF * attempt
                    logger.warning("Vision attempt %d/%d failed: %s — retrying in %ds", attempt, VISION_MAX_RETRIES, exc, wait)
                    await asyncio.sleep(wait)
                else:
                    logger.error("Vision description failed after %d attempts: %s", VISION_MAX_RETRIES, exc, exc_info=True)

    # All retries exhausted — notify webhook and return fallback
    if last_exc:
        await send_error_to_webhook(
            last_exc,
            context="vision_rate_limit",
            severity="warning",
            fallback=f"La descripción de imagen falló tras {VISION_MAX_RETRIES} reintentos (probablemente rate limit de OpenRouter/Gemini). Se usó fallback: el agente recibe 'El contacto envió una imagen' en lugar de la descripción. El mensaje se procesó normalmente.",
        )
    return None


async def _upload_audio_to_storage(audio_url: str, filename: str) -> str | None:
    """Download audio from Kapso URL and upload to Supabase Storage bucket."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            # Download audio from Kapso (Rails Active Storage redirect)
            dl_resp = await http.get(audio_url)
            dl_resp.raise_for_status()
            audio_bytes = dl_resp.content
            content_type = dl_resp.headers.get("content-type", "audio/ogg")
            logger.info(
                "Audio downloaded: %d bytes, content-type=%s, status=%d, final_url=%s",
                len(audio_bytes), content_type, dl_resp.status_code, str(dl_resp.url),
            )

        if len(audio_bytes) < 100:
            logger.warning("Audio download suspiciously small (%d bytes), might be an error page", len(audio_bytes))

        # Upload to Supabase Storage
        storage_url = f"{settings.SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{filename}"
        headers = {
            "apikey": settings.SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
            "Content-Type": content_type,
            "x-upsert": "true",
        }
        async with httpx.AsyncClient(timeout=30.0) as http:
            up_resp = await http.post(storage_url, content=audio_bytes, headers=headers)
            if up_resp.status_code >= 400:
                logger.error(
                    "Supabase Storage upload failed: status=%d body=%s",
                    up_resp.status_code, up_resp.text[:500],
                )
            up_resp.raise_for_status()

        public_url = f"{settings.SUPABASE_URL}/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/{filename}"
        logger.info("Audio uploaded to Supabase Storage: %s", public_url)
        return public_url
    except Exception as exc:
        logger.error("Failed to upload audio to Supabase Storage: %s", exc, exc_info=True)
        return None


async def _multimedia_pipeline(person_id: int, archivo_url: str, transcript: str | None) -> None:
    """Async fire-and-forget pipeline: crear-multimedia → guardar-multimedia.

    Step 1: Call crear-multimedia-inicial-v1 to register in fact_media.
    Step 2: Call guardar-multimedia-v4 with the multimedia_id + transcript.
    """
    settings = get_settings()
    auth_headers = {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            # --- Step 1: crear-multimedia-inicial ---
            crear_url = f"{settings.SUPABASE_URL}/functions/v1/{SUPABASE_EDGE_CREAR_MULTIMEDIA}"
            crear_payload = {
                "person_id": person_id,
                "tipo": "multimedia",
                "archivo_url": archivo_url,
            }
            resp1 = await http.post(crear_url, json=crear_payload, headers=auth_headers)
            if resp1.status_code >= 400:
                logger.error(
                    "Edge crear-multimedia failed: status=%d body=%s",
                    resp1.status_code, resp1.text[:500],
                )
                return
            resp1_data = resp1.json()
            logger.info(
                "Edge crear-multimedia ok: contacto=%s url=%s resp=%s",
                person_id, archivo_url, str(resp1_data)[:200],
            )

            # Extract multimedia_id from response
            multimedia_id = None
            data_block = resp1_data.get("data") or {}
            multimedia_id = data_block.get("id")
            if not multimedia_id:
                logger.warning("Edge crear-multimedia did not return multimedia id: %s", str(resp1_data)[:300])
                return

            # --- Step 2: guardar-multimedia ---
            guardar_url = f"{settings.SUPABASE_URL}/functions/v1/{SUPABASE_EDGE_GUARDAR_MULTIMEDIA}"
            content_text_val = transcript or f"Archivo multimedia: {archivo_url}"
            guardar_payload = {
                "multimedia_id": multimedia_id,
                "content_text": content_text_val,
            }
            resp2 = await http.post(guardar_url, json=guardar_payload, headers=auth_headers)
            if resp2.status_code >= 400:
                logger.error(
                    "Edge guardar-multimedia failed: status=%d body=%s",
                    resp2.status_code, resp2.text[:500],
                )
            else:
                logger.info(
                    "Edge guardar-multimedia ok: multimedia_id=%s resp=%s",
                    multimedia_id, resp2.text[:200],
                )
    except Exception as exc:
        logger.error("Multimedia pipeline error: %s", exc, exc_info=True)


async def _process_audio_message(inbound: KapsoInboundRequest, person_id: int | None = None) -> tuple[str | None, str | None]:
    """Process an audio message: extract transcript and upload to storage.

    Returns (transcript, storage_url).
    """
    message_type = str(inbound.message_type or "").strip().lower()
    if message_type != "audio":
        return None, None

    text = inbound.text or ""
    audio_url, transcript, filename = _parse_audio_content(text)
    logger.info(
        "Audio parse result: url=%s, transcript=%s, filename=%s",
        audio_url[:80] if audio_url else None,
        (transcript or "")[:60],
        filename,
    )

    # Upload audio to Supabase Storage
    storage_url = None
    if audio_url and filename:
        storage_url = await _upload_audio_to_storage(audio_url, filename)
    elif audio_url:
        # Generate filename from URL or message_id
        fallback_name = f"audio_{inbound.message_id.replace('=', '').replace('.', '_')[-20:]}.ogg"
        storage_url = await _upload_audio_to_storage(audio_url, fallback_name)
    else:
        logger.warning("Audio message but no audio_url found. Raw text: %s", text[:200])

    # Register multimedia via edge functions (async, non-blocking)
    if storage_url and person_id:
        asyncio.create_task(_multimedia_pipeline(person_id, storage_url, transcript))

    return transcript, storage_url


def _extract_media_url_from_raw(inbound: KapsoInboundRequest) -> tuple[str | None, str | None]:
    """Extract media URL and filename from media_raw/kapso enriched content.

    Falls back to kapso.content field which has the enriched text with URL.
    Returns (url, filename).
    """
    media = inbound.media_raw if isinstance(inbound.media_raw, dict) else {}
    msg_type = str(inbound.message_type or "").strip().lower()

    # Try kapso.content first (enriched text: "Image attached (file.jpg) ... URL: https://...")
    kapso_payload = media.get("kapso")
    if isinstance(kapso_payload, dict):
        content = kapso_payload.get("content")
        if content:
            url, filename = _parse_media_content(str(content))
            if url:
                return url, filename

    # Try direct media block keys
    media_block = media.get(msg_type)
    if isinstance(media_block, dict):
        for key in ("link", "url"):
            value = media_block.get(key)
            if value and str(value).startswith("http"):
                return str(value).strip(), None

    return None, None


async def _process_image_message(
    inbound: KapsoInboundRequest,
    person_id: int | None = None,
    instrucciones_multimedia: str | None = None,
) -> tuple[str | None, str | None]:
    """Process an image message: upload to storage, describe via vision model.

    Returns (description, storage_url).  Description is SYNC (needed by agent).
    """
    text = inbound.text or ""
    media_url, filename = _parse_media_content(text)

    # Fallback: extract URL from media_raw/kapso enriched content
    if not media_url:
        media_url, filename = _extract_media_url_from_raw(inbound)

    if not media_url:
        logger.warning("Image message but no URL found. Raw text: %s", text[:200])
        return None, None

    if not filename:
        filename = f"image_{inbound.message_id.replace('=', '').replace('.', '_')[-20:]}.jpg"

    logger.info("Image parse result: url=%s, filename=%s", media_url[:80], filename)

    # Upload to Supabase Storage (reuse audio uploader — same logic)
    storage_url = await _upload_audio_to_storage(media_url, filename)

    # Describe image via vision model (SYNC — result needed by agent)
    vision_prompt = instrucciones_multimedia or "Describe la imagen de forma detallada y útil."
    # Use the public storage URL if available, otherwise the original Kapso URL
    image_for_vision = storage_url or media_url
    description = await _describe_image_with_vision(image_for_vision, vision_prompt)

    # Fallback: if vision failed (rate limit, timeout), give the agent something useful
    if description is None:
        fallback_desc = "El contacto envió una imagen."
        if storage_url:
            fallback_desc += f" URL: {storage_url}"
        description = fallback_desc

    # Multimedia pipeline (async, non-blocking)
    if storage_url and person_id:
        contenido_for_edge = description or f"Imagen: {storage_url}"
        asyncio.create_task(_multimedia_pipeline(person_id, storage_url, contenido_for_edge))

    return description, storage_url


async def _process_document_message(
    inbound: KapsoInboundRequest,
    person_id: int | None = None,
) -> tuple[str | None, str | None]:
    """Process a document message: upload to storage.

    Returns (doc_reference_text, storage_url).
    """
    text = inbound.text or ""
    media_url, filename = _parse_media_content(text)

    # Fallback: extract URL from media_raw/kapso enriched content
    if not media_url:
        media_url, filename = _extract_media_url_from_raw(inbound)

    if not media_url:
        logger.warning("Document message but no URL found. Raw text: %s", text[:200])
        return None, None

    if not filename:
        filename = f"doc_{inbound.message_id.replace('=', '').replace('.', '_')[-20:]}.pdf"

    logger.info("Document parse result: url=%s, filename=%s", media_url[:80], filename)

    # Upload to Supabase Storage
    storage_url = await _upload_audio_to_storage(media_url, filename)

    # Build reference text for the agent
    doc_ref = f"El contacto envió un documento: {filename}"
    if storage_url:
        doc_ref += f"\nURL del documento: {storage_url}"

    # Multimedia pipeline (async, non-blocking)
    if storage_url and person_id:
        asyncio.create_task(_multimedia_pipeline(person_id, storage_url, doc_ref))

    return doc_ref, storage_url


def _extract_media_reference(inbound: KapsoInboundRequest) -> str:
    media = inbound.media_raw if isinstance(inbound.media_raw, dict) else {}
    message_type = str(inbound.message_type or "").strip().lower()
    media_block = media.get(message_type)

    if isinstance(media_block, dict):
        for key in ("link", "url", "id"):
            value = media_block.get(key)
            if value:
                return str(value).strip()
        caption = media_block.get("caption")
        if caption:
            return str(caption).strip()

    kapso_payload = media.get("kapso")
    if isinstance(kapso_payload, dict):
        content = kapso_payload.get("content")
        if content:
            return str(content).strip()

    return f"[media:{message_type or 'unknown'}]"


def _separate_message_parts(inbound: KapsoInboundRequest) -> list[dict[str, str]]:
    message = inbound.text.strip() if inbound.text and inbound.text.strip() else ""
    urls = _extract_multimedia_urls(message)

    if not urls and not inbound.has_media:
        return [{"content_text": message, "tipo": "texto"}] if message else []

    parts: list[dict[str, str]] = []
    if urls:
        text_with_placeholders = message
        for index, url in enumerate(urls, start=1):
            text_with_placeholders = text_with_placeholders.replace(url, f"(link-{index})")
        text_with_placeholders = re.sub(r"\n\s*\n+", "\n", text_with_placeholders).strip()
        if text_with_placeholders:
            parts.append({"content_text": text_with_placeholders, "tipo": "texto"})
        for url in urls:
            parts.append({"content_text": url, "tipo": "multimedia"})
    elif message:
        parts.append({"content_text": message, "tipo": "texto"})

    if inbound.has_media:
        media_reference = _extract_media_reference(inbound)
        if media_reference and media_reference not in {part["content_text"] for part in parts}:
            parts.append({"content_text": media_reference, "tipo": "multimedia"})

    return parts


def _build_user_message(inbound: KapsoInboundRequest, message_parts: list[dict[str, str]]) -> str:
    text_parts = [part["content_text"].strip() for part in message_parts if part["tipo"] == "texto" and part["content_text"].strip()]
    multimedia_parts = [
        part["content_text"].strip()
        for part in message_parts
        if part["tipo"] == "multimedia" and part["content_text"].strip()
    ]

    sections: list[str] = []
    if text_parts:
        sections.append("\n".join(text_parts))
    if multimedia_parts:
        sections.append(
            "Archivos o referencias multimedia del usuario:\n"
            + "\n".join(f"- {item}" for item in multimedia_parts)
        )

    if sections:
        return "\n\n".join(sections).strip()
    if inbound.has_media:
        return f"El usuario envió un mensaje multimedia de tipo {inbound.message_type} sin texto adicional."
    return f"El usuario envió un mensaje de tipo {inbound.message_type} sin contenido legible."


def _extract_slash_command(message: str | None) -> str | None:
    if not message:
        return None
    normalized = str(message).strip()
    if not normalized.startswith("/"):
        return None
    return normalized.split()[0].lower()


def _build_command_response(
    request: KapsoInboundRequest,
    conversation_id: str,
    agent_id: int,
    agent_name: str,
    model_used: str,
    reply_text: str,
    started_at: float,
) -> KapsoInboundResponse:
    total_ms = (time.perf_counter() - started_at) * 1000
    suppress_send = _should_suppress_kapso_send(reply_text)
    return KapsoInboundResponse(
        reply_type="text",
        reply_text=reply_text,
        suppress_send=suppress_send,
        reaction=None,
        recipient_phone=request.from_phone,
        phone_number_id=request.phone_number_id,
        message_id=request.message_id,
        conversation_id=conversation_id,
        agent_id=agent_id,
        agent_name=agent_name,
        model_used=model_used,
        timing=TimingInfo(
            total_ms=round(total_ms, 1),
            llm_ms=0,
            mcp_discovery_ms=0,
            graph_build_ms=0,
            tool_execution_ms=0,
        ),
        tools_used=[],
        agent_runs=[],
    )


def _build_message_error_update(message: dict, error_text: str, error_type: str) -> dict:
    metadata = message.get("metadata") if isinstance(message, dict) and isinstance(message.get("metadata"), dict) else {}
    return {
        "status": "error",
        "metadata": {
            **metadata,
            "processing_error": {
                "type": error_type,
                "detail": error_text,
                "failed_at": str(time.time()),
            },
        },
    }


async def _update_inbound_message_statuses(message_ids: list[int], status: str) -> None:
    for message_id in message_ids:
        try:
            await db.actualizar_mensaje(int(message_id), {"status": status})
        except Exception:
            logger.exception(
                "kapso.inbound_status_update_failed",
                extra={"message_id": message_id, "status": status},
            )


def _ensure_reply_text(reply_text: str | None) -> str:
    normalized = str(reply_text or "").strip()
    if normalized:
        return normalized
    return "Gracias por tu mensaje. ¿Podrías darme un poco más de detalle para ayudarte mejor?"


def _should_suppress_kapso_send(reply_text: str | None) -> bool:
    return str(reply_text or "").lstrip().startswith("❌")


def _merge_timings(started_at: float, conversational_timing: TimingInfo, funnel_timing: TimingInfo | None = None) -> TimingInfo:
    total_ms = (time.perf_counter() - started_at) * 1000
    funnel = funnel_timing or TimingInfo(total_ms=0, llm_ms=0, mcp_discovery_ms=0, graph_build_ms=0, tool_execution_ms=0)
    return TimingInfo(
        total_ms=round(total_ms, 1),
        llm_ms=round(float(conversational_timing.llm_ms) + float(funnel_timing.llm_ms), 1),
        mcp_discovery_ms=round(float(conversational_timing.mcp_discovery_ms), 1),
        graph_build_ms=round(float(conversational_timing.graph_build_ms) + float(funnel_timing.graph_build_ms), 1),
        tool_execution_ms=round(float(conversational_timing.tool_execution_ms) + float(funnel_timing.tool_execution_ms), 1),
    )


def _merge_tool_calls(conversational_tools: list[ToolCall], funnel_tools: list[ToolCall] | None = None) -> list[ToolCall]:
    return [*list(conversational_tools or []), *list(funnel_tools or [])]


def _merge_agent_runs(conversational_runs: list[AgentRunTrace], funnel_runs: list[AgentRunTrace] | None = None) -> list[AgentRunTrace]:
    return [*list(conversational_runs or []), *list(funnel_runs or [])]


def _build_funnel_error_response(
    *,
    model: str | None,
    conversation_db_id: int | None,
    error_text: str,
    timing: TimingInfo | None = None,
    tools_used: list[ToolCall] | None = None,
) -> FunnelAgentResponse:
    safe_timing = timing or TimingInfo(total_ms=0)
    trace = AgentRunTrace(
        agent_key="funnel_agent",
        agent_name="Agente de Embudo",
        agent_kind="analysis_error",
        conversation_id=str(conversation_db_id) if conversation_db_id else None,
        memory_session_id=None,
        model_used=model or get_settings().DEFAULT_MODEL,
        system_prompt="",
        user_prompt="",
        available_tools=[],
        tools_used=list(tools_used or []),
        timing=safe_timing,
        llm_iterations=0,
    )
    return FunnelAgentResponse(
        success=False,
        respuesta="Error al procesar el agente de embudo",
        error=error_text,
        timing=safe_timing,
        tools_used=list(tools_used or []),
        agent_runs=[trace],
    )


def _build_contact_update_error_response(
    *,
    model: str | None,
    conversation_db_id: int | None,
    person_id: int | None,
    error_text: str,
    timing: TimingInfo | None = None,
    tools_used: list[ToolCall] | None = None,
) -> ContactUpdateAgentResponse:
    safe_timing = timing or TimingInfo(total_ms=0)
    trace = AgentRunTrace(
        agent_key="contact_update_agent",
        agent_name="Agente de Actualización de Contacto",
        agent_kind="analysis_error",
        conversation_id=str(conversation_db_id) if conversation_db_id else None,
        memory_session_id=str(person_id) if person_id else None,
        model_used=model or get_settings().DEFAULT_MODEL,
        system_prompt="",
        user_prompt="",
        available_tools=[],
        tools_used=list(tools_used or []),
        timing=safe_timing,
        llm_iterations=0,
    )
    return ContactUpdateAgentResponse(
        success=False,
        respuesta="Error al procesar el agente de actualización de contacto",
        updated_fields=[],
        contact_updates=None,
        timing=safe_timing,
        tools_used=list(tools_used or []),
        agent_runs=[trace],
        error=error_text,
    )


def _should_run_funnel_agent(message: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", str(message or "").strip().lower())
    if not normalized:
        return False
    return normalized not in FUNNEL_SKIP_TEXTS


async def _run_both_agents(
    *,
    started_at: float,
    system_prompt: str,
    user_message: str,
    raw_user_text: str | None,
    model: str | None,
    mcp_servers: list[MCPServerConfig],
    conversation_id: str,
    memory_session_id: str,
    person_id: int | None,
    enterprise_id: int | None,
    agent_id: int,
    conversation_db_id: int | None,
):
    # ── Phase 1: Run funnel + contact_update in parallel (BEFORE conversational) ──
    funnel_result = None
    contact_update_result = None

    run_funnel = person_id is not None and enterprise_id is not None and _should_run_funnel_agent(raw_user_text)
    run_contact_update = person_id is not None and enterprise_id is not None

    if run_funnel or run_contact_update:
        analysis_tasks: list[asyncio.Task] = []
        task_names: list[str] = []

        if run_funnel:
            analysis_tasks.append(
                asyncio.create_task(
                    asyncio.wait_for(
                        run_funnel_agent(
                            FunnelAgentRequest(
                                person_id=person_id,
                                enterprise_id=enterprise_id,
                                agent_id=agent_id,
                                conversation_id=conversation_db_id,
                                memory_session_id=memory_session_id,
                                memory_window=20,
                                model=model,
                            )
                        ),
                        timeout=FUNNEL_TIMEOUT_SECONDS,
                    )
                )
            )
            task_names.append("funnel")

        if run_contact_update:
            analysis_tasks.append(
                asyncio.create_task(
                    asyncio.wait_for(
                        run_contact_update_agent(
                            ContactUpdateAgentRequest(
                                person_id=person_id,
                                enterprise_id=enterprise_id,
                                agent_id=agent_id,
                                conversation_id=conversation_db_id,
                                model=model,
                            )
                        ),
                        timeout=CONTACT_UPDATE_TIMEOUT_SECONDS,
                    )
                )
            )
            task_names.append("contact_update")

        analysis_results = await asyncio.gather(*analysis_tasks, return_exceptions=True)

        for name, result in zip(task_names, analysis_results):
            if name == "funnel":
                funnel_result = result
            elif name == "contact_update":
                contact_update_result = result

    # Handle funnel errors / failures
    if isinstance(funnel_result, Exception):
        logger.warning("Kapso inbound: funnel agent fallo pero la respuesta conversacional continua: %s", funnel_result, exc_info=True)
        funnel_result = _build_funnel_error_response(
            model=model,
            conversation_db_id=conversation_db_id,
            error_text=str(funnel_result),
        )

    if isinstance(funnel_result, FunnelAgentResponse) and not funnel_result.success:
        logger.warning("Kapso inbound: funnel agent devolvio success=false: %s", funnel_result.error)
        if not funnel_result.agent_runs:
            funnel_result = _build_funnel_error_response(
                model=model,
                conversation_db_id=conversation_db_id,
                error_text=funnel_result.error or "Funnel agent devolvio success=false",
                timing=funnel_result.timing,
                tools_used=funnel_result.tools_used,
            )

    # Handle contact_update errors / failures
    if isinstance(contact_update_result, Exception):
        logger.warning(
            "Kapso inbound: contact update agent fallo pero la respuesta conversacional continua: %s",
            contact_update_result,
            exc_info=True,
        )
        contact_update_result = _build_contact_update_error_response(
            model=model,
            conversation_db_id=conversation_db_id,
            person_id=person_id,
            error_text=str(contact_update_result),
        )

    if isinstance(contact_update_result, ContactUpdateAgentResponse) and not contact_update_result.success:
        logger.warning("Kapso inbound: contact update agent devolvio success=false: %s", contact_update_result.error)
        if not contact_update_result.agent_runs:
            contact_update_result = _build_contact_update_error_response(
                model=model,
                conversation_db_id=conversation_db_id,
                person_id=person_id,
                error_text=contact_update_result.error or "Contact update agent devolvio success=false",
                timing=contact_update_result.timing,
                tools_used=contact_update_result.tools_used,
            )

    # ── Phase 2: Enrich system prompt with funnel findings ──
    enriched_prompt = system_prompt
    if isinstance(funnel_result, FunnelAgentResponse) and funnel_result.success:
        funnel_sections = ["\n\n## 🔄 ANÁLISIS DE EMBUDO (actualización en tiempo real)"]
        funnel_sections.append(f"Análisis del agente de embudo: {funnel_result.respuesta}")
        if funnel_result.etapa_nueva is not None:
            funnel_sections.append(f"Etapa del embudo actualizada a orden: {funnel_result.etapa_nueva}")
        if funnel_result.metadata_actualizada:
            funnel_sections.append(
                f"Información capturada: {json.dumps(funnel_result.metadata_actualizada, ensure_ascii=False)}"
            )
        enriched_prompt = system_prompt + "\n".join(funnel_sections)
        logger.info("System prompt enriquecido con resultado del funnel agent")

    # ── Phase 3: Run conversational agent (with enriched context) ──
    conversational_result = await run_agent(
        ChatRequest(
            system_prompt=enriched_prompt,
            message=user_message,
            model=model,
            mcp_servers=mcp_servers,
            conversation_id=conversation_id,
            memory_session_id=memory_session_id,
            memory_window=8,
            person_id=person_id,
            enterprise_id=enterprise_id,
            channel="whatsapp",  # Activa tools y fast-paths exclusivos de WhatsApp/Kapso
        )
    )

    # ── Merge timings, tools, agent_runs ──
    merged_timing = _merge_timings(
        started_at,
        conversational_result.timing,
        TimingInfo(
            total_ms=0,
            llm_ms=round(
                float((funnel_result.timing if isinstance(funnel_result, FunnelAgentResponse) else TimingInfo(total_ms=0)).llm_ms)
                + float((contact_update_result.timing if isinstance(contact_update_result, ContactUpdateAgentResponse) else TimingInfo(total_ms=0)).llm_ms),
                1,
            ),
            mcp_discovery_ms=0,
            graph_build_ms=round(
                float((funnel_result.timing if isinstance(funnel_result, FunnelAgentResponse) else TimingInfo(total_ms=0)).graph_build_ms)
                + float((contact_update_result.timing if isinstance(contact_update_result, ContactUpdateAgentResponse) else TimingInfo(total_ms=0)).graph_build_ms),
                1,
            ),
            tool_execution_ms=round(
                float((funnel_result.timing if isinstance(funnel_result, FunnelAgentResponse) else TimingInfo(total_ms=0)).tool_execution_ms)
                + float((contact_update_result.timing if isinstance(contact_update_result, ContactUpdateAgentResponse) else TimingInfo(total_ms=0)).tool_execution_ms),
                1,
            ),
        ),
    )
    merged_tools = _merge_tool_calls(
        conversational_result.tools_used,
        [
            *(funnel_result.tools_used if isinstance(funnel_result, FunnelAgentResponse) else []),
            *(contact_update_result.tools_used if isinstance(contact_update_result, ContactUpdateAgentResponse) else []),
        ],
    )
    merged_agent_runs = _merge_agent_runs(
        conversational_result.agent_runs,
        [
            *(funnel_result.agent_runs if isinstance(funnel_result, FunnelAgentResponse) else []),
            *(contact_update_result.agent_runs if isinstance(contact_update_result, ContactUpdateAgentResponse) else []),
        ],
    )
    return conversational_result, funnel_result, contact_update_result, merged_timing, merged_tools, merged_agent_runs


def _build_mcp_servers(agent: dict) -> list[MCPServerConfig]:
    raw = agent.get("mcp_url")
    if not raw:
        return []

    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return []
        if value.startswith("["):
            try:
                parsed = json.loads(value)
                servers: list[MCPServerConfig] = []
                for item in parsed:
                    if isinstance(item, dict) and item.get("url"):
                        servers.append(MCPServerConfig(url=item["url"], name=item.get("name", "")))
                    elif isinstance(item, str) and item.strip():
                        servers.append(MCPServerConfig(url=item.strip(), name=""))
                return servers
            except json.JSONDecodeError:
                pass
        if "," in value:
            return [MCPServerConfig(url=item.strip(), name="") for item in value.split(",") if item.strip()]
        return [MCPServerConfig(url=value, name="")]

    return []


def _get_debug_config() -> dict:
    settings = get_settings()
    return {
        "app_name": settings.APP_NAME,
        "default_model": settings.DEFAULT_MODEL,
        "python_service_port": os.getenv("PYTHON_SERVICE_PORT", "8000"),
        "internal_agent_api_url": os.getenv("INTERNAL_AGENT_API_URL", "http://127.0.0.1:8000/api/v1/kapso/inbound"),
        "kapso_internal_token": mask_secret(settings.KAPSO_INTERNAL_TOKEN),
        "kapso_debug_token": mask_secret(settings.KAPSO_DEBUG_TOKEN),
        "kapso_public_debug": settings.KAPSO_PUBLIC_DEBUG,
        "supabase_url": mask_secret(settings.SUPABASE_URL),
        "fallback_phone": "(eliminado — error directo si no se resuelve)",
        "fallback_agent_id": "(eliminado — error directo si no se resuelve)",
    }


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client else None
    return host in {"127.0.0.1", "::1", "localhost"}


def _resolve_debug_access_token(request: Request, x_kapso_internal_token: str | None, x_kapso_debug_token: str | None) -> str | None:
    if x_kapso_debug_token:
        return x_kapso_debug_token
    if x_kapso_internal_token:
        return x_kapso_internal_token
    query_token = request.query_params.get("token")
    if query_token:
        return query_token
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return None


def _require_kapso_debug_access(request: Request, x_kapso_internal_token: str | None, x_kapso_debug_token: str | None) -> None:
    settings = get_settings()
    if settings.DEBUG or settings.KAPSO_PUBLIC_DEBUG or _is_loopback_client(request):
        return
    allowed_tokens = {token for token in [settings.KAPSO_DEBUG_TOKEN, settings.KAPSO_INTERNAL_TOKEN] if token}
    provided_token = _resolve_debug_access_token(request, x_kapso_internal_token, x_kapso_debug_token)
    if not allowed_tokens:
        raise HTTPException(status_code=503, detail="Debug disabled")
    if provided_token not in allowed_tokens:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/debug/events")
async def kapso_debug_events(
    request: Request,
    limit: int = 100,
    x_kapso_internal_token: str | None = Header(default=None),
    x_kapso_debug_token: str | None = Header(default=None),
):
    """Return merged events: in-memory (fresh) + Supabase (persistent)."""
    _require_kapso_debug_access(request, x_kapso_internal_token, x_kapso_debug_token)
    memory_events = get_kapso_debug_events(limit)

    # Load persisted events from Supabase so data survives deploys
    db_events: list[dict] = []
    try:
        supabase = await get_supabase()
        rows = await supabase.query(
            "debug_events",
            select="*",
            filters={},
            order="created_at",
            order_desc=True,
            limit=limit,
        )
        if rows and isinstance(rows, list):
            for row in rows:
                # Historical rows were stored with source='kapso'; the bridge
                # expects 'fastapi' for events generated by this service.
                raw_source = row.get("source", "fastapi")
                source = "fastapi" if raw_source == "kapso" else raw_source
                db_events.append({
                    "timestamp": row.get("created_at") or row.get("timestamp"),
                    "source": source,
                    "stage": row.get("stage", ""),
                    "payload": row.get("payload") or {},
                })
    except Exception as exc:
        logger.warning("debug/events: could not load from Supabase: %s", exc)

    # Dedup by (timestamp, stage) — memory wins
    seen = {(ev.get("timestamp", ""), ev.get("stage", "")) for ev in memory_events}
    merged = list(memory_events)
    for ev in db_events:
        key = (ev.get("timestamp", ""), ev.get("stage", ""))
        if key not in seen:
            merged.append(ev)
            seen.add(key)

    merged.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"events": merged[:limit]}


@router.get("/debug/config")
async def kapso_debug_config(
    request: Request,
    x_kapso_internal_token: str | None = Header(default=None),
    x_kapso_debug_token: str | None = Header(default=None),
):
    _require_kapso_debug_access(request, x_kapso_internal_token, x_kapso_debug_token)
    return _get_debug_config()


@router.get("/debug/empresas")
async def kapso_debug_empresas(
    request: Request,
    x_kapso_internal_token: str | None = Header(default=None),
    x_kapso_debug_token: str | None = Header(default=None),
):
    """Return list of empresas for dashboard filtering."""
    _require_kapso_debug_access(request, x_kapso_internal_token, x_kapso_debug_token)
    try:
        db = await get_supabase()
        rows = await db.query("dim_enterprise", select="id,nombre")
        if isinstance(rows, list):
            return {"empresas": [{"id": r["id"], "nombre": r.get("nombre") or f"Empresa {r['id']}"} for r in rows]}
    except Exception as exc:
        logger.warning("debug/empresas error: %s", exc)
    return {"empresas": []}


@router.get("/debug/stream")
async def kapso_debug_stream(
    request: Request,
    x_kapso_internal_token: str | None = Header(default=None),
    x_kapso_debug_token: str | None = Header(default=None),
):
    """SSE endpoint — streams debug events in real time."""
    _require_kapso_debug_access(request, x_kapso_internal_token, x_kapso_debug_token)
    q = subscribe_sse()

    async def _generate():
        try:
            while True:
                event = await q.get()
                data = json.dumps(event, default=str)
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe_sse(q)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# Las interacciones ahora se calculan directamente en JavaScript desde los eventos


@router.post("/inbound", response_model=KapsoInboundResponse)
async def kapso_inbound(
    request: KapsoInboundRequest,
    x_kapso_internal_token: str | None = Header(default=None),
):
    settings = get_settings()
    started_at = time.perf_counter()
    interaction_id = str(uuid.uuid4())
    channel_message: ChannelInboundMessage = normalize_kapso_inbound(request)
    add_kapso_debug_event(
        "fastapi",
        "inbound_received",
        {
            "phone_number_id": request.phone_number_id,
            "from": request.from_phone,
            "contact_name": request.contact_name,
            "conversation_id": channel_message.external_conversation_id,
            "message_id": request.message_id,
            "message_type": request.message_type,
            "interaction_id": interaction_id,
        },
    )
    logger.info(
        "Kapso inbound recibido phone_number_id=%s from=%s conversation_id=%s message_id=%s type=%s",
        request.phone_number_id,
        request.from_phone,
        request.kapso_conversation_id,
        request.message_id,
        request.message_type,
    )
    if settings.KAPSO_INTERNAL_TOKEN and x_kapso_internal_token != settings.KAPSO_INTERNAL_TOKEN:
        add_kapso_debug_event(
            "fastapi",
            "unauthorized",
            {"phone_number_id": request.phone_number_id, "message_id": request.message_id},
        )
        raise HTTPException(status_code=401, detail="Unauthorized Kapso bridge")
    try:
        # 1. Buscar primero por teléfono (columna phone_e164) en dim_channel
        numero = await db.get_numero_por_telefono(request.phone_number_id)
        resolved_via = "phone_e164" if numero else None

        # 2. Si no coincide como teléfono, buscar por id_kapso
        if not numero:
            numero = await db.get_numero_por_id_kapso(request.phone_number_id)
            resolved_via = "id_kapso" if numero else None

        # 3. Si no se encontró por ningún método → error directo
        if not numero:
            add_kapso_debug_event(
                "fastapi",
                "numero_no_encontrado",
                {
                    "phone_number_id": request.phone_number_id,
                    "message_id": request.message_id,
                    "from_phone": request.from_phone,
                    "error": "No se encontró ningún número en dim_channel que coincida "
                             "con phone_e164 o id_kapso. Registrar el número en la BD.",
                },
            )
            logger.error(
                "Kapso inbound: phone_number_id=%s no coincide con ningún registro en dim_channel "
                "(ni por phone_e164 ni por id_kapso). Mensaje descartado para evitar responder con agente incorrecto.",
                request.phone_number_id,
            )
            raise HTTPException(
                status_code=404,
                detail=f"Número no configurado: phone_number_id={request.phone_number_id} "
                       f"no existe en dim_channel. Registrar el número antes de recibir mensajes.",
            )

        if resolved_via:
            add_kapso_debug_event(
                "fastapi",
                f"numero_resuelto_por_{resolved_via}",
                {
                    "phone_number_id": request.phone_number_id,
                    "resolved_channel_id": numero.get("id"),
                    "resolved_agent_id": numero.get("agent_id"),
                    "resolved_enterprise_id": numero.get("enterprise_id"),
                    "message_id": request.message_id,
                },
            )

        if not numero.get("agent_id"):
            add_kapso_debug_event(
                "fastapi",
                "numero_sin_agente",
                {
                    "channel_id": numero.get("id"),
                    "phone_number_id": request.phone_number_id,
                    "message_id": request.message_id,
                    "error": "El número existe en dim_channel pero no tiene agent_id asignado.",
                },
            )
            logger.error(
                "Kapso inbound: número id=%s (tel=%s) no tiene agent_id asignado.",
                numero.get("id"),
                numero.get("phone_e164"),
            )
            raise HTTPException(
                status_code=404,
                detail=f"Número id={numero.get('id')} no tiene agente asignado. "
                       f"Configurar agent_id en dim_channel.",
            )

        agent_id = numero.get("agent_id")

        channel_id = int(numero["id"]) if numero and numero.get("id") is not None else None
        enterprise_id = int(numero["enterprise_id"]) if numero and numero.get("enterprise_id") is not None else None
        normalized_from_phone = _normalize_phone(request.from_phone) or request.from_phone
        slash_command = _extract_slash_command(request.text)
        contacto = None
        person_created = False
        conversation_db = None

        if not slash_command and enterprise_id and channel_id:
            contacto, person_created = await db.upsert_contacto_canal(
                normalized_from_phone,
                enterprise_id,
                canal=str(numero.get("canal") or channel_message.channel or "whatsapp"),
            )
            if contacto and contacto.get("id") is not None:
                conversation_db = await db.get_conversacion_activa(int(contacto["id"]), channel_id)
                if conversation_db and conversation_db.get("agent_id"):
                    conv_agent_id = conversation_db["agent_id"]
                    # Validar que el agente de la conversación pertenezca a la misma empresa
                    conv_agent = await db.get_agente(int(conv_agent_id))
                    if conv_agent and conv_agent.get("enterprise_id") == enterprise_id:
                        agent_id = conv_agent_id
                    else:
                        logger.warning(
                            "Conversación %s tiene agent_id=%s de enterprise_id=%s, "
                            "pero el número pertenece a enterprise_id=%s. Ignorando agente de conversación.",
                            conversation_db.get("id"),
                            conv_agent_id,
                            conv_agent.get("enterprise_id") if conv_agent else "N/A",
                            enterprise_id,
                        )

        agent = await db.get_agente(int(agent_id))
        # Si la conversación sobreescribió agent_id pero no existe, volver al del número
        if not agent and numero and numero.get("agent_id") and int(numero.get("agent_id")) != int(agent_id):
            agent_id = int(numero.get("agent_id"))
            agent = await db.get_agente(int(agent_id))
        if not agent:
            logger.error(
                "Kapso inbound: agent_id=%s no encontrado en dim_agent para phone_number_id=%s",
                agent_id,
                request.phone_number_id,
            )
            raise HTTPException(
                status_code=404,
                detail=f"Agente id={agent_id} no encontrado en dim_agent. "
                       f"Verificar configuración del número.",
            )

        if enterprise_id is None and agent.get("enterprise_id") is not None:
            enterprise_id = int(agent["enterprise_id"])
        if contacto is None and enterprise_id and normalized_from_phone:
            if slash_command:
                contacto = await db.get_contacto_por_telefono(normalized_from_phone, enterprise_id)
                person_created = False
            else:
                contacto, person_created = await db.upsert_contacto_canal(
                    normalized_from_phone,
                    enterprise_id,
                    canal=str(numero.get("canal") or channel_message.channel or "whatsapp"),
                )
            if channel_id and contacto and contacto.get("id") is not None:
                conversation_db = await db.get_conversacion_activa(int(contacto["id"]), channel_id)
        if not slash_command and enterprise_id and channel_id and contacto and contacto.get("id") is not None and conversation_db is None:
            try:
                conversation_db = await db.insertar_conversacion(
                    contacto_id=int(contacto["id"]),
                    agente_id=int(agent_id),
                    empresa_id=enterprise_id,
                    numero_id=channel_id,
                    canal=str(numero.get("canal") or "whatsapp"),
                    metadata=None,
                )
            except Exception:
                conversation_db = await db.get_conversacion_activa(int(contacto["id"]), channel_id)
                if conversation_db is None:
                    raise

        model = agent.get("llm") or None
        mcp_servers_list = _build_mcp_servers(agent)
        message_parts = _separate_message_parts(request)
        conversation_id = f"{channel_message.provider or 'kapso'}:{channel_message.external_conversation_id}"
        memory_session_id = normalized_from_phone
        if contacto and contacto.get("id") is not None:
            memory_session_id = str(contacto["id"])

        # Procesar audio: subir a Supabase Storage y extraer transcript
        audio_transcript: str | None = None
        audio_storage_url: str | None = None
        is_audio = str(request.message_type or "").strip().lower() == "audio"
        if is_audio:
            person_id_for_audio = contacto.get("id") if contacto else None
            audio_transcript, audio_storage_url = await _process_audio_message(request, person_id_for_audio)
            add_kapso_debug_event(
                "fastapi",
                "audio_processing",
                {
                    "transcript": (audio_transcript or "")[:120],
                    "storage_url": audio_storage_url,
                    "upload_ok": audio_storage_url is not None,
                },
            )
            if audio_transcript:
                # Replace message_parts with clean transcript as text
                message_parts = [{"content_text": audio_transcript, "tipo": "texto"}]
                if audio_storage_url:
                    message_parts.append({"content_text": audio_storage_url, "tipo": "multimedia"})
                logger.info(
                    "Audio procesado → transcript=%s... storage_url=%s",
                    audio_transcript[:60],
                    audio_storage_url,
                )
            elif audio_storage_url:
                logger.info("Audio subido pero sin transcript. storage_url=%s", audio_storage_url)

        # Procesar imagen: subir a Storage + describir con vision model (SYNC)
        msg_type_lower = str(request.message_type or "").strip().lower()
        is_image = msg_type_lower == "image"
        is_document = msg_type_lower == "document"

        if is_image:
            person_id_for_media = contacto.get("id") if contacto else None
            instrucciones_mm = agent.get("instrucciones_multimedia") or None
            img_description, img_storage_url = await _process_image_message(
                request, person_id_for_media, instrucciones_mm,
            )
            add_kapso_debug_event(
                "fastapi",
                "image_processing",
                {
                    "description": (img_description or "")[:150],
                    "storage_url": img_storage_url,
                    "upload_ok": img_storage_url is not None,
                    "vision_ok": img_description is not None,
                },
            )
            if img_description:
                # Keep the user's original caption text (if any) alongside the vision description
                user_caption = (request.text or "").strip()
                # Remove any enriched media text from the caption (in case text has "Image attached..." prefix)
                caption_clean = _MEDIA_URL_RE.sub("", user_caption).strip()
                caption_clean = _MEDIA_FILENAME_RE.sub("", caption_clean).strip()
                # Remove "Image attached" / "Imagen adjunta" prefix lines if present
                caption_clean = re.sub(
                    r"^(?:Image attached|Imagen adjunta)[^\n]*\n?",
                    "", caption_clean, flags=re.IGNORECASE,
                ).strip()

                new_parts: list[dict[str, str]] = []
                if caption_clean:
                    new_parts.append({"content_text": caption_clean, "tipo": "texto"})
                new_parts.append({
                    "content_text": f"[Descripción de la imagen enviada]: {img_description}",
                    "tipo": "texto",
                })
                if img_storage_url:
                    new_parts.append({"content_text": img_storage_url, "tipo": "multimedia"})
                message_parts = new_parts
                logger.info(
                    "Imagen procesada → description=%s... storage_url=%s caption=%s",
                    img_description[:80],
                    img_storage_url,
                    caption_clean[:60] if caption_clean else "(sin caption)",
                )

        # Procesar documento: subir a Storage (referencia para el agente)
        if is_document:
            person_id_for_media = contacto.get("id") if contacto else None
            doc_ref, doc_storage_url = await _process_document_message(request, person_id_for_media)
            add_kapso_debug_event(
                "fastapi",
                "document_processing",
                {
                    "reference": (doc_ref or "")[:150],
                    "storage_url": doc_storage_url,
                    "upload_ok": doc_storage_url is not None,
                },
            )
            if doc_ref:
                message_parts = [{"content_text": doc_ref, "tipo": "texto"}]
                if doc_storage_url:
                    message_parts.append({"content_text": doc_storage_url, "tipo": "multimedia"})
                logger.info(
                    "Documento procesado → ref=%s... storage_url=%s",
                    doc_ref[:80],
                    doc_storage_url,
                )

        # Marcar origen como Whatsapp si aún no tiene valor
        if contacto and contacto.get("id") is not None and not contacto.get("origen"):
            try:
                supabase = await get_supabase()
                await supabase.update("dim_person", filters={"id": int(contacto["id"])}, data={"origen": "Whatsapp"})
            except Exception as e:
                logger.warning("No se pudo actualizar origen del contacto %s: %s", contacto["id"], e)

        if slash_command:
            session_ids = {normalized_from_phone, request.from_phone}
            if contacto and contacto.get("id") is not None:
                session_ids.add(str(contacto["id"]))

            add_kapso_debug_event(
                "fastapi",
                "slash_command_detected",
                {
                    "message_id": request.message_id,
                    "command": slash_command,
                    "person_id": contacto.get("id") if contacto else None,
                    "conversation_db_id": conversation_db.get("id") if conversation_db else None,
                },
            )

            if slash_command == "/borrar":
                deleted_counts = await asyncio.gather(*[db.delete_agent_memory(session_id) for session_id in session_ids if session_id])
                reply_text = f"Memoria del agente borrada. Registros eliminados: {sum(deleted_counts)}."
            elif slash_command == "/borrar2":
                deleted_counts = await asyncio.gather(*[db.delete_agent_memory(session_id) for session_id in session_ids if session_id])
                reset_summary = {
                    "mensajes": 0,
                    "conversaciones": 0,
                    "notas": 0,
                    "contextos": 0,
                    "citas": 0,
                    "notificaciones": 0,
                    "actividades": 0,
                    "contactos": 0,
                }
                if contacto and contacto.get("id") is not None:
                    reset_summary = await db.reset_contacto_data(int(contacto["id"]))
                    reply_text = (
                        "Usuario eliminado correctamente. "
                        "Se borró su información y la siguiente interacción se tratará como un usuario nuevo."
                    )
                else:
                    reply_text = (
                        "No había información persistida del usuario. "
                        "La siguiente interacción se tratará como un usuario nuevo."
                    )
            else:
                reply_text = "Comando no reconocido. Usa /borrar o /borrar2."

            add_kapso_debug_event(
                "fastapi",
                "slash_command_done",
                {
                    "message_id": request.message_id,
                    "command": slash_command,
                    "person_id": contacto.get("id") if contacto else None,
                    "memory_session_id": memory_session_id,
                    "reply_text": reply_text,
                },
            )

            return _build_command_response(
                request=request,
                conversation_id=conversation_id,
                agent_id=int(agent_id),
                agent_name=agent.get("agent_name") or str(agent_id),
                model_used=agent.get("llm") or settings.DEFAULT_MODEL,
                reply_text=reply_text,
                started_at=started_at,
            )

        # ── Guard: contacto inactivo ─────────────────────────────────────────
        # Si is_active es explícitamente False, no se procesa ni responde.
        # Aplica a cualquier canal (WhatsApp, webhook, etc.).
        # is_active = None (campo no seteado) NO bloquea — solo False explícito.
        if contacto and contacto.get("is_active") is False:
            logger.info(
                "kapso.inbound_blocked person_id=%s is_active=False — sin respuesta",
                contacto.get("id"),
            )
            add_kapso_debug_event(
                "fastapi",
                "inbound_blocked",
                {"reason": "contacto_inactivo", "person_id": contacto.get("id")},
            )
            return _build_command_response(
                request=request,
                conversation_id=conversation_id,
                agent_id=int(agent_id),
                agent_name=agent.get("agent_name") or str(agent_id),
                model_used=agent.get("llm") or settings.DEFAULT_MODEL,
                reply_text="❌ contacto_inactivo",  # suppress_send=True vía _should_suppress_kapso_send
                started_at=started_at,
            )
        # ────────────────────────────────────────────────────────────────────

        person_id = int(contacto["id"]) if contacto and contacto.get("id") is not None else None
        conversation_db_id = int(conversation_db["id"]) if conversation_db and conversation_db.get("id") is not None else None
        prompt_context_data = await db.load_kapso_prompt_context(
            contacto_id=person_id,
            empresa_id=enterprise_id,
            conversacion_id=conversation_db_id,
            team_id=int(contacto["team_humano_id"]) if contacto and contacto.get("team_humano_id") is not None else None,
            agente_id=int(agent["id"]) if agent.get("id") is not None else None,
            agente_rol_id=int(agent["id_rol"]) if agent.get("id_rol") is not None else None,
            limite_mensajes=8,
        )
        context_payload, prompt_extras = build_kapso_context_payload(
            contacto=contacto,
            agent=agent,
            empresa=prompt_context_data.get("empresa"),
            rol_agente=prompt_context_data.get("rol_agente"),
            team_humano=prompt_context_data.get("team_humano"),
            contextos=prompt_context_data.get("contextos") or [],
            citas=prompt_context_data.get("citas") or [],
            notificaciones=prompt_context_data.get("notificaciones") or [],
            mensajes_recientes=prompt_context_data.get("mensajes_recientes") or [],
            etapas_embudo=prompt_context_data.get("etapas_embudo") or [],
            notas=prompt_context_data.get("notas") or [],
            contexto_embudo_snapshot=prompt_context_data.get("contexto_embudo_snapshot"),
            etapas_embudo_snapshot=prompt_context_data.get("etapas_embudo_snapshot"),
            conversacion_memoria_snapshot=prompt_context_data.get("conversacion_memoria_snapshot"),
            inbound=request,
        )

        system_prompt = build_kapso_system_prompt(
            agent=agent,
            inbound=request,
            contacto=contacto,
            context_payload=context_payload,
            extras=prompt_extras,
            rol_agente=prompt_context_data.get("rol_agente"),
        )
        user_message = _build_user_message(request, message_parts)
        mcp_servers = mcp_servers_list

        add_kapso_debug_event(
            "fastapi",
            "prompt_context_built",
            {
                "message_id": request.message_id,
                "person_id": person_id,
                "conversation_db_id": conversation_db_id,
                "timezone_empresa": prompt_extras.get("timezone_empresa"),
                "stage_actual": prompt_extras.get("funnel_stage"),
                "usuario_interno": prompt_extras.get("es_usuario_interno"),
                "historial_items": len(prompt_context_data.get("mensajes_recientes") or []),
                "citas_items": len(prompt_context_data.get("citas") or []),
            },
        )

        add_kapso_debug_event(
            "fastapi",
            "inbound_entities_resolved",
            {
                "message_id": request.message_id,
                "normalized_from_phone": normalized_from_phone,
                "enterprise_id": enterprise_id,
                "channel_id": channel_id,
                "person_id": contacto.get("id") if contacto else None,
                "person_created": person_created,
                "conversation_db_id": conversation_db.get("id") if conversation_db else None,
                "message_parts": message_parts,
            },
        )

        mensajes_guardados: list[dict] = []
        inbound_message_ids: list[int] = []
        if conversation_db and conversation_db.get("id") is not None:
            metadata_base = {
                "canal": str(numero.get("canal") or channel_message.channel or "whatsapp"),
                "channel_provider": channel_message.provider,
                "channel_account_id": channel_message.channel_account_id,
                "external_conversation_id": channel_message.external_conversation_id,
                "external_message_id": channel_message.external_message_id,
                "phone_number_id": request.phone_number_id,
                "kapso_conversation_id": request.kapso_conversation_id,
                "kapso_message_id": request.message_id,
                "contact_name": request.contact_name,
                "message_type": request.message_type,
                "has_media": request.has_media,
            }
            for part in message_parts or [{"content_text": user_message, "tipo": "texto"}]:
                mensajes_guardados.append(
                    await db.insertar_mensaje(
                        conversacion_id=int(conversation_db["id"]),
                        contenido=part["content_text"],
                        remitente="usuario",
                        tipo=part["tipo"],
                        status="buffer",
                        metadata=metadata_base,
                        empresa_id=enterprise_id,
                    )
                )
                inserted_message = mensajes_guardados[-1]
                if inserted_message and inserted_message.get("id") is not None:
                    inbound_message_ids.append(int(inserted_message["id"]))

        add_kapso_debug_event(
            "fastapi",
            "inbound_messages_persisted",
            {
                "message_id": request.message_id,
                "conversation_db_id": conversation_db.get("id") if conversation_db else None,
                "saved_messages": [
                    {
                        "id": item.get("id"),
                        "tipo": item.get("tipo"),
                        "status": item.get("status"),
                    }
                    for item in mensajes_guardados
                ],
            },
        )

        add_kapso_debug_event(
            "fastapi",
            "memory_session_resolved",
            {
                "memory_session_id": memory_session_id,
                "memory_source": "person_id" if contacto and contacto.get("id") is not None else "from_phone",
                "person_id": contacto.get("id") if contacto else None,
                "from": normalized_from_phone,
                "message_id": request.message_id,
            },
        )

        add_kapso_debug_event(
            "fastapi",
            "run_agent_start",
            {
                "agent_id": int(agent_id),
                "resolved_via": resolved_via,
                "phone_number_id": request.phone_number_id,
                "message_id": request.message_id,
                "conversation_id": conversation_id,
                "memory_session_id": memory_session_id,
                "model": model,
                "mcp_servers": len(mcp_servers),
            },
        )
        logger.info(
            "Kapso inbound procesando agent_id=%s resolved_via=%s phone_number_id=%s from=%s message_type=%s",
            agent_id,
            resolved_via,
            request.phone_number_id,
            request.from_phone,
            request.message_type,
        )

        await _update_inbound_message_statuses(inbound_message_ids, "procesando")

        conversational_result, funnel_result, contact_update_result, merged_timing, merged_tools, merged_agent_runs = await _run_both_agents(
            started_at=started_at,
            system_prompt=system_prompt,
            user_message=user_message,
            raw_user_text=request.text,
            model=model,
            mcp_servers=mcp_servers,
            conversation_id=conversation_id,
            memory_session_id=memory_session_id,
            person_id=person_id,
            enterprise_id=enterprise_id,
            agent_id=int(agent_id),
            conversation_db_id=conversation_db_id,
        )

        reaction_emoji: str | None = None
        comando_data: dict | None = None
        for tool_call in merged_tools:
            if tool_call.tool_name == "send_reaction" and tool_call.tool_input.get("emoji"):
                reaction_emoji = tool_call.tool_input["emoji"]
            if tool_call.tool_name == "ejecutar_comando" and tool_call.tool_output:
                try:
                    _parsed = json.loads(tool_call.tool_output)
                    if isinstance(_parsed, dict) and _parsed.get("__comando__"):
                        comando_data = _parsed
                except (json.JSONDecodeError, TypeError):
                    pass

        add_kapso_debug_event(
            "fastapi",
            "run_funnel_done",
            {
                "agent_id": int(agent_id),
                "person_id": person_id,
                "conversation_db_id": conversation_db_id,
                "message_id": request.message_id,
                "success": bool(funnel_result and funnel_result.success),
                "error": funnel_result.error if funnel_result else None,
                "timing": funnel_result.timing.model_dump() if funnel_result else None,
                "tools_used": [tool.model_dump() for tool in (funnel_result.tools_used if funnel_result else [])],
                "agent_runs": [agent_run.model_dump() for agent_run in (funnel_result.agent_runs if funnel_result else [])],
                "etapa_nueva": funnel_result.etapa_nueva if funnel_result else None,
                "metadata_actualizada": funnel_result.metadata_actualizada if funnel_result else None,
            },
        )

        add_kapso_debug_event(
            "fastapi",
            "run_contact_update_done",
            {
                "agent_id": int(agent_id),
                "person_id": person_id,
                "conversation_db_id": conversation_db_id,
                "message_id": request.message_id,
                "success": bool(contact_update_result and contact_update_result.success),
                "error": contact_update_result.error if contact_update_result else None,
                "timing": contact_update_result.timing.model_dump() if contact_update_result else None,
                "tools_used": [tool.model_dump() for tool in (contact_update_result.tools_used if contact_update_result else [])],
                "agent_runs": [agent_run.model_dump() for agent_run in (contact_update_result.agent_runs if contact_update_result else [])],
                "updated_fields": contact_update_result.updated_fields if contact_update_result else [],
                "contact_updates": contact_update_result.contact_updates if contact_update_result else None,
            },
        )

        add_kapso_debug_event(
            "fastapi",
            "run_agent_done",
            {
                "agent_id": int(agent_id),
                "agent_name": agent.get("agent_name") or str(agent_id),
                "enterprise_id": enterprise_id,
                "conversation_id": conversational_result.conversation_id,
                "model_used": conversational_result.model_used,
                "response_chars": len(conversational_result.response or ""),
                "response_preview": (conversational_result.response or "")[:600],
                "message_id": request.message_id,
                "timing": merged_timing.model_dump(),
                "tools_used": [tool.model_dump() for tool in merged_tools],
                "agent_runs": [agent_run.model_dump() for agent_run in merged_agent_runs],
                "reaction_emoji": reaction_emoji,
            },
        )
        logger.info(
            "Kapso inbound completado agent_id=%s conversation_id=%s model=%s response_chars=%s funnel_success=%s",
            agent_id,
            conversational_result.conversation_id,
            conversational_result.model_used,
            len(conversational_result.response or ""),
            bool(funnel_result and funnel_result.success),
        )

        # ── Detect closing followup (reaction-only, no text) ──
        is_closing_followup = (conversational_result.response or "").strip() == CLOSING_FOLLOWUP_MARKER

        if is_closing_followup:
            final_reply_text = ""
            add_kapso_debug_event(
                "fastapi",
                "closing_followup_detected",
                {
                    "message_id": request.message_id,
                    "conversation_id": conversational_result.conversation_id,
                    "user_message": (request.text or "")[:200],
                    "reaction_emoji": reaction_emoji,
                },
            )
            # Save in Supabase for review (tipo="closing_followup")
            if conversation_db_id:
                await db.insertar_mensaje(
                    conversacion_id=int(conversation_db_id),
                    contenido=f"[closing_followup] reacción: {reaction_emoji or '👍'}",
                    remitente="assistant",
                    tipo="texto",
                    status="closing_followup",
                    modelo_llm=conversational_result.model_used,
                    metadata={
                        "source": "kapso_outbound",
                        "message_id": request.message_id,
                        "agent_id": int(agent_id),
                        "closing_followup": True,
                        "user_message": (request.text or "")[:500],
                        "reaction_emoji": reaction_emoji,
                    },
                    empresa_id=enterprise_id,
                )
        else:
            final_reply_text = _ensure_reply_text(conversational_result.response)
            suppress_send = _should_suppress_kapso_send(final_reply_text)
            if final_reply_text != str(conversational_result.response or "").strip():
                add_kapso_debug_event(
                    "fastapi",
                    "empty_reply_fallback",
                    {
                        "message_id": request.message_id,
                        "conversation_id": conversational_result.conversation_id,
                        "fallback_text": final_reply_text,
                    },
                )

            if conversation_db_id and final_reply_text:
                await db.insertar_mensaje(
                    conversacion_id=int(conversation_db_id),
                    contenido=final_reply_text,
                    remitente="assistant",
                    tipo="texto",
                    status="suppressed" if suppress_send else "enviado",
                    modelo_llm=conversational_result.model_used,
                    metadata={
                        "source": "kapso_outbound",
                        "message_id": request.message_id,
                        "agent_id": int(agent_id),
                        "kapso_send_suppressed": suppress_send,
                    },
                    empresa_id=enterprise_id,
                )
            if suppress_send:
                add_kapso_debug_event(
                    "fastapi",
                    "kapso_send_suppressed",
                    {
                        "message_id": request.message_id,
                        "conversation_id": conversational_result.conversation_id,
                        "reply_preview": final_reply_text[:300],
                    },
                )

        if is_closing_followup:
            suppress_send = False

        await _update_inbound_message_statuses(inbound_message_ids, "enviado")

        reaction_payload = None
        if reaction_emoji:
            reaction_payload = KapsoReactionPayload(
                message_id=request.message_id,
                emoji=reaction_emoji,
            )

        # Resolve reply type and media fields
        reply_type = "reaction" if is_closing_followup and reaction_emoji else "text"
        image_url: str | None = None
        image_caption: str | None = None
        audio_url: str | None = None
        audio_caption: str | None = None
        video_url: str | None = None
        video_caption: str | None = None

        if comando_data:
            cmd = comando_data.get("comando", "")
            cmd_url = comando_data.get("solicitud", "")
            cmd_extra = comando_data.get("extra", "")
            if cmd == "image" and cmd_url:
                reply_type = "image"
                image_url = cmd_url
                image_caption = cmd_extra or final_reply_text
            elif cmd == "audio" and cmd_url:
                reply_type = "audio"
                audio_url = cmd_url
                audio_caption = cmd_extra or None
            elif cmd == "video" and cmd_url:
                reply_type = "video"
                video_url = cmd_url
                video_caption = cmd_extra or final_reply_text
            # "monica" keeps reply_type="text" — the agent response IS the analysis
            logger.info("Comando detectado: cmd=%s reply_type=%s url=%s", cmd, reply_type, cmd_url[:80] if cmd_url else "")

            # Append multimedia note to contact's notas in dim_person
            if person_id and cmd in ("image", "audio", "video"):
                try:
                    contacto_actual = await db.get_contacto(person_id)
                    notas_prev = (contacto_actual or {}).get("notas") or ""
                    nota_multimedia = f"Multimedia enviada: {cmd} de {cmd_extra}" if cmd_extra else f"Multimedia enviada: {cmd}"
                    nuevas_notas = f"{notas_prev}; {nota_multimedia}" if notas_prev else nota_multimedia
                    sb = await get_supabase()
                    await sb.update("dim_person", {"id": person_id}, {"notas": nuevas_notas})
                    logger.info("Notas multimedia actualizadas person_id=%s", person_id)
                except Exception:
                    logger.exception("Error actualizando notas multimedia person_id=%s", person_id)

        return KapsoInboundResponse(
            reply_type=reply_type,
            reply_text=final_reply_text or f"[closing_followup:{reaction_emoji or '👍'}]",
            suppress_send=suppress_send,
            reaction=reaction_payload,
            image_url=image_url,
            image_caption=image_caption,
            audio_url=audio_url,
            audio_caption=audio_caption,
            video_url=video_url,
            video_caption=video_caption,
            recipient_phone=request.from_phone,
            phone_number_id=request.phone_number_id,
            message_id=request.message_id,
            conversation_id=conversational_result.conversation_id,
            agent_id=int(agent_id),
            agent_name=agent.get("agent_name") or str(agent_id),
            model_used=conversational_result.model_used,
            timing=merged_timing,
            tools_used=merged_tools,
            agent_runs=merged_agent_runs,
        )
    except HTTPException as exc:
        mensajes_guardados_local = locals().get("mensajes_guardados", [])
        for message in mensajes_guardados_local:
            message_id = message.get("id") if isinstance(message, dict) else None
            if message_id is None:
                continue
            try:
                await db.actualizar_mensaje(
                    int(message_id),
                    _build_message_error_update(
                        message,
                        str(exc.detail),
                        "http_error",
                    ),
                )
            except Exception:
                logger.exception("kapso.fail_inbound_status_update", extra={"message_id": message_id})
        add_kapso_debug_event(
            "fastapi",
            "http_error",
            {"status_code": exc.status_code, "detail": str(exc.detail), "message_id": request.message_id},
        )
        await send_error_to_webhook(
            exc,
            context=f"kapso_inbound_http_{exc.status_code}",
            severity="error",
            fallback=f"El mensaje de {request.from_phone} (phone_number_id={request.phone_number_id}) "
                     f"no fue procesado. Error HTTP {exc.status_code}: {exc.detail}",
        )
        raise
    except Exception as exc:
        mensajes_guardados_local = locals().get("mensajes_guardados", [])
        for message in mensajes_guardados_local:
            message_id = message.get("id") if isinstance(message, dict) else None
            if message_id is None:
                continue
            try:
                await db.actualizar_mensaje(
                    int(message_id),
                    _build_message_error_update(
                        message,
                        str(exc),
                        type(exc).__name__,
                    ),
                )
            except Exception:
                logger.exception("kapso.fail_inbound_status_update", extra={"message_id": message_id})
        add_kapso_debug_event(
            "fastapi",
            "exception",
            {"error": str(exc), "message_id": request.message_id, "phone_number_id": request.phone_number_id},
        )
        logger.error("Kapso inbound error: %s", exc, exc_info=True)
        await send_error_to_webhook(
            exc,
            context="kapso_inbound_exception",
            severity="critical",
            fallback=f"El mensaje de {request.from_phone} (phone_number_id={request.phone_number_id}) "
                     f"no fue procesado. Excepción: {type(exc).__name__}: {exc}",
        )
        raise


# ════════════════════════════════════════════════════════════
# Retry stuck messages
# ════════════════════════════════════════════════════════════

STUCK_MESSAGE_MINUTES = 5  # Consider messages stuck after 5 minutes


async def _dispatch_to_bridge(reply_payload: dict) -> dict | None:
    """Send a processed reply to the Kapso bridge for WhatsApp delivery."""
    settings = get_settings()
    bridge_url = settings.KAPSO_BRIDGE_URL
    if not bridge_url:
        logger.warning("retry_stuck: KAPSO_BRIDGE_URL not configured, cannot dispatch")
        return None
    headers = {}
    if settings.KAPSO_INTERNAL_TOKEN:
        headers["x-kapso-internal-token"] = settings.KAPSO_INTERNAL_TOKEN
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{bridge_url}/api/v1/dispatch", json=reply_payload, headers=headers)
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        logger.error("retry_stuck: dispatch to bridge failed: %s", exc)
        return None


async def _retry_single_stuck_message(msg: dict) -> bool:
    """Re-process a single stuck inbound message and send the response via the bridge."""
    msg_id = msg.get("id")
    conversation_id_val = msg.get("conversation_id")
    content_text = msg.get("content_text") or ""
    metadata = msg.get("metadata") or {}
    timestamp = msg.get("timestamp") or ""

    if not conversation_id_val:
        logger.warning("retry_stuck: msg %s has no conversation_id, skipping", msg_id)
        await db.actualizar_mensaje(int(msg_id), {"status": "error", "metadata": {**metadata, "retry_error": "no conversation_id"}})
        return False

    # Check if there's already an agent response after this message (avoid duplicates)
    if timestamp and await db.has_agent_response_after(int(conversation_id_val), timestamp):
        logger.info("retry_stuck: msg %s already has agent response, marking enviado", msg_id)
        await db.actualizar_mensaje(int(msg_id), {"status": "enviado"})
        return True

    # Get conversation context
    conversacion = await db.get_conversacion(int(conversation_id_val))
    if not conversacion:
        logger.warning("retry_stuck: conversacion %s not found for msg %s", conversation_id_val, msg_id)
        await db.actualizar_mensaje(int(msg_id), {"status": "error", "metadata": {**metadata, "retry_error": "conversacion not found"}})
        return False

    person_id = conversacion.get("person_id")
    agent_id = conversacion.get("agent_id")
    enterprise_id = conversacion.get("enterprise_id")
    channel_id = conversacion.get("channel_id")

    if not agent_id:
        logger.warning("retry_stuck: no agent_id in conversacion %s", conversation_id_val)
        await db.actualizar_mensaje(int(msg_id), {"status": "error", "metadata": {**metadata, "retry_error": "no agent_id"}})
        return False

    agent = await db.get_agente(int(agent_id))
    if not agent:
        logger.warning("retry_stuck: agent %s not found", agent_id)
        await db.actualizar_mensaje(int(msg_id), {"status": "error", "metadata": {**metadata, "retry_error": "agent not found"}})
        return False

    # Get contact
    contacto = await db.get_contacto(int(person_id)) if person_id else None

    # Guard: contacto inactivo — no reintentar, marcar como enviado para no volver a procesar
    if contacto and contacto.get("is_active") is False:
        logger.info("retry_stuck: person_id=%s is_active=False — reintento cancelado", person_id)
        await db.actualizar_mensaje(int(msg_id), {"status": "enviado"})
        return True

    phone_number_id = metadata.get("phone_number_id", "")

    # Get numero for phone resolution
    numero = await db.get_numero(int(channel_id)) if channel_id else None

    # Resolve phone
    from_phone = ""
    if contacto and contacto.get("phone_e164"):
        from_phone = contacto["phone_e164"]
    elif metadata.get("contact_name"):
        from_phone = metadata.get("contact_name", "")

    if not from_phone and not phone_number_id:
        logger.warning("retry_stuck: cannot resolve phone for msg %s", msg_id)
        await db.actualizar_mensaje(int(msg_id), {"status": "error", "metadata": {**metadata, "retry_error": "cannot resolve phone"}})
        return False

    # Build memory session ID
    memory_session_id = str(person_id) if person_id else from_phone
    conversation_id_str = f"kapso:{metadata.get('kapso_conversation_id', conversation_id_val)}"

    # Build system prompt
    model = agent.get("llm") or None
    mcp_servers = _build_mcp_servers(agent)

    try:
        prompt_context_data = await db.load_kapso_prompt_context(
            contacto_id=int(person_id) if person_id else None,
            empresa_id=int(enterprise_id) if enterprise_id else None,
            conversacion_id=int(conversation_id_val),
            team_id=int(contacto["team_humano_id"]) if contacto and contacto.get("team_humano_id") is not None else None,
            agente_id=int(agent["id"]) if agent.get("id") is not None else None,
            agente_rol_id=int(agent["id_rol"]) if agent.get("id_rol") is not None else None,
            limite_mensajes=8,
        )

        # Build a minimal KapsoInboundRequest for prompt construction
        retry_inbound = KapsoInboundRequest(
            **{"from": from_phone},
            contact_name=metadata.get("contact_name"),
            phone_number_id=phone_number_id,
            kapso_conversation_id=str(metadata.get("kapso_conversation_id", conversation_id_val)),
            message_id=metadata.get("kapso_message_id", f"retry_{msg_id}"),
            message_type=metadata.get("message_type", "text"),
            text=content_text,
            timestamp=timestamp,
            has_media=metadata.get("has_media", False),
        )

        context_payload, prompt_extras = build_kapso_context_payload(
            contacto=contacto,
            agent=agent,
            empresa=prompt_context_data.get("empresa"),
            rol_agente=prompt_context_data.get("rol_agente"),
            team_humano=prompt_context_data.get("team_humano"),
            contextos=prompt_context_data.get("contextos") or [],
            citas=prompt_context_data.get("citas") or [],
            notificaciones=prompt_context_data.get("notificaciones") or [],
            mensajes_recientes=prompt_context_data.get("mensajes_recientes") or [],
            etapas_embudo=prompt_context_data.get("etapas_embudo") or [],
            notas=prompt_context_data.get("notas") or [],
            contexto_embudo_snapshot=prompt_context_data.get("contexto_embudo_snapshot"),
            etapas_embudo_snapshot=prompt_context_data.get("etapas_embudo_snapshot"),
            conversacion_memoria_snapshot=prompt_context_data.get("conversacion_memoria_snapshot"),
            inbound=retry_inbound,
        )

        system_prompt = build_kapso_system_prompt(
            agent=agent,
            inbound=retry_inbound,
            contacto=contacto,
            context_payload=context_payload,
            extras=prompt_extras,
            rol_agente=prompt_context_data.get("rol_agente"),
        )

        user_message = content_text.strip() or "El usuario envió un mensaje sin contenido legible."
        started_at = time.perf_counter()

        await db.actualizar_mensaje(int(msg_id), {"status": "procesando"})

        conversational_result, funnel_result, contact_update_result, merged_timing, merged_tools, merged_agent_runs = await _run_both_agents(
            started_at=started_at,
            system_prompt=system_prompt,
            user_message=user_message,
            raw_user_text=content_text,
            model=model,
            mcp_servers=mcp_servers,
            conversation_id=conversation_id_str,
            memory_session_id=memory_session_id,
            person_id=int(person_id) if person_id else None,
            enterprise_id=int(enterprise_id) if enterprise_id else None,
            agent_id=int(agent_id),
            conversation_db_id=int(conversation_id_val),
        )

        final_reply_text = _ensure_reply_text(conversational_result.response)
        suppress_send = _should_suppress_kapso_send(final_reply_text)

        # Save agent response in DB (no duplicate: we only save if no response exists yet)
        await db.insertar_mensaje(
            conversacion_id=int(conversation_id_val),
            contenido=final_reply_text,
            remitente="assistant",
            tipo="texto",
            status="suppressed" if suppress_send else "enviado",
            modelo_llm=conversational_result.model_used,
            metadata={
                "source": "retry_stuck",
                "original_message_id": msg_id,
                "agent_id": int(agent_id),
                "kapso_send_suppressed": suppress_send,
            },
            empresa_id=int(enterprise_id) if enterprise_id else None,
        )

        # Mark original inbound message as enviado
        await db.actualizar_mensaje(int(msg_id), {"status": "enviado"})

        # Dispatch to bridge for WhatsApp delivery
        if not suppress_send:
            reply_payload = {
                "reply_type": "text",
                "reply_text": final_reply_text,
                "suppress_send": False,
                "recipient_phone": from_phone,
                "phone_number_id": phone_number_id,
                "message_id": metadata.get("kapso_message_id", f"retry_{msg_id}"),
            }
            dispatch_result = await _dispatch_to_bridge(reply_payload)
            logger.info("retry_stuck: msg %s dispatched, result=%s", msg_id, dispatch_result)

        add_kapso_debug_event(
            "fastapi",
            "retry_stuck_success",
            {
                "original_message_id": msg_id,
                "conversation_id": conversation_id_val,
                "person_id": person_id,
                "response_chars": len(final_reply_text),
                "response_preview": final_reply_text[:200],
                "suppressed": suppress_send,
            },
        )
        logger.info("retry_stuck: msg %s processed successfully, response_chars=%s", msg_id, len(final_reply_text))
        return True

    except Exception as exc:
        logger.error("retry_stuck: failed to process msg %s: %s", msg_id, exc, exc_info=True)
        await db.actualizar_mensaje(int(msg_id), {
            "status": "error",
            "metadata": {**metadata, "retry_error": str(exc), "retry_error_type": type(exc).__name__},
        })
        add_kapso_debug_event(
            "fastapi",
            "retry_stuck_error",
            {"original_message_id": msg_id, "error": str(exc)},
        )
        return False


async def retry_stuck_messages() -> dict:
    """Find and re-process stuck messages. Called by the background task."""
    try:
        stuck = await db.get_stuck_messages(minutes_old=STUCK_MESSAGE_MINUTES, limit=10)
        if not stuck:
            return {"checked": True, "stuck_found": 0, "retried": 0, "success": 0}

        logger.info("retry_stuck: found %d stuck messages", len(stuck))
        add_kapso_debug_event(
            "fastapi",
            "retry_stuck_scan",
            {"stuck_count": len(stuck), "message_ids": [m.get("id") for m in stuck]},
        )

        success_count = 0
        for msg in stuck:
            try:
                ok = await _retry_single_stuck_message(msg)
                if ok:
                    success_count += 1
            except Exception as exc:
                logger.error("retry_stuck: unexpected error for msg %s: %s", msg.get("id"), exc)

        logger.info("retry_stuck: processed %d/%d stuck messages", success_count, len(stuck))
        return {"checked": True, "stuck_found": len(stuck), "retried": len(stuck), "success": success_count}
    except Exception as exc:
        logger.error("retry_stuck: scan failed: %s", exc, exc_info=True)
        return {"checked": True, "error": str(exc)}


@router.post("/retry-stuck")
async def kapso_retry_stuck(x_kapso_internal_token: str | None = Header(default=None)):
    """Manual trigger to retry stuck messages."""
    settings = get_settings()
    if settings.KAPSO_INTERNAL_TOKEN and x_kapso_internal_token != settings.KAPSO_INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = await retry_stuck_messages()
    return result
