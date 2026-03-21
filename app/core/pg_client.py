"""Pool de conexiones asyncpg para el Postgres de Railway."""
import logging
import asyncpg
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pg_pool() -> asyncpg.Pool | None:
    """Retorna el pool asyncpg singleton. Retorna None si DATABASE_URL no está configurada."""
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    if not settings.DATABASE_URL:
        logger.warning("DATABASE_URL no configurada — debug interactions deshabilitado en Postgres")
        return None

    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
        logger.info("asyncpg pool creado OK (Railway Postgres)")
    except Exception as exc:
        logger.error("No se pudo crear asyncpg pool: %s", exc)
        _pool = None

    return _pool


async def ensure_debug_table() -> None:
    """Crea la tabla kapso_debug_interactions si no existe. Llamado al arrancar el server."""
    pool = await get_pg_pool()
    if pool is None:
        return

    ddl = """
    CREATE TABLE IF NOT EXISTS kapso_debug_interactions (
        id                TEXT PRIMARY KEY,
        started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at       TIMESTAMPTZ,
        duration_ms       NUMERIC,
        status            TEXT NOT NULL DEFAULT 'processing',
        error             TEXT,
        from_phone        TEXT,
        contact_name      TEXT,
        message_id        TEXT,
        message_type      TEXT,
        message_text      TEXT,
        phone_number_id   TEXT,
        agent_id          INTEGER,
        agent_name        TEXT,
        model_used        TEXT,
        mcp_servers       JSONB,
        memory_session_id TEXT,
        timing            JSONB,
        tools_used        JSONB,
        reaction_emoji    TEXT,
        reply_type        TEXT,
        response_chars    INTEGER,
        response_preview  TEXT
    );
    CREATE INDEX IF NOT EXISTS kapso_debug_interactions_started_idx
        ON kapso_debug_interactions(started_at DESC);
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(ddl)
        logger.info("kapso_debug_interactions tabla lista")
    except Exception as exc:
        logger.error("Error creando tabla debug: %s", exc)


async def close_pg_pool() -> None:
    """Cierra el pool al apagar el servidor."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
