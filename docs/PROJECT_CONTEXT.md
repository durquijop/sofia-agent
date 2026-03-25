# Contexto del Proyecto

## Estado actual

Este proyecto usa **LangGraph** como stack principal para producción.

La decisión se tomó después de comparar `LangGraph` contra `Vercel AI SDK` en benchmarks controlados y en un flujo real con contexto de Supabase. El resultado fue que la latencia quedó muy pareja, pero `LangGraph` ofreció mejor base para:

- robustez
- trazabilidad
- control del loop agente -> tools -> respuesta
- manejo de errores
- crecimiento futuro del sistema multi-agente

Los archivos de benchmark comparativo con `Vercel AI SDK` se conservan solo como **referencia histórica**.

## Stack principal

- **API**: FastAPI
- **Orquestación de agentes**: LangGraph
- **LLM provider**: OpenRouter
- **Tooling dinámico**: MCP servers
- **Cliente HTTP**: `httpx.AsyncClient` compartido con pooling
- **Configuración**: `pydantic-settings`

## Objetivo funcional del sistema

Servir como base de un sistema multi-agente para operaciones comerciales, con estas capacidades:

- recibir `system_prompt` y `message`
- usar `LangGraph` para ejecutar el ciclo del agente
- descubrir y ejecutar herramientas MCP dinámicamente
- devolver respuesta final, tools usadas y métricas de tiempo
- permitir evolucionar a flujos más complejos con varios agentes y herramientas asíncronas

## Punto de entrada

- **`main.py`**
  - crea la app FastAPI
  - registra rutas de chat y base de datos
  - expone documentación Swagger y ReDoc

## Archivos principales

### API y entrada

- **`main.py`**
  - entrypoint del servicio

- **`app/api/routes.py`**
  - endpoint principal `POST /api/v1/chat`
  - health check
  - limpieza de cache

- **`docs/API_ENDPOINTS.md`**
  - referencia de endpoints HTTP

### Core del agente

- **`app/agents/conversational.py`**
  - implementación principal del agente con `LangGraph`
  - carga de tools MCP
  - construcción del grafo
  - ejecución del loop agente -> tools -> agent
  - tracking de tools usadas
  - timing del flujo

- **`app/mcp_client/client.py`**
  - cliente MCP
  - descubrimiento de tools remotas
  - adaptación de tools MCP a herramientas compatibles con LangChain/LangGraph

- **`app/schemas/chat.py`**
  - modelos de request/response
  - estructura de tools usadas y métricas

- **`app/core/config.py`**
  - variables de entorno y configuración central

- **`app/core/cache.py`**
  - cache de respuestas para requests sin MCP

## Flujo actual del request

1. `POST /api/v1/chat` recibe `system_prompt`, `message` y parámetros opcionales.
2. `run_agent()` resuelve el modelo y parámetros.
3. Si no hay MCP servers, intenta responder desde cache.
4. Si hay MCP servers, descubre tools en paralelo.
5. Se crea o reutiliza una instancia LLM.
6. Se construye el grafo de `LangGraph`.
7. El grafo ejecuta el loop:
   - nodo `agent`
   - nodo `tools`
   - nodo `tool_tracker`
8. Se extrae la respuesta final y se devuelven métricas.

## Invariantes importantes

- `LangGraph` es la implementación principal que se debe extender.
- El flujo principal del producto vive en Python, no en Node.
- Los benchmarks de Node existen solo para comparación o documentación histórica.
- Las tools MCP se cargan dinámicamente por request.
- El cache solo aplica a requests sin tools MCP.
- El modelo por defecto actual es `x-ai/grok-4.1-fast`.

## Variables de entorno relevantes

- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL`
- `DEFAULT_MODEL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `APP_NAME`
- `DEBUG`

## Benchmarks y utilidades

### Benchmarks LangGraph

- **`scripts/benchmark_parallel_langgraph.py`**
  - benchmark paralelo de agentes usando LangGraph

- **`scripts/documented_real_flow_langgraph.py`**
  - benchmark documentado del flujo real con contexto de embudo

### Benchmarks comparativos históricos

- **`benchmarks/vercel-ai-sdk-compare.mjs`**
- **`benchmarks/vercel-ai-sdk-embudo-agent.mjs`**
- **`benchmarks/vercel-ai-sdk-parallel-agents.mjs`**
- **`benchmarks/vercel-ai-sdk-documented-real-flow.mjs`**

Estos archivos no son la base del producto. Se mantienen porque contienen evidencia comparativa útil.

## Documentos clave

- **`docs/PROJECT_CONTEXT.md`**
  - este documento

- **`docs/architecture/OVERVIEW.md`**
  - vista de arquitectura y componentes

- **`docs/API_ENDPOINTS.md`**
  - documentación operativa de endpoints

- **`docs/BENCHMARK_REAL_FLOW_RESULTS.md`**
  - benchmark final documentado y hallazgos

- **`docs/NEXT_STEPS.md`**
  - backlog técnico recomendado para la siguiente fase

## Hallazgos importantes del benchmark real

- `LangGraph` y `Vercel AI SDK` quedaron muy parejos en latencia total.
- `LangGraph` fue una mejor base para control y robustez del loop con tools.
- La diferencia de velocidad no justificó cambiar de stack.
- El cuello principal sigue estando más en inferencia/modelo que en el runtime local.

## Riesgos y temas pendientes

- El contexto real de embudo puede traer inconsistencias, por ejemplo:
  - `contexto_embudo.data.etapa_actual = null`
  - pero metadata y `etapa_actual_orden` sí sugieren una etapa real

- En el benchmark comparativo apareció una discrepancia entre usar:
  - `contacto_id`
  - `conversacion_id`
  como identificador para tools del procesador

- `docs/API_ENDPOINTS.md` debe mantenerse alineado con los schemas reales cuando cambie la API.

## Convención para futuras sesiones

Si se retoma el proyecto en otra sesión, asumir lo siguiente:

- el stack principal es `LangGraph`
- la API principal es FastAPI
- OpenRouter es el provider activo
- MCP es el mecanismo para herramientas dinámicas
- los benchmarks de `Vercel AI SDK` no se deben extender salvo que haya una nueva comparación puntual
- cualquier cambio nuevo debe priorizar:
  - robustez
  - trazabilidad
  - manejo de errores
  - observabilidad

## Dónde empezar al retomar desarrollo

1. Leer este archivo.
2. Leer `README.md`.
3. Leer `docs/architecture/OVERVIEW.md`.
4. Leer `app/agents/conversational.py`.
5. Leer `app/api/routes.py`.
6. Leer `docs/API_ENDPOINTS.md`.
7. Revisar `docs/NEXT_STEPS.md`.
8. Si el cambio involucra benchmarking real, revisar `docs/BENCHMARK_REAL_FLOW_RESULTS.md`.

## Próximas mejoras recomendadas

- centralizar mejor el contexto de embudo real dentro del flujo productivo
- definir explícitamente qué identificador usan las tools internas
- agregar mejor observabilidad por nodo/tool
- endurecer estrategias de retry, timeout y fallback
- separar mejor utilidades de benchmark frente al runtime productivo
