"""
Kapso Debug — persistencia en Postgres de Railway (asyncpg).

- start_interaction / finish_interaction: fire-and-forget (no bloquean el flujo principal).
- get_interactions: query async a Railway Postgres.
- Los eventos en memoria (deque) se mantienen para el panel de eventos del bridge.
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


# ─── Interacciones en Postgres ────────────────────────────────────────────────

def _dumps(value: Any) -> str | None:
    """Serializa un valor a JSON string para Postgres JSONB."""
    if value is None:
        return None
    return json.dumps(value)


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
    from app.core.pg_client import get_pg_pool
    pool = await get_pg_pool()
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO kapso_debug_interactions
                  (id, from_phone, contact_name, message_id, message_type,
                   message_text, phone_number_id, status, started_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'processing', NOW())
                ON CONFLICT (id) DO NOTHING
                """,
                interaction_id,
                data.get("from_phone"),
                data.get("contact_name"),
                data.get("message_id"),
                data.get("message_type"),
                data.get("message_text"),
                data.get("phone_number_id"),
            )
    except Exception as exc:
        logger.warning("kapso_debug insert error: %s", exc)


async def _update_interaction(interaction_id: str, finish_data: dict[str, Any]) -> None:
    from app.core.pg_client import get_pg_pool
    pool = await get_pg_pool()
    if pool is None:
        return

    # Construir SET dinámico solo con campos que vienen en finish_data
    field_map = {
        "status": "status",
        "error": "error",
        "agent_id": "agent_id",
        "agent_name": "agent_name",
        "model_used": "model_used",
        "memory_session_id": "memory_session_id",
        "reaction_emoji": "reaction_emoji",
        "reply_type": "reply_type",
        "response_chars": "response_chars",
        "response_preview": "response_preview",
    }
    json_fields = {"mcp_servers", "timing", "tools_used"}

    sets = []
    values: list[Any] = []
    idx = 1

    for key, col in field_map.items():
        if key in finish_data:
            sets.append(f"{col} = ${idx}")
            values.append(finish_data[key])
            idx += 1

    for key in json_fields:
        if key in finish_data:
            sets.append(f"{key} = ${idx}::jsonb")
            values.append(_dumps(finish_data[key]))
            idx += 1

    if "status" in finish_data and finish_data["status"] in ("ok", "error"):
        sets.append(f"finished_at = NOW()")
        sets.append(
            f"duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000"
        )

    if not sets:
        return

    values.append(interaction_id)
    sql = f"UPDATE kapso_debug_interactions SET {', '.join(sets)} WHERE id = ${idx}"

    try:
        async with pool.acquire() as conn:
            await conn.execute(sql, *values)
    except Exception as exc:
        logger.warning("kapso_debug update error: %s", exc)


def start_interaction(interaction_id: str, data: dict[str, Any]) -> None:
    """Registra el inicio de una interacción (fire-and-forget)."""
    _fire(_insert_interaction(interaction_id, data))


def finish_interaction(interaction_id: str, finish_data: dict[str, Any]) -> None:
    """Actualiza la interacción con datos finales (fire-and-forget)."""
    _fire(_update_interaction(interaction_id, finish_data))


async def get_interactions(limit: int = 50, phone: str | None = None) -> list[dict[str, Any]]:
    """Obtiene las últimas interacciones desde Postgres."""
    from app.core.pg_client import get_pg_pool
    pool = await get_pg_pool()
    if pool is None:
        return []

    normalized_limit = max(1, min(limit, 100))
    try:
        async with pool.acquire() as conn:
            if phone:
                rows = await conn.fetch(
                    """
                    SELECT * FROM kapso_debug_interactions
                    WHERE from_phone ILIKE $1 OR contact_name ILIKE $1
                    ORDER BY started_at DESC LIMIT $2
                    """,
                    f"%{phone}%",
                    normalized_limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM kapso_debug_interactions
                    ORDER BY started_at DESC LIMIT $1
                    """,
                    normalized_limit,
                )
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("kapso_debug get_interactions error: %s", exc)
        return []


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
