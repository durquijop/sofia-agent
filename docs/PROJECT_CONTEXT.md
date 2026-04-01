# Contexto del Proyecto

## Estado actual

Este proyecto usa **LangGraph** como stack principal para producción.

La decisión se tomó después de comparar `LangGraph` contra `Vercel AI SDK` en benchmarks controlados y en un flujo real con contexto de Supabase. `LangGraph` ofreció mejor base para:

- robustez y control del loop agente → tools → respuesta
- trazabilidad por nodo
- manejo de errores
- crecimiento futuro del sistema multi-agente

Los archivos de benchmark comparativo con `Vercel AI SDK` se conservan solo como **referencia histórica** en `benchmarks/`.

## Stack principal

- **API**: FastAPI
- **Orquestación de agentes**: LangGraph
- **LLM provider**: OpenRouter
- **Tooling dinámico**: MCP servers
- **Base de datos**: Supabase (async REST client con connection pooling)
- **Canal de mensajería**: Kapso (WhatsApp)
- **Email**: Nylas
- **Cliente HTTP**: `httpx.AsyncClient` compartido con HTTP/2
- **Configuración**: `pydantic-settings`

## Objetivo funcional del sistema

Sistema multi-agente para operaciones comerciales que:

- recibe mensajes de WhatsApp vía Kapso
- ejecuta en paralelo: agente conversacional + funnel agent + contact update agent
- permite herramientas MCP dinámicas por empresa/agente
- actualiza automáticamente etapas de embudo y metadata de contactos
- devuelve respuesta final al usuario en WhatsApp

También expone un endpoint REST genérico (`/api/v1/chat`) para integraciones directas con system prompt arbitrario.

## Agentes implementados

### 1. Agente Conversacional (`app/agents/conversational.py`)
- Agente principal de respuesta al usuario
- Soporta tools MCP dinámicas por request
- Loop: `agent → tools → tool_tracker → agent → END`
- Timeout defensivo y límite de iteraciones para evitar loops
- Cache de respuestas con TTL de 5 min (solo sin MCP)

### 2. Funnel Agent (`app/agents/funnel.py`)
- Analiza el estado del contacto en el embudo comercial
- Carga contexto en paralelo: contacto + etapas + conversación + mensajes
- Herramientas: `update_etapa_embudo(orden_etapa, razon)` y `update_metadata(informacion_capturada, seccion)`
- Modelo fijo: `x-ai/grok-4.1-fast`
- Máx 2 iteraciones LLM
- Respuesta limitada a 3 líneas para consumo interno del equipo

### 3. Agente de Actualización de Contacto (`app/agents/contact_update.py`)
- Actualiza datos del contacto basado en la conversación
- Se ejecuta en paralelo con los otros agentes desde Kapso inbound

## Flujo Kapso (producción)

```text
WhatsApp
  -> Kapso webhook
  -> kapso-bridge/server.mjs     (agrupa mensajes, typing, envía a FastAPI)
  -> app/api/kapso_routes.py     (valida token, resuelve empresa/agente/contacto)
  -> asyncio.gather:
       - conversational.py       (respuesta al usuario)
       - funnel.py               (actualiza embudo)
       - contact_update.py       (actualiza contacto)
  -> Supabase                    (persiste mensajes y cambios)
  -> kapso-bridge/server.mjs     (envía respuesta a WhatsApp)
```

## Archivos principales

### API y entrada

- **`main.py`** — entrypoint, registra todos los routers, background task de retry
- **`app/api/routes.py`** — endpoint `POST /api/v1/chat` y health check
- **`app/api/funnel_routes.py`** — `POST /api/v1/funnel/analyze` + debug dashboard
- **`app/api/kapso_routes.py`** — inbound WhatsApp, routing multi-agente
- **`app/api/scheduling_routes.py`** — endpoints de agendamiento de citas
- **`app/api/graph_routes.py`** — endpoints de debug del grafo LangGraph
- **`app/api/db_routes.py`** — endpoints utilitarios de lectura sobre Supabase
- **`app/api/debug_dashboard.py`** — dashboard de debug del agente conversacional

### Core de agentes

- **`app/agents/conversational.py`** — agente conversacional con LangGraph + MCP
- **`app/agents/funnel.py`** — funnel agent
- **`app/agents/contact_update.py`** — agente de actualización de contacto

### Infraestructura

