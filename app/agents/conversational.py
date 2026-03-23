import asyncio
import logging
import time
import unicodedata
import uuid
from typing import Annotated, TypedDict

import httpx
from langchain_core.callbacks.base import Callbacks
from langchain_core.caches import BaseCache
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.core.cache import response_cache
from app.core.config import get_settings
from app.db import queries as db
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
AGENT_GRAPH_TIMEOUT_SECONDS = 60
MCP_DISCOVERY_TIMEOUT_SECONDS = 15


@tool
def send_reaction(emoji: str) -> str:
    """Envía una reacción de emoji al mensaje del usuario en WhatsApp.
    Úsala cuando sientas que el mensaje merece una reacción emocional
    (ej: mensajes de amor, gratitud, buenas noticias, logros, humor).
    Ejemplos de emojis: ❤️ 🙏 😂 🎉 👍 🔥 😍 💪
    """
    return f"reaction:{emoji}"


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
        if (
            tool_names
            and all(name == SEND_REACTION_TOOL_NAME for name in tool_names)
            and all_tools_ok
            and _is_reaction_only_request(original_user_message)
        ):
            short_circuit_after_tools = True
            short_circuit_response = _build_reaction_ack(original_user_message, reaction_emoji)

        return {
            "messages": tool_messages,
            "tools_used": tools_used,
            "reaction_emoji": reaction_emoji,
            "tool_execution_ms": round(tool_execution_ms, 1),
            "short_circuit_after_tools": short_circuit_after_tools,
            "short_circuit_response": short_circuit_response,
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
    tools = [send_reaction]
    if request.mcp_servers and not reaction_only_request:
        mcp_configs = [{"url": s.url, "name": s.name} for s in request.mcp_servers]
        tools.extend(await _load_mcp_tools(mcp_configs))
        logger.info(f"Total herramientas cargadas: {len(tools)}")
    elif request.mcp_servers and reaction_only_request:
        logger.info("Omitiendo carga de herramientas MCP para solicitud enfocada en reacción")
    mcp_discovery_ms = (time.perf_counter() - t_mcp) * 1000
    available_tools = _describe_available_tools(tools)

    # Crear LLM con parámetros del request
    llm = _create_llm(model, max_tokens, temperature)
    llm_with_tools = llm.bind_tools(tools)

    # Construir y compilar el grafo
    t_graph = time.perf_counter()
    graph = _build_graph(llm_with_tools, tools)
    compiled = graph.compile()
    graph_build_ms = (time.perf_counter() - t_graph) * 1000

    # Preparar mensajes iniciales
    messages = [SystemMessage(content=request.system_prompt)]
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

    try:
        final_state = await asyncio.wait_for(
            compiled.ainvoke(initial_state),
            timeout=AGENT_GRAPH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"run_agent excedió {AGENT_GRAPH_TIMEOUT_SECONDS}s") from exc

    # Extraer respuesta final
    short_circuit_response = final_state.get("short_circuit_response")
    if short_circuit_response:
        response_text = short_circuit_response
    else:
        last_message = final_state["messages"][-1]
        response_text = str(last_message.content or "") if isinstance(last_message, AIMessage) else ""

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
