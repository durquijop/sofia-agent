from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import AgentRunTrace, TimingInfo, ToolCall


class KapsoButton(BaseModel):
    id: str
    title: str


class KapsoListRow(BaseModel):
    id: str
    title: str
    description: str | None = None


class KapsoListSection(BaseModel):
    title: str
    rows: list[KapsoListRow]


class KapsoListPayload(BaseModel):
    button_text: str
    sections: list[KapsoListSection]


class KapsoReactionPayload(BaseModel):
    message_id: str
    emoji: str


class KapsoDocumentPayload(BaseModel):
    url: str
    filename: str
    caption: str | None = None


class KapsoInboundRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_phone: str = Field(..., alias="from", description="Número del remitente en formato WhatsApp")
    contact_name: str | None = Field(default=None, description="Nombre del contacto si Kapso lo provee")
    phone_number_id: str = Field(..., description="ID del número/canal conectado en Kapso")
    kapso_conversation_id: str = Field(..., description="ID de la conversación en Kapso")
    message_id: str = Field(..., description="ID único del mensaje en Kapso/WhatsApp")
    message_type: str = Field(default="text", description="Tipo de mensaje recibido")
    text: str | None = Field(default=None, description="Texto del mensaje si existe")
    timestamp: str = Field(..., description="Timestamp normalizado del mensaje")
    has_media: bool = Field(default=False, description="Indica si el mensaje tiene media adjunta")
    media_raw: dict | None = Field(default=None, description="Payload crudo de media cuando aplica")


class KapsoInboundResponse(BaseModel):
    reply_type: str = Field(default="text", description="Tipo de respuesta a enviar por Kapso")
    reply_text: str = Field(..., description="Texto final que debe enviarse al usuario por Kapso")
    buttons: list[KapsoButton] = Field(default_factory=list, description="Botones interactivos si aplica")
    list_payload: KapsoListPayload | None = Field(default=None, description="Payload de lista interactiva si aplica")
    reaction: KapsoReactionPayload | None = Field(default=None, description="Reacción de emoji si aplica")
    image_url: str | None = Field(default=None, description="URL pública de imagen si aplica")
    image_caption: str | None = Field(default=None, description="Caption de imagen si aplica")
    document: KapsoDocumentPayload | None = Field(default=None, description="Documento a enviar si aplica")
    recipient_phone: str = Field(..., description="Teléfono destinatario del reply")
    phone_number_id: str = Field(..., description="Número/canal de Kapso por el que se debe responder")
    message_id: str = Field(..., description="ID del mensaje original procesado")
    conversation_id: str = Field(..., description="ID lógico de conversación usado por el backend")
    agent_id: int = Field(..., description="ID del agente resuelto para este canal")
    agent_name: str = Field(..., description="Nombre del agente resuelto para este canal")
    model_used: str = Field(..., description="Modelo utilizado por el backend")
    timing: TimingInfo = Field(..., description="Métricas del procesamiento del agente")
    tools_used: list[ToolCall] = Field(default_factory=list, description="Herramientas usadas por el backend")
    agent_runs: list[AgentRunTrace] = Field(default_factory=list, description="Detalle de ejecución de cada agente del backend")
