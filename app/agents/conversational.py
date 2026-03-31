import asyncio
import logging
import re
import time
import unicodedata
import uuid
from typing import Annotated, TypedDict

import httpx
from langchain_core.callbacks.base import Callbacks

from app.core.error_webhook import send_error_to_webhook
from langchain_core.caches import BaseCache
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.core.cache import response_cache
from app.core.config import get_settings
from app.db import queries as db
from app.db.client import get_supabase
from app.mcp_client.client import MCPClient, mcp_tools_to_langchain
from app.schemas.chat import AgentRunTrace, ChatRequest, ChatResponse, TimingInfo, ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

ChatOpenAI.model_rebuild(_types_namespace={"BaseCache": BaseCache, "Callbacks": Callbacks})


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    tools_used: list[ToolCall]
    reaction_emoji: str | None
    tool_execution_ms: float
    llm_elapsed_ms: float
    llm_iterations: int
    original_user_message: str
    short_circuit_after_tools: bool
    short_circuit_response: str | None


_llm_cache: dict[str, ChatOpenAI] = {}
_shared_http_client: httpx.AsyncClient | None = None

MAX_CONVERSATIONAL_LLM_ITERATIONS = 4
AGENT_GRAPH_TIMEOUT_SECONDS = 90
MCP_DISCOVERY_TIMEOUT_SECONDS = 15


@tool
def send_reaction(emoji: str) -> str:
    """Envía una reacción de emoji al mensaje del usuario en WhatsApp.
    Úsala cuando sientas que el mensaje merece una reacción emocional
    (ej: mensajes de amor, gratitud, buenas noticias, logros, humor).
    Ejemplos de emojis: ❤️ 🙏 😂 🎉 👍 🔥 😍 💪
    """
    return f"reaction:{emoji}"


def _create_guardar_nota_tool(contacto_id: int):
    """Crea un tool guardar_nota con el contacto_id capturado por closure."""

    @tool
    async def guardar_nota(nota: str) -> str:
        """🧠 MEMORIA PERSISTENTE / Tu agenda - Guarda información importante aquí para recordarla en futuras conversaciones.

USA DESPUÉS DE:
✓ Consultar otras herramientas (búsquedas, cálculos, APIs)
✓ Hacer acuerdos o compromisos
✓ Descubrir contexto relevante del contacto
✓ Luego de una búsqueda en la web o información que se requiere para tener contexto en las siguientes interacciones.

GUARDA:
• Resultados de herramientas externas
• Acuerdos y fechas importantes
• Cualquier dato que necesites recordar después

⚠️ CRÍTICO: Sin guardar aquí, perderás toda la información en la próxima conversación. Usa formato: [FECHA] CATEGORÍA: detalles

* No añadir datos que tienen variaciones como disponibilidad de agendas.

Información relevante: deuda, situación financiera, contexto importante.
Sistema de memoria a largo plazo.
No sobre escribas, agrega. Si actualizas sin añadir las notas anteriores puedes perder las notas anteriores.

Args:
    nota: Texto de la nota a guardar. Usa formato [FECHA] CATEGORÍA: detalles
"""
        try:
            supabase = await get_supabase()

            # Leer notas existentes para no sobreescribirlas
            existing = await supabase.query(
                "wp_contactos",
                select="notas",
                filters={"id": contacto_id},
                single=True,
            )
            existing_notas = ""
            if existing and existing.get("notas"):
                existing_notas = str(existing["notas"]).strip()

            # Append: notas anteriores + nueva nota
            if existing_notas:
                updated_notas = f"{existing_notas}\n{nota}"
            else:
                updated_notas = nota

            await supabase.update(
                "wp_contactos",
                filters={"id": contacto_id},
                data={"notas": updated_notas},
            )
            return f"✅ Nota guardada exitosamente para contacto {contacto_id}."
        except Exception as exc:
            logger.error("Error guardando nota para contacto %s: %s", contacto_id, exc)
            return f"❌ Error al guardar nota: {exc}"

    return guardar_nota


