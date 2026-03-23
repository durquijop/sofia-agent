"""Agente de actualización de contacto - analiza conversación y actualiza wp_contactos."""
import asyncio
import time
import logging
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.agents.funnel import _create_llm
from app.db import queries as db
from app.schemas.chat import AgentRunTrace, TimingInfo, ToolCall, ToolDefinition
from app.schemas.contact_update import ContactUpdateAgentRequest, ContactUpdateAgentResponse

logger = logging.getLogger(__name__)

CONTACT_UPDATE_MODEL = "x-ai/grok-4.1-fast"
MAX_CONTACT_UPDATE_ITERATIONS = 2
ALLOWED_CONTACT_FIELDS = {
    "nombre",
    "apellido",
    "email",
    "telefono",
    "etapa_emocional",
    "timezone",
    "es_calificado",
    "estado",
}


class ContactUpdateAgentState(TypedDict):
    messages: Annotated[list, add_messages]
    tools_used: list[ToolCall]
    updated_fields: list[str]
    contact_updates: dict | None
    tool_execution_ms: float
    llm_elapsed_ms: float
    llm_iterations: int


def _stringify_safe(value: Any) -> str:
    import json

    return json.dumps(value if value is not None else None, ensure_ascii=False, indent=2)


def _normalize_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = " ".join(value.strip().split())
        if not normalized:
            return None
        if field == "email":
            return normalized.lower()
        return normalized
    return value


def _filter_changed_fields(current_contact: dict, proposed_updates: dict[str, Any]) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for field, value in proposed_updates.items():
        if field not in ALLOWED_CONTACT_FIELDS:
            continue
        normalized_new = _normalize_value(field, value)
        if normalized_new is None:
            continue
        normalized_current = _normalize_value(field, current_contact.get(field))
        if normalized_new == normalized_current:
            continue
        changed[field] = normalized_new
    return changed


def _build_messages_summary(messages: list[dict]) -> str:
    if not messages:
        return "Sin mensajes recientes"
    lines: list[str] = []
    for msg in messages:
        hora = str(msg.get("hora") or msg.get("fecha_hora") or msg.get("timestamp") or "?").strip()
        remitente = str(msg.get("remitente") or "desconocido").strip().lower()
        contenido = str(msg.get("mensaje") or msg.get("contenido") or "").strip()
        if not contenido:
            continue
        lines.append(f"- [{hora}] {remitente}: {contenido}")
    return "\n".join(lines) if lines else "Sin mensajes recientes"


def _build_appointments_summary(citas: list[dict]) -> str:
    if not citas:
        return "Sin citas registradas"
    lines: list[str] = []
    for cita in citas[:5]:
        titulo = str(cita.get("titulo") or "Cita").strip()
        fecha = str(cita.get("fecha_hora") or "sin fecha").strip()
        estado = str(cita.get("estado") or "sin estado").strip()
        lines.append(f"- {titulo} | {fecha} | estado={estado}")
    return "\n".join(lines)


def _build_contact_update_system_prompt() -> str:
    return """Eres un asistente de gestión de datos de contactos. Tu trabajo es:

1. Analizar conversaciones y extraer información nueva
2. Detectar el idioma del prospecto desde el primer mensaje
3. Identificar nombre, apellido, email, teléfono, estado emocional, timezone, estado y calificación
4. Mantener la tabla wp_contactos actualizada sin duplicados
5. Responder siempre en formato: ✅ OK Guardado o ⚪ OK Sin acción

REGLAS CRÍTICAS:
- Solo actualiza si hay información NUEVA y explícita del remitente usuario
- No hagas llamadas a tools sin datos concretos
- Preserva datos existentes
- Sé conciso en respuestas
- Solo puedes persistir estos campos de wp_contactos: nombre, apellido, email, telefono, etapa_emocional, timezone, es_calificado, estado
- Si un dato no es explícito o no está claro, devuelve null y no lo actualices

Reglas específicas de extracción:

nombre:
- Solo cuando el usuario indique explícitamente su nombre real
- Ejemplos válidos: "Mi nombre es Juan", "Soy María García", "Me llamo Carlos"
- Devuelve null si no está claro o si es una suposición

apellido:
- Solo cuando el usuario indique explícitamente su apellido real
- Ejemplos válidos: "Mi apellido es García", "Soy Juan Pérez"
- Devuelve null si no está claro o tiene menos de 2 caracteres

email:
- Solo cuando el usuario indique explícitamente su email real
- Acepta cualquier string completo que el usuario entregue como email
- Devuelve null si no está claro que sea su email

telefono:
- Solo cuando el usuario comparta explícitamente su celular
- Devuelve null si no aparece de forma explícita

etapa_emocional:
- Basado en la conversación del usuario, no en suposiciones
- Valores esperados: preocupado, ansioso, esperanzado, decidido, interesado, evaluando, negociando, ganado, perdido, escéptico
- Devuelve null si no hay señales claras

timezone:
- Usa formato IANA solo si el usuario menciona país o ciudad y es deducible
- Ejemplos: America/Bogota, America/Mexico_City, America/New_York
- Devuelve null si no es deducible con claridad

es_calificado:
- Solo usa exactamente si, no, evaluando o null
- Devuelve null si aún no hay suficiente información

estado:
- Solo usa exactamente prospecto, cliente, inactivo, rechazado o null
- Devuelve null si no hay razón clara para cambiarlo

Salida obligatoria:
- Si no hay datos nuevos: ⚪ OK Sin acción - [razón breve]
- Si hay información nueva y se guardó: ✅ OK Guardado - [campos actualizados]
"""


