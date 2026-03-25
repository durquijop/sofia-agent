# Contexto del Proyecto

## Estado actual

Este repositorio opera hoy como un sistema productivo basado en **FastAPI + LangGraph + OpenRouter + Supabase**, con una **capa pública Node/Kapso** para webhooks de WhatsApp y proxy de rutas operativas.

La base del producto sigue estando en Python, pero **Node ya no es solo benchmark**: `kapso-bridge/server.mjs` es parte del runtime real desplegado en Railway.

## Stack principal

- **API interna de negocio**: FastAPI
- **Bridge público**: Express + Kapso SDK
- **Orquestación de agentes**: LangGraph
- **LLM provider**: OpenRouter
- **Base de datos y storage**: Supabase
- **Tooling dinámico**: MCP servers
- **Scheduling**: Nylas + Supabase
- **Cliente HTTP**: `httpx.AsyncClient`
- **Configuración**: `pydantic-settings`

## Objetivo funcional del sistema

Servir como plataforma multi-agente para operación comercial y mensajería, con estas capacidades:

- recibir tráfico de WhatsApp vía Kapso
- enrutar mensajes al backend FastAPI interno
- ejecutar agentes LangGraph con tools MCP
- exponer rutas auxiliares de DB, funnel, introspección y debug
- gestionar disponibilidad y citas con Nylas
- responder por Kapso y mantener trazabilidad operativa

## Runtime real

### Capa pública

- **`kapso-bridge/server.mjs`**
  - recibe el webhook público de Kapso
  - despacha respuestas vía Kapso SDK
  - mantiene debug básico del bridge
  - proxyea rutas de scheduling y `openapi.json` al FastAPI interno

### Capa interna

- **`main.py`**
  - crea la app FastAPI
  - registra routers de chat, Kapso, DB, funnel, graph y scheduling
  - expone Swagger / ReDoc
  - centraliza manejo global de errores

## Rutas y módulos principales

### Conversacional

- **`app/api/routes.py`**
  - `POST /api/v1/chat`
  - `GET /api/v1/health`
  - `DELETE /api/v1/cache`

- **`app/agents/conversational.py`**
  - agente principal LangGraph
  - carga MCP dinámica
  - loop agente -> tools -> tracker -> agente

### Kapso y operación WhatsApp

- **`app/api/kapso_routes.py`**
  - `POST /api/v1/kapso/inbound`
  - debug JSON y SSE de Kapso
  - coordinación del flujo de inbound y respuesta

- **`app/api/debug_dashboard.py`**
  - dashboard HTML en `/debug/kapso`

### Scheduling

- **`app/api/scheduling_routes.py`**
  - `POST /api/v1/scheduling/disponibilidad`
  - `POST /api/v1/scheduling/crear-evento`
  - `POST /api/v1/scheduling/reagendar-evento`
  - `POST /api/v1/scheduling/eliminar-evento`

- **`app/nylas_client/client.py`**
  - free/busy
  - list/create/update/delete de eventos

### Funnel y soporte comercial

- **`app/api/funnel_routes.py`**
  - `POST /api/v1/funnel/analyze`
  - dashboards y eventos de debug del funnel

### Base de datos e introspección

- **`app/api/db_routes.py`**
  - health y consultas auxiliares sobre Supabase

- **`app/api/graph_routes.py`**
  - `GET /api/v1/graph/schema`
  - introspección del grafo y dependencias externas

## Modos de ejecución

### FastAPI directo

- comando: `python main.py`
- puerto por defecto: `8080`
- útil para backend puro y Swagger local

### Stack completo estilo Railway

- FastAPI interno en `PYTHON_SERVICE_PORT=8000`
- bridge Node público con `node kapso-bridge/server.mjs`
- `railway-start.sh` levanta ambos procesos

## Invariantes importantes

- `LangGraph` es la implementación principal a extender.
- `FastAPI` es la fuente de verdad del negocio.
- `kapso-bridge/server.mjs` es crítico para producción en Railway.
- Los endpoints de scheduling públicos se exponen hoy a través del bridge.
- Las tools MCP se cargan dinámicamente por request.
- El cache en memoria solo aplica al chat sin MCP.
- El modelo por defecto actual es `x-ai/grok-4.1-fast`.
- Los benchmarks de `Vercel AI SDK` se conservan solo como referencia histórica.

## Scheduling: comportamiento operativo actual

El módulo de scheduling incorpora una **cuarentena local en memoria de 1 hora** para `grant_id` inválidos de Nylas.

- se activa ante `401`, `403` o `404`
- evita reintentos intermitentes con grants rotos
- no persiste en Supabase
- se pierde si el proceso reinicia

## Variables de entorno relevantes

### Core

- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL`
- `DEFAULT_MODEL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `APP_NAME`
- `DEBUG`

### Kapso / bridge

- `KAPSO_API_KEY`
- `KAPSO_WEBHOOK_SECRET`
- `KAPSO_INTERNAL_TOKEN`
- `KAPSO_BASE_URL`
- `INTERNAL_AGENT_API_URL`
- `PYTHON_SERVICE_PORT`

### Nylas

- `NYLAS_API_KEY`
- `NYLAS_API_KEY_2`
- `NYLAS_API_URL`

## Riesgos operativos conocidos

- inconsistencias de contexto comercial en Supabase
- fallas temporales de red hacia OpenRouter, Kapso o Supabase
- grants inválidos de Nylas en asesores específicos
- variaciones de schemas MCP remotos
- diferencias entre probar directo contra FastAPI y pasar por el bridge público

## Documentos clave

- **`README.md`**
- **`docs/PROJECT_CONTEXT.md`**
- **`docs/architecture/OVERVIEW.md`**
- **`docs/API_ENDPOINTS.md`**
- **`docs/RAILWAY_KAPSO_DEPLOY.md`**
- **`docs/NEXT_STEPS.md`**

## Dónde empezar al retomar desarrollo

1. Leer este archivo.
2. Leer `README.md`.
3. Leer `docs/architecture/OVERVIEW.md`.
4. Revisar `main.py`.
5. Revisar `kapso-bridge/server.mjs`.
6. Revisar `app/api/kapso_routes.py` y `app/api/scheduling_routes.py`.
7. Revisar `docs/API_ENDPOINTS.md`.
8. Revisar `docs/RAILWAY_KAPSO_DEPLOY.md` si el cambio toca deploy o tráfico público.
