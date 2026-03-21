import json
import logging
import os

from fastapi import APIRouter, Header, HTTPException

from app.agents.conversational import run_agent
from app.core.config import get_settings
from app.core.kapso_debug import add_kapso_debug_event, get_kapso_debug_events, mask_secret
from app.db import queries as db
from app.schemas.chat import ChatRequest, MCPServerConfig
from app.schemas.kapso import KapsoInboundRequest, KapsoInboundResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kapso", tags=["kapso"])
DEFAULT_KAPSO_FALLBACK_PHONE = "14705500109"
DEFAULT_KAPSO_FALLBACK_AGENT_ID = 4


def _build_system_prompt(agent: dict, inbound: KapsoInboundRequest) -> str:
    sections: list[str] = []
    nombre_agente = agent.get("nombre_agente") or agent.get("rol") or "Agente"
    sections.append(f"Eres {nombre_agente}.")

    instrucciones = agent.get("instrucciones")
    if instrucciones:
        sections.append(f"INSTRUCCIONES:\n{instrucciones}")

    comportamiento = agent.get("comportamiento")
    if comportamiento:
        sections.append(f"COMPORTAMIENTO:\n{comportamiento}")

    restricciones = agent.get("restricciones")
    if restricciones:
        sections.append(f"RESTRICCIONES:\n{restricciones}")

    instrucciones_mensajes = agent.get("instrucciones_mensajes")
    if instrucciones_mensajes:
        sections.append(f"INSTRUCCIONES DE MENSAJES:\n{instrucciones_mensajes}")

    instrucciones_multimedia = agent.get("instrucciones_multimedia")
    if instrucciones_multimedia:
        sections.append(f"INSTRUCCIONES MULTIMEDIA:\n{instrucciones_multimedia}")

    sections.append(
        "CONTEXTO DEL CANAL:\n"
        f"- Canal: WhatsApp via Kapso\n"
        f"- Contacto: {inbound.contact_name or 'Sin nombre'}\n"
        f"- Teléfono: {inbound.from_phone}\n"
        f"- Tipo de mensaje: {inbound.message_type}\n"
        f"- Tiene media: {'sí' if inbound.has_media else 'no'}"
    )

    return "\n\n".join(section for section in sections if section).strip()


def _build_user_message(inbound: KapsoInboundRequest) -> str:
    if inbound.text and inbound.text.strip():
        return inbound.text.strip()
    if inbound.has_media:
        return f"El usuario envió un mensaje multimedia de tipo {inbound.message_type} sin texto adicional."
    return f"El usuario envió un mensaje de tipo {inbound.message_type} sin contenido legible."


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


@router.post("/inbound", response_model=KapsoInboundResponse)
async def kapso_inbound(
    request: KapsoInboundRequest,
    x_kapso_internal_token: str | None = Header(default=None),
):
    settings = get_settings()
    add_kapso_debug_event(
        "fastapi",
        "inbound_received",
        {
            "phone_number_id": request.phone_number_id,
            "from": request.from_phone,
            "conversation_id": request.kapso_conversation_id,
            "message_id": request.message_id,
            "message_type": request.message_type,
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

        agent = await db.get_agente(int(agente_id))
        if not agent:
            raise HTTPException(status_code=404, detail="No se encontró el agente configurado para este canal")

        system_prompt = _build_system_prompt(agent, request)
        user_message = _build_user_message(request)
        mcp_servers = _build_mcp_servers(agent)
        model = agent.get("llm") or None
        conversation_id = f"kapso:{request.kapso_conversation_id}"

        add_kapso_debug_event(
            "fastapi",
            "run_agent_start",
            {
                "agent_id": int(agente_id),
                "fallback": resolved_via_fallback,
                "phone_number_id": request.phone_number_id,
                "message_id": request.message_id,
                "conversation_id": conversation_id,
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

        result = await run_agent(
            ChatRequest(
                system_prompt=system_prompt,
                message=user_message,
                model=model,
                mcp_servers=mcp_servers,
                conversation_id=conversation_id,
            )
        )

        add_kapso_debug_event(
            "fastapi",
            "run_agent_done",
            {
                "agent_id": int(agente_id),
                "conversation_id": result.conversation_id,
                "model_used": result.model_used,
                "response_chars": len(result.response or ""),
                "message_id": request.message_id,
            },
        )
        logger.info(
            "Kapso inbound completado agent_id=%s conversation_id=%s model=%s response_chars=%s",
            agente_id,
            result.conversation_id,
            result.model_used,
            len(result.response or ""),
        )

        return KapsoInboundResponse(
            reply_type="text",
            reply_text=result.response,
            recipient_phone=request.from_phone,
            phone_number_id=request.phone_number_id,
            message_id=request.message_id,
            conversation_id=result.conversation_id,
            agent_id=int(agente_id),
            agent_name=agent.get("nombre_agente") or str(agente_id),
            model_used=result.model_used,
            timing=result.timing,
        )
    except HTTPException as exc:
        add_kapso_debug_event(
            "fastapi",
            "http_error",
            {"status_code": exc.status_code, "detail": str(exc.detail), "message_id": request.message_id},
        )
        raise
    except Exception as exc:
        add_kapso_debug_event(
            "fastapi",
            "exception",
            {"error": str(exc), "message_id": request.message_id, "phone_number_id": request.phone_number_id},
        )
        logger.error("Kapso inbound error: %s", exc, exc_info=True)
        raise