def _build_contact_update_user_prompt(contacto: dict, mensajes: list[dict], citas: list[dict]) -> str:
    total_mensajes = len(mensajes)
    ultimo_remitente = mensajes[-1].get("remitente") if mensajes else None
    proximas_citas = len(citas)
    return (
        "Analiza la conversación e identifica qué información nueva debe registrarse:\n\n"
        "## Historial de conversación\n"
        f"{_build_messages_summary(mensajes)}\n\n"
        "## Información de citas\n"
        f"{_build_appointments_summary(citas)}\n\n"
        f"Total de mensajes: {total_mensajes}\n"
        f"Último remitente: {ultimo_remitente or 'desconocido'}\n"
        f"Citas próximas: {proximas_citas}\n\n"
        "Información del usuario actual\n"
        f"{_stringify_safe(contacto)}\n\n"
        "INSTRUCCIONES\n"
        "Si no hay datos nuevos, responde: ⚪ OK Sin acción - [razón breve]\n\n"
        "Si hay información nueva, identifica nombre, apellido, email, telefono, etapa_emocional, timezone, es_calificado y estado.\n"
        "NO hagas llamadas a tools si solo estás analizando.\n"
        "Si necesitas guardar datos, ENTONCES usa las tools disponibles."
    )


async def _load_contact_update_context(contacto_id: int, empresa_id: int, conversacion_id: int | None) -> tuple[dict, list[dict], list[dict]]:
    contexto_local_task = db.load_contexto_completo_local(
        contacto_id=contacto_id,
        empresa_id=empresa_id,
        agente_id=None,
        conversacion_id=conversacion_id,
        limite_mensajes=20,
    )
    citas_task = db.get_citas_contacto_detalladas(contacto_id, limit=5)
    contexto_local, citas = await asyncio.gather(contexto_local_task, citas_task)
    contexto_data = (contexto_local.get("contexto_embudo") or {}).get("data") or {}
    conversacion_data = (contexto_local.get("conversacion_memoria") or {}).get("data") or {}
    contacto = contexto_data.get("informacion_contacto") or {}
    mensajes = list(conversacion_data.get("mensajes") or [])
    if not contacto:
        raise ValueError(f"Contacto {contacto_id} no encontrado")
    return contacto, mensajes, list(citas or [])


