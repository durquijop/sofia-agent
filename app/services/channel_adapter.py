"""
Channel Adapter — conversiones inbound/outbound entre canales y el núcleo.

Cada canal tiene:
  - Adaptador INBOUND:  convierte el payload del canal → ChatRequest (genérico)
  - Adaptador OUTBOUND: convierte ChatResponse (genérico) → respuesta del canal

Flujo:
    [Payload del canal]
          ↓ adaptador inbound
    [ChatRequest]  →  run_agent()  →  [ChatResponse]
                                            ↓ adaptador outbound
                                    [Respuesta del canal]

Canales implementados:
    ─ whatsapp/kapso  →  normalize_kapso_inbound()   +  kapso_routes.py (outbound)
    ─ webhook/api     →  (entrada directa, ya es JSON) + format_webhook_outbound()

Canales futuros (agregar aquí):
    ─ facebook        →  normalize_facebook_inbound() + format_facebook_outbound()
    ─ sms/twilio      →  normalize_twilio_inbound()   + format_twilio_outbound()
    ─ telegram        →  normalize_telegram_inbound()  + format_telegram_outbound()
"""

from app.schemas.channel import ChannelInboundMessage, ChannelMedia, ChannelOutboundMessage, WebhookOutboundResponse
from app.schemas.chat import ChatResponse
from app.schemas.kapso import KapsoInboundRequest


# ── INBOUND adapters ───────────────────────────────────────────────────────────

def normalize_kapso_inbound(request: KapsoInboundRequest) -> ChannelInboundMessage:
    """
    WhatsApp/Kapso → ChannelInboundMessage.

    Convierte el payload del webhook de Kapso al formato interno genérico.
    """
    media_items: list[ChannelMedia] = []
    if request.has_media:
        media_items.append(
            ChannelMedia(
                kind=str(request.message_type or "unknown"),
                raw=request.media_raw if isinstance(request.media_raw, dict) else None,
            )
        )

    return ChannelInboundMessage(
        channel="whatsapp",
        provider="kapso",
        sender_id=request.from_phone,
        sender_phone=request.from_phone,
        sender_name=request.contact_name,
        external_conversation_id=request.kapso_conversation_id,
        external_message_id=request.message_id,
        channel_account_id=request.phone_number_id,
        message_type=str(request.message_type or "text"),
        text=request.text,
        timestamp=request.timestamp,
        has_media=bool(request.has_media),
        media=media_items,
        raw_payload={
            "phone_number_id": request.phone_number_id,
            "media_raw": request.media_raw,
        },
    )


# ── OUTBOUND adapters ──────────────────────────────────────────────────────────

def format_webhook_outbound(
    response: ChatResponse,
    channel: str = "webhook",
    error: str | None = None,
) -> WebhookOutboundResponse:
    """
    ChatResponse → WebhookOutboundResponse.

    Formatea la respuesta del agente para un consumidor tipo webhook/API.
    Devuelve el resultado completo: respuesta, timing, tools y trazas de agente.

    El consumidor es un sistema externo (desarrollador), no un usuario final,
    por lo que recibe el trace completo a diferencia de los canales de mensajería.
    """
    return WebhookOutboundResponse(
        channel=channel,
        success=error is None,
        response=response.response,
        conversation_id=response.conversation_id,
        model_used=response.model_used,
        tools_used=[t.model_dump() for t in response.tools_used],
        timing=response.timing.model_dump(),
        agent_runs=[r.model_dump() for r in response.agent_runs],
        error=error,
    )


def format_generic_messaging_outbound(
    response: ChatResponse,
    channel: str,
    recipient_id: str,
    recipient_phone: str | None = None,
    channel_account_id: str | None = None,
    external_message_id: str | None = None,
    external_conversation_id: str | None = None,
    suppress_send: bool = False,
) -> ChannelOutboundMessage:
    """
    ChatResponse → ChannelOutboundMessage (mensajería genérica).

    Produce el mensaje normalizado para canales de mensajería (Facebook, SMS,
    Telegram, etc.). Cada canal tomará este objeto y lo convertirá al formato
    específico de su proveedor.

    Para WhatsApp/Kapso este paso NO se usa — kapso_routes.py maneja
    directamente la construcción de KapsoInboundResponse porque necesita
    campos específicos de WhatsApp (reacciones, botones, multimedia, etc.).
    """
    return ChannelOutboundMessage(
        channel=channel,
        recipient_id=recipient_id,
        recipient_phone=recipient_phone,
        channel_account_id=channel_account_id,
        external_message_id=external_message_id,
        external_conversation_id=external_conversation_id,
        reply_type="text",
        text=response.response,
        suppress_send=suppress_send,
        metadata={
            "conversation_id": response.conversation_id,
            "model_used": response.model_used,
            "total_ms": response.timing.total_ms,
        },
    )
