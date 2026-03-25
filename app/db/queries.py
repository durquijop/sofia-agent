"""Queries Supabase async via httpx pooled client."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any
import httpx
from app.db.client import get_supabase

logger = logging.getLogger(__name__)

MAX_CONTEXT_MESSAGES = 4000
DEFAULT_CONTEXT_LIMIT = 400
MIN_CONTEXT_LIMIT = 1


def _safe_json_loads(value: Any) -> Any:
    if isinstance(value, str):
        raw = value.strip()
        if raw and raw[0] in "[{":
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return value
    return value


def _contacto_base(contacto: dict[str, Any]) -> dict[str, Any]:
    return {
        "contacto_id": contacto.get("id"),
        "nombre": contacto.get("nombre") or None,
        "apellido": contacto.get("apellido") or None,
        "nombre_completo": f"{contacto.get('nombre') or ''} {contacto.get('apellido') or ''}".strip(),
        "telefono": contacto.get("telefono") or None,
        "email": contacto.get("email") or None,
        "origen": contacto.get("origen") or None,
        "notas": contacto.get("notas") or None,
        "fecha_registro": contacto.get("fecha_registro") or None,
        "ultima_interaccion": contacto.get("ultima_interaccion") or None,
        "subscriber_id": contacto.get("subscriber_id") or None,
        "avatar_url": contacto.get("avatar_url") or None,
        "etapa_emocional": contacto.get("etapa_emocional") or None,
        "timezone": contacto.get("timezone") or None,
        "estado": contacto.get("estado") or None,
        "es_calificado": contacto.get("es_calificado"),
        "is_active": contacto.get("is_active"),
        "team_humano_id": contacto.get("team_humano_id"),
        "url_drive": contacto.get("url_drive") or None,
        "metadata": _safe_json_loads(contacto.get("metadata")) or {},
    }


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_fecha_hora(value: str | None) -> str | None:
    dt = _parse_timestamp(value)
    if dt is None:
        return None
    dias_semana = [
        "Lunes",
        "Martes",
        "Miercoles",
        "Jueves",
        "Viernes",
        "Sabado",
        "Domingo",
    ]
    return f"{dias_semana[dt.weekday()]} {dt.strftime('%d/%m/%Y %H:%M')}"


def _format_hora(value: str | None) -> str | None:
    dt = _parse_timestamp(value)
    if dt is None:
        return None
    return dt.strftime("%H:%M:%S")


def _calcular_tiempo_respuesta(timestamp_actual: str | None, timestamp_anterior: str | None) -> str | None:
    actual = _parse_timestamp(timestamp_actual)
    anterior = _parse_timestamp(timestamp_anterior)
    if actual is None or anterior is None:
        return None

    diferencia_segundos = int((actual - anterior).total_seconds())
    if diferencia_segundos < 0:
        diferencia_segundos = abs(diferencia_segundos)

    minutos = diferencia_segundos // 60
    horas = minutos // 60
    dias = horas // 24

    if dias > 0:
        return f"{dias}d {horas % 24}h"
    if horas > 0:
        return f"{horas}h {minutos % 60}m"
    if minutos > 0:
        return f"{minutos}m {diferencia_segundos % 60}s"
    return f"{diferencia_segundos}s"


def _procesar_uso_herramientas(uso_herramientas: Any) -> str | None:
    if not uso_herramientas:
        return None

    payload = uso_herramientas
    if isinstance(payload, str):
        cleaned = payload.replace("\\", "")
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            return None

    if isinstance(payload, list):
        herramientas: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            if isinstance(action, dict) and action.get("tool"):
                herramientas.append(str(action["tool"]))
            if item.get("tool"):
                herramientas.append(str(item["tool"]))
            nombre = item.get("nombre") or item.get("name") or item.get("herramienta")
            if nombre:
                herramientas.append(str(nombre))
        herramientas_unicas = list(dict.fromkeys(herramientas))
        return ", ".join(herramientas_unicas) if herramientas_unicas else None

    if isinstance(payload, dict):
        action = payload.get("action")
        if isinstance(action, dict) and action.get("tool"):
            return str(action["tool"])
        if payload.get("tool"):
            return str(payload["tool"])

    return None


def _normalizar_etapa_actual(contacto_stage_value: Any, etapa: dict[str, Any]) -> bool:
    if contacto_stage_value is None:
        return False
    return contacto_stage_value in {etapa.get("id"), etapa.get("orden_etapa")}


def _metadata_stage_value(metadata: Any) -> Any:
    metadata_dict = _safe_json_loads(metadata) or {}
    if not isinstance(metadata_dict, dict):
        return None
    etapa_actual = metadata_dict.get("etapa_actual")
    if not isinstance(etapa_actual, dict):
        return None
    embudo = etapa_actual.get("embudo")
    if not isinstance(embudo, dict):
        return None
    for key in ("etapa_id", "id", "orden_etapa"):
        value = embudo.get(key)
        if value is not None:
            return value
    return None


def _resolved_stage_order(contacto_stage_value: Any, etapas: list[dict[str, Any]]) -> Any:
    for etapa in etapas or []:
        if _normalizar_etapa_actual(contacto_stage_value, etapa):
            return etapa.get("orden_etapa")
    return contacto_stage_value


def _organizar_mensajes_para_contexto(mensajes: list[dict[str, Any]], limite: int) -> list[dict[str, Any]]:
    if not mensajes:
        return []

    mensajes_filtrados = [
        msg
        for msg in mensajes
        if (msg.get("contenido") or "").strip() and "❌" not in str(msg.get("contenido") or "")
    ]
    mensajes_filtrados.sort(key=lambda msg: _parse_timestamp(msg.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    mensajes_filtrados = mensajes_filtrados[:limite]
    mensajes_filtrados.reverse()

    mensajes_procesados: list[dict[str, Any]] = []
    for index, msg in enumerate(mensajes_filtrados):
        mensaje_anterior = mensajes_filtrados[index - 1] if index > 0 else None
        mensajes_procesados.append(
            {
                "fecha_hora": _format_fecha_hora(msg.get("timestamp")),
                "hora": _format_hora(msg.get("timestamp")),
                "timestamp": msg.get("timestamp"),
                "remitente": msg.get("remitente") or "desconocido",
                "mensaje": (msg.get("contenido") or "").strip(),
                "uso_herramientas": _procesar_uso_herramientas(msg.get("uso_herramientas")),
                "tiempo_respuesta": _calcular_tiempo_respuesta(
                    msg.get("timestamp"),
                    mensaje_anterior.get("timestamp") if mensaje_anterior else None,
                ),
                "tipo": msg.get("tipo"),
                "modelo_llm": msg.get("modelo_llm"),
            }
        )
    return mensajes_procesados


def _deep_merge_dicts(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(current, value)
        else:
            merged[key] = value
    return merged


def _is_missing_table_error(exc: Exception) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code != 404:
        return False
    request_url = str(exc.request.url) if exc.request else ""
    return "/rest/v1/" in request_url


async def _safe_optional_query(
    table: str,
    *,
    select: str = "*",
    filters: dict[str, Any] | None = None,
    order: str | None = None,
    order_desc: bool = False,
    limit: int | None = None,
    single: bool = False,
):
    sb = await get_supabase()
    try:
        return await sb.query(
            table,
            select=select,
            filters=filters,
            order=order,
            order_desc=order_desc,
            limit=limit,
            single=single,
        )
    except Exception as exc:
        if _is_missing_table_error(exc):
            logger.warning("Tabla opcional %s no existe en Supabase; se continua sin esos datos", table)
            return None if single else []
        raise


async def _safe_optional_delete(table: str, filters: dict[str, Any]) -> list[dict]:
    sb = await get_supabase()
    try:
        return await sb.delete(table, filters)
    except Exception as exc:
        if _is_missing_table_error(exc):
            logger.warning("Tabla opcional %s no existe en Supabase; se omite el borrado", table)
            return []
        raise


# ─── Empresa ─────────────────────────────────────────────────────────────────

async def get_empresa(empresa_id: int) -> dict | None:
    """Obtiene el perfil completo de una empresa."""
    sb = await get_supabase()
    return await sb.query("wp_empresa_perfil", filters={"id": empresa_id}, single=True)


async def get_empresa_embudo(empresa_id: int) -> list[dict]:
    """Obtiene las etapas del embudo de una empresa ordenadas."""
    sb = await get_supabase()
    return await sb.query(
        "wp_empresa_embudo",
        select="id,nombre_etapa,orden_etapa,descripcion",
        filters={"empresa_id": empresa_id},
        order="orden_etapa",
    ) or []


# ─── Agentes ─────────────────────────────────────────────────────────────────

async def get_agente(agente_id: int) -> dict | None:
    """Obtiene la configuración completa de un agente."""
    sb = await get_supabase()
    return await sb.query("wp_agentes", filters={"id": agente_id, "archivado": False}, single=True)


async def get_agente_tools(agente_id: int) -> list[dict]:
    """Obtiene las herramientas MCP asignadas a un agente."""
    sb = await get_supabase()
    return await sb.query(
        "wp_agente_tools",
        select="*,wp_mcp_tools_catalog(*)",
        filters={"agente_id": agente_id, "activo": True},
        order="prioridad",
    ) or []


async def get_agentes_por_empresa(empresa_id: int) -> list[dict]:
    """Lista agentes activos de una empresa."""
    sb = await get_supabase()
    return await sb.query(
        "wp_agentes",
        select="id,nombre_agente,rol,llm,mcp_url",
        filters={"empresa_id": empresa_id, "archivado": False},
    ) or []


# ─── Contactos ───────────────────────────────────────────────────────────────

async def get_contacto(contacto_id: int) -> dict | None:
    """Obtiene un contacto por ID."""
    sb = await get_supabase()
    return await sb.query("wp_contactos", filters={"id": contacto_id}, single=True)


async def get_contacto_empresa(contacto_id: int, empresa_id: int) -> dict | None:
    """Obtiene un contacto validando que pertenezca a la empresa."""
    sb = await get_supabase()
    return await sb.query(
        "wp_contactos",
        select="id,nombre,apellido,etapa_embudo,empresa_id,metadata,telefono,email,fecha_registro,ultima_interaccion,origen,notas,is_active,created_at,updated_at,subscriber_id,avatar_url,etapa_emocional,team_humano_id,timezone,es_calificado,estado,url_drive",
        filters={"id": contacto_id, "empresa_id": empresa_id},
        single=True,
    )


async def get_contacto_por_telefono(telefono: str, empresa_id: int) -> dict | None:
    """Busca un contacto por teléfono dentro de una empresa."""
    sb = await get_supabase()
    return await sb.query(
        "wp_contactos",
        filters={"telefono": telefono, "empresa_id": empresa_id},
        single=True,
    )


async def upsert_contacto_whatsapp(telefono: str, empresa_id: int) -> tuple[dict, bool]:
    """Crea o actualiza un contacto de WhatsApp minimizando consultas."""
    sb = await get_supabase()
    timestamp = datetime.now(timezone.utc).isoformat()
    existente = await get_contacto_por_telefono(telefono, empresa_id)

    if existente and existente.get("id") is not None:
        updated = await sb.update(
            "wp_contactos",
            {"id": existente["id"]},
            {"ultima_interaccion": timestamp},
        )
        return ((updated[0] if updated else existente), False)

    creado = await sb.insert(
        "wp_contactos",
        {
            "telefono": telefono,
            "empresa_id": empresa_id,
            "origen": "Whatsapp",
            "notas": "",
            "fecha_registro": timestamp,
            "ultima_interaccion": timestamp,
        },
    )
    return (creado, True)


async def get_contacto_notas(contacto_id: int, limit: int = 10) -> list[dict]:
    """Obtiene las notas visibles para IA de un contacto."""
    sb = await get_supabase()
    return await sb.query(
        "wp_contactos_nota",
        select="id,titulo,descripcion,etiquetas,es_fijado,created_at",
        filters={"contacto_id": contacto_id, "visible_ia": True},
        order="created_at", order_desc=True,
        limit=limit,
    ) or []


async def get_contacto_contextos(contacto_id: int) -> list[dict]:
    """Obtiene contextos adicionales asociados al contacto."""
    return await _safe_optional_query(
        "wp_contextos",
        select="clave,valor",
        filters={"contacto_id": contacto_id},
    ) or []


async def get_contacto_con_contexto(contacto_id: int) -> dict | None:
    """Obtiene contacto + notas + citas recientes en paralelo."""
    contacto = await get_contacto(contacto_id)
    if not contacto:
        return None

    notas, citas = await asyncio.gather(
        get_contacto_notas(contacto_id),
        get_citas_contacto(contacto_id, limit=5),
    )
    contacto["_notas"] = notas
    contacto["_citas_recientes"] = citas
    return contacto


# ─── Conversaciones y Mensajes ───────────────────────────────────────────────

async def get_conversacion(conversacion_id: int) -> dict | None:
    """Obtiene una conversación por ID."""
    sb = await get_supabase()
    return await sb.query("wp_conversaciones", filters={"id": conversacion_id}, single=True)


async def get_conversacion_contexto(conversacion_id: int, empresa_id: int, contacto_id: int) -> dict | None:
    """Obtiene la conversación usada por el contexto normalizado del agente."""
    sb = await get_supabase()
    return await sb.query(
        "wp_conversaciones",
        select="id,empresa_id,agente_id,contacto_id,fecha_inicio,canal,resumen,seguimiento,evaluacion",
        filters={"id": conversacion_id, "empresa_id": empresa_id, "contacto_id": contacto_id},
        single=True,
    )


async def get_conversacion_activa(contacto_id: int, numero_id: int) -> dict | None:
    """Busca la conversación más reciente entre un contacto y un número."""
    sb = await get_supabase()
    results = await sb.query(
        "wp_conversaciones",
        filters={"contacto_id": contacto_id, "numero_id": numero_id},
        order="created_at", order_desc=True,
        limit=1,
    )
    return results[0] if results else None


async def get_conversaciones_contacto(contacto_id: int) -> list[dict]:
    """Lista conversaciones de un contacto."""
    sb = await get_supabase()
    return await sb.query(
        "wp_conversaciones",
        select="id,numero_id,agente_id",
        filters={"contacto_id": contacto_id},
        order="created_at",
        order_desc=True,
    ) or []


async def insertar_conversacion(
    contacto_id: int,
    agente_id: int | None,
    empresa_id: int,
    numero_id: int,
    canal: str,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Inserta una nueva conversación."""
    sb = await get_supabase()
    payload: dict[str, Any] = {
        "contacto_id": contacto_id,
        "agente_id": agente_id,
        "empresa_id": empresa_id,
        "numero_id": numero_id,
        "canal": canal,
        "metadata": metadata,
    }
    return await sb.insert("wp_conversaciones", payload)


