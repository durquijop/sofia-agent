import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Annotated, TypedDict

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
    tool_traces: list[dict]


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


def _stringify_safe(value) -> str:
    return json.dumps(value if value is not None else None, ensure_ascii=False, indent=2)


async def _fetch_embudo_context(contacto_id: int, empresa_id: int, agente_id: int, conversacion_id: int, limite_mensajes: int):
    settings = get_settings()
    started = time.perf_counter()
    response = await _get_http_client().post(
        f"{settings.SUPABASE_URL}/functions/v1/obtener-contexto-completo-v1",
        headers={
            "Content-Type": "application/json",
            "apikey": settings.SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_KEY}",
        },
        json={
            "contacto_id": contacto_id,
            "empresa_id": empresa_id,
            "agente_id": agente_id,
            "conversacion_id": conversacion_id,
            "limite_mensajes": limite_mensajes,
        },
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return response.json(), elapsed_ms


def _build_temporal_context() -> str:
    now = time.localtime()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", now)
    year = now.tm_year
    month = now.tm_mon
    day = now.tm_mday
    week_day = now.tm_wday
    today_start = time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
    today_end = time.mktime((year, month, day, 23, 59, 59, 0, 0, -1))
    monday_offset = -week_day
    sunday_offset = 6 - week_day
    week_start_struct = time.localtime(today_start + monday_offset * 86400)
    week_end_struct = time.localtime(today_end + sunday_offset * 86400)
    month_start_struct = time.strptime(f"{year}-{month:02d}-01", "%Y-%m-%d")
    if month == 12:
        next_month_start = time.strptime(f"{year + 1}-01-01", "%Y-%m-%d")
    else:
        next_month_start = time.strptime(f"{year}-{month + 1:02d}-01", "%Y-%m-%d")
    month_start = time.mktime(month_start_struct)
    month_end = time.mktime(next_month_start) - 1
    year_start = time.mktime(time.strptime(f"{year}-01-01", "%Y-%m-%d"))
    next_year_start = time.mktime(time.strptime(f"{year + 1}-01-01", "%Y-%m-%d"))
    now_epoch = time.time()
    month_progress = ((now_epoch - month_start) / (month_end - month_start) * 100) if month_end > month_start else 0
    week_start_epoch = time.mktime(week_start_struct)
    week_end_epoch = time.mktime(week_end_struct)
    week_progress = ((now_epoch - week_start_epoch) / (week_end_epoch - week_start_epoch) * 100) if week_end_epoch > week_start_epoch else 0
    day_of_year = now.tm_yday
    days_in_year = int((next_year_start - year_start) / 86400)
    year_progress = (day_of_year / days_in_year * 100) if days_in_year else 0
    hours_left_today = max(0, int((today_end - now_epoch) / 3600 + 0.9999))
    days_left_week = max(0, int((week_end_epoch - now_epoch) / 86400 + 0.9999))
    days_left_month = max(0, int((month_end - now_epoch) / 86400 + 0.9999))
    days_done_week = max(0, int((today_start - week_start_epoch) / 86400))
    days_done_month = max(0, int((today_start - month_start) / 86400))
    quarter = ((month - 1) // 3) + 1
    is_weekend = "Sí" if week_day >= 5 else "No"

    def fmt_date(struct_time):
        return time.strftime("%Y-%m-%d", struct_time)

    def fmt_datetime(struct_time):
        return time.strftime("%Y-%m-%d %H:%M:%S", struct_time)

    next_days = []
    for index in range(7):
        future = time.localtime(today_start + (index + 1) * 86400)
        next_days.append(f"En {index + 1} día(s): {fmt_date(future)}")

    return (
        f"Ahora: {fmt_datetime(now)} | ISO: {iso}\n"
        f"¿Fin de semana hoy?: {is_weekend}\n"
        f"Quedan {hours_left_today} hora(s) para que termine el día\n"
        f"Semana actual: {fmt_date(week_start_struct)} a {fmt_date(week_end_struct)} | avance {week_progress:.1f}% | transcurridos {days_done_week} día(s) | restantes {days_left_week} día(s)\n"
        f"Mes actual: {fmt_date(month_start_struct)} a {fmt_date(time.localtime(month_end))} | avance {month_progress:.1f}% | transcurridos {days_done_month} día(s) | restantes {days_left_month} día(s)\n"
        f"Año actual: día {day_of_year}/{days_in_year} | avance {year_progress:.1f}% | trimestre Q{quarter}\n"
        f"Próximos 7 días:\n" + "\n".join(next_days)
    )


def _build_funnel_system_prompt(context: dict) -> str:
    contacto_nombre = (((context.get("etapas_embudo") or {}).get("data") or {}).get("contacto") or {}).get("nombre_completo") or "Contacto sin nombre"
    etapa_actual = (((context.get("contexto_embudo") or {}).get("data") or {}).get("etapa_actual") or {})
    etapa_actual_texto = f"{etapa_actual.get('nombre')} (Orden: {etapa_actual.get('orden', 'N/A')})" if etapa_actual.get("nombre") else "Sin etapa asignada"
    etapas_disponibles = (((context.get("etapas_embudo") or {}).get("data") or {}).get("etapas") or [])
    contexto_embudo = ((context.get("contexto_embudo") or {}).get("data") or {})
    temporal_context = _build_temporal_context()
    return f"# IDENTIDAD Y MISIÓN\n\nEres un analista conversacional que identifica etapas del embudo y registra información del prospecto.\n\n## Objetivos:\n- IDENTIFICAR etapa actual del prospecto\n- ACTUALIZAR etapa usando `actualizar-etapa-embudo`\n- REGISTRAR información usando `actualizar-metadata-v1`\n\n# Datos claves\n\nEl contacto {contacto_nombre} se encuentra en la etapa {etapa_actual_texto}\n\n---\n\n# CONTEXTO DEL EMBUDO\n\n**Etapas disponibles:**\n```json\n{_stringify_safe(etapas_disponibles)}\n```\n\n**Etapa actual identificada + Metadata registrada:**\n```json\nEtapa Actual: {_stringify_safe(contexto_embudo)}\n```\n\nSi la etapa actual y la etapa identificada son la misma, no es necesario actualizarla.\n\nCada etapa tiene:\n- `nombre_etapa` / `id_etapa` (identificador único)\n- `orden_etapa` (posición secuencial, solo referencial)\n- `senales`: comportamientos observables\n- `metadata.informacion_registrar`: datos a capturar (array de `{{id, texto}}`)\n\n---\n\n# HERRAMIENTAS\n\n## 1. `update_etapa_embudo`\nUsa `id_etapa` (identificador único) para actualizar la etapa del contacto.\n\n## 2. `update-metadata`\n\n### Cuándo usar:\n- Después de actualizar etapa (SIEMPRE)\n- Cuando el prospecto comparte información clave\n- Al finalizar descubrimiento (3+ preguntas contestadas)\n\n## Reglas del uso de la herramienta:\n\n- Úsalas solo si tienes algo para actualizar; si el nuevo mensaje es irrelevante y ya tienes la metadata actualizada, solo genera un output con un \"ok\"\n\n---\n\n## CÓMO RELLENAR informacion_capturada\n\nPara cada objeto en `informacion_registrar`:\n\n1. Lee el campo `texto` (qué debes capturar)\n2. Busca ese dato en la conversación\n3. Si lo encontraste: usa el `id` como clave + valor capturado\n4. Si NO lo encontraste: omite ese `id`\n\n---\n\n## REGLAS DE MERGE\n\n- Campos existentes se preservan\n- Nuevos campos se agregan\n- Valores existentes se actualizan\n- Secciones se mantienen separadas\n\n---\n\n## REGLAS JSON OBLIGATORIAS\n\n1. Todas las claves entre comillas dobles\n2. Todos los strings entre comillas dobles\n3. No comillas simples\n4. No comas al final del último elemento\n5. Balancear llaves\n\n---\n\n## Reglas extras:\n\n- Si la empresa no tiene embudo creado, no asignar etapa al contacto\n- Si no ha cambiado nada no actualices ni la metadata ni la etapa\n\n## CHECKLIST FINAL\n\nAntes de responder:\n\n- ¿Cambió de etapa? → update_etapa_embudo + update-metadata\n- ¿Usé id_etapa correcto?\n- ¿Registré TODO según informacion_registrar?\n- ¿Usé IDs correctos (info_reg_X) como claves?\n- ¿JSON válido con comillas dobles?\n- ¿Valores REALES (no ejemplos)?\n\nSi falta algo, complétalo antes de continuar.\n\n{temporal_context}\n\n---\n\nOutput esperado:\n\nTu respuesta final debe estar orientada a guiar al equipo en el estado actual del embudo. La respuesta debe ser de máximo 3 líneas.\n\nNo le respondas al prospecto. Ese no es tu trabajo."


def _build_shared_user_message(context: dict) -> str:
    historial = ((context.get("conversacion_memoria") or {}).get("data") or [])
    return f"Historial de la conversación:\n{_stringify_safe(historial)}"


def _build_processor_system_prompt(context: dict) -> str:
    contacto_nombre = (((context.get("etapas_embudo") or {}).get("data") or {}).get("contacto") or {}).get("nombre_completo") or "Contacto sin nombre"
    etapa_actual = ((((context.get("contexto_embudo") or {}).get("data") or {}).get("etapa_actual") or {}).get("nombre")) or "Sin etapa asignada"
    return f"Eres un agente procesador comercial interno. Debes analizar al contacto {contacto_nombre}, considerar que su etapa actual es {etapa_actual} y definir la siguiente acción operativa. Antes de responder, llama exactamente una vez a get_processing_policy, get_conversation_summary y get_followup_constraints. No llames ninguna tool más de una vez. Responde en máximo 3 líneas para el equipo interno."


def _build_processor_user_message(context: dict) -> str:
    return _build_shared_user_message(context) + "\n\nDebes preparar la siguiente acción comercial interna, la prioridad y el mejor canal/horario permitido."


def _build_graph(llm_with_tools, tools: list):
    tool_map = {tool_item.name: tool_item for tool_item in tools}

    async def agent_node(state: AgentState) -> dict:
        response = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [response]}

    async def tools_node(state: AgentState) -> dict:
        last_message = state["messages"][-1]
        tool_calls = last_message.tool_calls if isinstance(last_message, AIMessage) else []

        async def execute_tool(tool_call: dict):
            started = time.perf_counter()
            result = await tool_map[tool_call["name"]].ainvoke(tool_call["args"])
            elapsed_ms = (time.perf_counter() - started) * 1000
            trace = {
                "tool_name": tool_call["name"],
                "input": tool_call["args"],
                "output": result,
                "ms": elapsed_ms,
            }
            message = ToolMessage(
                content=json.dumps(result, ensure_ascii=False),
                name=tool_call["name"],
                tool_call_id=tool_call.get("id"),
            )
            return trace, message

        results = await asyncio.gather(*[execute_tool(call) for call in tool_calls])
        traces = list(state.get("tool_traces", []))
        messages = []
        for trace, message in results:
            traces.append(trace)
            messages.append(message)
        return {"messages": messages, "tool_traces": traces}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