def _create_marcar_calificado_tool(contacto_id: int):
    """Crea un tool marcar_prospecto_calificado con el contacto_id capturado por closure."""

    @tool
    async def marcar_prospecto_calificado(es_calificado: str) -> str:
        """✅ Marcar_Prospecto_Calificado — Actualiza estado de calificación del contacto en base de datos.

PROPÓSITO: Registrar en el sistema cuando un prospecto cumple criterios de calificación.

MARCAR "si" cuando el contacto:
- Completó todas las preguntas de perfilación
- Cumple criterios de elegibilidad del servicio
- Está listo para agendar consulta
- No tiene objeciones bloqueantes

MARCAR "no" cuando el contacto:
- No cumple criterios mínimos
- Está fuera del mercado objetivo
- Tiene restricciones que impiden el servicio
- Expresamente no está interesado
- Solicita explícitamente que no quiere recibir más mensajes

MOMENTO DE EJECUCIÓN:
1. Después de completar perfilación
2. Antes de enviar link de calendario (si califica)
3. Una sola vez por contacto en la conversación

IMPORTANTE:
• Esta marca es permanente en el sistema
• Afecta el seguimiento y remarketing futuro
• Se usa para métricas de conversión
• NO cambiar si ya está marcado

NO USAR si no has completado la perfilación, para marcar interés temporal, o si el estado es ambiguo.

FLUJO: Perfilar → Evaluar → Marcar calificación → Si califica: continuar flujo.

Args:
    es_calificado: "si" o "no" (solo minúsculas)
"""
        valor = es_calificado.strip().lower()
        if valor not in ("si", "no"):
            return f"❌ Valor inválido: '{es_calificado}'. Debe ser 'si' o 'no'."
        try:
            supabase = await get_supabase()
            await supabase.update(
                "wp_contactos",
                filters={"id": contacto_id},
                data={"es_calificado": valor},
            )
            return f"✅ Contacto {contacto_id} marcado como es_calificado='{valor}'."
        except Exception as exc:
            logger.error("Error marcando calificación para contacto %s: %s", contacto_id, exc)
            return f"❌ Error al marcar calificación: {exc}"

    return marcar_prospecto_calificado


EJECUTAR_COMANDO_TOOL_NAME = "ejecutar_comando"

_VALID_COMMANDS = {"image", "audio", "video", "monica"}


def _create_comandos_tool(contacto_id: int):
    """Crea un tool ejecutar_comando con el contacto_id capturado por closure."""

    @tool
    async def ejecutar_comando(comando: str, solicitud: str, extra: str = "") -> str:
        """Ejecuta un comando especial del sistema para enviar multimedia o ejecutar análisis.

## Campos Requeridos

### 1. `comando`
Especifica el tipo de multimedia a enviar. **Selecciona solo una opción:**
- `image` - Para imágenes
- `audio` - Para archivos de audio
- `video` - Para videos

### 2. `solicitud`
Proporciona la URL del archivo multimedia.
- **Formato:** URL limpia, sin caracteres o elementos adicionales.
- **Ejemplo:** `https://ejemplo.com/imagen.jpg`

### 3. `extra`
Información sintetizada y concreta que se envía en el caption según el tipo de contenido:
- **Para imágenes:** Texto del caption (descripción)
- **Para audios:** Texto del caption (descripción)
- **Para videos:** Texto del caption (descripción concreta, que complementa el video pero no repite palabras del contenido)

IMPORTANTE:
• Para multimedia (image/audio/video), solicitud DEBE ser una URL pública válida.
• Revisa la sección de "Manejo de herramientas" y "Multimedia" en tus instrucciones del sistema para saber cuándo y cómo enviar multimedia, qué URLs usar y qué contenido asignar.
• No inventes URLs. Usa exclusivamente las URLs proporcionadas en tus instrucciones.

Args:
    comando: Tipo de comando a ejecutar ("image", "audio", "video")
    solicitud: URL pública del archivo multimedia
    extra: Texto del caption que acompaña al multimedia
"""
        import json as _json

        cmd = comando.strip().lower()
        if cmd not in _VALID_COMMANDS:
            return f"❌ Comando inválido: '{comando}'. Comandos válidos: {', '.join(sorted(_VALID_COMMANDS))}"

        result = {
            "__comando__": True,
            "comando": cmd,
            "solicitud": solicitud.strip(),
            "extra": extra.strip() if extra else "",
            "contacto_id": contacto_id,
        }
        logger.info(
            "ejecutar_comando: cmd=%s contacto_id=%s solicitud=%s",
            cmd, contacto_id, solicitud[:80],
        )
        return _json.dumps(result, ensure_ascii=False)

    return ejecutar_comando


DESACTIVAR_SPAM_URL = "https://vecspltvmyopwbjzerow.supabase.co/functions/v1/apagar-contacto-spam-v1"