async def get_mensajes_recientes(conversacion_id: int, limit: int = 20) -> list[dict]:
    """Obtiene los últimos mensajes de una conversación (orden cronológico)."""
    sb = await get_supabase()
    data = await sb.query(
        "wp_mensajes",
        select="id,contenido,tipo,remitente,timestamp,modelo_llm,uso_herramientas",
        filters={"conversacion_id": conversacion_id},
        order="timestamp", order_desc=True,
        limit=limit,
    ) or []
    data.reverse()
    return data


async def insertar_mensaje(
    conversacion_id: int,
    contenido: str,
    remitente: str,
    tipo: str = "text",
    status: str = "sent",
    modelo_llm: str | None = None,
    uso_herramientas: dict | None = None,
    metadata: dict[str, Any] | None = None,
    empresa_id: int | None = None,
) -> dict:
    """Inserta un mensaje en una conversación."""
    sb = await get_supabase()
    payload: dict[str, Any] = {
        "conversacion_id": conversacion_id,
        "contenido": contenido,
        "remitente": remitente,
        "tipo": tipo,
        "status": status,
    }
    if modelo_llm:
        payload["modelo_llm"] = modelo_llm
    if uso_herramientas:
        payload["uso_herramientas"] = uso_herramientas
    if metadata is not None:
        payload["metadata"] = metadata
    if empresa_id:
        payload["empresa_id"] = empresa_id
    return await sb.insert("wp_mensajes", payload)


