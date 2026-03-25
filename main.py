import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


class _MaxLevelFilter(logging.Filter):
    def __init__(self, exclusive_upper_bound: int):
        super().__init__()
        self.exclusive_upper_bound = exclusive_upper_bound

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.exclusive_upper_bound


def _configure_logging() -> None:
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.addFilter(_MaxLevelFilter(logging.ERROR))
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(stderr_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        logger_instance = logging.getLogger(logger_name)
        logger_instance.handlers.clear()
        logger_instance.propagate = True
        logger_instance.setLevel(logging.INFO)

from app.api.kapso_routes import router as kapso_router
from app.api.routes import router
from app.api.db_routes import router as db_router
from app.api.debug_dashboard import router as debug_dashboard_router
from app.api.funnel_routes import router as funnel_router
from app.api.graph_routes import router as graph_router
from app.api.scheduling_routes import router as scheduling_router
from app.core.config import get_settings
from app.core.error_webhook import send_error_to_webhook

_configure_logging()
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


@app.middleware("http")
async def error_webhook_middleware(request: Request, call_next):
    """Intercept any response with status >= 500 and notify the webhook."""
    try:
        response = await call_next(request)
    except Exception as exc:
        # Unhandled exception that escaped everything
        logger.error("Middleware caught unhandled error on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
        await send_error_to_webhook(
            exc, request=request,
            context="middleware_unhandled",
            severity="critical",
            fallback="Se devolvió HTTP 500 genérico al cliente. La petición falló pero el servidor sigue activo.",
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    if response.status_code >= 500:
        # The route handler already returned a 500 (e.g. via HTTPException)
        await send_error_to_webhook(
            RuntimeError(f"HTTP {response.status_code} on {request.method} {request.url.path}"),
            request=request,
            context="http_500_response",
            severity="error",
            fallback="El endpoint devolvió error 500 al cliente. El error fue capturado internamente por el handler del endpoint — el servidor sigue funcionando normal.",
        )

    return response

app.include_router(router)
app.include_router(kapso_router)
app.include_router(db_router)
app.include_router(debug_dashboard_router)
app.include_router(funnel_router)
app.include_router(graph_router)
app.include_router(scheduling_router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    await send_error_to_webhook(
        exc, request=request,
        context="global_exception_handler",
        severity="critical",
        fallback="Excepción no capturada por ningún handler. Se devolvió HTTP 500. El servidor sigue activo, solo esta petición falló.",
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


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
            "scheduling_disponibilidad": "/api/v1/scheduling/disponibilidad",
            "scheduling_crear_evento": "/api/v1/scheduling/crear-evento",
            "scheduling_reagendar_evento": "/api/v1/scheduling/reagendar-evento",
            "scheduling_eliminar_evento": "/api/v1/scheduling/eliminar-evento",
            "health": "/api/v1/health",
            "db_health": "/api/v1/db/health",
            "db_docs": "/docs#/database",
        },
    }


if __name__ == "__main__":
    logger.info(f"Iniciando {settings.APP_NAME}")
    logger.info(f"Modelo default: {settings.DEFAULT_MODEL}")
    python_service_port = int(os.getenv("PYTHON_SERVICE_PORT", "8080"))
    uvicorn_target = "main:app" if settings.DEBUG else app
    uvicorn.run(uvicorn_target, host="0.0.0.0", port=python_service_port, reload=settings.DEBUG, log_config=None)