def _should_continue(state: AgentState) -> str:
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return END


def _build_processor_tools():
    @tool
    async def get_processing_policy(lead_id: int) -> dict:
        """Obtiene políticas internas de atención y priorización."""
        await asyncio.sleep(0.22)
        return {
            "lead_id": lead_id,
            "priority_level": "P1",
            "preferred_action": "ofrecer llamada",
            "escalation_if_no_reply_hours": 4,
        }

    @tool
    async def get_conversation_summary(lead_id: int) -> dict:
        """Obtiene resumen operativo de la conversación reciente."""
        await asyncio.sleep(0.34)
        return {
            "lead_id": lead_id,
            "summary": "Lead con interés alto, pidió precios y una posible demostración.",
            "objections": ["tiempo", "comparación con competencia"],
            "sentiment": "positivo",
        }

    @tool
    async def get_followup_constraints(lead_id: int) -> dict:
        """Obtiene restricciones para seguimiento como horario y canal permitido."""
        await asyncio.sleep(0.16)
        return {
            "lead_id": lead_id,
            "allowed_channels": ["whatsapp", "llamada"],
            "preferred_window": "09:00-12:00",
        }

    return [get_processing_policy, get_conversation_summary, get_followup_constraints]


async def _run_funnel_agent(system_prompt: str, user_message: str, model: str, max_tokens: int, temperature: float):
    llm = _create_llm(model, max_tokens, temperature)
    graph = _build_graph(llm, [])
    started = time.perf_counter()
    final_state = await graph.ainvoke(
        {
            "messages": [SystemMessage(content=system_prompt), HumanMessage(content=user_message)],
            "tool_traces": [],
        }
    )
    total_ms = (time.perf_counter() - started) * 1000
    ai_messages = [message for message in final_state["messages"] if isinstance(message, AIMessage)]
    response = str(final_state["messages"][-1].content)
    return {
        "totalMs": total_ms,
        "response": response,
        "steps": len(ai_messages),
        "toolTraces": [],
    }


