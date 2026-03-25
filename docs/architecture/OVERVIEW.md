# Arquitectura del Sistema

## Resumen

El sistema actual está construido sobre una arquitectura mixta de **FastAPI + LangGraph + OpenRouter + Supabase**, con un **bridge Node/Kapso** como capa pública en Railway.

Objetivo principal:

- recibir tráfico externo de Kapso, n8n y clientes HTTP
- enrutar ese tráfico al backend FastAPI interno
- ejecutar agentes LangGraph y rutas operativas
- integrar MCP, Supabase y Nylas según el caso de uso
- devolver respuestas y trazabilidad operativa

## Vista de alto nivel

```text
Kapso / n8n / clientes HTTP
  -> kapso-bridge/server.mjs
  -> FastAPI (main.py)
  -> agentes / DB / scheduling / debug
  -> OpenRouter / Supabase / MCP / Nylas
```

## Componentes principales

### Capa pública

- **`kapso-bridge/server.mjs`**
  - expone el webhook público de Kapso
  - envía respuestas por Kapso SDK
  - mantiene eventos de debug del bridge
  - proxyea `openapi.json` y rutas públicas de scheduling al FastAPI interno

### Capa HTTP interna

- **`main.py`**
  - inicializa FastAPI
  - registra routers
  - configura CORS
  - centraliza manejo global de errores

- **`app/api/routes.py`**
  - chat principal
  - health check
  - limpieza de cache

- **`app/api/kapso_routes.py`**
  - inbound interno de Kapso
  - debug JSON y SSE
  - coordinación del flujo conversacional y operativo

- **`app/api/scheduling_routes.py`**
  - disponibilidad
  - crear, reagendar y eliminar eventos
  - integración con Nylas y Supabase

- **`app/api/funnel_routes.py`**
  - análisis de embudo
  - dashboards y eventos de debug

- **`app/api/db_routes.py`**
  - endpoints utilitarios de lectura sobre Supabase

- **`app/api/graph_routes.py`**
  - introspección dinámica del grafo y sus dependencias

### Capa de orquestación

- **`app/agents/conversational.py`**
  - agente principal del sistema
  - carga tools MCP en paralelo
  - construye y ejecuta el grafo LangGraph
  - rastrea herramientas usadas y timings

- **`app/agents/funnel.py`**
  - agente especializado de embudo
  - soporte comercial y actualización contextual

### Capa de integración

- **`app/mcp_client/client.py`**
  - cliente MCP remoto
  - descubrimiento de herramientas disponibles
  - adaptación a tools compatibles con LangGraph

- **`app/nylas_client/client.py`**
  - cliente asíncrono de calendario
  - free/busy y CRUD de eventos

- **`app/db/client.py`** y **`app/db/queries.py`**
  - acceso a Supabase
  - consultas de negocio y debug

## Flujos principales

### Flujo de chat directo

```text
HTTP Request
  -> FastAPI route
  -> run_agent()
  -> cache lookup (solo sin MCP)
  -> MCP discovery (si aplica)
  -> LLM creation/reuse
  -> LangGraph compile
  -> agent
  -> tools
  -> tool_tracker
  -> respuesta final
```

### Flujo Kapso productivo

```text
Kapso Webhook
  -> kapso-bridge/server.mjs
  -> /api/v1/kapso/inbound
  -> funnel/contact update/conversacional
  -> respuesta al bridge
  -> Kapso SDK
```

### Flujo de scheduling público

```text
n8n / Postman / cliente HTTP
  -> kapso-bridge/server.mjs
  -> /api/v1/scheduling/*
  -> app/api/scheduling_routes.py
  -> Nylas + Supabase
```

## Decisiones arquitectónicas vigentes

- `LangGraph` es el runtime principal de orquestación.
- `FastAPI` concentra la lógica de negocio.
- `Node` es parte del runtime productivo por el bridge Kapso.
- `OpenRouter` es el proveedor principal de modelos.
- `MCP` es el mecanismo elegido para descubrimiento y ejecución de herramientas.
- `Nylas` soporta la capa de disponibilidad y citas.

## Trazabilidad disponible hoy

El sistema ya expone o genera:

- `tools_used`
- `conversation_id`
- `model_used`
- `timing.total_ms`
- `timing.llm_ms`
- `timing.mcp_discovery_ms`
- `timing.graph_build_ms`
- `timing.tool_execution_ms`
- eventos de debug Kapso en memoria y Supabase
- dashboards HTML de debug para Kapso y funnel

## Limitaciones actuales

- `tool_execution_ms` sigue siendo mejorable en precisión.
- No hay tracing persistente fino por nodo del grafo.
- La cuarentena de grants Nylas es local en memoria y se pierde al reiniciar.
- Hay diferencias entre probar directo en FastAPI y usar el bridge público.

## Riesgos operativos

- inconsistencias entre `etapa_actual` y metadata comercial
- variaciones de schemas MCP remotos
- fallas transitorias de red hacia OpenRouter, Kapso, Supabase o Nylas
- grants inválidos de Nylas en asesores específicos
- necesidad futura de retries y fallbacks más finos por tool y por nodo

## Extensiones naturales del diseño

- agregar retries por nodo
- agregar timeouts por tool
- persistir trazas del grafo
- introducir validadores de output
- construir flujos multi-agente más explícitos
