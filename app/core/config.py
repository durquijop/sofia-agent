from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    OPENROUTER_API_KEY: str
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DEFAULT_MODEL: str = "x-ai/grok-4.1-fast"
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    DATABASE_URL: str | None = None
    DATABASE_PUBLIC_URL: str | None = None
    PGHOST: str | None = None
    PGPORT: str | None = "5432"
    PGUSER: str | None = None
    PGPASSWORD: str | None = None
    PGDATABASE: str | None = None
    KAPSO_INTERNAL_TOKEN: str | None = None
    APP_NAME: str = "URPE AI Lab - Multi-Agent System"
    DEBUG: bool = False

    def get_pg_dsn(self) -> str | None:
        if self.DATABASE_URL and "${{" not in self.DATABASE_URL:
            return self.DATABASE_URL
        if self.DATABASE_PUBLIC_URL and "${{" not in self.DATABASE_PUBLIC_URL:
            return self.DATABASE_PUBLIC_URL
        if self.PGHOST and self.PGUSER and self.PGPASSWORD and self.PGDATABASE:
            return f"postgresql://{self.PGUSER}:{self.PGPASSWORD}@{self.PGHOST}:{self.PGPORT}/{self.PGDATABASE}"
        return None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
