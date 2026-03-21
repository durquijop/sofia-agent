"""Cliente async de Redis usando redis-py."""
import logging
from typing import Optional
from redis.asyncio import Redis, from_url
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_redis: Optional[Redis] = None


async def get_redis() -> Optional[Redis]:
    """Obtiene el cliente singleton de Redis. Devuelve None si no hay URL configurada."""
    global _redis
    if _redis is not None:
        return _redis

    settings = get_settings()
    url = settings.get_redis_url()
    
    if not url:
        logger.warning("No se encontró configuración de Redis (REDIS_URL), la persistencia en caché/debug puede estar deshabilitada")
        return None
        
    try:
        _redis = from_url(
            url, 
            decode_responses=True, 
            socket_timeout=5.0, 
            socket_connect_timeout=5.0
        )
        # Check conexión
        await _redis.ping()
        logger.info("Cliente Redis inicializado correctamente")
    except Exception as exc:
        logger.error("Error inicializando cliente Redis: %s", exc)
        _redis = None
        
    return _redis


async def close_redis() -> None:
    """Cierra las conexiones del pool de Redis."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.info("Cliente Redis cerrado")
