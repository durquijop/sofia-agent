"""Cliente Supabase REST directo con httpx.AsyncClient + connection pooling HTTP/2."""
import logging
from typing import Any
import httpx
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: "SupabaseClient | None" = None


class SupabaseClient:
    """Cliente ligero sobre PostgREST con httpx pooled."""

    def __init__(self, url: str, service_key: str):
        self.base_url = f"{url}/rest/v1"
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=15,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        logger.info("SupabaseClient inicializado (httpx pooled, HTTP/2)")

    async def query(
        self,
        table: str,
        select: str = "*",
        filters: dict[str, Any] | None = None,
        order: str | None = None,
        order_desc: bool = False,
        limit: int | None = None,
        single: bool = False,
        count: bool = False,
        raw_filters: dict[str, str] | None = None,
    ) -> dict | list[dict] | None:
        """Ejecuta un SELECT contra PostgREST.

        raw_filters allows PostgREST operators directly, e.g.
        {"status": "in.(buffer,procesando)", "timestamp": "lt.2024-01-01T00:00:00"}
        """
        params: dict[str, str] = {"select": select}
        if filters:
            for key, val in filters.items():
                if isinstance(val, bool):
                    params[key] = f"eq.{str(val).lower()}"
                else:
                    params[key] = f"eq.{val}"
        if raw_filters:
            for key, val in raw_filters.items():
                params[key] = val
        if order:
            params["order"] = f"{order}.{'desc' if order_desc else 'asc'}"
        if limit:
            params["limit"] = str(limit)

        headers = {}
        if single:
            headers["Accept"] = "application/vnd.pgrst.object+json"
        if count:
            headers["Prefer"] = "count=exact"

        r = await self._http.get(f"/{table}", params=params, headers=headers)

        if r.status_code == 406 and single:
            return None
        r.raise_for_status()

        if count:
            content_range = r.headers.get("content-range", "")
            total = content_range.split("/")[1] if "/" in content_range else "0"
            return {"data": r.json(), "count": int(total) if total != "*" else 0}

        return r.json()

    async def insert(self, table: str, data: dict[str, Any]) -> dict:
        """Inserta un registro."""
        r = await self._http.post(f"/{table}", json=data)
        if r.status_code >= 400:
            logger.error("INSERT %s FAILED (%s): %s | payload keys: %s", table, r.status_code, r.text[:500], list(data.keys()))
        r.raise_for_status()
        result = r.json()
        return result[0] if isinstance(result, list) and result else result

    async def update(self, table: str, filters: dict[str, Any], data: dict[str, Any]) -> list[dict]:
        """Actualiza registros que cumplan los filtros."""
        params = {k: f"eq.{v}" for k, v in filters.items()}
        r = await self._http.patch(f"/{table}", params=params, json=data)
        r.raise_for_status()
        return r.json()

    async def delete(self, table: str, filters: dict[str, Any]) -> list[dict]:
        """Elimina registros que cumplan los filtros."""
        params = {k: f"eq.{v}" for k, v in filters.items()}
        r = await self._http.delete(f"/{table}", params=params)
        r.raise_for_status()
        return r.json() if r.content else []

    async def rpc(self, function_name: str, params: dict[str, Any] | None = None) -> Any:
        """Llama a una función RPC de Supabase."""
        r = await self._http.post(f"/rpc/{function_name}", json=params or {})
        r.raise_for_status()
        return r.json()

    async def close(self):
        """Cierra el cliente HTTP."""
        await self._http.aclose()


async def get_supabase() -> SupabaseClient:
    """Retorna el cliente Supabase singleton."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = SupabaseClient(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _client
