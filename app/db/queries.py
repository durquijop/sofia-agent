"""Queries Supabase async via httpx pooled client."""
import asyncio
import logging
from typing import Any
from app.db.client import get_supabase

logger = logging.getLogger(__name__)


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


async def get_contacto_por_telefono(telefono: str, empresa_id: int) -> dict | None:
    """Busca un contacto por teléfono dentro de una empresa."""
    sb = await get_supabase()
    return await sb.query(
        "wp_contactos",
        filters={"telefono": telefono, "empresa_id": empresa_id},
        single=True,
    )


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
    modelo_llm: str | None = None,
    uso_herramientas: dict | None = None,
    empresa_id: int | None = None,
) -> dict:
    """Inserta un mensaje en una conversación."""
    sb = await get_supabase()
    payload: dict[str, Any] = {
        "conversacion_id": conversacion_id,
        "contenido": contenido,
        "remitente": remitente,
        "tipo": tipo,
        "status": "sent",
    }
    if modelo_llm:
        payload["modelo_llm"] = modelo_llm
    if uso_herramientas:
        payload["uso_herramientas"] = uso_herramientas
    if empresa_id:
        payload["empresa_id"] = empresa_id
    return await sb.insert("wp_mensajes", payload)


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


# ─── Team Humano ─────────────────────────────────────────────────────────────

async def get_team_member(team_id: int) -> dict | None:
    """Obtiene un miembro del equipo."""
    sb = await get_supabase()
    return await sb.query(
        "wp_team_humano",
        select="id,nombre,apellido,email,telefono,rol,especialidad,is_active,disponibilidad,calendly,acepta_citas",
        filters={"id": team_id},
        single=True,
    )


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
