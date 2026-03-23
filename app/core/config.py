from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    OPENROUTER_API_KEY: str
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL: str = "x-ai/grok-4.1-fast"
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    SUPABASE_EDGE_FUNCTION_URL: str = "https://vecspltvmyopwbjzerow.supabase.co/functions/v1"
    SUPABASE_EDGE_FUNCTION_TOKEN: str | None = None
    KAPSO_INTERNAL_TOKEN: str | None = None
    KAPSO_API_KEY: str | None = None
    KAPSO_WEBHOOK_SECRET: str | None = None
    KAPSO_BRIDGE_PORT: int | None = None
    KAPSO_BASE_URL: str | None = None
    INTERNAL_AGENT_API_URL: str | None = None
    APP_NAME: str = "URPE AI Lab - Multi-Agent System"
    DEBUG: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
