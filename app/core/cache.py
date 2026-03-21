import hashlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ResponseCache:
    """Cache en memoria para respuestas del LLM con TTL configurable."""

    def __init__(self, default_ttl_seconds: int = 300):
        self._cache: dict[str, dict[str, Any]] = {}
        self._default_ttl = default_ttl_seconds

    def _make_key(self, system_prompt: str, message: str, model: str) -> str:
        raw = f"{system_prompt}|{message}|{model}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, system_prompt: str, message: str, model: str) -> str | None:
        key = self._make_key(system_prompt, message, model)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            del self._cache[key]
            return None
        logger.info(f"Cache HIT - key: {key[:12]}...")
        return entry["response"]

    def set(self, system_prompt: str, message: str, model: str, response: str, ttl: int | None = None):
        key = self._make_key(system_prompt, message, model)
        self._cache[key] = {
            "response": response,
            "expires_at": time.time() + (ttl or self._default_ttl),
        }
        logger.info(f"Cache SET - key: {key[:12]}... ttl: {ttl or self._default_ttl}s")

    def clear(self):
        self._cache.clear()
        logger.info("Cache cleared")

    @property
    def size(self) -> int:
        return len(self._cache)


response_cache = ResponseCache(default_ttl_seconds=300)
