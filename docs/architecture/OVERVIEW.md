# Arquitectura del Sistema

## Resumen

Sistema construido sobre `FastAPI + LangGraph + OpenRouter + MCP`, integrado con Kapso (WhatsApp) para automatización comercial.

Objetivo principal:

- recibir mensajes de WhatsApp o requests HTTP directos
- ejecutar múltiples agentes LangGraph en paralelo
- cargar herramientas MCP dinámicamente cuando aplique
- actualizar embudos y contactos en Supabase
- devolver respuesta final, herramientas usadas y métricas de tiempo

## Componentes principales

### Capa HTTP (FastAPI)

- **`main.py`**
  - inicializa FastAPI
  - registra todos los routers
  - configura CORS
  - background task: `_retry_stuck_loop()` cada 10 min para mensajes atascados

- **`app/api/routes.py`**
  - `POST /api/v1/chat` — endpoint genérico del agente conversacional
  - `GET /api/v1/health` — health check
  - `DELETE /api/v1/cache` — limpieza de cache

- **`app/api/funnel_routes.py`**
  - `POST /api/v1/funnel/analyze` — ejecuta el funnel agent
  - `GET /api/v1/funnel/debug` — dashboard HTML de debug
  - `GET /api/v1/funnel/debug/events` — eventos de debug en JSON

- **`app/api/kapso_routes.py`**
  - `POST /api/v1/kapso/inbound` — recibe mensajes del bridge de Kapso
  - valida token interno
  - resuelve empresa, agente, contacto y canal
  - ejecuta agentes en paralelo: conversacional + funnel + contact_update
  - persiste mensajes entrantes y salientes

- **`app/api/scheduling_routes.py`**
  - endpoints de agendamiento de citas (integración de calendario)

- **`app/api/graph_routes.py`**
  - endpoints de inspección y debug del grafo LangGraph

- **`app/api/db_routes.py`**
  - endpoints utilitarios de lectura sobre Supabase
  - inspección de empresa, agente, contacto, conversación y canal

- **`app/api/debug_dashboard.py`**
  - dashboard de debug del flujo conversacional

### Capa de orquestación (Agentes LangGraph)

- **`app/agents/conversational.py`**
  - agente principal de respuesta al usuario
  - carga tools MCP en paralelo por request
  - construye el grafo de LangGraph
  - loop: `agent → tools → tool_tracker → agent → END`
  - timeout defensivo en ejecución del grafo
  - límite de iteraciones para evitar loops infinitos
  - tracking de tools usadas y timing por fase

- **`app/agents/funnel.py`**
  - analiza el estado del contacto en el embudo
  - carga contexto en paralelo: contacto, etapas, conversación, mensajes
  - loop: `agent → tools → agent → END` (máx 2 iteraciones LLM)
  - herramienta `update_etapa_embudo(orden_etapa, razon)` → actualiza BD directamente
  - herramienta `update_metadata(informacion_capturada, seccion)` → POST a Supabase Edge Function
  - modelo fijo: `x-ai/grok-4.1-fast`
  - respuesta limitada a 3 líneas para uso interno del equipo

- **`app/agents/contact_update.py`**
  - actualiza datos del contacto basado en la conversación
  - se ejecuta en paralelo con los otros agentes desde Kapso inbound

### Capa de herramientas (MCP)

- **`app/mcp_client/client.py`**
  - cliente MCP remoto
  - descubre herramientas disponibles en servidores externos
  - convierte definiciones MCP a tools compatibles con LangChain/LangGraph
  - carga en paralelo con timeout defensivo

### Capa de integración externa

- **`kapso-bridge/server.mjs`**
  - bridge Node.js que recibe el webhook de Kapso
  - agrupa mensajes del mismo usuario en una ventana de tiempo
  - marca presencia/typing en WhatsApp
  - llama al endpoint interno de FastAPI
  - envía la respuesta final a WhatsApp vía SDK de Kapso
  - registra eventos de debug

- **`app/nylas_client/client.py`**
  - cliente de email Nylas para integración con correo

### Capa de base de datos

- **`app/db/client.py`**
  - cliente Supabase async con HTTP/2 y connection pooling (20 máx, 10 keep-alive)
  - métodos: `query()`, `insert()`, `update()`, `delete()`, `rpc()`
  - soporte para filtros PostgREST (eq, in, lt, etc.)

- **`app/db/queries.py`**
  - funciones de consulta de alto nivel
  - `load_funnel_context()` — carga contexto del embudo en paralelo
  - `get_conversacion_con_mensajes()` — conversación + mensajes en paralelo
  - `actualizar_etapa_contacto()` — actualiza etapa del embudo en BD
  - queries de empresa, agente, contacto, número

