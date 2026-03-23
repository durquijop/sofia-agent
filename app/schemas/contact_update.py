from pydantic import BaseModel, Field

from app.schemas.chat import AgentRunTrace, TimingInfo, ToolCall


class ContactUpdateAgentRequest(BaseModel):
    contacto_id: int = Field(..., description="ID del contacto a analizar")
    empresa_id: int = Field(..., description="ID de la empresa del contacto")
    agente_id: int = Field(..., description="ID del agente que ejecuta")
    conversacion_id: int | None = Field(default=None, description="ID de conversación para contexto")
    model: str | None = Field(default=None, description="Modelo LLM a usar")
    max_tokens: int | None = Field(default=512, description="Máximo de tokens")
    temperature: float | None = Field(default=0.2, description="Temperatura del modelo")


class ContactUpdateAgentResponse(BaseModel):
    success: bool = True
    respuesta: str = Field(..., description="Resultado breve del análisis o guardado")
    updated_fields: list[str] = Field(default_factory=list)
    contact_updates: dict | None = None
    tools_used: list[ToolCall] = Field(default_factory=list)
    timing: TimingInfo = Field(default_factory=lambda: TimingInfo(total_ms=0))
    agent_runs: list[AgentRunTrace] = Field(default_factory=list)
    error: str | None = None