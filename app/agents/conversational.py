import asyncio
import logging
import time
import uuid
from typing import Annotated, TypedDict

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.core.cache import response_cache
from app.core.config import get_settings
from app.mcp_client.client import MCPClient, mcp_tools_to_langchain
from app.schemas.chat import ChatRequest, ChatResponse, TimingInfo, ToolCall

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    tools_used: list[ToolCall]


_llm_cache: dict[str, ChatOpenAI] = {}
_shared_http_client: httpx.AsyncClient | None = None


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
        tools = await mcp_tools_to_langchain(client)
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


def _build_graph(llm_with_tools, tools: list) -> StateGraph:
    """Construye el grafo LangGraph para el agente conversacional."""

    async def agent_node(state: AgentState) -> dict:
        """Nodo principal del agente: genera respuesta o decide usar herramientas."""
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    async def tool_tracker_node(state: AgentState) -> dict:
        """Nodo que trackea las herramientas usadas después de ejecutarlas."""
        tools_used = list(state.get("tools_used", []))
        for msg in state["messages"]:
            if isinstance(msg, ToolMessage):
                ai_messages = [m for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls]
                for ai_msg in ai_messages:
                    for tc in ai_msg.tool_calls:
                        if tc["name"] == msg.name or (hasattr(msg, 'tool_call_id') and tc.get("id") == msg.tool_call_id):
                            tool_call = ToolCall(
                                tool_name=tc["name"],
                                tool_input=tc["args"],
                                tool_output=str(msg.content)[:500],
                            )
                            if not any(t.tool_name == tool_call.tool_name and t.tool_input == tool_call.tool_input for t in tools_used):
                                tools_used.append(tool_call)
        return {"tools_used": tools_used}

    tool_node = ToolNode(tools)

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)
    graph.add_node("tool_tracker", tool_tracker_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _should_use_tools, {"tools": "tools", END: END})
    graph.add_edge("tools", "tool_tracker")
    graph.add_edge("tool_tracker", "agent")

    return graph


async def run_agent(request: ChatRequest) -> ChatResponse:
    """Ejecuta el agente conversacional completo."""
    t_start = time.perf_counter()
    settings = get_settings()
    model = request.model or settings.DEFAULT_MODEL
    max_tokens = request.max_tokens or 1024
    temperature = request.temperature if request.temperature is not None else 0.7
    conversation_id = request.conversation_id or str(uuid.uuid4())

    logger.info(f"run_agent: model={model}, max_tokens={max_tokens}")

    # Verificar cache (solo para requests sin MCP tools)
    if not request.mcp_servers:
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
            )

    # Cargar herramientas MCP (en paralelo)
    t_mcp = time.perf_counter()
    tools = []
    if request.mcp_servers:
        mcp_configs = [{"url": s.url, "name": s.name} for s in request.mcp_servers]
        tools = await _load_mcp_tools(mcp_configs)
        logger.info(f"Total herramientas cargadas: {len(tools)}")
    mcp_discovery_ms = (time.perf_counter() - t_mcp) * 1000

    # Crear LLM con parámetros del request
    llm = _create_llm(model, max_tokens, temperature)
    if tools:
        llm_with_tools = llm.bind_tools(tools)
    else:
        llm_with_tools = llm

    # Construir y compilar el grafo
    t_graph = time.perf_counter()
    graph = _build_graph(llm_with_tools, tools)
    compiled = graph.compile()
    graph_build_ms = (time.perf_counter() - t_graph) * 1000

    # Preparar mensajes iniciales
    messages = [
        SystemMessage(content=request.system_prompt),
        HumanMessage(content=request.message),
    ]

    # Ejecutar el grafo
    initial_state: AgentState = {
        "messages": messages,
        "tools_used": [],
    }

    t_llm = time.perf_counter()
    final_state = await compiled.ainvoke(initial_state)
    llm_ms = (time.perf_counter() - t_llm) * 1000

    # Extraer respuesta final
    last_message = final_state["messages"][-1]
    response_text = last_message.content if isinstance(last_message, AIMessage) else str(last_message.content)

    # Guardar en cache (solo sin MCP tools)
    if not request.mcp_servers:
        response_cache.set(request.system_prompt, request.message, model, response_text)

    total_ms = (time.perf_counter() - t_start) * 1000

    timing = TimingInfo(
        total_ms=round(total_ms, 1),
        llm_ms=round(llm_ms, 1),
        mcp_discovery_ms=round(mcp_discovery_ms, 1),
        graph_build_ms=round(graph_build_ms, 1),
    )
    logger.info(f"Timing - total: {timing.total_ms}ms | llm: {timing.llm_ms}ms | mcp: {timing.mcp_discovery_ms}ms | graph: {timing.graph_build_ms}ms")

    return ChatResponse(
        response=response_text,
        conversation_id=conversation_id,
        model_used=model,
        tools_used=final_state.get("tools_used", []),
        timing=timing,
    )
