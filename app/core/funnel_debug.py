"""
Funnel Debug — Trazas de ejecución del agente de embudo
en memoria + persistencia Supabase (realtime)

Mantiene un registro circular en memoria para dashboards instantáneos
y persiste cada ejecución a ``debug_events`` para historial permanente.
"""
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ─── Trazas en memoria ───────────────────────────────────────────────────

_MAX_FUNNEL_DEBUG_RUNS = 50
_runs: deque[dict[str, Any]] = deque(maxlen=_MAX_FUNNEL_DEBUG_RUNS)
_lock = Lock()


async def _persist_funnel_event(entry: dict[str, Any]) -> None:
    """Inserta la corrida de funnel en Supabase de forma silenciosa."""
    try:
        from app.db.client import get_supabase

        db = await get_supabase()
        await db.insert(
            "debug_events",
            {
                "source": "funnel",
                "stage": "funnel_completed" if entry.get("success") else "funnel_error",
                "payload": {
                    "success": entry.get("success"),
                    "error": entry.get("error"),
                    "respuesta": entry.get("respuesta"),
                    "etapa_anterior": entry.get("etapa_anterior"),
                    "etapa_nueva": entry.get("etapa_nueva"),
                    "timing": entry.get("timing"),
                    "tools_used": entry.get("tools_used"),
                    "agent_runs": entry.get("agent_runs"),
                },
                "enterprise_id": entry.get("enterprise_id"),
                "person_id": entry.get("person_id"),
            },
        )
    except Exception as exc:
        logger.debug("debug_events persist failed (funnel): %s", exc)


def add_funnel_debug_run(
    person_id: int,
    enterprise_id: int,
    agent_runs: list[dict] | None = None,
    timing: dict | None = None,
    tools_used: list[dict] | None = None,
    success: bool = True,
    error: str | None = None,
    respuesta: str | None = None,
    etapa_anterior: str | None = None,
    etapa_nueva: int | None = None,
) -> None:
    """Registra una ejecución del agente de embudo en el debug."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "person_id": person_id,
        "enterprise_id": enterprise_id,
        "success": success,
        "error": error,
        "respuesta": respuesta,
        "etapa_anterior": etapa_anterior,
        "etapa_nueva": etapa_nueva,
        "agent_runs": agent_runs or [],
        "timing": timing or {},
        "tools_used": tools_used or [],
    }
    with _lock:
        _runs.appendleft(entry)
    logger.debug(f"Funnel debug event added: person={person_id}, success={success}")

    # Persistir a Supabase async fire-and-forget
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_persist_funnel_event(entry))
    except RuntimeError:
        pass  # No event loop running — skip persistence


def get_funnel_debug_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Retorna las últimas N ejecuciones del funnel agent."""
    normalized_limit = max(1, min(limit, _MAX_FUNNEL_DEBUG_RUNS))
    with _lock:
        return list(reversed(list(_runs)))[:normalized_limit]


def get_funnel_debug_stats() -> dict[str, Any]:
    """Retorna estadísticas de ejecución del funnel agent."""
    with _lock:
        if not _runs:
            return {
                "total_runs": 0,
                "successful": 0,
                "failed": 0,
                "avg_duration_ms": 0,
            }
        
        successful = sum(1 for r in _runs if r["success"])
        failed = len(_runs) - successful
        avg_duration = sum(r.get("timing", {}).get("total_ms", 0) for r in _runs) / len(_runs) if _runs else 0
        
        return {
            "total_runs": len(_runs),
            "successful": successful,
            "failed": failed,
            "avg_duration_ms": round(avg_duration, 1),
            "max_capacity": _MAX_FUNNEL_DEBUG_RUNS,
        }


def clear_funnel_debug_runs() -> None:
    """Limpia el historial de debug."""
    with _lock:
        _runs.clear()
    logger.info("Funnel debug runs cleared")