async def actualizar_mensaje(mensaje_id: int, data: dict[str, Any]) -> dict | None:
    """Actualiza un mensaje existente por ID."""
    sb = await get_supabase()
    updated = await sb.update("wp_mensajes", {"id": mensaje_id}, data)
    if updated:
        return updated[0]
    return await sb.query("wp_mensajes", filters={"id": mensaje_id}, single=True)


async def get_stuck_messages(minutes_old: int = 5, limit: int = 20) -> list[dict]:
    """Obtiene mensajes de usuario atascados en status buffer/procesando por más de N minutos."""
    from datetime import timedelta
    sb = await get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes_old)).isoformat()
    return await sb.query(
        "wp_mensajes",
        select="id,conversacion_id,contenido,tipo,remitente,status,metadata,timestamp,empresa_id",
        raw_filters={
            "status": "in.(buffer,procesando)",
            "remitente": "eq.usuario",
            "timestamp": f"lt.{cutoff}",
        },
        order="timestamp",
        limit=limit,
    ) or []


async def has_agent_response_after(conversacion_id: int, after_timestamp: str) -> bool:
    """Verifica si ya existe una respuesta del agente después de cierto timestamp."""
    sb = await get_supabase()
    rows = await sb.query(
        "wp_mensajes",
        select="id",
        filters={"conversacion_id": conversacion_id, "remitente": "agente"},
        raw_filters={"timestamp": f"gt.{after_timestamp}"},
        limit=1,
    )
    return bool(rows)


