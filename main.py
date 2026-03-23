import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.kapso_routes import router as kapso_router
from app.api.routes import router
from app.api.db_routes import router as db_router
from app.api.funnel_routes import router as funnel_router
from app.core.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Iniciando %s", settings.APP_NAME)
    yield
    # Shutdown
    logger.info("Servidor apagado limpiamente")


app = FastAPI(
    title=settings.APP_NAME,
    description="Sistema multi-agente basado en LangGraph con soporte MCP para múltiples empresas. Incluye: Agente Conversacional + Agente de Embudo",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(kapso_router)
app.include_router(db_router)
app.include_router(funnel_router)


@app.get("/")
async def root():
    return {
        "service": settings.APP_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": {
            "chat": "/api/v1/chat",
            "funnel_analyze": "/api/v1/funnel/analyze",
            "kapso_inbound": "/api/v1/kapso/inbound",
            "health": "/api/v1/health",
            "db_health": "/api/v1/db/health",
            "db_docs": "/docs#/database",
        },
    }


if __name__ == "__main__":
    logger.info(f"Iniciando {settings.APP_NAME}")
    logger.info(f"Modelo default: {settings.DEFAULT_MODEL}")
    python_service_port = int(os.getenv("PYTHON_SERVICE_PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=python_service_port, reload=settings.DEBUG)
