# URPE AI Lab - Sistema Multi-Agente

Sistema de inteligencia artificial multi-agente basado en **LangGraph** con soporte para **MCP servers** y **OpenRouter**.

## Estado actual del proyecto

- **Stack principal**: `LangGraph`
- **API**: `FastAPI`
- **Provider LLM**: `OpenRouter`
- **Herramientas dinámicas**: `MCP`

Los benchmarks comparativos con `Vercel AI SDK` se conservan solo como referencia histórica. El camino principal de desarrollo y producción de este repositorio está centrado en `LangGraph`.

## Contexto recomendado antes de desarrollar

Leer primero:

- **`docs/PROJECT_CONTEXT.md`**
- **`docs/architecture/OVERVIEW.md`**
- **`docs/API_ENDPOINTS.md`**
- **`docs/NEXT_STEPS.md`**
- **`app/agents/conversational.py`**

## Arquitectura

```text
FastAPI (Endpoint) → LangGraph (Agente) → OpenRouter (LLM)
                                        → MCP Servers (Herramientas dinámicas)
```

## Estructura del Proyecto

```text
├── main.py                         # Punto de entrada FastAPI
├── app/
│   ├── api/routes.py               # Endpoints REST
│   ├── agents/
│   │   └── conversational.py       # Agente principal LangGraph
│   ├── core/
│   │   ├── config.py               # Configuración central
│   │   └── cache.py                # Cache de respuestas con TTL
│   ├── mcp_client/
│   │   └── client.py               # Cliente MCP para herramientas dinámicas
│   └── schemas/chat.py             # Modelos Pydantic
├── docs/
│   ├── PROJECT_CONTEXT.md          # Fuente de verdad del proyecto
│   ├── API_ENDPOINTS.md            # Documentación de endpoints
│   └── BENCHMARK_REAL_FLOW_RESULTS.md
├── scripts/
│   ├── benchmark_parallel_langgraph.py
│   └── documented_real_flow_langgraph.py
├── benchmarks/                     # Benchmarks comparativos históricos
├── artifacts/                      # Resultados JSON generados por benchmarks
├── .env                            # Variables de entorno
├── requirements.txt                # Dependencias Python
└── package.json                    # Utilidades Node para benchmarks comparativos
```

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Editar `.env`:

```env
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DEFAULT_MODEL=x-ai/grok-4.1-fast
SUPABASE_URL=https://...
SUPABASE_SERVICE_KEY=...
```

## Ejecución

```bash
python main.py
```

La API estará disponible en `http://localhost:8080`

- Contexto del proyecto: `docs/PROJECT_CONTEXT.md`
- Arquitectura: `docs/architecture/OVERVIEW.md`
- Documentación Swagger: `http://localhost:8080/docs`
- Documentación detallada: `docs/API_ENDPOINTS.md`
- Próximos pasos técnicos: `docs/NEXT_STEPS.md`
- Benchmark real documentado: `docs/BENCHMARK_REAL_FLOW_RESULTS.md`

## Uso

### Chat simple

```json
POST /api/v1/chat
{
    "system_prompt": "Eres un asistente de ventas de la empresa X...",
    "message": "¿Cuáles son los productos disponibles?",
    "max_tokens": 512
}
```

### Chat con MCP servers (herramientas dinámicas)

```json
POST /api/v1/chat
{
    "system_prompt": "Eres el asistente de Marketia...",
    "message": "Busca los clientes activos",
    "mcp_servers": [
        {
            "url": "https://marketia.app.n8n.cloud/mcp/aa0f6b46-...",
            "name": "marketia-crm"
        }
    ]
}
```

## Optimizaciones

- **Cache de respuestas** con TTL de 5 min (0.5ms en cache hit vs ~4s)
- **Connection pooling HTTP** con keep-alive para reutilizar conexiones
- **Carga paralela de MCP tools** via `asyncio.gather`
- **Cache de instancias LLM** por modelo+params
- **Timing metrics** en cada response para monitoreo

## Benchmarks disponibles

- **LangGraph principal**
  - `python scripts/benchmark_parallel_langgraph.py`
  - `python scripts/documented_real_flow_langgraph.py`

- **Comparativos históricos con Vercel AI SDK**
  - ver `package.json`
  - se mantienen solo para comparación y documentación
