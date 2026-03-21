"""Endpoints para consultas a Supabase."""
import logging
from fastapi import APIRouter, HTTPException, Query

from app.db import queries as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/db", tags=["database"])


@router.get("/health")
async def db_health():
    """Verifica conexión a Supabase."""
    try:
        from app.db.client import get_supabase
        sb = await get_supabase()
        res = await sb.query("wp_empresa_perfil", select="id", count=True, limit=1)
        return {
            "status": "ok",
            "supabase": "connected",
            "empresas_count": res["count"] if isinstance(res, dict) else 0,
        }
    except Exception as e:
        logger.error(f"DB health check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")


@router.get("/empresa/{empresa_id}")
async def get_empresa(empresa_id: int):
    """Obtiene perfil de empresa."""
    data = await db.get_empresa(empresa_id)
    if not data:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    return data


@router.get("/empresa/{empresa_id}/agentes")
async def get_agentes(empresa_id: int):
    """Lista agentes activos de una empresa."""
    return await db.get_agentes_por_empresa(empresa_id)


@router.get("/empresa/{empresa_id}/embudo")
async def get_embudo(empresa_id: int):
    """Obtiene las etapas del embudo de una empresa."""
    return await db.get_empresa_embudo(empresa_id)


@router.get("/empresa/{empresa_id}/team")
async def get_team(empresa_id: int):
    """Lista miembros activos del equipo."""
    return await db.get_team_disponible(empresa_id)


@router.get("/agente/{agente_id}")
async def get_agente(agente_id: int):
    """Obtiene configuración completa de un agente."""
    data = await db.get_agente(agente_id)
    if not data:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    return data


@router.get("/agente/{agente_id}/tools")
async def get_agente_tools(agente_id: int):
    """Obtiene herramientas MCP de un agente."""
    return await db.get_agente_tools(agente_id)


@router.get("/contacto/{contacto_id}")
async def get_contacto(contacto_id: int):
    """Obtiene un contacto con contexto (notas, citas)."""
    data = await db.get_contacto_con_contexto(contacto_id)
    if not data:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
    return data


@router.get("/contacto/buscar/telefono")
async def buscar_contacto_telefono(
    telefono: str = Query(..., description="Teléfono del contacto"),
    empresa_id: int = Query(..., description="ID de la empresa"),
):
    """Busca contacto por teléfono dentro de una empresa."""
    data = await db.get_contacto_por_telefono(telefono, empresa_id)
    if not data:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
    return data


@router.get("/conversacion/{conversacion_id}/mensajes")
async def get_mensajes(
    conversacion_id: int,
    limit: int = Query(default=20, le=100, description="Máximo de mensajes"),
):
    """Obtiene los últimos mensajes de una conversación."""
    return await db.get_mensajes_recientes(conversacion_id, limit)


@router.get("/numero/{numero_id}")
async def get_numero(numero_id: int):
    """Obtiene configuración de un número/canal."""
    data = await db.get_numero(numero_id)
    if not data:
        raise HTTPException(status_code=404, detail="Número no encontrado")
    return data