def _create_desactivar_contacto_spam_tool(contacto_id: int, empresa_id: int):
    """Crea un tool para desactivar contacto spam via Edge Function."""

    @tool
    async def desactivar_contacto_spam() -> str:
        """🚫 Desactivar contacto por comportamiento inadecuado (spam/abuso).

USA ESTA HERRAMIENTA cuando el usuario muestre comportamiento inadecuado:
- Mensajes de spam repetitivos
- Contenido ofensivo, acoso o amenazas
- Intentos de phishing o estafa
- Abuso persistente del canal de comunicación

EFECTO: Desactiva permanentemente el contacto y bloquea futuras conversaciones.

⚠️ IMPORTANTE: Solo usar en casos claros de abuso. Esta acción es irreversible.
No requiere parámetros, se ejecuta automáticamente con los datos del contacto actual.
"""
        try:
            client = _get_http_client()
            resp = await client.post(
                DESACTIVAR_SPAM_URL,
                json={"contacto_id": contacto_id, "empresa_id": empresa_id},
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("success"):
                return f"✅ Contacto {contacto_id} desactivado correctamente. {data.get('message', '')}"
            return f"❌ Error al desactivar contacto: {data.get('error', resp.text)}"
        except Exception as exc:
            logger.error("Error desactivando contacto spam %s: %s", contacto_id, exc)
            return f"❌ Error al desactivar contacto: {exc}"

    return desactivar_contacto_spam


def _get_http_client() -> httpx.AsyncClient:
    """Retorna un cliente HTTP compartido con connection pooling."""
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_http_client


def _create_llm(model: str, max_tokens: int = 1024, temperature: float = 0.7) -> ChatOpenAI:
    """Crea una instancia del LLM usando OpenRouter. Cachea por modelo+params."""
    settings = get_settings()
    cache_key = f"{model}:{max_tokens}:{temperature}"

    if cache_key not in _llm_cache:
        _llm_cache[cache_key] = ChatOpenAI(
            model=model,
            openai_api_key=settings.OPENROUTER_API_KEY,
            openai_api_base=settings.OPENROUTER_BASE_URL,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout=30,
            http_async_client=_get_http_client(),
        )
        logger.info(f"LLM creado: model={model}, max_tokens={max_tokens}")
    return _llm_cache[cache_key]


async def _load_single_mcp(server_config: dict) -> list:
    """Carga herramientas de un solo MCP server."""
    try:
        client = MCPClient(
            server_url=server_config["url"],
            server_name=server_config.get("name", ""),
        )
        tools = await asyncio.wait_for(
            mcp_tools_to_langchain(client),
            timeout=MCP_DISCOVERY_TIMEOUT_SECONDS,
        )
        logger.info(f"Cargadas {len(tools)} herramientas desde MCP: {server_config['url']}")
        return tools
    except Exception as e:
        logger.error(f"Error cargando herramientas MCP desde {server_config['url']}: {e}")
        return []


async def _load_mcp_tools(mcp_servers: list[dict]) -> list:
    """Carga herramientas de todos los MCP servers EN PARALELO."""
    if not mcp_servers:
        return []
    results = await asyncio.gather(*[_load_single_mcp(cfg) for cfg in mcp_servers])
    all_tools = []
    for tool_list in results:
        all_tools.extend(tool_list)
    return all_tools


def _should_use_tools(state: AgentState) -> str:
    """Decide si el agente debe usar herramientas o terminar."""
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return END


def _tool_source(tool_obj) -> str:
    name = getattr(tool_obj, "name", "") or ""
    if name == SEND_REACTION_TOOL_NAME:
        return "kapso"
    return "mcp"


def _tool_description(tool_obj) -> str | None:
    description = getattr(tool_obj, "description", None)
    return str(description).strip() if description else None


def _describe_available_tools(tools: list) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            tool_name=getattr(tool_obj, "name", "unknown"),
            description=_tool_description(tool_obj),
            source=_tool_source(tool_obj),
        )
        for tool_obj in tools
    ]


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(without_accents.lower().split())


# Patterns that indicate the LLM leaked tool-calling instructions into text
_TOOL_LEAK_PATTERNS = re.compile(
    r"^("
    r"[Uu]sar\s+herramienta[s]?\s*[:：].*"
    r"|[Ll]lamar\s+herramienta[s]?\s*[:：].*"
    r"|[Uu]se\s+tool[s]?\s*[:：].*"
    r"|[Cc]all\s+tool[s]?\s*[:：].*"
    r"|[Tt]ool\s+call[s]?\s*[:：].*"
    r"|[Hh]erramienta\s*[:：]\s*\w+.*"
    r"|[Aa]cción\s*[:：]\s*\w+.*"
    r"|→\s*\w+\(.*\).*"
    r")$",
    re.MULTILINE,
)


