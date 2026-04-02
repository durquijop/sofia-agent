"""
Kapso Debug — Eventos en memoria + persistencia Supabase (realtime)

Mantiene una lista circular en memoria para respuesta instantánea,
y persiste cada evento a la tabla ``debug_events`` de Supabase de forma
asíncrona (fire-and-forget) para historial permanente y suscripción realtime.

También mantiene un mecanismo de SSE (Server-Sent Events) para streaming
en tiempo real hacia dashboards conectados.
"""
import asyncio
import json
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

# ─── SSE subscribers ─────────────────────────────────────────────────────
_sse_subscribers: set[asyncio.Queue] = set()
_sse_lock = Lock()


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
                "enterprise_id": payload.get("enterprise_id"),
                "person_id": payload.get("person_id"),
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

    # Notificar a subscribers SSE en tiempo real
    _broadcast_sse(entry)

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


# ─── SSE helpers ─────────────────────────────────────────────────────────

def _broadcast_sse(entry: dict[str, Any]) -> None:
    """Push event to all SSE subscriber queues (non-blocking)."""
    with _sse_lock:
        dead: list[asyncio.Queue] = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _sse_subscribers.discard(q)


def subscribe_sse() -> asyncio.Queue:
    """Register a new SSE subscriber. Returns a Queue to await events from."""
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    with _sse_lock:
        _sse_subscribers.add(q)
    return q


def unsubscribe_sse(q: asyncio.Queue) -> None:
    """Remove an SSE subscriber."""
    with _sse_lock:
        _sse_subscribers.discard(q)