async def get_agent_memory(session_id: str, limit: int = 16) -> list[dict]:
    sb = await get_supabase()
    data = await sb.query(
        "agent_memory",
        filters={"session_id": session_id},
        order="id",
        order_desc=True,
        limit=limit,
    ) or []
    data.reverse()
    return data


async def insert_agent_memory(session_id: str, message: dict[str, Any]) -> dict:
    sb = await get_supabase()
    return await sb.insert(
        "agent_memory",
        {
            "session_id": session_id,
            "message": message,
        },
    )


async def delete_agent_memory(session_id: str) -> int:
    """Elimina toda la memoria conversacional de una sesión."""
    sb = await get_supabase()
    deleted = await sb.delete("agent_memory", {"session_id": session_id})
    return len(deleted or [])


async def reset_contacto_data(contacto_id: int) -> dict[str, int]:
    """Elimina la información persistida del contacto para reiniciar su estado."""
    sb = await get_supabase()
    conversaciones = await get_conversaciones_contacto(contacto_id)
    conversation_ids = [int(item["id"]) for item in conversaciones if item.get("id") is not None]

    mensajes_deleted = 0
    if conversation_ids:
        deleted_messages = await asyncio.gather(
            *[sb.delete("wp_mensajes", {"conversacion_id": conversation_id}) for conversation_id in conversation_ids]
        )
        mensajes_deleted = sum(len(items or []) for items in deleted_messages)

    (
        conversaciones_deleted,
        notas_deleted,
        contextos_deleted,
        citas_deleted,
        notificaciones_deleted,
        actividades_deleted,
    ) = await asyncio.gather(
        sb.delete("wp_conversaciones", {"contacto_id": contacto_id}),
        sb.delete("wp_contactos_nota", {"contacto_id": contacto_id}),
        _safe_optional_delete("wp_contextos", {"contacto_id": contacto_id}),
        sb.delete("wp_citas", {"contacto_id": contacto_id}),
        _safe_optional_delete("wp_notificaciones_team", {"contacto_id": contacto_id}),
        _safe_optional_delete("wp_actividades_log", {"contacto_id": contacto_id}),
    )
    try:
        contactos_deleted = await sb.delete("wp_contactos", {"id": contacto_id})
    except Exception as exc:
        logger.warning("No se pudo eliminar wp_contactos id=%s (posible FK): %s — reseteando campos", contacto_id, exc)
        contactos_deleted = []
        # Cannot delete row (FK constraints) → reset all user-specific fields to NULL
        reset_payload = {
            "nombre": None,
            "apellido": None,
            "email": None,
            "etapa_embudo": None,
            "metadata": None,
            "origen": None,
            "notas": None,
            "avatar_url": None,
            "etapa_emocional": None,
            "timezone": None,
            "es_calificado": None,
            "estado": None,
            "url_drive": None,
        }
        try:
            await sb.update("wp_contactos", {"id": contacto_id}, reset_payload)
            logger.info("Campos de wp_contactos id=%s reseteados a NULL", contacto_id)
        except Exception as reset_exc:
            logger.error("Error reseteando campos de wp_contactos id=%s: %s", contacto_id, reset_exc)

    return {
        "mensajes": mensajes_deleted,
        "conversaciones": len(conversaciones_deleted or []),
        "notas": len(notas_deleted or []),
        "contextos": len(contextos_deleted or []),
        "citas": len(citas_deleted or []),
        "notificaciones": len(notificaciones_deleted or []),
        "actividades": len(actividades_deleted or []),
        "contactos": len(contactos_deleted or []),
    }