def _build_graph(llm_with_tools, current_contact: dict, request: ContactUpdateAgentRequest):
    async def agent_node(state: ContactUpdateAgentState) -> dict:
        start = time.perf_counter()
        response = await llm_with_tools.ainvoke(state["messages"])
        llm_elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "messages": [response],
            "llm_elapsed_ms": round(float(state.get("llm_elapsed_ms", 0)) + llm_elapsed_ms, 1),
            "llm_iterations": int(state.get("llm_iterations", 0)) + 1,
        }

    async def tool_execution_node(state: ContactUpdateAgentState) -> dict:
        tools_used = list(state.get("tools_used", []))
        tool_messages: list[ToolMessage] = []
        tool_execution_ms = float(state.get("tool_execution_ms", 0))
        updated_fields = list(state.get("updated_fields", []))
        contact_updates = state.get("contact_updates")
        last_message = state["messages"][-1]

        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {
                "messages": tool_messages,
                "tools_used": tools_used,
                "updated_fields": updated_fields,
                "contact_updates": contact_updates,
                "tool_execution_ms": round(tool_execution_ms, 1),
            }

        for tc in last_message.tool_calls:
            tool_name = tc.get("name") or "unknown"
            raw_args = tc.get("args") or {}
            tool_input = raw_args if isinstance(raw_args, dict) else {"input": raw_args}
            start = time.perf_counter()
            status = "ok"
            error_text = None
            try:
                if tool_name != "update_contact_info":
                    raise ValueError(f"Herramienta no reconocida: {tool_name}")
                changed_fields = _filter_changed_fields(current_contact, tool_input)
                if not changed_fields:
                    result = "⚪ OK Sin acción - sin campos nuevos válidos"
                else:
                    updated_contact = await db.actualizar_campos_contacto(request.contacto_id, changed_fields)
                    if not updated_contact:
                        result = "Error al actualizar wp_contactos"
                    else:
                        updated_fields = list(changed_fields.keys())
                        contact_updates = changed_fields
                        current_contact.update(changed_fields)
                        result = f"✅ OK Guardado - {', '.join(updated_fields)}"
                if str(result).lower().startswith("error"):
                    status = "error"
                    error_text = str(result)
                tool_output = str(result)
            except Exception as exc:
                status = "error"
                error_text = str(exc)
                tool_output = f"Error: {exc}"

            duration_ms = (time.perf_counter() - start) * 1000
            tool_execution_ms += duration_ms
            tool_messages.append(ToolMessage(content=tool_output, name=tool_name, tool_call_id=tc.get("id")))
            tools_used.append(
                ToolCall(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_output=tool_output,
                    duration_ms=round(duration_ms, 1),
                    status=status,
                    error=error_text,
                    source="contact_update",
                    description="Actualiza columnas permitidas de wp_contactos",
                )
            )

        return {
            "messages": tool_messages,
            "tools_used": tools_used,
            "updated_fields": updated_fields,
            "contact_updates": contact_updates,
            "tool_execution_ms": round(tool_execution_ms, 1),
        }

    def _should_use_tools(state: ContactUpdateAgentState) -> str:
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        return END

    def _should_continue(state: ContactUpdateAgentState) -> str:
        if int(state.get("llm_iterations", 0)) >= MAX_CONTACT_UPDATE_ITERATIONS:
            return END
        return "agent"

    graph = StateGraph(ContactUpdateAgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_execution_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _should_use_tools, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _should_continue, {"agent": "agent", END: END})
    return graph


