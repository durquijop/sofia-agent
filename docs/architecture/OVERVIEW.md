# Arquitectura del Sistema

## Resumen

El sistema actual está construido sobre `FastAPI + LangGraph + OpenRouter + MCP`.

Objetivo principal:

- recibir una instrucción de sistema y un mensaje
- ejecutar un agente LangGraph
- cargar herramientas MCP dinámicamente cuando aplique
- devolver respuesta final, herramientas usadas y métricas de tiempo

## Componentes principales

### Capa HTTP

- **`main.py`**
  - inicializa FastAPI
  - registra routers
  - configura CORS

- **`app/api/routes.py`**
  - expone el endpoint principal de chat
  - health check del servicio
  - limpieza de cache

- **`app/api/db_routes.py`**
  - endpoints utilitarios de lectura sobre Supabase
  - soporte para inspección de empresa, agente, contacto, conversación y número

### Capa de orquestación

- **`app/agents/conversational.py`**
  - corazón del sistema
  - resuelve modelo y parámetros del request
  - carga tools MCP en paralelo
  - construye el grafo de LangGraph
  - ejecuta el loop de tool calling
  - rastrea herramientas utilizadas
  - calcula timings

### Capa de tools

- **`app/mcp_client/client.py`**
  - cliente MCP remoto
  - descubre herramientas disponibles
  - convierte definiciones MCP a tools compatibles con LangChain/LangGraph

### Capa de configuración y soporte

- **`app/core/config.py`**
  - centraliza variables de entorno

- **`app/core/cache.py`**
  - cache en memoria para requests sin MCP

- **`app/schemas/chat.py`**
  - contratos de entrada y salida del endpoint de chat

## Flujo principal del chat

```text
HTTP Request
  -> FastAPI route
  -> run_agent()
  -> cache lookup (solo sin MCP)
  -> MCP discovery (si aplica)
  -> LLM creation/reuse
  -> LangGraph compile
  -> agent node
  -> tools node (si hay tool_calls)
  -> tool_tracker node
  -> respuesta final
  -> HTTP Response
```

## Grafo actual del agente

```text
agent
 ├─ si hay tool_calls -> tools -> tool_tracker -> agent
 └─ si no hay tool_calls -> END
```

## Decisiones arquitectónicas vigentes

- `LangGraph` es el runtime principal de orquestación.
- `Python` es la base del flujo productivo.
- `Node` queda solo para benchmarks comparativos e investigación.
- `OpenRouter` es el proveedor principal de modelos.
- `MCP` es el mecanismo elegido para descubrimiento y ejecución de herramientas.

## Trazabilidad disponible hoy

El sistema ya devuelve:

- `tools_used`
- `conversation_id`
- `model_used`
- `timing.total_ms`
- `timing.llm_ms`
- `timing.mcp_discovery_ms`
- `timing.graph_build_ms`
- `timing.tool_execution_ms`

## Limitaciones actuales

- `tool_execution_ms` existe en el schema, pero el runtime principal aún no lo calcula explícitamente.
- No hay tracing persistente por nodo del grafo.
- El manejo de errores está concentrado en la ruta HTTP y no por nodo del flujo.
- El benchmark real mostró que el contexto de embudo puede llegar inconsistente desde Supabase.

## Riesgos operativos

- inconsistencias entre `etapa_actual` y metadata comercial
- tool schemas dinámicos con variaciones de MCP remoto
- fallas transitorias de red hacia OpenRouter o Supabase
- necesidad futura de retries y fallbacks por tool y por nodo

## Extensiones naturales del diseño

- agregar retries por nodo
- agregar timeouts por tool
- persistir trazas del grafo
- introducir validadores de output
- construir flujos multi-agente explícitos sobre LangGraph
