import json
import logging

from fastapi import APIRouter, Header, HTTPException

from app.agents.conversational import run_agent
from app.core.config import get_settings
from app.db import queries as db
from app.schemas.chat import ChatRequest, MCPServerConfig
from app.schemas.kapso import KapsoInboundRequest, KapsoInboundResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/kapso", tags=["kapso"])


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


@router.post("/inbound", response_model=KapsoInboundResponse)
async def kapso_inbound(
    request: KapsoInboundRequest,
    x_kapso_internal_token: str | None = Header(default=None),
):
    settings = get_settings()
    if settings.KAPSO_INTERNAL_TOKEN and x_kapso_internal_token != settings.KAPSO_INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized Kapso bridge")

    numero = await db.get_numero_por_id_kapso(request.phone_number_id)
    if not numero:
        raise HTTPException(status_code=404, detail="No se encontró un canal activo para ese phone_number_id de Kapso")

    agente_id = numero.get("agente_id")
    if not agente_id:
        raise HTTPException(status_code=400, detail="El canal Kapso no tiene agente asignado")

    agent = await db.get_agente(int(agente_id))
    if not agent:
        raise HTTPException(status_code=404, detail="No se encontró el agente configurado para este canal")

    system_prompt = _build_system_prompt(agent, request)
    user_message = _build_user_message(request)
    mcp_servers = _build_mcp_servers(agent)
    model = agent.get("llm") or None
    conversation_id = f"kapso:{request.kapso_conversation_id}"

    logger.info(
        "Kapso inbound - agent_id=%s phone_number_id=%s from=%s message_type=%s",
        agente_id,
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
