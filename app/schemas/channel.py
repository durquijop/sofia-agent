"""
Schemas de canal — contratos agnósticos al proveedor.

Cada canal tiene su propio inbound/outbound específico (KapsoInboundRequest,
WebhookOutboundResponse, etc.), pero todos comparten estas abstracciones
como contrato interno entre la capa de rutas y los agentes.

Flujo general:
    Entrada canal → [Adaptador inbound]  → ChatRequest → run_agent() → ChatResponse
                                                                              ↓
    Salida canal  ← [Adaptador outbound] ←─────────────────────────── ChatResponse
"""

from pydantic import BaseModel, Field


# ── Inbound (normalización de entrada) ────────────────────────────────────────

class ChannelMedia(BaseModel):
    """Archivo multimedia adjunto en un mensaje entrante."""
    kind: str                          # "image", "audio", "video", "document"
    url: str | None = None
    caption: str | None = None
    filename: str | None = None
    raw: dict | None = None            # Payload crudo del proveedor


class ChannelInboundMessage(BaseModel):
    """Mensaje de entrada normalizado, independiente del canal."""
    channel: str                       # "whatsapp", "webhook", "facebook", "sms", …
    provider: str | None = None        # "kapso", "twilio", "meta", …
    sender_id: str                     # ID único del remitente en el canal
    sender_phone: str | None = None
    sender_name: str | None = None
    external_conversation_id: str      # ID de conversación en el sistema del proveedor
    external_message_id: str           # ID de mensaje en el sistema del proveedor
    channel_account_id: str | None = None   # ID del número/canal receptor
    message_type: str = "text"         # "text", "image", "audio", "video", "document"
    text: str | None = None
    timestamp: str
    has_media: bool = False
    media: list[ChannelMedia] = Field(default_factory=list)
    raw_payload: dict | None = None    # Payload completo del proveedor (para debug)


# ── Outbound para canales de mensajería ───────────────────────────────────────
# (WhatsApp, Facebook Messenger, SMS, Telegram, …)
# Para estos canales el agente produce un mensaje que se envía a un usuario final.

class ChannelOutboundMessage(BaseModel):
    """
    Mensaje de salida normalizado para canales de mensajería.

    Cada canal tiene su propio adaptador outbound que convierte este objeto
    al formato específico del proveedor:
        WhatsApp/Kapso → KapsoInboundResponse  (en kapso_routes.py)
        Facebook       → FBMessengerResponse   (en facebook_routes.py — futuro)
        SMS/Twilio     → TwilioOutbound        (en sms_routes.py — futuro)
    """
    channel: str                       # Canal de destino
    provider: str | None = None        # Proveedor específico
    recipient_id: str                  # ID del destinatario en el canal
    recipient_phone: str | None = None
    channel_account_id: str | None = None
    external_message_id: str | None = None       # ID del mensaje original al que se responde
    external_conversation_id: str | None = None
    reply_type: str = "text"           # "text", "image", "audio", "video", "reaction", "document"
    text: str | None = None
    suppress_send: bool = False        # Si es True: persistir pero no enviar al usuario
    metadata: dict | None = None       # Datos adicionales opcionales por canal


# ── Outbound para canales API/webhook ─────────────────────────────────────────
# Para estos canales quien consume es un sistema (desarrollador), no un usuario final.
# Reciben el response completo del agente incluyendo trazas, timing y tools.

class WebhookOutboundResponse(BaseModel):
    """
    Respuesta de salida para canales tipo webhook/API.

    Devuelve el resultado completo del agente: respuesta, timing, tools usadas
    y trazas de ejecución. El consumidor es un sistema externo, no un usuario final.
    """
    channel: str = "webhook"           # Canal de origen ("webhook", "api", …)
    success: bool = True
    response: str                      # Respuesta generada por el agente
    conversation_id: str
    model_used: str
    tools_used: list[dict] = Field(default_factory=list)
    timing: dict = Field(default_factory=dict)
    agent_runs: list[dict] = Field(default_factory=list)
    error: str | None = None           # Mensaje de error si success=False
