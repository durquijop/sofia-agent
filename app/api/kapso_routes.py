import asyncio
import json
import logging
import os
import re
import time
import uuid

import httpx
from fastapi import APIRouter, Header, HTTPException

from app.agents.contact_update import run_contact_update_agent
from app.agents.conversational import run_agent
from app.agents.funnel import run_funnel_agent
from app.core.config import get_settings
from app.core.kapso_debug import (
    add_kapso_debug_event,
    get_kapso_debug_events,
    mask_secret,
)
from app.core.kapso_prompt import build_kapso_context_payload, build_kapso_system_prompt
from app.db import queries as db
from app.db.client import get_supabase
from app.schemas.chat import AgentRunTrace, ChatRequest, MCPServerConfig, TimingInfo, ToolCall
from app.schemas.contact_update import ContactUpdateAgentRequest, ContactUpdateAgentResponse
from app.schemas.funnel import FunnelAgentRequest, FunnelAgentResponse
from app.schemas.kapso import KapsoInboundRequest, KapsoInboundResponse, KapsoReactionPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kapso", tags=["kapso"])
DEFAULT_KAPSO_FALLBACK_PHONE = "14705500109"
DEFAULT_KAPSO_FALLBACK_AGENT_ID = 4
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

# Regex to parse Kapso audio enrichment text
_AUDIO_URL_RE = re.compile(r"URL:\s*(https?://[^\s]+)", re.IGNORECASE)
_AUDIO_TRANSCRIPT_RE = re.compile(r"Transcript:\s*(.+)", re.IGNORECASE | re.DOTALL)
_AUDIO_FILENAME_RE = re.compile(r"\(([^)]+\.ogg)\)", re.IGNORECASE)
SUPABASE_STORAGE_BUCKET = "multimedia"
SUPABASE_EDGE_CREAR_MULTIMEDIA = "crear-multimedia-inicial-v1"
SUPABASE_EDGE_GUARDAR_MULTIMEDIA = "guardar-multimedia-v4"


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

    url_match = _AUDIO_URL_RE.search(text)
    if url_match:
        audio_url = url_match.group(1).strip()

    transcript_match = _AUDIO_TRANSCRIPT_RE.search(text)
    if transcript_match:
        transcript = transcript_match.group(1).strip()

    filename_match = _AUDIO_FILENAME_RE.search(text)
    if filename_match:
        filename = filename_match.group(1).strip()

    return audio_url, transcript, filename


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