async def _run_processor_agent(system_prompt: str, user_message: str, model: str, max_tokens: int, temperature: float):
    tools = _build_processor_tools()
    llm = _create_llm(model, max_tokens, temperature).bind_tools(tools)
    graph = _build_graph(llm, tools)
    started = time.perf_counter()
    final_state = await graph.ainvoke(
        {
            "messages": [SystemMessage(content=system_prompt), HumanMessage(content=user_message)],
            "tool_traces": [],
        }
    )
    total_ms = (time.perf_counter() - started) * 1000
    ai_messages = [message for message in final_state["messages"] if isinstance(message, AIMessage)]
    tool_calls = []
    for message in ai_messages:
        for tool_call in message.tool_calls or []:
            tool_calls.append({"tool_name": tool_call["name"], "input": tool_call["args"]})
    return {
        "totalMs": total_ms,
        "response": str(final_state["messages"][-1].content),
        "steps": len(ai_messages),
        "toolTraces": final_state.get("tool_traces", []),
        "toolCalls": tool_calls,
    }


async def _run_workflow(context: dict, model: str, max_tokens: int, temperature: float):
    funnel_system_prompt = _build_funnel_system_prompt(context)
    funnel_user_message = _build_shared_user_message(context)
    processor_system_prompt = _build_processor_system_prompt(context)
    processor_user_message = _build_processor_user_message(context)
    started = time.perf_counter()
    funnel, processor = await asyncio.gather(
        _run_funnel_agent(funnel_system_prompt, funnel_user_message, model, max_tokens, temperature),
        _run_processor_agent(processor_system_prompt, processor_user_message, model, max_tokens, temperature),
    )
    return {
        "workflowMs": (time.perf_counter() - started) * 1000,
        "funnelSystemPrompt": funnel_system_prompt,
        "funnelUserMessage": funnel_user_message,
        "processorSystemPrompt": processor_system_prompt,
        "processorUserMessage": processor_user_message,
        "funnel": funnel,
        "processor": processor,
    }


