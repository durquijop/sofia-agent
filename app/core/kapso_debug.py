"""
Kapso Debug — persistencia en Redis de Railway.

- Guarda interacciones como JSON en un Hashes, y mantiene una Lista para el ordenamiento (ultimas 100).
- start_interaction / finish_interaction: fire-and-forget
"""
import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ─── Eventos en memoria (internos al bridge, no críticos) ────────────────────

_MAX_KAPSO_DEBUG_EVENTS = 200
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_KAPSO_DEBUG_EVENTS)
_lock = Lock()


def add_kapso_debug_event(source: str, stage: str, payload: dict[str, Any] | None = None) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "stage": stage,
        "payload": payload or {},
    }
    with _lock:
        _events.appendleft(entry)


def get_kapso_debug_events(limit: int = 100) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(limit, _MAX_KAPSO_DEBUG_EVENTS))
    with _lock:
        return list(_events)[:normalized_limit]


# ─── Interacciones en Redis ───────────────────────────────────────────────────

REDIS_HKEY_DATA = "kapso:debug:interactions:data"
REDIS_LKEY_ORDER = "kapso:debug:interactions:list"
MAX_STORED_INTERACTIONS = 100


def _fire(coro) -> None:
    """Lanza una coroutine de forma fire-and-forget sin bloquear el caller."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(coro)
        else:
            loop.run_until_complete(coro)
    except Exception as exc:
        logger.warning("fire-and-forget error: %s", exc)


async def _insert_interaction(interaction_id: str, data: dict[str, Any]) -> None:
    from app.core.redis_client import get_redis
    redis = await get_redis()
    if not redis:
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "id": interaction_id,
        "started_at": now_iso,
        "finished_at": None,
        "duration_ms": None,
        "status": "processing",
        "error": None,
        "from_phone": data.get("from_phone"),
        "contact_name": data.get("contact_name"),
        "message_id": data.get("message_id"),
        "message_type": data.get("message_type"),
        "message_text": data.get("message_text"),
        "phone_number_id": data.get("phone_number_id"),
        "agent_id": None,
        "agent_name": None,
        "model_used": None,
        "mcp_servers": [],
        "memory_session_id": None,
        "timing": None,
        "tools_used": [],
        "reaction_emoji": None,
        "reply_type": None,
        "response_chars": None,
        "response_preview": None,
    }

    try:
        # Guardamos en el hash
        await redis.hset(REDIS_HKEY_DATA, interaction_id, json.dumps(entry))
        # Añadimos a la lista (al principio)
        await redis.lpush(REDIS_LKEY_ORDER, interaction_id)
        # Recortamos la lista a MAX_STORED_INTERACTIONS para no llenar la RAM
        await redis.ltrim(REDIS_LKEY_ORDER, 0, MAX_STORED_INTERACTIONS - 1)
        
        # Opcional: limpiar las viejas del HASH para no dejar basura infinita
        # Hacemos esto async: si la lista se truncó, habría IDs huerfanos, pero está bien por ahora.
    except Exception as exc:
        logger.warning("kapso_debug insert error: %s", exc)


async def _update_interaction(interaction_id: str, finish_data: dict[str, Any]) -> None:
    from app.core.redis_client import get_redis
    redis = await get_redis()
    if not redis:
        return

    try:
        # Buscar la interacción existente
        existing_json = await redis.hget(REDIS_HKEY_DATA, interaction_id)
        if not existing_json:
            return  # No se encontró, no actualizamos
        
        entry = json.loads(existing_json)
        entry.update(finish_data)

        if "status" in finish_data and finish_data["status"] in ("ok", "error"):
            now = datetime.now(timezone.utc)
            entry["finished_at"] = now.isoformat()
            try:
                started = datetime.fromisoformat(entry["started_at"])
                entry["duration_ms"] = round((now - started).total_seconds() * 1000, 1)
            except Exception:
                pass

        await redis.hset(REDIS_HKEY_DATA, interaction_id, json.dumps(entry))
    except Exception as exc:
        logger.warning("kapso_debug update error: %s", exc)


def start_interaction(interaction_id: str, data: dict[str, Any]) -> None:
    """Registra el inicio de una interacción (fire-and-forget)."""
    _fire(_insert_interaction(interaction_id, data))


def finish_interaction(interaction_id: str, finish_data: dict[str, Any]) -> None:
    """Actualiza la interacción con datos finales (fire-and-forget)."""
    _fire(_update_interaction(interaction_id, finish_data))


async def get_interactions(limit: int = 50, phone: str | None = None) -> list[dict[str, Any]]:
    """Obtiene las últimas interacciones desde Redis resolviendo la lista de IDs."""
    from app.core.redis_client import get_redis
    redis = await get_redis()
    if not redis:
        return []

    normalized_limit = max(1, min(limit, 100))
    try:
        # 1. Obtener la lista de los IDs más recientes
        ids = await redis.lrange(REDIS_LKEY_ORDER, 0, MAX_STORED_INTERACTIONS - 1)
        if not ids:
            return []

        # 2. Hacer Multi-Get (hmget) para traer el JSON de todos
        jsons = await redis.hmget(REDIS_HKEY_DATA, ids)
        
        # 3. Parsear y filtrar
        items = []
        for j in jsons:
            if j:
                items.append(json.loads(j))

        if phone:
            phone_lower = phone.lower()
            items = [
                i for i in items
                if phone_lower in (i.get("from_phone") or "").lower() 
                or phone_lower in (i.get("contact_name") or "").lower()
            ]

        return items[:normalized_limit]
    except Exception as exc:
        logger.warning("kapso_debug get_interactions error: %s", exc)
        return []


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