# ─── Citas ───────────────────────────────────────────────────────────────────

async def get_citas_contacto(contacto_id: int, limit: int = 5) -> list[dict]:
    """Obtiene las citas más recientes de un contacto."""
    sb = await get_supabase()
    return await sb.query(
        "wp_citas",
        select="id,titulo,fecha_hora,duracion,estado,ubicacion,team_humano_id",
        filters={"contacto_id": contacto_id},
        order="fecha_hora", order_desc=True,
        limit=limit,
    ) or []


async def get_citas_contacto_detalladas(contacto_id: int, limit: int = 10) -> list[dict]:
    """Obtiene citas recientes con campos extendidos para el prompt de Kapso."""
    sb = await get_supabase()
    return await sb.query(
        "wp_citas",
        select=(
            "id,fecha_hora,duracion,titulo,ubicacion,estado,descripcion,event_id,"
            "cuestionario_asesor,evaluacion_asesor,team_humano_id,timezone_cliente,preguntas_calendario"
        ),
        filters={"contacto_id": contacto_id},
        order="fecha_hora",
        order_desc=True,
        limit=limit,
    ) or []


async def get_notificaciones_contacto(contacto_id: int, limit: int = 20) -> list[dict]:
    """Obtiene notificaciones del team relacionadas con el contacto."""
    return await _safe_optional_query(
        "wp_notificaciones_team",
        select="id,tipo,mensaje,fecha_envio,estado,respuesta,fecha_respuesta,asesor_id,agente_id",
        filters={"contacto_id": contacto_id},
        order="fecha_envio",
        order_desc=True,
        limit=limit,
    ) or []


# ─── Team Humano ─────────────────────────────────────────────────────────────

async def get_team_member(team_id: int) -> dict | None:
    """Obtiene un miembro del equipo."""
    sb = await get_supabase()
    return await sb.query(
        "wp_team_humano",
        select=(
            "id,nombre,apellido,email,telefono,rol,especialidad,is_active,disponibilidad,"
            "calendly,acepta_citas,grupo_whatsapp,webinar,timezone"
        ),
        filters={"id": team_id},
        single=True,
    )


async def get_agente_rol(rol_id: int) -> dict | None:
    """Obtiene la configuración del rol asignado a un agente."""
    sb = await get_supabase()
    return await sb.query(
        "wp_agente_roles",
        select="id,nombre_rol,instrucciones_rol",
        filters={"id": rol_id},
        single=True,
    )


async def _resolved_value(value: Any) -> Any:
    return value


