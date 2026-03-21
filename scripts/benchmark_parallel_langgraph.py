import asyncio
import json
import sys
import time
from typing import Annotated, TypedDict
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


_shared_http_client: httpx.AsyncClient | None = None
_llm_cache: dict[str, ChatOpenAI] = {}


def _get_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_http_client


def _create_llm(model: str, max_tokens: int, temperature: float) -> ChatOpenAI:
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
    return _llm_cache[cache_key]


def _format_ms(value: float) -> str:
    return f"{value:.1f}ms"


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    total = sum(ordered)
    avg = total / len(ordered)
    mid = len(ordered) // 2
    p50 = (ordered[mid - 1] + ordered[mid]) / 2 if len(ordered) % 2 == 0 else ordered[mid]
    return {
        "min": ordered[0],
        "max": ordered[-1],
        "avg": avg,
        "p50": p50,
    }


def _build_agent_spec(agent_name: str):
    if agent_name == "funnel":
        system = "Eres un agente de embudo. Debes llamar get_funnel_rules y get_contact_signals antes de responder. Si es útil, también puedes llamar get_recent_activity. Responde en máximo 3 líneas para el equipo interno."
        prompt = "Analiza el lead 328159. Debes usar get_funnel_rules y get_contact_signals antes de concluir. Si existe una señal reciente importante, consulta get_recent_activity también. No inventes datos."

        def tools_factory(metrics: list[dict]):
            @tool
            async def get_funnel_rules(lead_id: int) -> dict:
                """Obtiene reglas del embudo y criterios para clasificar la etapa actual."""
                started = time.perf_counter()
                await asyncio.sleep(0.24)
                result = {
                    "lead_id": lead_id,
                    "available_stages": ["nuevo", "interesado", "calificado", "cita"],
                    "current_stage_hint": "interesado",
                    "required_fields": ["presupuesto", "necesidad", "canal_origen"],
                }
                metrics.append({"name": "get_funnel_rules", "ms": (time.perf_counter() - started) * 1000})
                return result

            @tool
            async def get_contact_signals(lead_id: int) -> dict:
                """Obtiene señales recientes del lead como intención, presupuesto y urgencia."""
                started = time.perf_counter()
                await asyncio.sleep(0.36)
                result = {
                    "lead_id": lead_id,
                    "intent": "alta",
                    "budget": "medio",
                    "urgency": "esta_semana",
                    "asks_for_pricing": True,
                }
                metrics.append({"name": "get_contact_signals", "ms": (time.perf_counter() - started) * 1000})
                return result

            @tool
            async def get_recent_activity(lead_id: int) -> dict:
                """Obtiene actividad reciente del lead y del equipo comercial."""
                started = time.perf_counter()
                await asyncio.sleep(0.18)
                result = {
                    "lead_id": lead_id,
                    "last_inbound_minutes_ago": 12,
                    "asked_for_demo": True,
                    "has_assigned_advisor": False,
                }
                metrics.append({"name": "get_recent_activity", "ms": (time.perf_counter() - started) * 1000})
                return result

            return [get_funnel_rules, get_contact_signals, get_recent_activity]

        return system, prompt, tools_factory

    system = "Eres un agente procesador. Debes llamar get_processing_policy y get_conversation_summary antes de responder. Si hace falta, llama get_followup_constraints. Responde en máximo 3 líneas para el equipo interno."
    prompt = "Prepara la siguiente acción para el lead 328159. Debes usar get_processing_policy y get_conversation_summary antes de concluir. Si hay restricciones horarias o de canal, usa get_followup_constraints."

    def tools_factory(metrics: list[dict]):
        @tool
        async def get_processing_policy(lead_id: int) -> dict:
            """Obtiene políticas internas de atención y priorización."""
            started = time.perf_counter()
            await asyncio.sleep(0.22)
            result = {
                "lead_id": lead_id,
                "priority_level": "P1",
                "preferred_action": "ofrecer llamada",
                "escalation_if_no_reply_hours": 4,
            }
            metrics.append({"name": "get_processing_policy", "ms": (time.perf_counter() - started) * 1000})
            return result

        @tool
        async def get_conversation_summary(lead_id: int) -> dict:
            """Obtiene resumen operativo de la conversación reciente."""
            started = time.perf_counter()
            await asyncio.sleep(0.34)
            result = {
                "lead_id": lead_id,
                "summary": "Lead con interés alto, pidió precios y una posible demostración.",
                "objections": ["tiempo", "comparación con competencia"],
                "sentiment": "positivo",
            }
            metrics.append({"name": "get_conversation_summary", "ms": (time.perf_counter() - started) * 1000})
            return result

        @tool
        async def get_followup_constraints(lead_id: int) -> dict:
            """Obtiene restricciones para seguimiento como horario y canal permitido."""
            started = time.perf_counter()
            await asyncio.sleep(0.16)
            result = {
                "lead_id": lead_id,
                "allowed_channels": ["whatsapp", "llamada"],
                "preferred_window": "09:00-12:00",
            }
            metrics.append({"name": "get_followup_constraints", "ms": (time.perf_counter() - started) * 1000})
            return result

        return [get_processing_policy, get_conversation_summary, get_followup_constraints]

    return system, prompt, tools_factory


def _should_continue(state: AgentState) -> str:
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return END


