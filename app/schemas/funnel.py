from pydantic import BaseModel, Field
from typing import Optional
from app.schemas.chat import TimingInfo, AgentRunTrace, ToolCall


class FunnelContextRequest(BaseModel):
    """Request para cargar contexto del embudo."""
    contacto_id: int = Field(..., description="ID del contacto")
    empresa_id: int = Field(..., description="ID de la empresa")
    agente_id: int = Field(..., description="ID del agente")
    conversacion_id: Optional[int] = Field(default=None, description="ID de la conversación (opcional)")
    limite_mensajes: int = Field(default=20, description="Límite de mensajes a recuperar")


class FunnelStageInfo(BaseModel):
    """Información de una etapa del embudo."""
    id: int
    nombre_etapa: str
    orden_etapa: int
    descripcion: Optional[dict] = None
    es_etapa_actual: bool = False


class FunnelCurrentStage(BaseModel):
    """Etapa actual del contacto."""
    id: int
    orden: int
    nombre: str
    que_es: Optional[str] = None
    senales: Optional[list[str]] = None


class ContactInfo(BaseModel):
    """Información básica del contacto."""
    contacto_id: int
    nombre_completo: str
    nombre: Optional[str] = None
    apellido: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    origen: Optional[str] = None
    notas: Optional[str] = None
    fecha_registro: Optional[str] = None
    ultima_interaccion: Optional[str] = None
    subscriber_id: Optional[str] = None
    avatar_url: Optional[str] = None
    etapa_emocional: Optional[str] = None
    timezone: Optional[str] = None
    estado: Optional[str] = None
    es_calificado: Optional[bool | str] = None
    is_active: Optional[bool] = None
    team_humano_id: Optional[int] = None
    url_drive: Optional[str] = None
    etapa_actual_orden: Optional[int] = None
    metadata: Optional[dict] = None


class FunnelContextResponse(BaseModel):
    """Contexto completo del embudo para el agente."""
    contacto: ContactInfo
    etapa_actual: Optional[FunnelCurrentStage] = None
    todas_etapas: list[FunnelStageInfo] = Field(default_factory=list)
    tiene_embudo: bool = False
    conversacion_resumen: Optional[str] = None
    ultimos_mensajes: Optional[list[dict]] = None
    contexto_embudo: Optional[dict] = None
    etapas_embudo: Optional[dict] = None
    conversacion_memoria: Optional[dict] = None


class FunnelAgentRequest(BaseModel):
    """Request hacia el agente de embudo."""
    contacto_id: int = Field(..., description="ID del contacto a analizar")
    empresa_id: int = Field(..., description="ID de la empresa")
    agente_id: int = Field(..., description="ID del agente que ejecuta")
    conversacion_id: Optional[int] = Field(default=None, description="ID de conversación para contexto")
    memory_session_id: Optional[str] = Field(default=None, description="ID de memoria persistente para recuperar turnos previos")
    memory_window: Optional[int] = Field(default=8, description="Cantidad de turnos persistentes a cargar")
    model: Optional[str] = Field(default=None, description="Modelo LLM a usar")
    max_tokens: Optional[int] = Field(default=512, description="Máximo de tokens")
    temperature: Optional[float] = Field(default=0.5, description="Temperatura del modelo")


class FunnelAgentResponse(BaseModel):
    """Respuesta del agente de embudo."""
    success: bool = True
    respuesta: str = Field(..., description="Respuesta del agente para el equipo (máx 3 líneas)")
    etapa_anterior: Optional[str] = None
    etapa_nueva: Optional[int] = None  # Tipo: int (orden_etapa)
    metadata_actualizada: Optional[dict] = None
    tools_used: list[ToolCall] = Field(default_factory=list)
    timing: TimingInfo = Field(default_factory=lambda: TimingInfo(total_ms=0))
    agent_runs: list[AgentRunTrace] = Field(default_factory=list)
    error: Optional[str] = None
