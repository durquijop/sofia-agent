from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

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


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
