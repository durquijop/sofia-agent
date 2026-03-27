import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.kapso_routes import router as kapso_router, retry_stuck_messages
from app.api.routes import router
from app.api.db_routes import router as db_router
from app.api.debug_dashboard import router as debug_dashboard_router
from app.api.funnel_routes import router as funnel_router
from app.api.graph_routes import router as graph_router
from app.api.scheduling_routes import router as scheduling_router
from app.core.config import get_settings
from app.core.error_webhook import send_error_to_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

settings = get_settings()

RETRY_STUCK_INTERVAL_SECONDS = 10 * 60  # 10 minutes


async def _retry_stuck_loop():
    """Background loop that checks for stuck messages every 10 minutes."""
    await asyncio.sleep(60)  # Initial delay: wait 1 min after startup
    while True:
        try:
            result = await retry_stuck_messages()
            if result.get("stuck_found", 0) > 0:
                logger.info("retry_stuck_loop: %s", result)
        except Exception as exc:
            logger.error("retry_stuck_loop error: %s", exc, exc_info=True)
        await asyncio.sleep(RETRY_STUCK_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Iniciando %s", settings.APP_NAME)
    task = asyncio.create_task(_retry_stuck_loop())
    logger.info("Background task: retry_stuck_loop iniciado (cada %ds)", RETRY_STUCK_INTERVAL_SECONDS)
    yield
    # Shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
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
    python_service_host = settings.PYTHON_SERVICE_HOST or ("0.0.0.0" if settings.DEBUG else "127.0.0.1")
    if not settings.DEBUG and not settings.KAPSO_INTERNAL_TOKEN:
        logger.warning("KAPSO_INTERNAL_TOKEN no está configurado; los endpoints internos quedan menos protegidos")
    uvicorn.run("main:app", host=python_service_host, port=python_service_port, reload=settings.DEBUG)