def _clean_tool_leaks(text: str) -> str:
    """Remove lines where the LLM leaked tool usage instructions into the response."""
    if not text:
        return text
    cleaned = _TOOL_LEAK_PATTERNS.sub("", text)
    # Collapse multiple blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _is_reaction_only_request(message: str | None) -> bool:
    normalized = _normalize_text(message)
    if not normalized:
        return False

    reaction_markers = (
        "reacciona",
        "reacciona",
        "reaccion",
        "reaction",
        "emoji",
        "react to my message",
    )
    business_markers = (
        "visa",
        "cita",
        "agendar",
        "oferta",
        "puesto",
        "trabajo",
        "empleo",
        "precio",
        "asesor",
        "vacante",
        "asilo",
    )

    if not any(marker in normalized for marker in reaction_markers):
        return False
    if any(marker in normalized for marker in business_markers):
        return False

    return len(normalized.split()) <= 24


def _build_reaction_ack(_message: str | None, _emoji: str | None = None) -> str:
    return "Ya reaccioné."


# ── Closing followup: reaction-only for conversation-ending messages ──

# Emoji-only messages (user just sent an emoji, no text)
_EMOJI_ONLY_RE = re.compile(
    r"^[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    r"\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0000200D\U00002600-\U000026FF"
    r"\U0000231A-\U0000231B\U00002934-\U00002935\U000025AA-\U000025AB"
    r"\U000023E9-\U000023F3\U000023F8-\U000023FA\s]+$"
)

_CLOSING_PHRASES = {
    # Solo despedidas reales — NO confirmaciones como "si", "ok", "dale", "listo"
    # que pueden ser respuestas a preguntas del agente y requieren seguimiento.
    "bye", "chao", "adios", "nos vemos", "hasta luego", "hasta pronto",
    "cuídate", "cuidate", "buenas noches", "que descanses", "descansa",
    "un abrazo", "saludos", "bendiciones",
}

_CLOSING_BUSINESS_BLOCKERS = {
    "visa", "cita", "agendar", "agenda", "oferta", "puesto", "trabajo",
    "empleo", "precio", "costo", "cuanto", "cuánto", "asesor", "vacante",
    "asilo", "pregunta", "duda", "consulta", "como", "cómo", "cuando",
    "cuándo", "donde", "dónde", "porque", "por que", "necesito", "quiero",
    "ayuda", "problema", "urgente", "llamar", "llama", "informacion",
    "información", "detalles", "explica", "pero", "pago", "cobro",
    "proceso", "proseso",
}

CLOSING_FOLLOWUP_MARKER = "__closing_followup__"


def _is_closing_followup(message: str | None) -> bool:
    """Detect short farewell/goodbye messages that don't need a text reply.
    
    ONLY true despedidas (bye, chao, adios, buenas noches, etc.).
    Confirmaciones (si, ok, dale, listo, gracias) NO son closing — el usuario
    puede estar respondiendo a una pregunta del agente.
    """
    if not message:
        return False

    raw = str(message).strip()

    normalized = _normalize_text(raw)
    if not normalized:
        return False

    # Too long = probably a real question
    words = normalized.split()
    if len(words) > 4:
        return False

    # Contains business/question markers = real request
    if any(blocker in normalized for blocker in _CLOSING_BUSINESS_BLOCKERS):
        return False

    # Contains a question mark = probably needs a response
    if "?" in raw:
        return False

    # Exact match with a farewell phrase only
    if normalized in _CLOSING_PHRASES:
        return True
    if any(normalized.startswith(phrase) for phrase in _CLOSING_PHRASES if len(phrase.split()) <= 2):
        if len(words) <= 3:
            return True

    return False


def _infer_closing_emoji(message: str | None) -> str:
    """Pick the most appropriate emoji for a closing reaction."""
    normalized = _normalize_text(message)

    if any(w in normalized for w in ("gracias", "thanks", "bendicion", "bendiciones")):
        return "🙏"
    if any(w in normalized for w in ("abrazo", "carino", "cariño", "love", "te quiero")):
        return "❤️"
    if any(w in normalized for w in ("bye", "chao", "adios", "nos vemos", "hasta luego", "cuídate", "cuidate", "noches", "descanses")):
        return "👋"
    if any(w in normalized for w in ("perfecto", "excelente", "genial")):
        return "🔥"
    return "👍"