def _build_graph(llm_with_tools, tools: list):
    tool_map = {tool_item.name: tool_item for tool_item in tools}

    async def agent_node(state: AgentState) -> dict:
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    async def tools_node(state: AgentState) -> dict:
        last_message = state["messages"][-1]
        tool_calls = last_message.tool_calls if isinstance(last_message, AIMessage) else []

        async def execute_tool(tool_call: dict):
            result = await tool_map[tool_call["name"]].ainvoke(tool_call["args"])
            return ToolMessage(
                content=json.dumps(result, ensure_ascii=False),
                name=tool_call["name"],
                tool_call_id=tool_call.get("id"),
            )

        tool_messages = await asyncio.gather(*[execute_tool(call) for call in tool_calls])
        return {"messages": list(tool_messages)}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


async def _run_agent(agent_name: str, iteration: int, model: str, max_tokens: int, temperature: float):
    system, prompt, tools_factory = _build_agent_spec(agent_name)
    metrics: list[dict] = []
    tools = tools_factory(metrics)
    llm = _create_llm(model, max_tokens, temperature).bind_tools(tools)
    graph = _build_graph(llm, tools)
    started = time.perf_counter()
    final_state = await graph.ainvoke(
        {
            "messages": [
                SystemMessage(content=f"{system}\nEjecución benchmark: {agent_name} iteración {iteration}. Si puedes, llama múltiples herramientas antes de responder."),
                HumanMessage(content=prompt),
            ]
        }
    )
    total_ms = (time.perf_counter() - started) * 1000
    tool_ms = sum(item["ms"] for item in metrics)
    ai_messages = [message for message in final_state["messages"] if isinstance(message, AIMessage)]
    tool_calls = sum(len(message.tool_calls or []) for message in ai_messages)
    unique_tools = sorted({item["name"] for item in metrics})
    response_length = len(str(final_state["messages"][-1].content))
    return {
        "agent_name": agent_name,
        "total_ms": total_ms,
        "tool_ms": tool_ms,
        "tool_calls": tool_calls,
        "unique_tools": unique_tools,
        "steps": len(ai_messages),
        "response_length": response_length,
    }


async def _run_workflow(iteration: int, model: str, max_tokens: int, temperature: float):
    started = time.perf_counter()
    funnel, processor = await asyncio.gather(
        _run_agent("funnel", iteration, model, max_tokens, temperature),
        _run_agent("processor", iteration, model, max_tokens, temperature),
    )
    workflow_ms = (time.perf_counter() - started) * 1000
    return {"workflow_ms": workflow_ms, "funnel": funnel, "processor": processor}


async def main():
    settings = get_settings()
    model = settings.DEFAULT_MODEL
    max_tokens = 220
    temperature = 0
    iterations = 3
    runs: list[dict] = []

    print("Benchmark LangGraph - agentes paralelos")
    print(f"Model: {model}")
    print(f"Iterations: {iterations}")
    print()

    print("Warm-up no medido...")
    await _run_workflow(0, model, max_tokens, temperature)
    print("Warm-up completado")
    print()

    for iteration in range(1, iterations + 1):
        run = await _run_workflow(iteration, model, max_tokens, temperature)
        runs.append(run)
        print(f"Iteración {iteration}")
        print(f"  Workflow total     -> {_format_ms(run['workflow_ms'])}")
        print(
            f"  Funnel agent       -> total={_format_ms(run['funnel']['total_ms'])} | "
            f"tools={_format_ms(run['funnel']['tool_ms'])} | steps={run['funnel']['steps']} | "
            f"tool_calls={run['funnel']['tool_calls']} | unique_tools={','.join(run['funnel']['unique_tools'])}"
        )
        print(
            f"  Processor agent    -> total={_format_ms(run['processor']['total_ms'])} | "
            f"tools={_format_ms(run['processor']['tool_ms'])} | steps={run['processor']['steps']} | "
            f"tool_calls={run['processor']['tool_calls']} | unique_tools={','.join(run['processor']['unique_tools'])}"
        )
        print()

    workflow_stats = _stats([run["workflow_ms"] for run in runs])
    funnel_stats = _stats([run["funnel"]["total_ms"] for run in runs])
    processor_stats = _stats([run["processor"]["total_ms"] for run in runs])
    funnel_tool_stats = _stats([run["funnel"]["tool_ms"] for run in runs])
    processor_tool_stats = _stats([run["processor"]["tool_ms"] for run in runs])

    print("=== RESUMEN ===")
    print(
        f"Workflow total    -> avg={_format_ms(workflow_stats['avg'])} | p50={_format_ms(workflow_stats['p50'])} | "
        f"min={_format_ms(workflow_stats['min'])} | max={_format_ms(workflow_stats['max'])}"
    )
    print(
        f"Funnel total      -> avg={_format_ms(funnel_stats['avg'])} | p50={_format_ms(funnel_stats['p50'])} | "
        f"min={_format_ms(funnel_stats['min'])} | max={_format_ms(funnel_stats['max'])}"
    )
    print(
        f"Processor total   -> avg={_format_ms(processor_stats['avg'])} | p50={_format_ms(processor_stats['p50'])} | "
        f"min={_format_ms(processor_stats['min'])} | max={_format_ms(processor_stats['max'])}"
    )
    print(
        f"Funnel tools      -> avg={_format_ms(funnel_tool_stats['avg'])} | p50={_format_ms(funnel_tool_stats['p50'])} | "
        f"min={_format_ms(funnel_tool_stats['min'])} | max={_format_ms(funnel_tool_stats['max'])}"
    )
    print(
        f"Processor tools   -> avg={_format_ms(processor_tool_stats['avg'])} | p50={_format_ms(processor_tool_stats['p50'])} | "
        f"min={_format_ms(processor_tool_stats['min'])} | max={_format_ms(processor_tool_stats['max'])}"
    )


if __name__ == "__main__":
    asyncio.run(main())
