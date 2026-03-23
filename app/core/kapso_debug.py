"""
Kapso Debug — Eventos en memoria + persistencia Supabase (realtime)

Mantiene una lista circular en memoria para respuesta instantánea,
y persiste cada evento a la tabla ``debug_events`` de Supabase de forma
asíncrona (fire-and-forget) para historial permanente y suscripción realtime.
"""
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ─── Eventos en memoria ──────────────────────────────────────────────────

_MAX_KAPSO_DEBUG_EVENTS = 200
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_KAPSO_DEBUG_EVENTS)
_lock = Lock()


async def _persist_debug_event(entry: dict[str, Any]) -> None:
    """Inserta el evento en Supabase de forma silenciosa (fire-and-forget)."""
    try:
        from app.db.client import get_supabase

        db = await get_supabase()
        payload = entry.get("payload") or {}
        await db.insert(
            "debug_events",
            {
                "source": entry.get("source", "kapso"),
                "stage": entry["stage"],
                "payload": payload,
                "empresa_id": payload.get("empresa_id"),
                "contacto_id": payload.get("contacto_id"),
                "message_id": payload.get("message_id"),
            },
        )
    except Exception as exc:
        logger.debug("debug_events persist failed (kapso): %s", exc)


def add_kapso_debug_event(source: str, stage: str, payload: dict[str, Any] | None = None) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "stage": stage,
        "payload": payload or {},
    }
    with _lock:
        _events.appendleft(entry)

    # Persistir a Supabase async fire-and-forget
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_debug_event(entry))
    except RuntimeError:
        pass  # No event loop running — skip persistence


def get_kapso_debug_events(limit: int = 100) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(limit, _MAX_KAPSO_DEBUG_EVENTS))
    with _lock:
        return list(_events)[:normalized_limit]


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
