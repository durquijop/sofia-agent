from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

_MAX_KAPSO_DEBUG_EVENTS = 200
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_KAPSO_DEBUG_EVENTS)
_lock = Lock()

_MAX_INTERACTIONS = 100
_interactions: deque[dict[str, Any]] = deque(maxlen=_MAX_INTERACTIONS)
_interactions_lock = Lock()


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


def start_interaction(interaction_id: str, data: dict[str, Any]) -> None:
    """Registra el inicio de una interacción entrante con los datos conocidos hasta ese momento."""
    entry: dict[str, Any] = {
        "id": interaction_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "duration_ms": None,
        "status": "processing",
        "error": None,
        "timing": None,
        "tools_used": [],
        "reaction_emoji": None,
        "reply_type": None,
        "response_chars": None,
        "response_preview": None,
        "model_used": None,
        "agent_id": None,
        "agent_name": None,
        "mcp_servers": [],
        "memory_session_id": None,
    }
    entry.update(data)
    with _interactions_lock:
        _interactions.appendleft(entry)


def finish_interaction(interaction_id: str, finish_data: dict[str, Any]) -> None:
    """Actualiza la interacción con los datos finales una vez que el agente termina."""
    now = datetime.now(timezone.utc)
    with _interactions_lock:
        for interaction in _interactions:
            if interaction["id"] == interaction_id:
                interaction["finished_at"] = now.isoformat()
                try:
                    started = datetime.fromisoformat(interaction["started_at"])
                    interaction["duration_ms"] = round((now - started).total_seconds() * 1000, 1)
                except Exception:
                    pass
                interaction.update(finish_data)
                break


def get_interactions(limit: int = 50, phone: str | None = None) -> list[dict[str, Any]]:
    normalized_limit = max(1, min(limit, _MAX_INTERACTIONS))
    with _interactions_lock:
        items = list(_interactions)
    if phone:
        items = [
            i for i in items
            if phone in (i.get("from_phone") or "") or phone.lower() in (i.get("contact_name") or "").lower()
        ]
    return items[:normalized_limit]


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
