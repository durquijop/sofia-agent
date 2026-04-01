import logging
from fastapi import APIRouter, HTTPException

from app.core.cache import response_cache
from app.schemas.chat import ChatRequest, ChatResponse
from app.agents.conversational import run_agent
from app.db import queries as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Endpoint principal de chat multi-agente.

    Recibe:
    - system_prompt: Define el comportamiento del agente para la empresa
    - message: Mensaje del usuario
    - model: (opcional) Modelo LLM a usar via OpenRouter
    - mcp_servers: (opcional) Lista de MCP servers para herramientas dinámicas
    - conversation_id: (opcional) ID para mantener contexto
    - max_tokens: (opcional) Máximo de tokens en respuesta. Menor = más rápido
    - temperature: (opcional) Temperatura del modelo (0-2)
    """
    try:
        # Guard: contacto inactivo — aplica a webhook y cualquier canal que use /chat directamente
        if request.contacto_id:
            try:
                contacto = await db.get_contacto(request.contacto_id)
                if contacto and contacto.get("is_active") is False:
                    logger.warning(
                        "chat: contacto_id=%s is_active=False — request bloqueado",
                        request.contacto_id,
                    )
                    raise HTTPException(status_code=403, detail="Contacto inactivo")
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning(
                    "chat: no se pudo verificar is_active para contacto_id=%s: %s",
                    request.contacto_id, exc,
                )

        logger.info(f"Chat request - model: {request.model}, mcp_servers: {len(request.mcp_servers)}, max_tokens: {request.max_tokens}")
        response = await run_agent(request)
        logger.info(f"Chat response - tools_used: {len(response.tools_used)}, total_ms: {response.timing.total_ms}")
        return response
    except Exception as e:
        logger.error(f"Error en /chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error procesando la solicitud: {str(e)}")


@router.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "urpe-multiagent", "cache_size": response_cache.size}


@router.delete("/cache")
async def clear_cache():
    """Limpia el cache de respuestas."""
    response_cache.clear()
    return {"status": "ok", "message": "Cache limpiado"}