async def _multimedia_pipeline(contacto_id: int, archivo_url: str, transcript: str | None) -> None:
    """Async fire-and-forget pipeline: crear-multimedia → guardar-multimedia.

    Step 1: Call crear-multimedia-inicial-v1 to register in wp_multimedia.
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
                "contacto_id": contacto_id,
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
                contacto_id, archivo_url, str(resp1_data)[:200],
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
            contenido = transcript or f"Archivo multimedia: {archivo_url}"
            guardar_payload = {
                "multimedia_id": multimedia_id,
                "contenido": contenido,
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


async def _process_audio_message(inbound: KapsoInboundRequest, contacto_id: int | None = None) -> tuple[str | None, str | None]:
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
    if storage_url and contacto_id:
        asyncio.create_task(_multimedia_pipeline(contacto_id, storage_url, transcript))

    return transcript, storage_url


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
        return [{"contenido": message, "tipo": "texto"}] if message else []

    parts: list[dict[str, str]] = []
    if urls:
        text_with_placeholders = message
        for index, url in enumerate(urls, start=1):
            text_with_placeholders = text_with_placeholders.replace(url, f"(link-{index})")
        text_with_placeholders = re.sub(r"\n\s*\n+", "\n", text_with_placeholders).strip()
        if text_with_placeholders:
            parts.append({"contenido": text_with_placeholders, "tipo": "texto"})
        for url in urls:
            parts.append({"contenido": url, "tipo": "multimedia"})
    elif message:
        parts.append({"contenido": message, "tipo": "texto"})

    if inbound.has_media:
        media_reference = _extract_media_reference(inbound)
        if media_reference and media_reference not in {part["contenido"] for part in parts}:
            parts.append({"contenido": media_reference, "tipo": "multimedia"})

    return parts


def _build_user_message(inbound: KapsoInboundRequest, message_parts: list[dict[str, str]]) -> str:
    text_parts = [part["contenido"].strip() for part in message_parts if part["tipo"] == "texto" and part["contenido"].strip()]
    multimedia_parts = [
        part["contenido"].strip()
        for part in message_parts
        if part["tipo"] == "multimedia" and part["contenido"].strip()
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
    return KapsoInboundResponse(
        reply_type="text",
        reply_text=reply_text,
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


def _ensure_reply_text(reply_text: str | None) -> str:
    normalized = str(reply_text or "").strip()
    return normalized or DEFAULT_EMPTY_REPLY_TEXT


def _merge_timings(started_at: float, conversational_timing: TimingInfo, funnel_timing: TimingInfo | None = None) -> TimingInfo:
    funnel_timing = funnel_timing or TimingInfo(total_ms=0)
    total_ms = (time.perf_counter() - started_at) * 1000
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
    conversacion_db_id: int | None,
    error_text: str,
    timing: TimingInfo | None = None,
    tools_used: list[ToolCall] | None = None,
) -> FunnelAgentResponse:
    safe_timing = timing or TimingInfo(total_ms=0)
    trace = AgentRunTrace(
        agent_key="funnel_agent",
        agent_name="Agente de Embudo",
        agent_kind="analysis_error",
        conversation_id=str(conversacion_db_id) if conversacion_db_id else None,
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
    conversacion_db_id: int | None,
    contacto_id: int | None,
    error_text: str,
    timing: TimingInfo | None = None,
    tools_used: list[ToolCall] | None = None,
) -> ContactUpdateAgentResponse:
    safe_timing = timing or TimingInfo(total_ms=0)
    trace = AgentRunTrace(
        agent_key="contact_update_agent",
        agent_name="Agente de Actualización de Contacto",
        agent_kind="analysis_error",
        conversation_id=str(conversacion_db_id) if conversacion_db_id else None,
        memory_session_id=str(contacto_id) if contacto_id else None,
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
    contacto_id: int | None,
    empresa_id: int | None,
    agente_id: int,
    conversacion_db_id: int | None,
):
    # ── Phase 1: Run funnel + contact_update in parallel (BEFORE conversational) ──
    funnel_result = None
    contact_update_result = None

    run_funnel = contacto_id is not None and empresa_id is not None and _should_run_funnel_agent(raw_user_text)
    run_contact_update = contacto_id is not None and empresa_id is not None

    if run_funnel or run_contact_update:
        analysis_tasks: list[asyncio.Task] = []
        task_names: list[str] = []

        if run_funnel:
            analysis_tasks.append(
                asyncio.create_task(
                    asyncio.wait_for(
                        run_funnel_agent(
                            FunnelAgentRequest(
                                contacto_id=contacto_id,
                                empresa_id=empresa_id,
                                agente_id=agente_id,
                                conversacion_id=conversacion_db_id,
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
                                contacto_id=contacto_id,
                                empresa_id=empresa_id,
                                agente_id=agente_id,
                                conversacion_id=conversacion_db_id,
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
            conversacion_db_id=conversacion_db_id,
            error_text=str(funnel_result),
        )

    if isinstance(funnel_result, FunnelAgentResponse) and not funnel_result.success:
        logger.warning("Kapso inbound: funnel agent devolvio success=false: %s", funnel_result.error)
        if not funnel_result.agent_runs:
            funnel_result = _build_funnel_error_response(
                model=model,
                conversacion_db_id=conversacion_db_id,
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
            conversacion_db_id=conversacion_db_id,
            contacto_id=contacto_id,
            error_text=str(contact_update_result),
        )

    if isinstance(contact_update_result, ContactUpdateAgentResponse) and not contact_update_result.success:
        logger.warning("Kapso inbound: contact update agent devolvio success=false: %s", contact_update_result.error)
        if not contact_update_result.agent_runs:
            contact_update_result = _build_contact_update_error_response(
                model=model,
                conversacion_db_id=conversacion_db_id,
                contacto_id=contacto_id,
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
            contacto_id=contacto_id,
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
        "supabase_url": mask_secret(settings.SUPABASE_URL),
        "fallback_phone": DEFAULT_KAPSO_FALLBACK_PHONE,
        "fallback_agent_id": DEFAULT_KAPSO_FALLBACK_AGENT_ID,
    }


@router.get("/debug/events")
async def kapso_debug_events(limit: int = 100):
    return {"events": get_kapso_debug_events(limit)}


@router.get("/debug/config")
async def kapso_debug_config():
    return _get_debug_config()


# Las interacciones ahora se calculan directamente en JavaScript desde los eventos


@router.post("/inbound", response_model=KapsoInboundResponse)
async def kapso_inbound(
    request: KapsoInboundRequest,
    x_kapso_internal_token: str | None = Header(default=None),
):
    settings = get_settings()
    started_at = time.perf_counter()
    interaction_id = str(uuid.uuid4())
    add_kapso_debug_event(
        "fastapi",
        "inbound_received",
        {
            "phone_number_id": request.phone_number_id,
            "from": request.from_phone,
            "contact_name": request.contact_name,
            "conversation_id": request.kapso_conversation_id,
            "message_id": request.message_id,
            "message_type": request.message_type,
            "text": request.text,
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
        numero = await db.get_numero_por_id_kapso(request.phone_number_id)
        resolved_via_fallback = False
        if not numero:
            numero = await db.get_numero_por_telefono(DEFAULT_KAPSO_FALLBACK_PHONE)
            if numero:
                resolved_via_fallback = True
                add_kapso_debug_event(
                    "fastapi",
                    "fallback_numero",
                    {
                        "fallback_phone": DEFAULT_KAPSO_FALLBACK_PHONE,
                        "resolved_numero_id": numero.get("id"),
                        "resolved_agente_id": numero.get("agente_id"),
                        "phone_number_id": request.phone_number_id,
                        "message_id": request.message_id,
                    },
                )
                logger.warning(
                    "Kapso inbound usando fallback telefono=%s para phone_number_id=%s",
                    DEFAULT_KAPSO_FALLBACK_PHONE,
                    request.phone_number_id,
                )

        if numero and numero.get("agente_id"):
            agente_id = numero.get("agente_id")
        else:
            agente_id = DEFAULT_KAPSO_FALLBACK_AGENT_ID
            resolved_via_fallback = True

        numero_id = int(numero["id"]) if numero and numero.get("id") is not None else None
        empresa_id = int(numero["empresa_id"]) if numero and numero.get("empresa_id") is not None else None
        normalized_from_phone = _normalize_phone(request.from_phone) or request.from_phone
        slash_command = _extract_slash_command(request.text)
        contacto = None
        contacto_creado = False
        conversacion_db = None

        if not slash_command and empresa_id and numero_id:
            contacto, contacto_creado = await db.upsert_contacto_whatsapp(normalized_from_phone, empresa_id)
            if contacto and contacto.get("id") is not None:
                conversacion_db = await db.get_conversacion_activa(int(contacto["id"]), numero_id)
                if conversacion_db and conversacion_db.get("agente_id"):
                    agente_id = conversacion_db["agente_id"]

        agent = await db.get_agente(int(agente_id))
        if not agent and numero and numero.get("agente_id") and int(numero.get("agente_id")) != int(agente_id):
            agente_id = int(numero.get("agente_id"))
            agent = await db.get_agente(int(agente_id))
        if not agent and int(agente_id) != DEFAULT_KAPSO_FALLBACK_AGENT_ID:
            agente_id = DEFAULT_KAPSO_FALLBACK_AGENT_ID
            resolved_via_fallback = True
            agent = await db.get_agente(int(agente_id))
        if resolved_via_fallback:
            add_kapso_debug_event(
                "fastapi",
                "fallback_agent",
                {"agent_id": agente_id, "phone_number_id": request.phone_number_id, "message_id": request.message_id},
            )
            logger.warning(
                "Kapso inbound usando fallback agent_id=%s para phone_number_id=%s",
                agente_id,
                request.phone_number_id,
            )
        if not agent:
            raise HTTPException(status_code=404, detail="No se encontró el agente configurado para este canal")

        if empresa_id is None and agent.get("empresa_id") is not None:
            empresa_id = int(agent["empresa_id"])
        if contacto is None and empresa_id and normalized_from_phone:
            if slash_command:
                contacto = await db.get_contacto_por_telefono(normalized_from_phone, empresa_id)
                contacto_creado = False
            else:
                contacto, contacto_creado = await db.upsert_contacto_whatsapp(normalized_from_phone, empresa_id)
            if numero_id and contacto and contacto.get("id") is not None:
                conversacion_db = await db.get_conversacion_activa(int(contacto["id"]), numero_id)
        if not slash_command and empresa_id and numero_id and contacto and contacto.get("id") is not None and conversacion_db is None:
            try:
                conversacion_db = await db.insertar_conversacion(
                    contacto_id=int(contacto["id"]),
                    agente_id=int(agente_id),
                    empresa_id=empresa_id,
                    numero_id=numero_id,
                    canal=str(numero.get("canal") or "whatsapp"),
                    metadata=None,
                )
            except Exception:
                conversacion_db = await db.get_conversacion_activa(int(contacto["id"]), numero_id)
                if conversacion_db is None:
                    raise

        model = agent.get("llm") or None
        mcp_servers_list = _build_mcp_servers(agent)
        message_parts = _separate_message_parts(request)
        conversation_id = f"kapso:{request.kapso_conversation_id}"
        memory_session_id = normalized_from_phone
        if contacto and contacto.get("id") is not None:
            memory_session_id = str(contacto["id"])

        # Procesar audio: subir a Supabase Storage y extraer transcript
        audio_transcript: str | None = None
        audio_storage_url: str | None = None
        is_audio = str(request.message_type or "").strip().lower() == "audio"
        if is_audio:
            contacto_id_for_audio = contacto.get("id") if contacto else None
            audio_transcript, audio_storage_url = await _process_audio_message(request, contacto_id_for_audio)
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
                message_parts = [{"contenido": audio_transcript, "tipo": "texto"}]
                if audio_storage_url:
                    message_parts.append({"contenido": audio_storage_url, "tipo": "multimedia"})
                logger.info(
                    "Audio procesado → transcript=%s... storage_url=%s",
                    audio_transcript[:60],
                    audio_storage_url,
                )
            elif audio_storage_url:
                logger.info("Audio subido pero sin transcript. storage_url=%s", audio_storage_url)

        # Marcar origen como Whatsapp si aún no tiene valor
        if contacto and contacto.get("id") is not None and not contacto.get("origen"):
            try:
                supabase = await get_supabase()
                await supabase.update("wp_contactos", filters={"id": int(contacto["id"])}, data={"origen": "Whatsapp"})
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
                    "contacto_id": contacto.get("id") if contacto else None,
                    "conversation_db_id": conversacion_db.get("id") if conversacion_db else None,
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
                    "contacto_id": contacto.get("id") if contacto else None,
                    "memory_session_id": memory_session_id,
                    "reply_text": reply_text,
                },
            )

            return _build_command_response(
                request=request,
                conversation_id=conversation_id,
                agent_id=int(agente_id),
                agent_name=agent.get("nombre_agente") or str(agente_id),
                model_used=agent.get("llm") or settings.DEFAULT_MODEL,
                reply_text=reply_text,
                started_at=started_at,
            )

        contacto_id = int(contacto["id"]) if contacto and contacto.get("id") is not None else None
        conversacion_db_id = int(conversacion_db["id"]) if conversacion_db and conversacion_db.get("id") is not None else None
        prompt_context_data = await db.load_kapso_prompt_context(
            contacto_id=contacto_id,
            empresa_id=empresa_id,
            conversacion_id=conversacion_db_id,
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
                "contacto_id": contacto_id,
                "conversation_db_id": conversacion_db_id,
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
                "empresa_id": empresa_id,
                "numero_id": numero_id,
                "contacto_id": contacto.get("id") if contacto else None,
                "contacto_creado": contacto_creado,
                "conversacion_db_id": conversacion_db.get("id") if conversacion_db else None,
                "message_parts": message_parts,
            },
        )

        mensajes_guardados: list[dict] = []
        inbound_message_ids: list[int] = []
        if conversacion_db and conversacion_db.get("id") is not None:
            metadata_base = {
                "canal": str(numero.get("canal") or "whatsapp"),
                "phone_number_id": request.phone_number_id,
                "kapso_conversation_id": request.kapso_conversation_id,
                "kapso_message_id": request.message_id,
                "contact_name": request.contact_name,
                "message_type": request.message_type,
                "has_media": request.has_media,
            }
            for part in message_parts or [{"contenido": user_message, "tipo": "texto"}]:
                status = "procesando" if part["tipo"] == "multimedia" else "buffer"
                mensajes_guardados.append(
                    await db.insertar_mensaje(
                        conversacion_id=int(conversacion_db["id"]),
                        contenido=part["contenido"],
                        remitente="usuario",
                        tipo=part["tipo"],
                        status=status,
                        metadata=metadata_base,
                        empresa_id=empresa_id,
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
                "conversacion_db_id": conversacion_db.get("id") if conversacion_db else None,
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
                "memory_source": "contacto_id" if contacto and contacto.get("id") is not None else "from_phone",
                "contacto_id": contacto.get("id") if contacto else None,
                "from": normalized_from_phone,
                "message_id": request.message_id,
            },
        )

        add_kapso_debug_event(
            "fastapi",
            "run_agent_start",
            {
                "agent_id": int(agente_id),
                "fallback": resolved_via_fallback,
                "phone_number_id": request.phone_number_id,
                "message_id": request.message_id,
                "conversation_id": conversation_id,
                "memory_session_id": memory_session_id,
                "model": model,
                "mcp_servers": len(mcp_servers),
            },
        )
        logger.info(
            "Kapso inbound procesando agent_id=%s fallback=%s phone_number_id=%s from=%s message_type=%s",
            agente_id,
            resolved_via_fallback,
            request.phone_number_id,
            request.from_phone,
            request.message_type,
        )

        conversational_result, funnel_result, contact_update_result, merged_timing, merged_tools, merged_agent_runs = await _run_both_agents(
            started_at=started_at,
            system_prompt=system_prompt,
            user_message=user_message,
            raw_user_text=request.text,
            model=model,
            mcp_servers=mcp_servers,
            conversation_id=conversation_id,
            memory_session_id=memory_session_id,
            contacto_id=contacto_id,
            empresa_id=empresa_id,
            agente_id=int(agente_id),
            conversacion_db_id=conversacion_db_id,
        )

        reaction_emoji: str | None = None
        for tool_call in merged_tools:
            if tool_call.tool_name == "send_reaction" and tool_call.tool_input.get("emoji"):
                reaction_emoji = tool_call.tool_input["emoji"]
                break

        add_kapso_debug_event(
            "fastapi",
            "run_funnel_done",
            {
                "agent_id": int(agente_id),
                "contacto_id": contacto_id,
                "conversation_db_id": conversacion_db_id,
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
                "agent_id": int(agente_id),
                "contacto_id": contacto_id,
                "conversation_db_id": conversacion_db_id,
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
                "agent_id": int(agente_id),
                "agent_name": agent.get("nombre_agente") or str(agente_id),
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
            agente_id,
            conversational_result.conversation_id,
            conversational_result.model_used,
            len(conversational_result.response or ""),
            bool(funnel_result and funnel_result.success),
        )

        final_reply_text = _ensure_reply_text(conversational_result.response)
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

        if conversacion_db_id and final_reply_text:
            await db.insertar_mensaje(
                conversacion_id=int(conversacion_db_id),
                contenido=final_reply_text,
                remitente="agente",
                tipo="texto",
                status="sent",
                modelo_llm=conversational_result.model_used,
                metadata={
                    "source": "kapso_outbound",
                    "message_id": request.message_id,
                    "agent_id": int(agente_id),
                },
                empresa_id=empresa_id,
            )

        for inbound_message_id in inbound_message_ids:
            try:
                await db.actualizar_mensaje(inbound_message_id, {"status": "processed"})
            except Exception:
                logger.exception(
                    "kapso.finalize_inbound_status_failed",
                    extra={"message_id": inbound_message_id},
                )

        reaction_payload = None
        if reaction_emoji:
            reaction_payload = KapsoReactionPayload(
                message_id=request.message_id,
                emoji=reaction_emoji,
            )


        return KapsoInboundResponse(
            reply_type="text",
            reply_text=final_reply_text,
            reaction=reaction_payload,
            recipient_phone=request.from_phone,
            phone_number_id=request.phone_number_id,
            message_id=request.message_id,
            conversation_id=conversational_result.conversation_id,
            agent_id=int(agente_id),
            agent_name=agent.get("nombre_agente") or str(agente_id),
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
        raise