async def run_contact_update_agent(request: ContactUpdateAgentRequest) -> ContactUpdateAgentResponse:
    t_start = time.perf_counter()
    model = request.model or CONTACT_UPDATE_MODEL
    max_tokens = request.max_tokens or 512
    temperature = request.temperature if request.temperature is not None else 0.2

    try:
        contacto, mensajes, citas = await _load_contact_update_context(
            contacto_id=request.contacto_id,
            empresa_id=request.empresa_id,
            conversacion_id=request.conversacion_id,
        )

        @tool
        async def update_contact_info(
            nombre: str | None = None,
            apellido: str | None = None,
            email: str | None = None,
            telefono: str | None = None,
            etapa_emocional: str | None = None,
            timezone: str | None = None,
            es_calificado: str | None = None,
            estado: str | None = None,
        ) -> str:
            """Actualiza columnas permitidas de wp_contactos solo cuando hay datos nuevos y explícitos."""
            proposed_updates = {
                "nombre": nombre,
                "apellido": apellido,
                "email": email,
                "telefono": telefono,
                "etapa_emocional": etapa_emocional,
                "timezone": timezone,
                "es_calificado": es_calificado,
                "estado": estado,
            }
            changed_fields = _filter_changed_fields(contacto, proposed_updates)
            if not changed_fields:
                return "⚪ OK Sin acción - sin campos nuevos válidos"
            updated_contact = await db.actualizar_campos_contacto(request.contacto_id, changed_fields)
            if not updated_contact:
                return "Error al actualizar wp_contactos"
            contacto.update(changed_fields)
            return f"✅ OK Guardado - {', '.join(changed_fields.keys())}"

        llm = _create_llm(model, max_tokens, temperature)
        tools = [update_contact_info]
        llm_with_tools = llm.bind_tools(tools)

        graph_start = time.perf_counter()
        graph = _build_graph(llm_with_tools, contacto, request)
        compiled = graph.compile()
        graph_build_ms = (time.perf_counter() - graph_start) * 1000

        system_prompt = _build_contact_update_system_prompt()
        user_prompt = _build_contact_update_user_prompt(contacto, mensajes, citas)
        initial_state: ContactUpdateAgentState = {
            "messages": [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)],
            "tools_used": [],
            "updated_fields": [],
            "contact_updates": None,
            "tool_execution_ms": 0,
            "llm_elapsed_ms": 0,
            "llm_iterations": 0,
        }

        final_state = await compiled.ainvoke(initial_state)
        last_message = final_state["messages"][-1]
        if isinstance(last_message, ToolMessage):
            updated_fields = list(final_state.get("updated_fields", []))
            response_text = (
                f"✅ OK Guardado - {', '.join(updated_fields)}"
                if updated_fields
                else "⚪ OK Sin acción - sin datos nuevos"
            )
        else:
            response_text = last_message.content if isinstance(last_message, AIMessage) else str(last_message.content)
        response_text = "\n".join(str(response_text).split("\n")[:2]).strip()

        timing = TimingInfo(
            total_ms=round((time.perf_counter() - t_start) * 1000, 1),
            llm_ms=round(float(final_state.get("llm_elapsed_ms", 0)), 1),
            mcp_discovery_ms=0,
            graph_build_ms=round(graph_build_ms, 1),
            tool_execution_ms=round(float(final_state.get("tool_execution_ms", 0)), 1),
        )
        agent_runs = [
            AgentRunTrace(
                agent_key="contact_update_agent",
                agent_name="Agente de Actualización de Contacto",
                agent_kind="analysis",
                conversation_id=str(request.conversacion_id) if request.conversacion_id else None,
                memory_session_id=str(request.contacto_id),
                model_used=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                available_tools=[
                    ToolDefinition(
                        tool_name="update_contact_info",
                        description="Actualiza columnas permitidas de wp_contactos sin duplicar datos",
                        source="contact_update",
                    )
                ],
                tools_used=final_state.get("tools_used", []),
                timing=timing,
                llm_iterations=int(final_state.get("llm_iterations", 0)),
            )
        ]
        return ContactUpdateAgentResponse(
            success=True,
            respuesta=response_text,
            updated_fields=list(final_state.get("updated_fields", [])),
            contact_updates=final_state.get("contact_updates"),
            tools_used=final_state.get("tools_used", []),
            timing=timing,
            agent_runs=agent_runs,
        )
    except Exception as exc:
        logger.error("Error en run_contact_update_agent: %s", exc, exc_info=True)
        timing = TimingInfo(total_ms=round((time.perf_counter() - t_start) * 1000, 1))
        error_tool = ToolCall(
            tool_name="contact_update_error",
            tool_input={},
            tool_output=str(exc),
            duration_ms=timing.total_ms,
            status="error",
            error=str(exc),
            source="contact_update",
            description="Error interno antes de completar la actualización de contacto",
        )
        error_trace = AgentRunTrace(
            agent_key="contact_update_agent",
            agent_name="Agente de Actualización de Contacto",
            agent_kind="analysis_error",
            conversation_id=str(request.conversacion_id) if request.conversacion_id else None,
            memory_session_id=str(request.contacto_id),
            model_used=model,
            system_prompt="",
            user_prompt="",
            available_tools=[
                ToolDefinition(
                    tool_name="contact_update_error",
                    description="Error interno capturado durante la ejecución del agente de contacto",
                    source="contact_update",
                )
            ],
            tools_used=[error_tool],
            timing=timing,
            llm_iterations=0,
        )
        return ContactUpdateAgentResponse(
            success=False,
            respuesta="Error al procesar el agente de actualización de contacto",
            updated_fields=[],
            contact_updates=None,
            tools_used=[error_tool],
            timing=timing,
            agent_runs=[error_trace],
            error=str(exc),
        )