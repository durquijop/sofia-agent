from pydantic import BaseModel, Field
from typing import Optional


class MCPServerConfig(BaseModel):
    url: str = Field(..., description="URL del MCP server (ej: https://marketia.app.n8n.cloud/mcp/aa0f6b46-...)")
    name: str = Field(default="", description="Nombre identificador del MCP server")


class ChatRequest(BaseModel):
    system_prompt: str = Field(..., description="System prompt que define el comportamiento del agente para la empresa")
    message: str = Field(..., description="Mensaje del usuario")
    model: Optional[str] = Field(default=None, description="Modelo a usar via OpenRouter (ej: x-ai/grok-4.1-fast). Si no se provee, usa el default")
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list, description="Lista de MCP servers para herramientas")
    conversation_id: Optional[str] = Field(default=None, description="ID de conversación para mantener contexto entre mensajes")
    max_tokens: Optional[int] = Field(default=1024, description="Máximo de tokens en la respuesta. Menor = más rápido. Default: 1024")
    temperature: Optional[float] = Field(default=0.7, description="Temperatura del modelo (0-2). Default: 0.7")
    memory_session_id: Optional[str] = None
    memory_window: Optional[int] = 8
    person_id: Optional[int] = None
    enterprise_id: Optional[int] = None
    channel: str = Field(
        default="generic",
        description=(
            "Canal de entrada del mensaje. "
            "Define qué tools built-in se inyectan en el agente. "
            "Valores conocidos: 'whatsapp' (Kapso), 'webhook', 'api', 'generic'."
        ),
    )


class ToolCall(BaseModel):
    tool_name: str
    tool_input: dict
    tool_output: str
    duration_ms: float | None = None
    status: str = "ok"
    error: str | None = None
    source: str | None = None
    description: str | None = None


class ToolDefinition(BaseModel):
    tool_name: str
    description: str | None = None
    source: str | None = None


class TimingInfo(BaseModel):
    total_ms: float = Field(..., description="Tiempo total de procesamiento en milisegundos")
    llm_ms: float = Field(default=0, description="Tiempo del LLM en milisegundos")
    mcp_discovery_ms: float = Field(default=0, description="Tiempo descubriendo herramientas MCP en milisegundos")
    graph_build_ms: float = Field(default=0, description="Tiempo construyendo el grafo en milisegundos")
    tool_execution_ms: float = Field(default=0, description="Tiempo ejecutando herramientas en milisegundos")


class AgentRunTrace(BaseModel):
    agent_key: str
    agent_name: str
    agent_kind: str = Field(default="response", description="Tipo lógico del agente dentro del flujo")
    conversation_id: str | None = None
    memory_session_id: str | None = None
    model_used: str
    system_prompt: str = ""
    user_prompt: str = ""
    available_tools: list[ToolDefinition] = Field(default_factory=list)
    tools_used: list[ToolCall] = Field(default_factory=list)
    timing: TimingInfo
    llm_iterations: int = 0


class ChatResponse(BaseModel):
    response: str = Field(..., description="Respuesta del agente")
    conversation_id: str = Field(..., description="ID de la conversación")
    model_used: str = Field(..., description="Modelo utilizado")
    tools_used: list[ToolCall] = Field(default_factory=list, description="Herramientas utilizadas durante la respuesta")
    timing: TimingInfo = Field(..., description="Métricas de tiempo de cada fase del procesamiento")
    agent_runs: list[AgentRunTrace] = Field(default_factory=list, description="Traza detallada de cada agente ejecutado en la interacción")