async def load_kapso_prompt_context(
    contacto_id: int | None,
    empresa_id: int | None,
    conversacion_id: int | None = None,
    team_id: int | None = None,
    agente_id: int | None = None,
    agente_rol_id: int | None = None,
    limite_mensajes: int = 8,
) -> dict[str, Any]:
    """Carga en paralelo el contexto requerido para construir el prompt de Kapso."""
    empresa_task = get_empresa(empresa_id) if empresa_id else _resolved_value(None)
    etapas_task = get_empresa_embudo(empresa_id) if empresa_id else _resolved_value([])
    contextos_task = get_contacto_contextos(contacto_id) if contacto_id else _resolved_value([])
    citas_task = get_citas_contacto_detalladas(contacto_id, limit=10) if contacto_id else _resolved_value([])
    notificaciones_task = get_notificaciones_contacto(contacto_id, limit=20) if contacto_id else _resolved_value([])
    notas_task = get_contacto_notas(contacto_id, limit=10) if contacto_id else _resolved_value([])
    team_task = get_team_member(team_id) if team_id else _resolved_value(None)
    rol_task = get_agente_rol(agente_rol_id) if agente_rol_id else _resolved_value(None)
    mensajes_task = get_mensajes_recientes(conversacion_id, limit=limite_mensajes) if conversacion_id else _resolved_value([])
    contexto_local_task = (
        load_contexto_completo_local(
            contacto_id=contacto_id,
            empresa_id=empresa_id,
            agente_id=agente_id,
            conversacion_id=conversacion_id,
            limite_mensajes=limite_mensajes,
        )
        if contacto_id and empresa_id
        else _resolved_value(None)
    )

    empresa, etapas_embudo, contextos, citas, notificaciones, notas, team_humano, rol_agente, mensajes, contexto_local = await asyncio.gather(
        empresa_task,
        etapas_task,
        contextos_task,
        citas_task,
        notificaciones_task,
        notas_task,
        team_task,
        rol_task,
        mensajes_task,
        contexto_local_task,
    )

    return {
        "empresa": empresa,
        "etapas_embudo": etapas_embudo,
        "contextos": contextos,
        "citas": citas,
        "notificaciones": notificaciones,
        "notas": notas,
        "team_humano": team_humano,
        "rol_agente": rol_agente,
        "mensajes_recientes": mensajes,
        "contexto_embudo_snapshot": (contexto_local or {}).get("contexto_embudo"),
        "etapas_embudo_snapshot": (contexto_local or {}).get("etapas_embudo"),
        "conversacion_memoria_snapshot": (contexto_local or {}).get("conversacion_memoria"),
    }


