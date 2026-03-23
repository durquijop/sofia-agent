import asyncio
import json
import logging
import os
import re
import time
import uuid

from fastapi import APIRouter, Header, HTTPException

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
from app.schemas.chat import AgentRunTrace, ChatRequest, MCPServerConfig, TimingInfo, ToolCall
from app.schemas.funnel import FunnelAgentRequest, FunnelAgentResponse
from app.schemas.kapso import KapsoInboundRequest, KapsoInboundResponse, KapsoReactionPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kapso", tags=["kapso"])
DEFAULT_KAPSO_FALLBACK_PHONE = "14705500109"
DEFAULT_KAPSO_FALLBACK_AGENT_ID = 4
FUNNEL_TIMEOUT_SECONDS = 25
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


async def _run_both_agents(
    *,
    started_at: float,
    system_prompt: str,
    user_message: str,
    model: str | None,
    mcp_servers: list[MCPServerConfig],
    conversation_id: str,
    memory_session_id: str,
    contacto_id: int | None,
    empresa_id: int | None,
    agente_id: int,
    conversacion_db_id: int | None,
):
    conversational_task = asyncio.create_task(
        run_agent(
            ChatRequest(
                system_prompt=system_prompt,
                message=user_message,
                model=model,
                mcp_servers=mcp_servers,
                conversation_id=conversation_id,
                memory_session_id=memory_session_id,
                memory_window=8,
            )
        )
    )

    funnel_task = None
    if contacto_id is not None and empresa_id is not None:
        funnel_task = asyncio.create_task(
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
            )
        )

    funnel_awaitable = asyncio.sleep(0, result=None)
    if funnel_task is not None:
        funnel_awaitable = asyncio.wait_for(funnel_task, timeout=FUNNEL_TIMEOUT_SECONDS)

    results = await asyncio.gather(
        conversational_task,
        funnel_awaitable,
        return_exceptions=True,
    )
    conversational_result = results[0]
    funnel_result = results[1]

    if isinstance(conversational_result, Exception):
        raise conversational_result

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

    merged_timing = _merge_timings(
        started_at,
        conversational_result.timing,
        funnel_result.timing if isinstance(funnel_result, FunnelAgentResponse) else None,
    )
    merged_tools = _merge_tool_calls(
        conversational_result.tools_used,
        funnel_result.tools_used if isinstance(funnel_result, FunnelAgentResponse) else None,
    )
    merged_agent_runs = _merge_agent_runs(
        conversational_result.agent_runs,
        funnel_result.agent_runs if isinstance(funnel_result, FunnelAgentResponse) else None,
    )
    return conversational_result, funnel_result, merged_timing, merged_tools, merged_agent_runs


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

        conversational_result, funnel_result, merged_timing, merged_tools, merged_agent_runs = await _run_both_agents(
            started_at=started_at,
            system_prompt=system_prompt,
            user_message=user_message,
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

        if conversacion_db_id and (conversational_result.response or "").strip():
            await db.insertar_mensaje(
                conversacion_id=int(conversacion_db_id),
                contenido=(conversational_result.response or "").strip(),
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
            reply_text=conversational_result.response,
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