def _infer_reaction_emoji(message: str | None) -> str:
    normalized = _normalize_text(message)

    if any(marker in normalized for marker in ("amor", "te amo", "love", "corazon", "corazón", "carino", "cariño")):
        return "❤️"
    if any(marker in normalized for marker in ("gracias", "thanks", "agrade", "bendicion", "bendición")):
        return "🙏"
    if any(marker in normalized for marker in ("felicidades", "logro", "buenas noticias", "celebra", "gané", "gane")):
        return "🎉"
    if any(marker in normalized for marker in ("jaja", "jajaja", "gracioso", "chiste", "haha")):
        return "😂"
    if any(marker in normalized for marker in ("hola", "buenos dias", "buenas tardes", "saludos", "hello", "hi")):
        return "👋"
    return "👍"


def _should_continue_after_tools(state: AgentState) -> str:
    if state.get("short_circuit_after_tools"):
        return END
    if int(state.get("llm_iterations", 0)) >= MAX_CONVERSATIONAL_LLM_ITERATIONS:
        return END
    return "agent"


def _build_graph(llm_with_tools, tools: list) -> StateGraph:
    """Construye el grafo LangGraph para el agente conversacional."""

    tool_map = {getattr(tool_obj, "name", ""): tool_obj for tool_obj in tools}

    async def agent_node(state: AgentState) -> dict:
        """Nodo principal del agente: genera respuesta o decide usar herramientas."""
        t_llm = time.perf_counter()
        response = await llm_with_tools.ainvoke(state["messages"])
        llm_elapsed_ms = (time.perf_counter() - t_llm) * 1000
        return {
            "messages": [response],
            "llm_elapsed_ms": round(float(state.get("llm_elapsed_ms", 0)) + llm_elapsed_ms, 1),
            "llm_iterations": int(state.get("llm_iterations", 0)) + 1,
        }

    async def tool_execution_node(state: AgentState) -> dict:
        """Ejecuta herramientas y captura trazas detalladas por invocación."""
        tools_used = list(state.get("tools_used", []))
        reaction_emoji: str | None = state.get("reaction_emoji") or None
        tool_messages: list[ToolMessage] = []
        tool_execution_ms = float(state.get("tool_execution_ms", 0))
        short_circuit_after_tools = False
        short_circuit_response: str | None = state.get("short_circuit_response")
        last_message = state["messages"][-1]

        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {
                "messages": tool_messages,
                "tools_used": tools_used,
                "reaction_emoji": reaction_emoji,
                "tool_execution_ms": round(tool_execution_ms, 1),
                "short_circuit_after_tools": short_circuit_after_tools,
                "short_circuit_response": short_circuit_response,
            }

        tool_names: list[str] = []
        all_tools_ok = True
        for tc in last_message.tool_calls:
            tool_name = tc.get("name") or "unknown"
            raw_args = tc.get("args") or {}
            tool_input = raw_args if isinstance(raw_args, dict) else {"input": raw_args}
            tool_obj = tool_map.get(tool_name)
            tool_start = time.perf_counter()
            status = "ok"
            error_text: str | None = None

            try:
                if tool_obj is None:
                    raise ValueError(f"Tool no encontrada: {tool_name}")
                result = await tool_obj.ainvoke(tool_input)
                tool_output = str(result)[:1000]
            except Exception as exc:
                status = "error"
                error_text = str(exc)
                tool_output = f"Error ejecutando {tool_name}: {exc}"
                all_tools_ok = False

            duration_ms = (time.perf_counter() - tool_start) * 1000
            tool_execution_ms += duration_ms
            tool_names.append(tool_name)

            tool_messages.append(
                ToolMessage(
                    content=tool_output,
                    name=tool_name,
                    tool_call_id=tc.get("id"),
                )
            )

            tool_call = ToolCall(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output,
                duration_ms=round(duration_ms, 1),
                status=status,
                error=error_text,
                source=_tool_source(tool_obj) if tool_obj is not None else "unknown",
                description=_tool_description(tool_obj) if tool_obj is not None else None,
            )
            tools_used.append(tool_call)

            if tool_name == SEND_REACTION_TOOL_NAME and tool_input.get("emoji"):
                reaction_emoji = str(tool_input["emoji"])

        original_user_message = state.get("original_user_message") or ""
        only_reaction_tools = (
            tool_names
            and all(name == SEND_REACTION_TOOL_NAME for name in tool_names)
            and all_tools_ok
        )
        if only_reaction_tools and _is_reaction_only_request(original_user_message):
            short_circuit_after_tools = True

        # When only reaction was called but the message needs more processing
        # (e.g. user provided email + expects availability check), give back
        # the iteration so the agent keeps full budget for real tool work.
        current_iterations = int(state.get("llm_iterations", 0))
        if only_reaction_tools and not short_circuit_after_tools:
            current_iterations = max(0, current_iterations - 1)

        return {
            "messages": tool_messages,
            "tools_used": tools_used,
            "reaction_emoji": reaction_emoji,
            "tool_execution_ms": round(tool_execution_ms, 1),
            "short_circuit_after_tools": short_circuit_after_tools,
            "short_circuit_response": short_circuit_response,
            "llm_iterations": current_iterations,
        }

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_execution_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _should_use_tools, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _should_continue_after_tools, {"agent": "agent", END: END})

    return graph


