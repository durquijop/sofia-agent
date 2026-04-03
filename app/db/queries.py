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
        "nombre": contacto.get("first_name") or None,
        "apellido": contacto.get("last_name") or None,
        "nombre_completo": contacto.get("canonical_name") or f"{contacto.get('first_name') or ''} {contacto.get('last_name') or ''}".strip(),
        "telefono": contacto.get("_phone_e164") or None,
        "email": contacto.get("email") or None,
        "origen": contacto.get("lead_source") or None,
        "notas": contacto.get("_notas") or None,  # populated via separate dim_person_attribute query
        "fecha_registro": contacto.get("created_at") or None,
        "ultima_interaccion": contacto.get("updated_at") or None,
        "estado": contacto.get("crm_stage") or None,
        "es_calificado": contacto.get("is_qualified"),
        "team_humano_id": contacto.get("assigned_team_member_id"),
        "metadata": {},
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


def _map_direction(remitente: str) -> str:
    """Map old remitente values to new direction values."""
    mapping = {"usuario": "inbound", "agente": "outbound", "system": "outbound"}
    return mapping.get(remitente, "inbound")


def _map_sender_type(remitente: str) -> str:
    """Map old remitente values to new sender_type values."""
    mapping = {"usuario": "contact", "agente": "ai_agent", "system": "system"}
    return mapping.get(remitente, "contact")


def _map_direction_to_remitente(direction: str, sender_type: str | None = None) -> str:
    """Map new direction/sender_type back to old remitente for compatibility."""
    if direction == "inbound":
        return "usuario"
    if sender_type == "system":
        return "system"
    return "agente"


