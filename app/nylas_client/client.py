"""Cliente Nylas API v3 con httpx async — equivalente a sdk-vercel-test-master/src/lib/nylas.js"""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: "NylasClient | None" = None
_client2: "NylasClient | None" = None


class NylasClient:
    """Cliente async ligero sobre Nylas API v3."""

    def __init__(self, api_key: str, api_url: str = "https://api.us.nylas.com"):
        self.api_url = api_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=f"{self.api_url}/v3",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        logger.info("NylasClient inicializado (%s)", self.api_url)

    # ── Calendars ──────────────────────────────────────────────

    async def get_free_busy(
        self, grant_id: str, email: str, start_time: int, end_time: int
    ) -> list[dict[str, Any]]:
        """Obtiene free/busy de un email en un rango de tiempo."""
        r = await self._http.post(
            f"/grants/{grant_id}/calendars/free-busy",
            json={"start_time": start_time, "end_time": end_time, "emails": [email]},
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def list_calendars(self, grant_id: str) -> list[dict[str, Any]]:
        """Lista los calendarios de un grant."""
        r = await self._http.get(f"/grants/{grant_id}/calendars")
        r.raise_for_status()
        return r.json().get("data", [])

    async def list_events(
        self, grant_id: str, calendar_id: str, start_time: int, end_time: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Lista eventos de un calendario en un rango."""
        r = await self._http.get(
            f"/grants/{grant_id}/events",
            params={
                "calendar_id": calendar_id,
                "start": str(start_time),
                "end": str(end_time),
                "limit": str(limit),
            },
        )
        r.raise_for_status()
        return r.json().get("data", [])

    # ── Events CRUD ────────────────────────────────────────────

    async def create_event(
        self, grant_id: str, calendar_id: str, event_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Crea un evento en el calendario."""
        body = self._build_event_body(event_data)
        r = await self._http.post(
            f"/grants/{grant_id}/events",
            params={"calendar_id": calendar_id},
            json=body,
        )
        r.raise_for_status()
        return r.json().get("data", r.json())

    async def update_event(
        self, grant_id: str, calendar_id: str, event_id: str, event_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Actualiza un evento existente."""
        body = self._build_event_body(event_data, partial=True)
        r = await self._http.put(
            f"/grants/{grant_id}/events/{event_id}",
            params={"calendar_id": calendar_id},
            json=body,
        )
        r.raise_for_status()
        return r.json().get("data", r.json())

    async def delete_event(self, grant_id: str, calendar_id: str, event_id: str) -> bool:
        """Elimina un evento."""
        r = await self._http.delete(
            f"/grants/{grant_id}/events/{event_id}",
            params={"calendar_id": calendar_id},
        )
        r.raise_for_status()
        return True

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _build_event_body(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        """Construye el body para create/update de evento en formato Nylas v3."""
        body: dict[str, Any] = {}

        if "title" in data:
            body["title"] = data["title"]
        if "description" in data:
            body["description"] = data["description"]

        if "when" in data:
            when: dict[str, Any] = {
                "start_time": data["when"]["start_time"],
                "end_time": data["when"]["end_time"],
            }
            if data["when"].get("start_timezone"):
                when["start_timezone"] = data["when"]["start_timezone"]
                when["end_timezone"] = data["when"].get("end_timezone", data["when"]["start_timezone"])
            body["when"] = when

        if "participants" in data:
            body["participants"] = data["participants"]
        if "location" in data:
            body["location"] = data["location"]
        if "conferencing" in data:
            body["conferencing"] = data["conferencing"]
        if "reminders" in data:
            body["reminders"] = data["reminders"]

        return body

    async def close(self):
        await self._http.aclose()


async def get_nylas() -> "NylasClient":
    """Retorna el cliente Nylas principal (singleton)."""
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.NYLAS_API_KEY:
            raise RuntimeError("NYLAS_API_KEY no configurada")
        _client = NylasClient(settings.NYLAS_API_KEY, settings.NYLAS_API_URL)
    return _client


async def get_nylas2() -> "NylasClient | None":
    """Retorna el segundo cliente Nylas (si existe)."""
    global _client2
    if _client2 is None:
        settings = get_settings()
        if not settings.NYLAS_API_KEY_2:
            return None
        _client2 = NylasClient(settings.NYLAS_API_KEY_2, settings.NYLAS_API_URL)
    return _client2
