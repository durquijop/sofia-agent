"""Agente de Embudo (Funnel Agent) - Identifica etapas y actualiza contactos."""
import asyncio
import json
import logging
import time
from typing import Annotated, TypedDict

import httpx
from langchain_core.callbacks.base import Callbacks
from langchain_core.caches import BaseCache
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.core.config import get_settings
from app.core.funnel_debug import add_funnel_debug_run
from app.db import queries as db
from app.schemas.funnel import (
    FunnelAgentRequest,
    FunnelAgentResponse,
    FunnelContextResponse,
    ContactInfo,
    FunnelCurrentStage,
    FunnelStageInfo,
)
from app.schemas.chat import ToolCall, ToolDefinition, TimingInfo, AgentRunTrace

logger = logging.getLogger(__name__)

ChatOpenAI.model_rebuild(_types_namespace={"BaseCache": BaseCache, "Callbacks": Callbacks})


class FunnelAgentState(TypedDict):
    """Estado del agente de embudo."""
    messages: Annotated[list, add_messages]
    tools_used: list[ToolCall]
    etapa_anterior: str | None
    etapa_nueva: str | None
    metadata_actualizada: dict | None
    tool_execution_ms: float
    llm_elapsed_ms: float
    llm_iterations: int
    short_circuit: bool
    short_circuit_response: str | None


def _is_current_stage_match(contacto_stage_value, etapa: dict) -> bool:
    if contacto_stage_value is None:
        return False
    return contacto_stage_value in {etapa.get("id"), etapa.get("orden_etapa")}


def _resolve_stage_by_id(context: FunnelContextResponse, id_etapa: int) -> FunnelStageInfo | None:
    for etapa in context.todas_etapas:
        if etapa.id == id_etapa:
            return etapa
    return None


def _build_conversation_history_payload(mensajes: list[dict] | None) -> list[dict]:
    history = []
    for msg in mensajes or []:
        history.append(
            {
                "timestamp": msg.get("timestamp"),
                "remitente": msg.get("remitente"),
                "contenido": msg.get("contenido") or msg.get("mensaje", ""),
                "tipo": msg.get("tipo", "text"),
            }
        )
    return history


def _safe_json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw and raw[0] in "[{":
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
    return {}


def _build_current_stage(payload: dict | None) -> FunnelCurrentStage | None:
    if not isinstance(payload, dict):
        return None
    stage_id = payload.get("id")
    stage_order = payload.get("orden")
    stage_name = payload.get("nombre")
    if stage_id is None or stage_order is None or not stage_name:
        return None
    descripcion_json = _safe_json_dict(payload)
    return FunnelCurrentStage(
        id=stage_id,
        orden=stage_order,
        nombre=stage_name,
        que_es=descripcion_json.get("que_es"),
        senales=descripcion_json.get("senales"),
    )


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


def _create_llm(model: str, max_tokens: int = 512, temperature: float = 0.5) -> ChatOpenAI:
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


def _format_context_para_prompt(context: FunnelContextResponse) -> str:
    """Formatea el contexto del embudo para incluir en el system prompt."""
    lines = []
    
    # Información del contacto
    lines.append(f"**Contacto:** {context.contacto.nombre_completo} (+{context.contacto.telefono})")
    
    # Etapa actual
    if context.etapa_actual:
        lines.append(f"**Etapa Actual:** {context.etapa_actual.nombre} (Orden: {context.etapa_actual.orden})")
        if context.etapa_actual.senales:
            lines.append(f"  Señales esperadas: {', '.join(context.etapa_actual.senales)}")
    else:
        lines.append("**Etapa Actual:** Sin asignar")
    
    # Todas las etapas disponibles
    if context.todas_etapas:
        lines.append("\n**Etapas Disponibles:**")
        for etapa in context.todas_etapas:
            marker = " <- ACTUAL" if etapa.es_etapa_actual else ""
            lines.append(f"  id={etapa.id} | orden={etapa.orden_etapa} | {etapa.nombre_etapa}{marker}")
    
    # Metadata del contacto
    if context.contacto.metadata:
        lines.append(f"\n**Metadata Registrada:** {json.dumps(context.contacto.metadata, ensure_ascii=False, indent=2)}")
    
    return "\n".join(lines)