- **`app/mcp_client/client.py`** — descubrimiento y adaptación de tools MCP remotas
- **`app/nylas_client/client.py`** — cliente de email Nylas
- **`app/db/client.py`** — cliente Supabase async con connection pooling (HTTP/2)
- **`app/db/queries.py`** — funciones de consulta: contactos, embudos, conversaciones, mensajes
- **`app/core/config.py`** — variables de entorno centralizadas con `pydantic-settings`
- **`app/core/cache.py`** — cache en memoria con TTL de 5 minutos
- **`app/core/error_webhook.py`** — notificación de errores HTTP 500+ vía webhook
- **`app/core/funnel_debug.py`** — buffer circular de debug del funnel agent (últimas 50 ejecuciones)
- **`app/core/kapso_debug.py`** — utilidades de debug para el flujo Kapso
- **`app/core/kapso_prompt.py`** — system prompts configurados para Kapso
- **`kapso-bridge/server.mjs`** — bridge Node.js: recibe webhook de Kapso, agrupa mensajes, llama a FastAPI

### Schemas

- **`app/schemas/chat.py`** — `ChatRequest`, `ChatResponse`, `ToolCall`, `TimingInfo`, `AgentRunTrace`
- **`app/schemas/funnel.py`** — `FunnelAgentRequest`, `FunnelAgentResponse`, `FunnelContextResponse`
- **`app/schemas/contact_update.py`** — schemas del agente de contacto
- **`app/schemas/kapso.py`** — schemas de Kapso/WhatsApp
- **`app/schemas/scheduling.py`** — schemas de agendamiento
- **`app/schemas/channel.py`** — schemas de configuración de canal

## Variables de entorno relevantes

### Core

- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL` (default: `https://openrouter.ai/api/v1`)
- `DEFAULT_MODEL` (default: `x-ai/grok-4.1-fast`)

### Supabase

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `SUPABASE_ANON_KEY`
- `SUPABASE_EDGE_FUNCTION_URL`
- `SUPABASE_EDGE_FUNCTION_TOKEN` (opcional)

### Kapso

- `KAPSO_API_KEY`
- `KAPSO_WEBHOOK_SECRET`
- `KAPSO_INTERNAL_TOKEN`
- `KAPSO_BASE_URL`
- `INTERNAL_AGENT_API_URL`
- `PYTHON_SERVICE_PORT`

### Otros

- `NYLAS_API_KEY`
- `ERROR_WEBHOOK_URL` (opcional, para notificaciones de error)
- `APP_NAME`
- `DEBUG`

## Invariantes importantes

- `LangGraph` es la implementación principal. No extender los benchmarks de Node.
- El flujo productivo vive en Python. Node solo para el bridge de Kapso y benchmarks históricos.
- Las tools MCP se cargan dinámicamente por request (solo en agente conversacional).
- El cache solo aplica a requests sin MCP.
- El modelo por defecto es `x-ai/grok-4.1-fast`.
- El funnel agent siempre usa `x-ai/grok-4.1-fast` (fijo).
- El funnel agent usa `orden_etapa` (número de etapa), no `id_etapa` (ID de base de datos).
- Metadata se actualiza vía Supabase Edge Function (`actualizar-metadata-v3`), no directamente en BD.

## Riesgos y temas pendientes

- `etapa_actual` puede venir `null` en el contexto del embudo aunque la metadata sí tenga datos
- La discrepancia de identificador (`contacto_id` vs `conversacion_id`) en tools debe manejarse explícitamente en los prompts
- No hay tracing persistente por nodo del grafo LangGraph
- No hay tests automatizados formales para el flujo completo Kapso + multi-agente

## Dónde empezar al retomar desarrollo

1. Leer este archivo
2. Leer `README.md`
3. Leer `docs/architecture/OVERVIEW.md`
4. Leer `app/agents/conversational.py` (agente principal)
5. Leer `app/api/kapso_routes.py` (flujo productivo completo)
6. Leer `docs/API_ENDPOINTS.md`
7. Revisar `docs/NEXT_STEPS.md`

## Convención para futuras sesiones

- Stack principal: `LangGraph + FastAPI + OpenRouter + MCP`
- Canal de producción: Kapso (WhatsApp)
- Los benchmarks de `Vercel AI SDK` no se extienden
- Cualquier cambio nuevo debe priorizar: robustez, trazabilidad, manejo de errores, observabilidad
- Al agregar nuevas tools o lógica multi-agente, ejecutar el protocolo de testeo en `docs/AGENT_TESTING_PROTOCOL.md`
