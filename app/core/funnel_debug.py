"""
Funnel Debug — Trazas de ejecución del agente de embudo en memoria

Mantiene un registro circular de las últimas ejecuciones del agente de embudo
para visualización en el dashboard de debug.
"""
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


def add_funnel_debug_run(
    contacto_id: int,
    empresa_id: int,
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
        "contacto_id": contacto_id,
        "empresa_id": empresa_id,
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
    logger.debug(f"Funnel debug event added: contacto={contacto_id}, success={success}")


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