def _format_mensajes_para_contexto(mensajes: list[dict]) -> str:
    """Formatea los mensajes para el contexto del usuario."""
    if not mensajes:
        return "Sin mensajes previos"
    
    lines = ["**Historial:**"]
    for msg in mensajes[-5:]:  # Últimos 5 mensajes
        remitente = msg.get("remitente", "?").upper()
        contenido = msg.get("contenido", "").strip()[:100]
        timestamp = msg.get("timestamp", "?")
        lines.append(f"  [{remitente}] {contenido}...")
    
    return "\n".join(lines)


async def _load_funnel_context(
    contacto_id: int,
    empresa_id: int,
    conversacion_id: int | None = None,
) -> FunnelContextResponse:
    """Carga el contexto completo del embudo en paralelo."""
    contexto_local = await db.load_contexto_completo_local(
        contacto_id=contacto_id,
        empresa_id=empresa_id,
        agente_id=None,
        conversacion_id=conversacion_id,
    )

    contexto_embudo = contexto_local.get("contexto_embudo") or {}
    etapas_embudo = contexto_local.get("etapas_embudo") or {}
    conversacion_memoria = contexto_local.get("conversacion_memoria") or {}
    contexto_data = contexto_embudo.get("data") or {}
    etapas_data = etapas_embudo.get("data") or {}
    conversacion_data = conversacion_memoria.get("data") or {}
    contacto = contexto_data.get("informacion_contacto") or {}
    etapa_actual_data = contexto_data.get("etapa_actual")
    etapas = etapas_data.get("etapas") or []

    if not contacto:
        raise ValueError(f"Contacto {contacto_id} no encontrado")

    info_contacto = ContactInfo(
        contacto_id=contacto["contacto_id"],
        nombre_completo=contacto.get("nombre_completo") or "",
        telefono=contacto.get("telefono"),
        etapa_actual_orden=contacto.get("etapa_actual_orden"),
        metadata=_safe_json_dict(contacto.get("metadata")),
    )

    tiene_embudo = bool(contexto_data.get("tiene_embudo"))
    etapas_info = []
    etapa_actual_completa = None

    if etapas:
        for etapa in etapas:
            if etapa.get("id") is None or etapa.get("orden_etapa") is None or not etapa.get("nombre_etapa"):
                continue
            etapa_info = FunnelStageInfo(
                id=etapa.get("id"),
                nombre_etapa=etapa.get("nombre_etapa", ""),
                orden_etapa=etapa.get("orden_etapa", 0),
                descripcion=_safe_json_dict(etapa.get("descripcion")),
                es_etapa_actual=_is_current_stage_match(contacto.get("etapa_actual_orden"), etapa),
            )
            etapas_info.append(etapa_info)

        if etapa_actual_data and etapa_actual_completa is None:
            etapa_actual_completa = _build_current_stage(etapa_actual_data)
        elif etapas_info:
            for etapa_info in etapas_info:
                if etapa_info.es_etapa_actual:
                    descripcion_json = etapa_info.descripcion or {}
                    etapa_actual_completa = FunnelCurrentStage(
                        id=etapa_info.id,
                        orden=etapa_info.orden_etapa,
                        nombre=etapa_info.nombre_etapa,
                        que_es=descripcion_json.get("que_es"),
                        senales=descripcion_json.get("senales"),
                    )
                    break

    conversacion_resumen = conversacion_data.get("resumen")
    mensajes_raw = conversacion_data.get("mensajes") or []
    ultimos_mensajes = [
        {
            "timestamp": msg.get("timestamp"),
            "remitente": msg.get("remitente"),
            "contenido": msg.get("contenido") or msg.get("mensaje", ""),
            "tipo": msg.get("tipo", "text"),
        }
        for msg in mensajes_raw
    ] or None
    
    return FunnelContextResponse(
        contacto=info_contacto,
        etapa_actual=etapa_actual_completa,
        todas_etapas=etapas_info,
        tiene_embudo=tiene_embudo,
        conversacion_resumen=conversacion_resumen,
        ultimos_mensajes=ultimos_mensajes,
        contexto_embudo=contexto_embudo,
        etapas_embudo=etapas_embudo,
        conversacion_memoria=conversacion_memoria,
    )


