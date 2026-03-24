import logging
import traceback
from datetime import datetime, timezone

import httpx
from fastapi import Request

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10)
    return _http_client


async def send_error_to_webhook(
    exc: Exception,
    *,
    request: Request | None = None,
    context: str | None = None,
    severity: str = "error",
    fallback: str | None = None,
) -> None:
    """Fire-and-forget: send error details to the configured n8n webhook.

    Parameters
    ----------
    exc : Exception
        The exception that occurred.
    request : Request | None
        The incoming HTTP request (if available).
    context : str | None
        Short label identifying WHERE the error happened.
    severity : str
        One of "critical", "error", "warning", "info".
        - critical: the request failed completely with no recovery.
        - error: something broke but a plan B kicked in.
        - warning: degraded operation, non-essential feature unavailable.
        - info: informational, nothing visible to the end user.
    fallback : str | None
        Description of what plan B was executed so the reader knows
        the system is still running and what the user actually received.
    """
    settings = get_settings()
    url = settings.ERROR_WEBHOOK_URL
    if not url:
        return

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": settings.APP_NAME,
        "severity": severity,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": traceback.format_exception(type(exc), exc, exc.__traceback__),
        "context": context,
        "fallback": fallback,
        "status": "🟢 Sistema operativo" if fallback else "🔴 Sin plan B — revisar",
    }

    if request:
        payload["request"] = {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "client_ip": request.client.host if request.client else None,
            "headers": {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in ("authorization", "cookie", "x-api-key")
            },
        }

    try:
        client = _get_client()
        resp = await client.post(url, json=payload)
        logger.info("Error webhook sent (%s)", resp.status_code)
    except Exception as wh_exc:
        logger.warning("Failed to send error webhook: %s", wh_exc)
