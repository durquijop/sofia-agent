"""Rutas para el agente de embudo."""
import logging
from fastapi import APIRouter, HTTPException

from app.agents.funnel import run_funnel_agent
from app.schemas.funnel import FunnelAgentRequest, FunnelAgentResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/funnel", tags=["funnel"])


@router.post("/analyze", response_model=FunnelAgentResponse)
async def analyze_funnel_status(request: FunnelAgentRequest):
    """
    Ejecuta el agente de embudo para analizar el estado de un contacto.
    
    Retorna:
    - Análisis del estado actual (máx 3 líneas)
    - Cambios de etapa realizados (si aplica)
    - Metadata actualizada (si aplica)
    - Métricas de ejecución
    """
    try:
        logger.info(f"Funnel analysis request - contacto: {request.contacto_id}, empresa: {request.empresa_id}")
        response = await run_funnel_agent(request)
        logger.info(f"Funnel analysis completed - success: {response.success}, tools_used: {len(response.tools_used)}")
        return response
    except Exception as e:
        logger.error(f"Error en /funnel/analyze: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error procesando análisis de embudo: {str(e)}")