def _build_graph(llm_with_tools, context: FunnelContextResponse, requestData: FunnelAgentRequest):
    """Construye el grafo LangGraph para el agente de embudo."""
    
    # Estado para rastrear cambios
    etapa_anterior = context.etapa_actual.nombre if context.etapa_actual else None
    
    async def agent_node(state: FunnelAgentState) -> dict:
        """Nodo principal del agente: analiza el contexto y genera análisis."""
        t_llm = time.perf_counter()
        response = await llm_with_tools.ainvoke(state["messages"])
        llm_elapsed_ms = (time.perf_counter() - t_llm) * 1000
        return {
            "messages": [response],
            "llm_elapsed_ms": round(float(state.get("llm_elapsed_ms", 0)) + llm_elapsed_ms, 1),
            "llm_iterations": int(state.get("llm_iterations", 0)) + 1,
        }
    
    async def tool_execution_node(state: FunnelAgentState) -> dict:
        """Ejecuta herramientas (actualizar etapa/metadata)."""
        tools_used = list(state.get("tools_used", []))
        tool_messages: list[ToolMessage] = []
        tool_execution_ms = float(state.get("tool_execution_ms", 0))
        etapa_nueva = None
        metadata_actualizada = None
        
        last_message = state["messages"][-1]
        
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {
                "messages": tool_messages,
                "tools_used": tools_used,
                "etapa_nueva": etapa_nueva,
                "metadata_actualizada": metadata_actualizada,
                "tool_execution_ms": round(tool_execution_ms, 1),
            }
        
        # Ejecutar herramientas
        for tc in last_message.tool_calls:
            tool_name = tc.get("name") or "unknown"
            raw_args = tc.get("args") or {}
            tool_input = raw_args if isinstance(raw_args, dict) else {"input": raw_args}
            tool_start = time.perf_counter()
            status = "ok"
            error_text = None
            result = None

            try:
                if tool_name == "update_etapa_embudo":
                    id_etapa = tool_input.get("id_etapa")
                    razon = tool_input.get("razon", "Cambio identificado por agente")
                    etapa_objetivo = _resolve_stage_by_id(context, int(id_etapa)) if id_etapa is not None else None
                    if etapa_objetivo is None:
                        raise ValueError(f"Etapa {id_etapa} no es válida en este embudo")

                    contacto_actualizado = await db.actualizar_etapa_contacto(
                        contacto_id=requestData.contacto_id,
                        nueva_etapa_id=etapa_objetivo.id,
                    )
                    etapa_nueva = etapa_objetivo.id
                    result = (
                        f"✓ Etapa actualizada a {etapa_objetivo.nombre_etapa} (id={etapa_objetivo.id}, orden={etapa_objetivo.orden_etapa}): {razon}"
                        if contacto_actualizado
                        else "Error al actualizar etapa"
                    )

                elif tool_name == "update_metadata":
                    informacion_capturada = tool_input.get("informacion_capturada", {})
                    seccion = tool_input.get("seccion", "etapa_actual")
                    if not informacion_capturada:
                        result = "Sin cambios de metadata: no se capturó información nueva"
                    else:
                        metadata_json = {
                            seccion: {
                                "informacion_capturada": informacion_capturada,
                                "actualizado_en": str(time.time()),
                            }
                        }
                        contacto_actualizado = await db.actualizar_metadata_contacto(
                            contacto_id=requestData.contacto_id,
                            nueva_metadata=metadata_json,
                        )
                        if contacto_actualizado:
                            metadata_actualizada = informacion_capturada
                            result = f"✓ Metadata registrada en BD: {len(informacion_capturada)} campos capturados"
                        else:
                            result = "Error al actualizar metadata en BD"

                else:
                    raise ValueError(f"Herramienta no reconocida: {tool_name}")

                if isinstance(result, str) and result.lower().startswith("error"):
                    status = "error"
                    error_text = result
                tool_output = str(result)[:500]
            except Exception as exc:
                status = "error"
                error_text = str(exc)
                tool_output = f"Error: {exc}"

            duration_ms = (time.perf_counter() - tool_start) * 1000
            tool_execution_ms += duration_ms

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
                source="funnel",
                description=None,
            )
            tools_used.append(tool_call)
        
        return {
            "messages": tool_messages,
            "tools_used": tools_used,
            "etapa_nueva": etapa_nueva,
            "metadata_actualizada": metadata_actualizada,
            "tool_execution_ms": round(tool_execution_ms, 1),
        }
    
    def _should_use_tools(state: FunnelAgentState) -> str:
        """Decide si el agente debe usar herramientas."""
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        return END
    
    def _should_continue(state: FunnelAgentState) -> str:
        """Después de ejecutar herramientas, volver al agente para análisis final.
        Limita a máximo 2 iteraciones LLM para evitar loops infinitos."""
        llm_iterations = int(state.get("llm_iterations", 0))
        max_iterations = 2
        
        # Si ya hizo 2 iteraciones, terminar
        if llm_iterations >= max_iterations:
            return END
        
        # Si no, volver al agente para análisis final
        return "agent"
    
    graph = StateGraph(FunnelAgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_execution_node)
    
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _should_use_tools, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _should_continue, {"agent": "agent", END: END})
    
    return graph


