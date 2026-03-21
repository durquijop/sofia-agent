from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    OPENROUTER_API_KEY: str
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL: str = "x-ai/grok-4.1-fast"
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    REDIS_URL: str | None = None
    REDIS_PUBLIC_URL: str | None = None
    KAPSO_INTERNAL_TOKEN: str | None = None
    APP_NAME: str = "URPE AI Lab - Multi-Agent System"
    DEBUG: bool = False

    def get_redis_url(self) -> str | None:
        if self.REDIS_URL and "${{" not in self.REDIS_URL:
            return self.REDIS_URL
        if self.REDIS_PUBLIC_URL and "${{" not in self.REDIS_PUBLIC_URL:
            return self.REDIS_PUBLIC_URL
        return None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