def _organizar_mensajes_para_contexto(mensajes: list[dict[str, Any]], limite: int) -> list[dict[str, Any]]:
    if not mensajes:
        return []

    mensajes_filtrados = [
        msg
        for msg in mensajes
        if (msg.get("content_text") or "").strip() and "❌" not in str(msg.get("content_text") or "")
    ]
    mensajes_filtrados.sort(key=lambda msg: _parse_timestamp(msg.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    mensajes_filtrados = mensajes_filtrados[:limite]
    mensajes_filtrados.reverse()

    mensajes_procesados: list[dict[str, Any]] = []
    for index, msg in enumerate(mensajes_filtrados):
        mensaje_anterior = mensajes_filtrados[index - 1] if index > 0 else None
        remitente = _map_direction_to_remitente(msg.get("direction", ""), msg.get("sender_type"))
        mensajes_procesados.append(
            {
                "fecha_hora": _format_fecha_hora(msg.get("created_at")),
                "hora": _format_hora(msg.get("created_at")),
                "timestamp": msg.get("created_at"),
                "remitente": remitente,
                "mensaje": (msg.get("content_text") or "").strip(),
                "uso_herramientas": _procesar_uso_herramientas(msg.get("tool_calls")),
                "tiempo_respuesta": _calcular_tiempo_respuesta(
                    msg.get("created_at"),
                    mensaje_anterior.get("created_at") if mensaje_anterior else None,
                ),
                "tipo": msg.get("content_type"),
                "modelo_llm": (msg.get("metadata") or {}).get("model_id") if isinstance(msg.get("metadata"), dict) else None,
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
    return await sb.query("dim_enterprise", filters={"id": empresa_id}, single=True)


async def get_empresa_embudo(empresa_id: int) -> list[dict]:
    """Obtiene las etapas del embudo de una empresa.

    NOTE: In Monica Intelligence v2, the funnel (embudo) is no longer a separate
    table.  CRM stage lives on dim_person.crm_stage.  This function is kept for
    backward compatibility but returns an empty list.  Callers that need stage
    information should read dim_person.crm_stage directly.
    """
    logger.warning("get_empresa_embudo: wp_empresa_embudo removed in v2 schema; crm_stage is now on dim_person. Returning empty list.")
    return []


# ─── Agentes ─────────────────────────────────────────────────────────────────

async def get_agente(agente_id: int) -> dict | None:
    """Obtiene la configuración completa de un agente."""
    sb = await get_supabase()
    return await sb.query("dim_agent", filters={"id": agente_id, "is_active": True}, single=True)


async def get_agente_tools(agente_id: int) -> list[dict]:
    """Obtiene las herramientas asignadas a un agente.

    In v2, tools are stored as a tools_enabled array on dim_agent.
    Returns a list of dicts with tool names for backward compat.
    """
    sb = await get_supabase()
    agent = await sb.query("dim_agent", select="tools_enabled", filters={"id": agente_id, "is_active": True}, single=True)
    if not agent or not agent.get("tools_enabled"):
        return []
    tools = agent["tools_enabled"]
    if isinstance(tools, str):
        tools = _safe_json_loads(tools) or []
    if isinstance(tools, list):
        return [{"tool_name": t} if isinstance(t, str) else t for t in tools]
    return []


async def get_agentes_por_empresa(empresa_id: int) -> list[dict]:
    """Lista agentes activos de una empresa."""
    sb = await get_supabase()
    return await sb.query(
        "dim_agent",
        select="id,name,agent_type,model_id,slug",
        filters={"enterprise_id": empresa_id, "is_active": True},
    ) or []


# ─── Contactos ───────────────────────────────────────────────────────────────

async def get_contacto(contacto_id: int) -> dict | None:
    """Obtiene un contacto por ID."""
    sb = await get_supabase()
    return await sb.query("dim_person", filters={"id": contacto_id}, single=True)


async def get_contacto_empresa(contacto_id: int, empresa_id: int) -> dict | None:
    """Obtiene un contacto validando que pertenezca a la empresa."""
    sb = await get_supabase()
    return await sb.query(
        "dim_person",
        select="id,canonical_name,first_name,last_name,crm_stage,enterprise_id,email,created_at,updated_at,lead_source,is_qualified,assigned_team_member_id,person_type,lead_source_detail",
        filters={"id": contacto_id, "enterprise_id": empresa_id},
        single=True,
    )


async def get_contacto_por_telefono(telefono: str, empresa_id: int) -> dict | None:
    """Busca un contacto por teléfono dentro de una empresa (via dim_person_phone join)."""
    sb = await get_supabase()
    # Look up the phone in dim_person_phone first
    phone_row = await sb.query(
        "dim_person_phone",
        select="person_id",
        filters={"phone_e164": telefono, "enterprise_id": empresa_id},
        single=True,
    )
    if not phone_row or not phone_row.get("person_id"):
        return None
    return await sb.query("dim_person", filters={"id": phone_row["person_id"]}, single=True)


async def upsert_contacto_canal(telefono: str, empresa_id: int, canal: str = "whatsapp") -> tuple[dict, bool]:
    sb = await get_supabase()
    timestamp = datetime.now(timezone.utc).isoformat()
    existente = await get_contacto_por_telefono(telefono, empresa_id)
    origen = canal.strip().lower() if canal else "whatsapp"
    origen_label = {
        "whatsapp": "Whatsapp",
        "manychat": "ManyChat",
    }.get(origen, origen.title())

    if existente and existente.get("id") is not None:
        updated = await sb.update(
            "dim_person",
            {"id": existente["id"]},
            {"updated_at": timestamp},
        )
        return ((updated[0] if updated else existente), False)

    # canonical_name is NOT NULL — use phone as temporary name until we learn real name
    creado = await sb.insert(
        "dim_person",
        {
            "enterprise_id": empresa_id,
            "canonical_name": telefono,
            "lead_source": origen_label,
            "person_type": "prospect",
        },
    )
    # Also create the phone record in dim_person_phone
    if creado and creado.get("id"):
        # phone_normalized: digits only, no + prefix
        normalized = "".join(c for c in telefono if c.isdigit())
        await sb.insert(
            "dim_person_phone",
            {
                "person_id": creado["id"],
                "phone_e164": telefono,
                "phone_normalized": normalized,
                "enterprise_id": empresa_id,
            },
        )
    return (creado, True)


async def upsert_contacto_whatsapp(telefono: str, empresa_id: int) -> tuple[dict, bool]:
    """Crea o actualiza un contacto de WhatsApp minimizando consultas."""
    return await upsert_contacto_canal(telefono, empresa_id, canal="whatsapp")


async def get_contacto_notas(contacto_id: int, limit: int = 10) -> list[dict]:
    """Obtiene las notas visibles para IA de un contacto.

    In v2, notes may be stored as a field on dim_person or in dim_person_attribute.
    This queries dim_person_attribute with attribute_key = 'note'.
    """
    sb = await get_supabase()
    return await sb.query(
        "dim_person_attribute",
        select="id,attribute_key,attribute_value,created_at",
        filters={"person_id": contacto_id, "attribute_key": "note"},
        order="created_at", order_desc=True,
        limit=limit,
    ) or []


async def get_contacto_contextos(contacto_id: int) -> list[dict]:
    """Obtiene contextos adicionales asociados al contacto (dim_person_attribute)."""
    return await _safe_optional_query(
        "dim_person_attribute",
        select="attribute_key,attribute_value",
        filters={"person_id": contacto_id},
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
    return await sb.query("fact_conversation", filters={"id": conversacion_id}, single=True)


async def get_conversacion_contexto(conversacion_id: int, empresa_id: int, contacto_id: int) -> dict | None:
    """Obtiene la conversación usada por el contexto normalizado del agente."""
    sb = await get_supabase()
    return await sb.query(
        "fact_conversation",
        select="id,enterprise_id,agent_id,person_id,started_at,channel_id,status,message_count",
        filters={"id": conversacion_id, "enterprise_id": empresa_id, "person_id": contacto_id},
        single=True,
    )


async def get_conversacion_activa(contacto_id: int, numero_id: int) -> dict | None:
    """Busca la conversación más reciente entre un contacto y un canal."""
    sb = await get_supabase()
    results = await sb.query(
        "fact_conversation",
        filters={"person_id": contacto_id, "channel_id": numero_id},
        order="created_at", order_desc=True,
        limit=1,
    )
    return results[0] if results else None


async def get_conversaciones_contacto(contacto_id: int) -> list[dict]:
    """Lista conversaciones de un contacto."""
    sb = await get_supabase()
    return await sb.query(
        "fact_conversation",
        select="id,channel_id,agent_id",
        filters={"person_id": contacto_id},
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
        "person_id": contacto_id,
        "agent_id": agente_id,
        "enterprise_id": empresa_id,
        "channel_id": numero_id,
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    return await sb.insert("fact_conversation", payload)


async def get_mensajes_recientes(conversacion_id: int, limit: int = 20) -> list[dict]:
    """Obtiene los últimos mensajes de una conversación (orden cronológico)."""
    sb = await get_supabase()
    data = await sb.query(
        "fact_interaction",
        select="id,content_text,content_type,direction,sender_type,created_at,tool_calls,metadata",
        filters={"conversation_id": conversacion_id},
        order="created_at", order_desc=True,
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
    channel_id: int | None = None,
    person_id: int | None = None,
    agent_id: int | None = None,
) -> dict:
    """Inserta un mensaje en una conversación."""
    sb = await get_supabase()

    # If channel_id not provided, try to get it from the conversation
    if not channel_id or not empresa_id:
        conv = await get_conversacion(conversacion_id)
        if conv:
            channel_id = channel_id or conv.get("channel_id")
            empresa_id = empresa_id or conv.get("enterprise_id")
            person_id = person_id or conv.get("person_id")

    payload: dict[str, Any] = {
        "conversation_id": conversacion_id,
        "content_text": contenido,
        "direction": _map_direction(remitente),
        "sender_type": _map_sender_type(remitente),
        "content_type": {"texto": "text", "imagen": "image", "audio": "audio", "video": "video", "documento": "document", "ubicacion": "location"}.get(tipo, tipo),
    }
    # NOT NULL fields
    if empresa_id:
        payload["enterprise_id"] = empresa_id
    if channel_id:
        payload["channel_id"] = channel_id
    if person_id:
        payload["person_id"] = person_id
    if agent_id:
        payload["agent_id"] = agent_id
    # model_id and status don't exist on fact_interaction; store in metadata
    merged_metadata = dict(metadata or {})
    if modelo_llm:
        merged_metadata["model_id"] = modelo_llm
    if status and status != "sent":
        merged_metadata["status"] = status
    if merged_metadata:
        payload["metadata"] = merged_metadata
    if uso_herramientas:
        payload["tool_calls"] = uso_herramientas
    return await sb.insert("fact_interaction", payload)


async def actualizar_mensaje(mensaje_id: int, data: dict[str, Any]) -> dict | None:
    """Actualiza un mensaje existente por ID."""
    sb = await get_supabase()
    # Translate known old column names to new ones
    translated: dict[str, Any] = {}
    column_map = {
        "contenido": "content_text",
        "remitente": None,  # handled separately
        "tipo": "content_type",
        "timestamp": "created_at",
        "modelo_llm": None,  # model_id doesn't exist on fact_interaction; goes in metadata
        "uso_herramientas": "tool_calls",
        "conversacion_id": "conversation_id",
        "status": None,  # status doesn't exist on fact_interaction; goes in metadata
    }
    meta_updates: dict[str, Any] = {}
    for key, value in data.items():
        new_key = column_map.get(key, key)
        if key == "remitente":
            translated["direction"] = _map_direction(value)
            translated["sender_type"] = _map_sender_type(value)
        elif key in ("modelo_llm", "model_id"):
            meta_updates["model_id"] = value
        elif key == "status":
            meta_updates["status"] = value
        elif new_key is not None:
            translated[new_key] = value
    if meta_updates:
        translated["metadata"] = meta_updates

    updated = await sb.update("fact_interaction", {"id": mensaje_id}, translated)
    if updated:
        return updated[0]
    return await sb.query("fact_interaction", filters={"id": mensaje_id}, single=True)


async def get_stuck_messages(minutes_old: int = 5, limit: int = 20) -> list[dict]:
    """Obtiene mensajes de usuario atascados en status buffer/procesando por más de N minutos."""
    from datetime import timedelta
    sb = await get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes_old)).isoformat()
    return await sb.query(
        "fact_interaction",
        select="id,conversation_id,content_text,content_type,direction,sender_type,metadata,created_at,enterprise_id",
        raw_filters={
            "metadata->>status": "in.(buffer,procesando)",
            "direction": "eq.inbound",
            "created_at": f"lt.{cutoff}",
        },
        order="created_at",
        limit=limit,
    ) or []


async def has_agent_response_after(conversacion_id: int, after_timestamp: str) -> bool:
    """Verifica si ya existe una respuesta del agente después de cierto timestamp."""
    sb = await get_supabase()
    rows = await sb.query(
        "fact_interaction",
        select="id",
        filters={"conversation_id": conversacion_id, "direction": "outbound", "sender_type": "ai_agent"},
        raw_filters={"created_at": f"gt.{after_timestamp}"},
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
            *[sb.delete("fact_interaction", {"conversation_id": conversation_id}) for conversation_id in conversation_ids]
        )
        mensajes_deleted = sum(len(items or []) for items in deleted_messages)

    (
        conversaciones_deleted,
        attributes_deleted,
        citas_deleted,
    ) = await asyncio.gather(
        sb.delete("fact_conversation", {"person_id": contacto_id}),
        _safe_optional_delete("dim_person_attribute", {"person_id": contacto_id}),
        sb.delete("fact_appointment", {"person_id": contacto_id}),
    )

    # Also delete phone records
    phones_deleted = await _safe_optional_delete("dim_person_phone", {"person_id": contacto_id})

    try:
        contactos_deleted = await sb.delete("dim_person", {"id": contacto_id})
    except Exception as exc:
        logger.warning("No se pudo eliminar dim_person id=%s (posible FK): %s — reseteando campos", contacto_id, exc)
        contactos_deleted = []
        # Cannot delete row (FK constraints) → reset all user-specific fields to NULL
        reset_payload = {
            "canonical_name": None,
            "first_name": None,
            "last_name": None,
            "email": None,
            "crm_stage": None,
            "lead_source": None,
            "lead_source_detail": None,
            "is_qualified": None,
            "assigned_team_member_id": None,
        }
        try:
            await sb.update("dim_person", {"id": contacto_id}, reset_payload)
            logger.info("Campos de dim_person id=%s reseteados a NULL", contacto_id)
        except Exception as reset_exc:
            logger.error("Error reseteando campos de dim_person id=%s: %s", contacto_id, reset_exc)

    return {
        "mensajes": mensajes_deleted,
        "conversaciones": len(conversaciones_deleted or []),
        "notas": 0,
        "contextos": len(attributes_deleted or []),
        "citas": len(citas_deleted or []),
        "notificaciones": 0,
        "actividades": 0,
        "contactos": len(contactos_deleted or []),
        "phones": len(phones_deleted or []),
    }


# ─── Citas ───────────────────────────────────────────────────────────────────

async def get_citas_contacto(contacto_id: int, limit: int = 5) -> list[dict]:
    """Obtiene las citas más recientes de un contacto."""
    sb = await get_supabase()
    return await sb.query(
        "fact_appointment",
        select="id,appointment_type,scheduled_at,duration_minutes,status,location,team_member_id,outcome",
        filters={"person_id": contacto_id},
        order="scheduled_at", order_desc=True,
        limit=limit,
    ) or []


async def get_citas_contacto_detalladas(contacto_id: int, limit: int = 10) -> list[dict]:
    """Obtiene citas recientes con campos extendidos."""
    sb = await get_supabase()
    return await sb.query(
        "fact_appointment",
        select=(
            "id,scheduled_at,duration_minutes,appointment_type,location,status,"
            "outcome,calendar_event_id,team_member_id,agent_id,conversation_id"
        ),
        filters={"person_id": contacto_id},
        order="scheduled_at",
        order_desc=True,
        limit=limit,
    ) or []


async def get_notificaciones_contacto(contacto_id: int, limit: int = 20) -> list[dict]:
    """Obtiene notificaciones del team relacionadas con el contacto.

    NOTE: wp_notificaciones_team has been removed in v2. This is now a no-op.
    """
    logger.warning("get_notificaciones_contacto: wp_notificaciones_team removed in v2 schema. Returning empty list.")
    return []


# ─── Team Humano ─────────────────────────────────────────────────────────────

async def get_team_member(team_id: int) -> dict | None:
    """Obtiene un miembro del equipo."""
    sb = await get_supabase()
    return await sb.query(
        "dim_team_member",
        select="id,person_id,enterprise_id,auth_user_id,role,permissions,is_active,hired_at,created_at",
        filters={"id": team_id},
        single=True,
    )


async def get_agente_rol(rol_id: int) -> dict | None:
    """Obtiene la configuración del rol asignado a un agente.

    NOTE: In v2, wp_agente_roles is merged into dim_agent.system_prompt.
    This returns the agent's system_prompt as the role instructions.
    """
    sb = await get_supabase()
    agent = await sb.query(
        "dim_agent",
        select="id,name,system_prompt",
        filters={"id": rol_id},
        single=True,
    )
    if not agent:
        return None
    return {
        "id": agent.get("id"),
        "nombre_rol": agent.get("name"),
        "instrucciones_rol": agent.get("system_prompt"),
    }


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
    # In v2, crm_stage is directly on dim_person
    contacto_stage_value = contacto.get("crm_stage")
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
            "empresa_id": conversacion.get("enterprise_id"),
            "agente_id": conversacion.get("agent_id"),
            "contacto_id": conversacion.get("person_id"),
            "fecha_inicio": conversacion.get("started_at"),
            "canal": conversacion.get("channel_id"),
            "resumen": conversacion.get("status"),
            "seguimiento": None,
            "evaluacion": None,
            "total_mensajes": conversacion.get("message_count") or len(mensajes or []),
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
                "nombre": contacto.get("first_name") or None,
                "apellido": contacto.get("last_name") or None,
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
        "dim_team_member",
        select="id,person_id,role,permissions,is_active",
        filters={"enterprise_id": empresa_id, "is_active": True},
    ) or []


# ─── Números / Canales ──────────────────────────────────────────────────────

async def get_numero(numero_id: int) -> dict | None:
    """Obtiene la config de un número/canal."""
    sb = await get_supabase()
    return await sb.query("dim_channel", filters={"id": numero_id}, single=True)


async def get_numero_por_id_kapso(id_kapso: str) -> dict | None:
    """Busca un número/canal por el external_id (antes phone_number_id de Kapso)."""
    sb = await get_supabase()
    return await sb.query(
        "dim_channel",
        filters={"external_id": id_kapso, "is_active": True},
        single=True,
    )


async def get_numero_por_telefono(telefono: str) -> dict | None:
    """Busca un canal por teléfono."""
    sb = await get_supabase()
    return await sb.query(
        "dim_channel",
        filters={"external_phone": telefono, "is_active": True},
        single=True,
    )


# ─── MCP Tools Catalog ──────────────────────────────────────────────────────

async def get_mcp_tools_catalog(empresa_id: int | None = None) -> list[dict]:
    """Obtiene el catálogo de herramientas MCP disponibles.

    NOTE: In v2, wp_mcp_tools_catalog has been removed. Tools are stored in
    dim_agent.tools_enabled. This is now a no-op that returns an empty list.
    """
    logger.warning("get_mcp_tools_catalog: wp_mcp_tools_catalog removed in v2 schema. Tools are in dim_agent.tools_enabled. Returning empty list.")
    return []


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
    """Registra una actividad en el log del sistema.

    NOTE: In v2, wp_actividades_log has been removed. This logs a warning and returns
    an empty dict. If sys_webhook_log is available, activities could be routed there.
    """
    logger.warning(
        "registrar_actividad: wp_actividades_log removed in v2 schema. Activity not persisted: tipo=%s accion=%s desc=%s enterprise_id=%s",
        tipo, accion, descripcion, empresa_id,
    )
    return {}


# ─── Embudo (Funnel) ────────────────────────────────────────────────────────

async def actualizar_etapa_contacto(contacto_id: int, nueva_etapa_id: int) -> dict | None:
    """Actualiza la etapa CRM de un contacto."""
    sb = await get_supabase()
    await sb.update(
        "dim_person",
        {"id": contacto_id},
        {"crm_stage": nueva_etapa_id},
    )
    return await get_contacto(contacto_id)


async def actualizar_metadata_contacto(contacto_id: int, nueva_metadata: dict[str, Any]) -> dict | None:
    """Actualiza atributos del contacto (EAV pattern via dim_person_attribute).

    In v2, metadata is stored as key-value pairs in dim_person_attribute.
    This upserts each key from nueva_metadata as a separate attribute row.
    """
    sb = await get_supabase()
    contacto_actual = await get_contacto(contacto_id)
    if not contacto_actual:
        return None

    for key, value in nueva_metadata.items():
        serialized_value = json.dumps(value) if not isinstance(value, str) else value
        # Try to update existing attribute first
        existing = await sb.query(
            "dim_person_attribute",
            select="id",
            filters={"person_id": contacto_id, "attribute_key": key},
            single=True,
        )
        if existing and existing.get("id"):
            await sb.update(
                "dim_person_attribute",
                {"id": existing["id"]},
                {"attribute_value": serialized_value},
            )
        else:
            await sb.insert(
                "dim_person_attribute",
                {
                    "person_id": contacto_id,
                    "enterprise_id": contacto_actual.get("enterprise_id"),
                    "attribute_key": key,
                    "attribute_value": serialized_value,
                },
            )

    return await get_contacto(contacto_id)


async def actualizar_campos_contacto(contacto_id: int, cambios: dict[str, Any]) -> dict | None:
    """Actualiza columnas permitidas de dim_person preservando el resto del registro."""
    sb = await get_supabase()
    contacto_actual = await get_contacto(contacto_id)
    if not contacto_actual:
        return None

    # Map old field names to new field names
    field_map = {
        "nombre": "first_name",
        "apellido": "last_name",
        "email": "email",
        "telefono": None,  # phone is now in dim_person_phone, handled separately
        "es_calificado": "is_qualified",
        "estado": "crm_stage",
        "origen": "lead_source",
        # Also accept new field names directly
        "first_name": "first_name",
        "last_name": "last_name",
        "canonical_name": "canonical_name",
        "is_qualified": "is_qualified",
        "crm_stage": "crm_stage",
        "lead_source": "lead_source",
        "lead_source_detail": "lead_source_detail",
        "preferred_language": "preferred_language",
        "assigned_team_member_id": "assigned_team_member_id",
    }
    campos_permitidos = set(field_map.keys())

    payload: dict[str, Any] = {}
    for key, value in cambios.items():
        if key not in campos_permitidos or value is None:
            continue
        if key == "telefono":
            # Phone updates go to dim_person_phone, skip for dim_person update
            logger.info("actualizar_campos_contacto: phone update for person_id=%s should go through dim_person_phone", contacto_id)
            continue
        if isinstance(value, str):
            value = " ".join(value.strip().split())
            if not value:
                continue
        new_key = field_map.get(key, key)
        if new_key:
            payload[new_key] = value

    if not payload:
        return contacto_actual

    await sb.update(
        "dim_person",
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