SEND_REACTION_TOOL_NAME = "send_reaction"


def _memory_to_message(payload: dict | None):
    if not isinstance(payload, dict):
        return None
    role = str(payload.get("role") or "").strip().lower()
    content = payload.get("content")
    if not content:
        return None
    if role in {"user", "human"}:
        return HumanMessage(content=str(content))
    if role in {"assistant", "ai"}:
        return AIMessage(content=str(content))
    if role == "system":
        return SystemMessage(content=str(content))
    return None


async def _load_memory_messages(session_id: str, memory_window: int) -> list:
    try:
        rows = await db.get_agent_memory(session_id, limit=max(memory_window * 2, 2))
    except Exception as exc:
        logger.warning("No se pudo cargar agent_memory session_id=%s: %s", session_id, exc)
        return []

    messages: list = []
    for row in rows:
        message = _memory_to_message(row.get("message"))
        if message is not None:
            messages.append(message)
    return messages


async def _persist_memory_turn(session_id: str, user_message: str, assistant_message: str, conversation_id: str, model: str) -> None:
    try:
        await asyncio.gather(
            db.insert_agent_memory(
                session_id,
                {
                    "role": "user",
                    "content": user_message,
                    "conversation_id": conversation_id,
                },
            ),
            db.insert_agent_memory(
                session_id,
                {
                    "role": "assistant",
                    "content": assistant_message,
                    "conversation_id": conversation_id,
                    "model": model,
                },
            ),
        )
    except Exception as exc:
        logger.warning("No se pudo persistir agent_memory session_id=%s: %s", session_id, exc)