async def load_contexto_completo_local(
    contacto_id: int,
    empresa_id: int,
    agente_id: int | None = None,
    conversacion_id: int | None = None,
    limite_mensajes: int = 20,
) -> dict[str, Any]:
    """Replica localmente el contexto que antes entregaba la edge function obtener-contexto-completo-v1."""
    limite = max(MIN_CONTEXT_LIMIT, min(int(limite_mensajes or DEFAULT_CONTEXT_LIMIT), MAX_CONTEXT_MESSAGES))

    contacto_task = get_contacto_empresa(contacto_id, empresa_id)
    etapas_task = get_empresa_embudo(empresa_id)
    conversacion_task = (
        get_conversacion_contexto(conversacion_id, empresa_id, contacto_id)
        if conversacion_id
        else _resolved_value(None)
    )
    mensajes_task = (
        get_mensajes_recientes(conversacion_id, limit=min(limite * 2, MAX_CONTEXT_MESSAGES))
        if conversacion_id
        else _resolved_value([])
    )

    contacto, etapas, conversacion, mensajes = await asyncio.gather(
        contacto_task,
        etapas_task,
        conversacion_task,
        mensajes_task,
    )

    if not contacto:
        raise ValueError(f"Contacto no encontrado con ID {contacto_id} en empresa {empresa_id}")

    consultado_en = datetime.now(timezone.utc).isoformat()
    info_contacto = _contacto_base(contacto)

    todas_las_etapas: list[dict[str, Any]] = []
    etapa_actual_completa: dict[str, Any] | None = None
    tiene_embudo = bool(etapas)
    contacto_stage_value = _metadata_stage_value(contacto.get("metadata"))
    if contacto_stage_value is None:
        contacto_stage_value = contacto.get("etapa_embudo")
    contacto_stage_order = _resolved_stage_order(contacto_stage_value, etapas or [])

    for etapa in etapas or []:
        descripcion = etapa.get("descripcion") or {}
        descripcion_filtrada: dict[str, Any] = {}
        if isinstance(descripcion, dict):
            if descripcion.get("que_es") is not None:
                descripcion_filtrada["que_es"] = descripcion.get("que_es")
            if descripcion.get("senales") is not None:
                descripcion_filtrada["senales"] = descripcion.get("senales")
            if descripcion.get("metadata") is not None:
                descripcion_filtrada["metadata"] = descripcion.get("metadata")
            if descripcion.get("informacion_registrar") is not None:
                descripcion_filtrada["informacion_registrar"] = descripcion.get("informacion_registrar")

        es_etapa_actual = _normalizar_etapa_actual(contacto_stage_value, etapa)
        todas_las_etapas.append(
            {
                "id": etapa.get("id"),
                "nombre_etapa": etapa.get("nombre_etapa"),
                "orden_etapa": etapa.get("orden_etapa"),
                "descripcion": descripcion_filtrada,
                "es_etapa_actual": es_etapa_actual,
            }
        )

        if es_etapa_actual and etapa_actual_completa is None:
            etapa_actual_completa = {
                "id": etapa.get("id"),
                "orden": etapa.get("orden_etapa"),
                "nombre": etapa.get("nombre_etapa"),
                **descripcion,
            }

    mensajes_organizados = _organizar_mensajes_para_contexto(mensajes or [], limite)
    conversacion_data: dict[str, Any]
    if conversacion:
        conversacion_data = {
            "id": conversacion.get("id"),
            "empresa_id": conversacion.get("empresa_id"),
            "agente_id": conversacion.get("agente_id"),
            "contacto_id": conversacion.get("contacto_id"),
            "fecha_inicio": conversacion.get("fecha_inicio"),
            "canal": conversacion.get("canal"),
            "resumen": conversacion.get("resumen"),
            "seguimiento": conversacion.get("seguimiento"),
            "evaluacion": conversacion.get("evaluacion"),
            "total_mensajes": len(mensajes or []),
            "mensajes_retornados": len(mensajes_organizados),
            "mensajes": mensajes_organizados,
        }
    else:
        conversacion_data = {
            "id": conversacion_id,
            "total_mensajes": 0,
            "mensajes_retornados": 0,
            "mensajes": [],
        }

    contexto_embudo = {
        "success": True,
        "data": {
            "informacion_contacto": {
                **info_contacto,
                    "etapa_actual_orden": contacto_stage_order,
            },
            "etapa_actual": etapa_actual_completa,
            "tiene_embudo": tiene_embudo,
            "total_etapas": len(todas_las_etapas),
            "todas_etapas": todas_las_etapas,
        },
        "metadata": {
            "consultado_en": consultado_en,
            **({"agente_id": agente_id} if agente_id is not None else {}),
        },
    }

    etapas_embudo = {
        "success": True,
        "data": {
            "contacto": {
                "id": contacto.get("id"),
                "nombre": contacto.get("nombre") or None,
                "apellido": contacto.get("apellido") or None,
                "nombre_completo": info_contacto.get("nombre_completo"),
                "telefono": info_contacto.get("telefono"),
                "email": info_contacto.get("email"),
                "origen": info_contacto.get("origen"),
                "etapa_actual_orden": contacto_stage_order,
            },
            "empresa_id": empresa_id,
            "tiene_embudo": tiene_embudo,
            "total_etapas": len(todas_las_etapas),
            "etapas": todas_las_etapas,
        },
        "metadata": {
            "consultado_en": consultado_en,
            **({"agente_id": agente_id} if agente_id is not None else {}),
        },
    }

    conversacion_memoria = {
        "success": True,
        "data": conversacion_data,
        "metadata": {
            "consultado_en": consultado_en,
            **({"agente_id": agente_id} if agente_id is not None else {}),
        },
    }

    return {
        "success": True,
        "contexto_embudo": contexto_embudo,
        "etapas_embudo": etapas_embudo,
        "conversacion_memoria": conversacion_memoria,
    }


async def get_team_disponible(empresa_id: int) -> list[dict]:
    """Lista miembros activos del equipo de una empresa."""
    sb = await get_supabase()
    return await sb.query(
        "wp_team_humano",
        select="id,nombre,apellido,rol,especialidad,is_active,acepta_citas",
        filters={"empresa_id": empresa_id, "is_active": True},
    ) or []


# ─── Números / Canales ──────────────────────────────────────────────────────

async def get_numero(numero_id: int) -> dict | None:
    """Obtiene la config de un número/canal."""
    sb = await get_supabase()
    return await sb.query("wp_numeros", filters={"id": numero_id}, single=True)


async def get_numero_por_id_kapso(id_kapso: str) -> dict | None:
    """Busca un número/canal por el phone_number_id de Kapso."""
    sb = await get_supabase()
    return await sb.query(
        "wp_numeros",
        filters={"id_kapso": id_kapso, "activo": True},
        single=True,
    )


async def get_numero_por_telefono(telefono: str) -> dict | None:
    """Busca un número por teléfono."""
    sb = await get_supabase()
    return await sb.query(
        "wp_numeros",
        filters={"telefono": telefono, "activo": True},
        single=True,
    )


# ─── MCP Tools Catalog ──────────────────────────────────────────────────────

async def get_mcp_tools_catalog(empresa_id: int | None = None) -> list[dict]:
    """Obtiene el catálogo de herramientas MCP disponibles."""
    sb = await get_supabase()
    filters: dict[str, Any] = {"activo": True}
    if empresa_id:
        filters["empresa_id"] = empresa_id
    return await sb.query("wp_mcp_tools_catalog", filters=filters) or []


