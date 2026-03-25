# URPE AI Lab - Sistema Multi-Agente

Proyecto productivo basado en **FastAPI + LangGraph + OpenRouter + Supabase**, con un **bridge Node/Kapso** para exponer el webhook público de WhatsApp y proxyear rutas operativas de scheduling.

## Estado actual

- **Runtime principal de negocio**: `Python + FastAPI + LangGraph`
- **Capa pública en Railway**: `kapso-bridge/server.mjs`
- **Provider LLM**: `OpenRouter`
- **Herramientas dinámicas**: `MCP`
- **Scheduling**: `Nylas + Supabase`

Los benchmarks con `Vercel AI SDK` se conservan solo como referencia histórica. El flujo productivo vigente vive en Python, pero **Node sí forma parte del runtime de producción** por el bridge de Kapso.

## Lectura recomendada antes de tocar código

- **`docs/PROJECT_CONTEXT.md`**
- **`docs/architecture/OVERVIEW.md`**
- **`docs/API_ENDPOINTS.md`**
- **`docs/RAILWAY_KAPSO_DEPLOY.md`**
- **`docs/NEXT_STEPS.md`**

## Arquitectura actual

```text
Kapso / n8n / clientes HTTP
  -> kapso-bridge/server.mjs
  -> FastAPI (main.py)
  -> agentes LangGraph / rutas auxiliares / scheduling
  -> OpenRouter / Supabase / MCP / Nylas
```

## Componentes principales

```text
├── main.py                          # Entrada FastAPI y registro de routers
├── kapso-bridge/server.mjs          # Bridge público para Kapso + proxy de scheduling
├── railway-start.sh                 # Arranque combinado FastAPI + bridge en Railway
├── app/
│   ├── api/
│   │   ├── routes.py                # Chat, health y cache
│   │   ├── kapso_routes.py          # Inbound Kapso y debug operativo
│   │   ├── scheduling_routes.py     # Disponibilidad y CRUD de citas con Nylas
│   │   ├── funnel_routes.py         # Agente de embudo
│   │   ├── graph_routes.py          # Introspección del grafo
│   │   ├── db_routes.py             # Consultas utilitarias a Supabase
│   │   └── debug_dashboard.py       # Dashboard HTML de debug Kapso
│   ├── agents/                      # Agentes LangGraph y flujos relacionados
│   ├── db/                          # Cliente y queries de Supabase
│   ├── mcp_client/                  # Cliente MCP
│   ├── nylas_client/                # Cliente Nylas
│   ├── core/                        # Configuración, prompt y soporte operativo
│   └── schemas/                     # Schemas Pydantic
├── docs/                            # Documentación operativa vigente
├── benchmarks/                      # Benchmarks históricos / research
├── scripts/                         # Utilidades y pruebas manuales
├── requirements.txt                 # Dependencias Python
└── package.json                     # Dependencias Node del bridge y benchmarks
```

## Instalación

### Python

```bash
pip install -r requirements.txt
```

### Node

```bash
npm install
```

## Variables de entorno clave

```env
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DEFAULT_MODEL=x-ai/grok-4.1-fast
SUPABASE_URL=https://...
SUPABASE_SERVICE_KEY=...

KAPSO_API_KEY=...
KAPSO_WEBHOOK_SECRET=...
KAPSO_INTERNAL_TOKEN=...
KAPSO_BASE_URL=https://api.kapso.ai/meta/whatsapp

NYLAS_API_KEY=...
NYLAS_API_URL=https://api.us.nylas.com
```

## Modos de ejecución

### FastAPI directo

Útil para desarrollo de backend puro.

```bash
python main.py
```

- **Base local**: `http://localhost:8080`
- **Swagger**: `http://localhost:8080/docs`

### Stack completo con bridge

Útil cuando necesitas reproducir el comportamiento de Railway o probar Kapso/proxies públicos.

Terminal 1:

```powershell
$env:PYTHON_SERVICE_PORT=8000
python main.py
```

Terminal 2:

```powershell
node kapso-bridge/server.mjs
```

- **Bridge local**: `http://localhost:3001`
- **FastAPI interno**: `http://127.0.0.1:8000`

## Endpoints importantes

- **Chat**: `POST /api/v1/chat`
- **Health**: `GET /api/v1/health`
- **Kapso inbound interno**: `POST /api/v1/kapso/inbound`
- **Kapso webhook público vía bridge**: `POST /webhook/kapso`
- **Scheduling**:
  - `POST /api/v1/scheduling/disponibilidad`
  - `POST /api/v1/scheduling/crear-evento`
  - `POST /api/v1/scheduling/reagendar-evento`
  - `POST /api/v1/scheduling/eliminar-evento`
- **Funnel**: `POST /api/v1/funnel/analyze`
- **Graph schema**: `GET /api/v1/graph/schema`
- **Kapso debug dashboard**: `GET /debug/kapso`

## Nota operativa de scheduling

El módulo de scheduling mantiene una **cuarentena local en memoria de 1 hora** para `grant_id` inválidos de Nylas (`401`, `403`, `404`). Eso evita reintentos intermitentes con grants rotos sin tocar tablas adicionales en Supabase.

## Documentación vigente

- **Contexto general**: `docs/PROJECT_CONTEXT.md`
- **Arquitectura**: `docs/architecture/OVERVIEW.md`
- **Endpoints**: `docs/API_ENDPOINTS.md`
- **Deploy Railway + Kapso**: `docs/RAILWAY_KAPSO_DEPLOY.md`
- **Backlog técnico**: `docs/NEXT_STEPS.md`
- **Benchmarks históricos**: `docs/BENCHMARK_REAL_FLOW_RESULTS.md` y `benchmarks/README.md`