async def main():
    settings = get_settings()
    model = settings.DEFAULT_MODEL
    max_tokens = 350
    temperature = 0.1
    iterations = 3
    output_path = PROJECT_ROOT / "artifacts" / "benchmark_documented_langgraph.json"
    request_payload = {
        "contacto_id": 328159,
        "empresa_id": 2,
        "agente_id": 4,
        "conversacion_id": 63380,
        "limite_mensajes": 20,
    }
    context, edge_context_ms = await _fetch_embudo_context(**request_payload)
    await _run_workflow(context, model, max_tokens, temperature)
    documented_run = await _run_workflow(context, model, max_tokens, temperature)
    measured_runs = []
    for _ in range(iterations):
        measured_runs.append(await _run_workflow(context, model, max_tokens, temperature))
    report = {
        "framework": "langgraph",
        "model": model,
        "benchmarkRequest": request_payload,
        "edgeContextMs": edge_context_ms,
        "edgeContext": context,
        "documentedRun": documented_run,
        "measuredStats": {
            "workflowMs": _stats([run["workflowMs"] for run in measured_runs]),
            "funnelMs": _stats([run["funnel"]["totalMs"] for run in measured_runs]),
            "processorMs": _stats([run["processor"]["totalMs"] for run in measured_runs]),
            "processorToolMs": _stats([sum(trace["ms"] for trace in run["processor"]["toolTraces"]) for run in measured_runs]),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Reporte JSON escrito en {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