# ─── Actividades Log ────────────────────────────────────────────────────────

async def registrar_actividad(
    tipo: str,
    accion: str,
    descripcion: str,
    empresa_id: int,
    agente_id: int | None = None,
    contacto_id: int | None = None,
    datos_antes: dict | None = None,
    datos_despues: dict | None = None,
) -> dict:
    """Registra una actividad en el log del sistema."""
    sb = await get_supabase()
    payload: dict[str, Any] = {
        "tipo": tipo,
        "accion": accion,
        "descripcion": descripcion,
        "empresa_id": empresa_id,
    }
    if agente_id:
        payload["agente_id"] = agente_id
    if contacto_id:
        payload["contacto_id"] = contacto_id
    if datos_antes:
        payload["datos_antes"] = datos_antes
    if datos_despues:
        payload["datos_despues"] = datos_despues
    return await sb.insert("wp_actividades_log", payload)


# ─── Embudo (Funnel) ────────────────────────────────────────────────────────

async def actualizar_etapa_contacto(contacto_id: int, nueva_etapa_id: int) -> dict | None:
    """Actualiza la etapa del embudo de un contacto usando el id de la etapa."""
    sb = await get_supabase()
    await sb.update(
        "wp_contactos",
        {"id": contacto_id},
        {"etapa_embudo": nueva_etapa_id},
    )
    return await get_contacto(contacto_id)


async def actualizar_metadata_contacto(contacto_id: int, nueva_metadata: dict[str, Any]) -> dict | None:
    """Actualiza la metadata del contacto (merge con datos existentes)."""
    sb = await get_supabase()
    contacto_actual = await get_contacto(contacto_id)
    if not contacto_actual:
        return None
    
    metadata_existente = _safe_json_loads(contacto_actual.get("metadata")) or {}
    metadata_merged = _deep_merge_dicts(metadata_existente, nueva_metadata)
    
    await sb.update(
        "wp_contactos",
        {"id": contacto_id},
        {"metadata": metadata_merged},
    )
    return await get_contacto(contacto_id)


async def actualizar_campos_contacto(contacto_id: int, cambios: dict[str, Any]) -> dict | None:
    """Actualiza columnas permitidas de wp_contactos preservando el resto del registro."""
    sb = await get_supabase()
    contacto_actual = await get_contacto(contacto_id)
    if not contacto_actual:
        return None

    campos_permitidos = {
        "nombre",
        "apellido",
        "email",
        "telefono",
        "etapa_emocional",
        "timezone",
        "es_calificado",
        "estado",
    }
    payload: dict[str, Any] = {}
    for key, value in cambios.items():
        if key not in campos_permitidos or value is None:
            continue
        if isinstance(value, str):
            value = " ".join(value.strip().split())
            if not value:
                continue
        payload[key] = value

    if not payload:
        return contacto_actual

    await sb.update(
        "wp_contactos",
        {"id": contacto_id},
        payload,
    )
    return await get_contacto(contacto_id)


async def get_conversacion_con_mensajes(conversacion_id: int, limite_mensajes: int = 20) -> tuple[dict | None, list[dict]]:
    """Obtiene una conversación y sus mensajes en paralelo."""
    conversacion = await get_conversacion(conversacion_id)
    if not conversacion:
        return None, []
    
    mensajes = await get_mensajes_recientes(conversacion_id, limit=limite_mensajes)
    return conversacion, mensajes


async def load_funnel_context(
    contacto_id: int,
    empresa_id: int,
    conversacion_id: int | None = None,
    limite_mensajes: int = 20,
) -> tuple[dict | None, list[dict], tuple[dict | None, list[dict]]]:
    """
    Carga contexto del embudo en paralelo:
    - Contacto + Etapas del embudo
    - Conversación + Mensajes (si se proporciona conversacion_id)
    
    Retorna: (contacto, etapas, (conversacion, mensajes))
    """
    # Query 1: Contacto
    contacto_task = get_contacto(contacto_id)
    
    # Query 2: Etapas del embudo
    etapas_task = get_empresa_embudo(empresa_id)
    
    # Query 3: Conversación y mensajes (si existe)
    if conversacion_id:
        conv_msg_task = get_conversacion_con_mensajes(conversacion_id, limite_mensajes)
    else:
        conv_msg_task = None
    
    # Ejecutar en paralelo
    if conv_msg_task:
        contacto, etapas, (conversacion, mensajes) = await asyncio.gather(
            contacto_task,
            etapas_task,
            conv_msg_task,
        )
        return contacto, etapas, (conversacion, mensajes)
    else:
        contacto, etapas = await asyncio.gather(
            contacto_task,
            etapas_task,
        )
        return contacto, etapas, (None, [])