async def run_funnel_agent(request: FunnelAgentRequest) -> FunnelAgentResponse:
    """Ejecuta el agente de embudo."""
    t_start = time.perf_counter()
    
    # Usar x-ai/grok-4.1-fast para este agente (optimizado para embudo)
    model = "x-ai/grok-4.1-fast"
    max_tokens = request.max_tokens or 512
    temperature = request.temperature if request.temperature is not None else 0.5
    
    logger.info(f"run_funnel_agent: contacto={request.contacto_id}, empresa={request.empresa_id}, model={model}")
    
    try:
        # Cargar contexto del embudo en paralelo
        context = await _load_funnel_context(
            contacto_id=request.contacto_id,
            empresa_id=request.empresa_id,
            conversacion_id=request.conversacion_id,
        )
        
        # Definir herramientas que el LLM puede usar
        @tool
        async def update_etapa_embudo(id_etapa: int, razon: str = "Cambio identificado por agente") -> str:
            """
            Actualiza la etapa del embudo del contacto.
            
            Args:
                id_etapa: ID único de la nueva etapa (válido en el contexto)
                razon: Razón del cambio
            
            Returns:
                Confirmación de la actualización
            """
            logger.info(f"Tool: update_etapa_embudo({id_etapa}, {razon})")
            try:
                etapa_objetivo = _resolve_stage_by_id(context, id_etapa)
                if not etapa_objetivo:
                    return f"Error: Etapa {id_etapa} no es válida en este embudo"
                
                contacto_actualizado = await db.actualizar_etapa_contacto(
                    contacto_id=request.contacto_id,
                    nueva_etapa_id=etapa_objetivo.id,
                )
                
                if contacto_actualizado:
                    logger.info(f"Contacto {request.contacto_id} actualizado a etapa id={etapa_objetivo.id}")
                    return f"✓ Etapa actualizada a {etapa_objetivo.nombre_etapa} (id={etapa_objetivo.id}): {razon}"
                else:
                    return "Error: No se pudo actualizar la etapa"
            except Exception as e:
                logger.error(f"Error en update_etapa_embudo: {e}")
                return f"Error: {str(e)}"
        
        @tool
        async def update_metadata(informacion_capturada: dict, seccion: str = "etapa_actual") -> str:
            """
            Registra TODA la información capturada en la conversación según la etapa actual.
            
            Args:
                informacion_capturada: Diccionario con los campos capturados (ej: {"info_reg_1": "valor", "info_reg_2": "valor"})
                seccion: Sección de metadata donde guardar (default: etapa_actual)
            
            Returns:
                Confirmación de la actualización
            """
            logger.info(f"Tool: update_metadata(campos={len(informacion_capturada)}, seccion={seccion})")
            try:
                if not informacion_capturada:
                    return "Sin cambios de metadata: no se capturó información nueva"

                metadata_json = {
                    seccion: {
                        "informacion_capturada": informacion_capturada,
                        "actualizado_en": str(time.time()),
                    }
                }

                contacto_actualizado = await db.actualizar_metadata_contacto(
                    contacto_id=request.contacto_id,
                    nueva_metadata=metadata_json,
                )
                if contacto_actualizado:
                    logger.info(f"Metadata actualizada en BD para contacto {request.contacto_id}")
                    return f"✓ Metadata registrada en BD: {len(informacion_capturada)} campos capturados"
                return "Error al actualizar metadata en BD"
            except Exception as e:
                logger.error(f"Error en update_metadata: {e}")
                return f"Error: {str(e)}"
        
        # Crear LLM y bindearlo con las herramientas
        llm = _create_llm(model, max_tokens, temperature)
        tools = [update_etapa_embudo, update_metadata]
        llm_with_tools = llm.bind_tools(tools)
        
        # Construir grafo
        t_graph = time.perf_counter()
        graph = _build_graph(llm_with_tools, context, request)
        compiled = graph.compile()
        graph_build_ms = (time.perf_counter() - t_graph) * 1000
        
        conversacion_memoria_payload = (context.conversacion_memoria or {}).get("data") or {
            "id": request.conversacion_id,
            "total_mensajes": 0,
            "mensajes_retornados": 0,
            "mensajes": [],
        }
        etapas_payload = ((context.etapas_embudo or {}).get("data") or {}).get("etapas") or [
            etapa.model_dump() for etapa in context.todas_etapas
        ]
        contexto_embudo_payload = (context.contexto_embudo or {}).get("data") or {
            "informacion_contacto": context.contacto.model_dump(),
            "etapa_actual": context.etapa_actual.model_dump() if context.etapa_actual else None,
            "tiene_embudo": context.tiene_embudo,
            "total_etapas": len(context.todas_etapas),
            "todas_etapas": etapas_payload,
        }
        stage_label = (
            f"{context.etapa_actual.nombre} (Orden: {context.etapa_actual.orden})"
            if context.etapa_actual
            else "Sin etapa asignada"
        )
        
        system_prompt = f"""# IDENTIDAD Y MISIÓN

    Eres un analista conversacional que identifica etapas del embudo y registra información del prospecto.

    ## Objetivos:
    - **IDENTIFICAR** etapa actual del prospecto
    - **ACTUALIZAR** etapa usando `update_etapa_embudo`
    - **REGISTRAR** información usando `update_metadata`

    # Datos claves

    El contacto {context.contacto.nombre_completo or 'Sin nombre'} se encuentra en la etapa {stage_label}

    ---

    # CONTEXTO DEL EMBUDO

    **Etapas disponibles:**
    ```json
    {json.dumps(etapas_payload, ensure_ascii=False, indent=2)}
    ```

    **Etapa actual identificada + Metadata registrada:**
    ```json
    {json.dumps(contexto_embudo_payload, ensure_ascii=False, indent=2)}
    ```

    Si la etapa actual y la etapa identificada son la misma, no es necesario actualizarla.

    Cada etapa tiene:
    - `id` como identificador único de etapa
    - `orden_etapa` como posición secuencial, solo referencial
    - `descripcion.senales` como comportamientos observables
    - `descripcion.metadata.informacion_registrar` o `descripcion.informacion_registrar` como datos a capturar

    ---

    # HERRAMIENTAS

    ## 1. `update_etapa_embudo`
    Usa `id_etapa` como identificador único para actualizar la etapa del contacto.

    ## 2. `update_metadata`

    ### Cuándo usar:
    - Después de actualizar etapa, si hay datos nuevos que registrar
    - Cuando el prospecto comparte información clave
    - Al finalizar descubrimiento si hay 3 o más respuestas útiles

    ## Reglas del uso de la herramienta:

    - Úsalas solo si tienes algo que actualizar
    - Si el nuevo mensaje es irrelevante y no hay metadata nueva ni cambio de etapa, responde únicamente con `ok`

    ---

    ## CÓMO RELLENAR informacion_capturada

    Para cada objeto en `informacion_registrar`:

    1. Lee el campo `texto`
    2. Busca ese dato en la conversación
    3. Si lo encontraste: usa el `id` como clave y el valor real capturado
    4. Si no lo encontraste: omite ese `id`

    ---

    ## REGLAS DE MERGE

    - Campos existentes se preservan
    - Nuevos campos se agregan
    - Valores existentes se actualizan
    - Secciones se mantienen separadas

    ---

    ## REGLAS JSON OBLIGATORIAS

    1. Todas las claves entre comillas dobles
    2. Todos los strings entre comillas dobles
    3. No uses comillas simples
    4. No pongas comas al final del último elemento
    5. Balancea llaves correctamente

    ---

    ## Reglas extras:

    - Si la empresa no tiene embudo creado, no asignar etapa al contacto
    - Si no ha cambiado nada, no actualices ni metadata ni etapa

    ## CHECKLIST FINAL

    Antes de responder al equipo:
    - ¿Cambió de etapa? → `update_etapa_embudo`
    - ¿Usé `id_etapa` correcto?
    - ¿Registré TODO según `informacion_registrar`?
    - ¿Usé IDs correctos (`info_reg_X`) como claves?
    - ¿Valores reales y no ejemplos?
    - ¿Respuesta máxima de 3 líneas orientada al equipo?
    """

        user_message = (
            f"Historial de la conversacion: {json.dumps(conversacion_memoria_payload, ensure_ascii=False)} "
            "--- Use las tools si es necesario"
        )
        
        # Estado inicial
        initial_state: FunnelAgentState = {
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_message),
            ],
            "tools_used": [],
            "etapa_anterior": context.etapa_actual.nombre if context.etapa_actual else None,
            "etapa_nueva": None,
            "metadata_actualizada": None,
            "tool_execution_ms": 0,
            "llm_elapsed_ms": 0,
            "llm_iterations": 0,
            "short_circuit": False,
            "short_circuit_response": None,
        }
        
        # Ejecutar grafo
        final_state = await compiled.ainvoke(initial_state)
        
        # Extraer respuesta
        last_message = final_state["messages"][-1]
        response_text = last_message.content if isinstance(last_message, AIMessage) else str(last_message.content)
        
        # Asegurar máx 3 líneas
        response_text = "\n".join(response_text.split("\n")[:3]).strip()
        
        total_ms = (time.perf_counter() - t_start) * 1000
        llm_ms = float(final_state.get("llm_elapsed_ms", 0))
        tool_execution_ms = float(final_state.get("tool_execution_ms", 0))
        
        timing = TimingInfo(
            total_ms=round(total_ms, 1),
            llm_ms=round(llm_ms, 1),
            mcp_discovery_ms=0,
            graph_build_ms=round(graph_build_ms, 1),
            tool_execution_ms=round(tool_execution_ms, 1),
        )
        
        logger.info(f"Funnel agent completed - total: {timing.total_ms}ms | llm: {timing.llm_ms}ms | tools: {timing.tool_execution_ms}ms")
        
        agent_runs = [
            AgentRunTrace(
                agent_key="funnel_agent",
                agent_name="Agente de Embudo",
                agent_kind="analysis",
                conversation_id=str(request.conversacion_id) if request.conversacion_id else None,
                memory_session_id=None,
                model_used=model,
                system_prompt=system_prompt[:200],
                user_prompt=user_message,
                available_tools=[
                    ToolDefinition(
                        tool_name="update_etapa_embudo",
                        description="Actualiza la etapa del embudo usando id_etapa como identificador único"
                    ),
                    ToolDefinition(
                        tool_name="update_metadata",
                        description="Registra informacion capturada haciendo merge directo en metadata del contacto"
                    ),
                ],
                tools_used=final_state.get("tools_used", []),
                timing=timing,
                llm_iterations=int(final_state.get("llm_iterations", 0)),
            )
        ]
        
        # Registrar en debug
        add_funnel_debug_run(
            contacto_id=request.contacto_id,
            empresa_id=request.empresa_id,
            agent_runs=[run.model_dump() for run in agent_runs],
            timing=timing.model_dump(),
            tools_used=[t.model_dump() for t in final_state.get("tools_used", [])],
            success=True,
            respuesta=response_text,
            etapa_anterior=initial_state["etapa_anterior"],
            etapa_nueva=final_state.get("etapa_nueva"),
        )
        
        return FunnelAgentResponse(
            success=True,
            respuesta=response_text,
            etapa_anterior=initial_state["etapa_anterior"],
            etapa_nueva=final_state.get("etapa_nueva"),
            metadata_actualizada=final_state.get("metadata_actualizada"),
            tools_used=final_state.get("tools_used", []),
            timing=timing,
            agent_runs=agent_runs,
        )
    
    except Exception as e:
        logger.error(f"Error en run_funnel_agent: {e}", exc_info=True)
        total_ms = (time.perf_counter() - t_start) * 1000
        timing = TimingInfo(total_ms=round(total_ms, 1))
        error_tool = ToolCall(
            tool_name="funnel_error",
            tool_input={},
            tool_output=str(e),
            duration_ms=round(total_ms, 1),
            status="error",
            error=str(e),
            source="funnel",
            description="Error interno antes de completar la ejecucion del funnel",
        )
        error_trace = AgentRunTrace(
            agent_key="funnel_agent",
            agent_name="Agente de Embudo",
            agent_kind="analysis_error",
            conversation_id=str(request.conversacion_id) if request.conversacion_id else None,
            memory_session_id=None,
            model_used=model,
            system_prompt=f"Funnel fallo antes de completar la ejecucion: {e}",
            user_prompt="",
            available_tools=[
                ToolDefinition(
                    tool_name="funnel_error",
                    description="Error interno capturado durante la ejecucion del funnel",
                )
            ],
            tools_used=[error_tool],
            timing=timing,
            llm_iterations=0,
        )
        add_funnel_debug_run(
            contacto_id=request.contacto_id,
            empresa_id=request.empresa_id,
            agent_runs=[error_trace.model_dump()],
            timing=timing.model_dump(),
            tools_used=[error_tool.model_dump()],
            success=False,
            error=str(e),
            respuesta="Error al procesar el agente de embudo",
            etapa_anterior=None,
            etapa_nueva=None,
        )
        
        return FunnelAgentResponse(
            success=False,
            respuesta="Error al procesar el agente de embudo",
            error=str(e),
            timing=timing,
            tools_used=[error_tool],
            agent_runs=[error_trace],
        )