async def run_agent(request: ChatRequest) -> ChatResponse:
    """Ejecuta el agente conversacional completo."""
    t_start = time.perf_counter()
    settings = get_settings()
    model = request.model or settings.DEFAULT_MODEL
    max_tokens = request.max_tokens or 1024
    temperature = request.temperature if request.temperature is not None else 0.7
    conversation_id = request.conversation_id or str(uuid.uuid4())
    memory_session_id = request.memory_session_id.strip() if request.memory_session_id else None
    memory_window = max(1, request.memory_window or 8)
    reaction_only_request = _is_reaction_only_request(request.message)

    logger.info(f"run_agent: model={model}, max_tokens={max_tokens}")

    if reaction_only_request:
        emoji = _infer_reaction_emoji(request.message)
        available_tools = _describe_available_tools([send_reaction])
        tool_start = time.perf_counter()
        tool_output = await send_reaction.ainvoke({"emoji": emoji})
        tool_execution_ms = (time.perf_counter() - tool_start) * 1000
        total_ms = (time.perf_counter() - t_start) * 1000
        response_text = _build_reaction_ack(request.message, emoji)

        if memory_session_id:
            await _persist_memory_turn(memory_session_id, request.message, response_text, conversation_id, model)

        tool_call = ToolCall(
            tool_name=SEND_REACTION_TOOL_NAME,
            tool_input={"emoji": emoji},
            tool_output=str(tool_output),
            duration_ms=round(tool_execution_ms, 1),
            status="ok",
            error=None,
            source="kapso",
            description=_tool_description(send_reaction),
        )
        timing = TimingInfo(
            total_ms=round(total_ms, 1),
            llm_ms=0,
            mcp_discovery_ms=0,
            graph_build_ms=0,
            tool_execution_ms=round(tool_execution_ms, 1),
        )
        agent_runs = [
            AgentRunTrace(
                agent_key="conversational_agent",
                agent_name="Agente Conversacional",
                agent_kind="response",
                conversation_id=conversation_id,
                memory_session_id=memory_session_id,
                model_used=model,
                system_prompt=request.system_prompt,
                user_prompt=request.message,
                available_tools=available_tools,
                tools_used=[tool_call],
                timing=timing,
                llm_iterations=0,
            )
        ]
        logger.info("Fast-path de reacción aplicado conversation_id=%s emoji=%s total_ms=%.1f", conversation_id, emoji, timing.total_ms)
        return ChatResponse(
            response=response_text,
            conversation_id=conversation_id,
            model_used=model,
            tools_used=[tool_call],
            timing=timing,
            agent_runs=agent_runs,
        )

    # ── Fast-path: closing followup (reaction-only, no text) ──
    closing_followup = _is_closing_followup(request.message)
    if closing_followup:
        emoji = _infer_closing_emoji(request.message)
        available_tools = _describe_available_tools([send_reaction])
        tool_start = time.perf_counter()
        tool_output = await send_reaction.ainvoke({"emoji": emoji})
        tool_execution_ms = (time.perf_counter() - tool_start) * 1000
        total_ms = (time.perf_counter() - t_start) * 1000
        # Marker response — kapso_routes detects this to send reaction-only
        response_text = CLOSING_FOLLOWUP_MARKER

        if memory_session_id:
            await _persist_memory_turn(memory_session_id, request.message, f"[reacción de cierre: {emoji}]", conversation_id, model)

        tool_call = ToolCall(
            tool_name=SEND_REACTION_TOOL_NAME,
            tool_input={"emoji": emoji},
            tool_output=str(tool_output),
            duration_ms=round(tool_execution_ms, 1),
            status="ok",
            error=None,
            source="closing_followup",
            description="Reacción de cierre — mensaje no requiere respuesta de texto",
        )
        timing = TimingInfo(
            total_ms=round(total_ms, 1),
            llm_ms=0,
            mcp_discovery_ms=0,
            graph_build_ms=0,
            tool_execution_ms=round(tool_execution_ms, 1),
        )
        agent_runs = [
            AgentRunTrace(
                agent_key="conversational_agent",
                agent_name="Agente Conversacional",
                agent_kind="closing_followup",
                conversation_id=conversation_id,
                memory_session_id=memory_session_id,
                model_used=model,
                system_prompt="",
                user_prompt=request.message,
                available_tools=available_tools,
                tools_used=[tool_call],
                timing=timing,
                llm_iterations=0,
            )
        ]
        logger.info("Fast-path closing_followup conversation_id=%s emoji=%s message='%s' total_ms=%.1f", conversation_id, emoji, request.message[:50], timing.total_ms)
        return ChatResponse(
            response=response_text,
            conversation_id=conversation_id,
            model_used=model,
            tools_used=[tool_call],
            timing=timing,
            agent_runs=agent_runs,
        )

    # Verificar cache (solo para requests sin MCP tools)
    if not request.mcp_servers and not memory_session_id:
        cached = response_cache.get(request.system_prompt, request.message, model)
        if cached is not None:
            total_ms = (time.perf_counter() - t_start) * 1000
            logger.info(f"Cache HIT - total: {total_ms:.1f}ms")
            return ChatResponse(
                response=cached,
                conversation_id=conversation_id,
                model_used=model,
                tools_used=[],
                timing=TimingInfo(total_ms=round(total_ms, 1), llm_ms=0, mcp_discovery_ms=0, graph_build_ms=0),
                agent_runs=[],
            )

    # Cargar herramientas MCP (en paralelo)
    t_mcp = time.perf_counter()
    tools = []
    if request.mcp_servers and not reaction_only_request:
        mcp_configs = [{"url": s.url, "name": s.name} for s in request.mcp_servers]
        tools.extend(await _load_mcp_tools(mcp_configs))
        logger.info(f"Total herramientas cargadas: {len(tools)}")
    elif request.mcp_servers and reaction_only_request:
        logger.info("Omitiendo carga de herramientas MCP para solicitud enfocada en reacción")

    # Agregar tools built-in si hay contacto_id
    if request.contacto_id and not reaction_only_request:
        nota_tool = _create_guardar_nota_tool(request.contacto_id)
        calificado_tool = _create_marcar_calificado_tool(request.contacto_id)
        comandos_tool = _create_comandos_tool(request.contacto_id)
        tools.append(nota_tool)
        tools.append(calificado_tool)
        tools.append(comandos_tool)
        if request.empresa_id:
            spam_tool = _create_desactivar_contacto_spam_tool(request.contacto_id, request.empresa_id)
            tools.append(spam_tool)
        logger.info("Tools built-in agregadas para contacto_id=%s", request.contacto_id)

    mcp_discovery_ms = (time.perf_counter() - t_mcp) * 1000
    available_tools = _describe_available_tools(tools)

    # Enrich system prompt with explicit tool descriptions so the LLM knows what it can do
    system_prompt = request.system_prompt
    if tools:
        tool_lines = []
        for t in tools:
            name = getattr(t, "name", "unknown")
            desc = getattr(t, "description", "") or ""
            # Gather parameter names from the schema
            schema = getattr(t, "args_schema", None)
            if schema and hasattr(schema, "model_fields"):
                params = ", ".join(schema.model_fields.keys())
            else:
                params = ""
            tool_lines.append(f"- **{name}**({params}): {desc}")
        tool_section = (
            "\n\n---\n\n"
            "## 🔧 HERRAMIENTAS DISPONIBLES (MCP)\n"
            "Tienes acceso a las siguientes herramientas. DEBES usarlas cuando la situación lo requiera. "
            "No describas la herramienta al usuario ni menciones que la vas a usar; simplemente ejecútala internamente.\n\n"
            + "\n".join(tool_lines)
        )
        system_prompt = system_prompt + tool_section

    # Crear LLM con parámetros del request
    llm = _create_llm(model, max_tokens, temperature)
    llm_with_tools = llm.bind_tools(tools)

    # Construir y compilar el grafo
    t_graph = time.perf_counter()
    graph = _build_graph(llm_with_tools, tools)
    compiled = graph.compile()
    graph_build_ms = (time.perf_counter() - t_graph) * 1000

    # Preparar mensajes iniciales
    messages = [SystemMessage(content=system_prompt)]
    if memory_session_id and not reaction_only_request:
        memory_messages = await _load_memory_messages(memory_session_id, memory_window)
        messages.extend(memory_messages)
    messages.append(HumanMessage(content=request.message))

    # Ejecutar el grafo
    initial_state: AgentState = {
        "messages": messages,
        "tools_used": [],
        "reaction_emoji": None,
        "tool_execution_ms": 0,
        "llm_elapsed_ms": 0,
        "llm_iterations": 0,
        "original_user_message": request.message,
        "short_circuit_after_tools": False,
        "short_circuit_response": None,
    }

    timed_out = False
    try:
        final_state = await asyncio.wait_for(
            compiled.ainvoke(initial_state),
            timeout=AGENT_GRAPH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as timeout_exc:
        logger.warning("run_agent timeout after %ss — returning partial response", AGENT_GRAPH_TIMEOUT_SECONDS)
        await send_error_to_webhook(
            timeout_exc,
            context="conversational_agent_timeout",
            severity="warning",
            fallback="El agente conversacional excedió el timeout. Se devolvió respuesta parcial con el estado inicial. El usuario puede reintentar y el sistema sigue operativo.",
        )
        timed_out = True
        final_state = initial_state  # fallback to initial state

    # Extraer respuesta final
    short_circuit_response = final_state.get("short_circuit_response")
    if short_circuit_response:
        response_text = short_circuit_response
    elif timed_out:
        response_text = ""
    else:
        last_message = final_state["messages"][-1]
        response_text = str(last_message.content or "") if isinstance(last_message, AIMessage) else ""

    response_text = _clean_tool_leaks(response_text)

    # Guardar en cache (solo sin MCP tools)
    if memory_session_id:
        await _persist_memory_turn(memory_session_id, request.message, response_text, conversation_id, model)

    if not request.mcp_servers and not memory_session_id:
        response_cache.set(request.system_prompt, request.message, model, response_text)

    total_ms = (time.perf_counter() - t_start) * 1000
    llm_ms = float(final_state.get("llm_elapsed_ms", 0))
    tool_execution_ms = float(final_state.get("tool_execution_ms", 0))

    timing = TimingInfo(
        total_ms=round(total_ms, 1),
        llm_ms=round(llm_ms, 1),
        mcp_discovery_ms=round(mcp_discovery_ms, 1),
        graph_build_ms=round(graph_build_ms, 1),
        tool_execution_ms=round(tool_execution_ms, 1),
    )
    logger.info(f"Timing - total: {timing.total_ms}ms | llm: {timing.llm_ms}ms | mcp: {timing.mcp_discovery_ms}ms | graph: {timing.graph_build_ms}ms")

    agent_runs = [
        AgentRunTrace(
            agent_key="conversational_agent",
            agent_name="Agente Conversacional",
            agent_kind="response",
            conversation_id=conversation_id,
            memory_session_id=memory_session_id,
            model_used=model,
            system_prompt=request.system_prompt,
            user_prompt=request.message,
            available_tools=available_tools,
            tools_used=final_state.get("tools_used", []),
            timing=timing,
            llm_iterations=int(final_state.get("llm_iterations", 0)),
        )
    ]

    return ChatResponse(
        response=response_text,
        conversation_id=conversation_id,
        model_used=model,
        tools_used=final_state.get("tools_used", []),
        timing=timing,
        agent_runs=agent_runs,
    )