### Capa de configuración y soporte

- **`app/core/config.py`** — centraliza variables de entorno con `pydantic-settings`
- **`app/core/cache.py`** — cache en memoria con TTL de 5 min (solo para requests sin MCP)
- **`app/core/error_webhook.py`** — middleware que notifica errores HTTP 500+ vía webhook
- **`app/core/funnel_debug.py`** — buffer circular en memoria de las últimas 50 ejecuciones del funnel agent
- **`app/core/kapso_debug.py`** — utilidades de debug para el flujo Kapso
- **`app/core/kapso_prompt.py`** — system prompts configurados para Kapso

## Flujos principales

### Flujo Kapso (producción)

```text
WhatsApp
  -> Kapso webhook
  -> kapso-bridge/server.mjs
       (agrupa mensajes, typing, envía a FastAPI)
  -> POST /api/v1/kapso/inbound
       (valida token, resuelve empresa/agente/contacto/canal)
  -> asyncio.gather:
       ├─ conversational.py   → respuesta para el usuario
       ├─ funnel.py           → actualiza embudo
       └─ contact_update.py   → actualiza contacto
  -> Supabase                 (persiste mensajes y cambios)
  -> kapso-bridge/server.mjs  → responde en WhatsApp
```

### Flujo del agente conversacional

```text
POST /api/v1/chat
  -> run_agent()
  -> cache lookup              (solo sin MCP)
  -> MCP discovery en paralelo (si hay mcp_servers)
  -> LLM creation/reuse        (cache por modelo + params)
  -> LangGraph compile
  -> agent node
  -> tools node (si hay tool_calls)
  -> tool_tracker node
  -> agent node
  -> END
  -> HTTP Response con timing y tools_used
```

### Flujo del funnel agent

```text
POST /api/v1/funnel/analyze
  -> load_funnel_context() en paralelo:
       ├─ get_contacto()
       ├─ get_empresa_embudo()
       └─ get_conversacion_con_mensajes()
  -> _build_graph()
  -> agent node (analiza contexto)
  -> tools node (si hay tool_calls):
       ├─ update_etapa_embudo → UPDATE wp_contactos
       └─ update_metadata → POST Supabase Edge Function
  -> agent node (análisis final, máx 2 iteraciones)
  -> END
  -> FunnelAgentResponse
```

## Grafo del agente conversacional

```text
agent
 ├─ si hay tool_calls → tools → tool_tracker → agent
 └─ si no hay tool_calls → END
```

## Grafo del funnel agent

```text
agent
 ├─ si hay tool_calls y < 2 iteraciones → tools → agent
 └─ si no hay tool_calls o >= 2 iteraciones → END
```

## Decisiones arquitectónicas vigentes

- `LangGraph` es el runtime principal de orquestación.
- `Python` es la base del flujo productivo.
- `Node.js` solo para el bridge de Kapso y benchmarks comparativos históricos.
- `OpenRouter` es el proveedor principal de modelos.
- `MCP` es el mecanismo para descubrimiento y ejecución de herramientas dinámicas.
- `Supabase Edge Functions` para operaciones críticas de metadata (no se escribe metadata directo en BD).

## Trazabilidad disponible

El sistema devuelve en cada response:

- `tools_used` — tools ejecutadas con input, output, duration y status
- `conversation_id`
- `model_used`
- `timing.total_ms`
- `timing.llm_ms`
- `timing.mcp_discovery_ms`
- `timing.graph_build_ms`
- `timing.tool_execution_ms`
- `agent_runs` — trazas detalladas por iteración del agente

Dashboard de debug disponible en:

- Funnel agent: `GET /api/v1/funnel/debug`
- Kapso/conversacional: a través de `app/api/debug_dashboard.py`

## Limitaciones actuales

- No hay tracing persistente por nodo del grafo (solo en memoria).
- No hay tests automatizados formales para el flujo Kapso + multi-agente end-to-end.
- `etapa_actual` puede llegar `null` desde Supabase aunque la metadata ya tenga información válida.
- El historial de debug del funnel se pierde al reiniciar el servidor (buffer en memoria).

## Riesgos operativos

- Inconsistencias entre `etapa_actual` y metadata comercial en Supabase
- Tool schemas dinámicos con variaciones de servidores MCP remotos
- Fallas transitorias de red hacia OpenRouter, Supabase o servidores MCP
- Timeout del bridge de Kapso si el agente tarda más de lo esperado

## Extensiones naturales del diseño

- Persistir trazas del grafo en BD para observabilidad histórica
- Agregar retries por nodo con backoff configurable
- Introducir validadores de output por agente
- Construir flujos multi-agente más complejos sobre LangGraph
- Tests automatizados de integración end-to-end
