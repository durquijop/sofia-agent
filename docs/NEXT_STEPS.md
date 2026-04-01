# Próximos Pasos Recomendados

## Prioridad alta

- **Agregar tracing por nodo de LangGraph**
  - tiempo por nodo
  - inputs y outputs resumidos por transición
  - errores por nodo
  - persistir trazas en BD para observabilidad histórica (hoy solo en memoria)

- **Definir estrategia de errores por tool**
  - timeout configurable por tool
  - retries controlados con backoff
  - fallback cuando una tool falle
  - mensajes internos más accionables

- **Unificar identificadores internos para tools**
  - definir explícitamente si las tools internas deben trabajar con `contacto_id`, `conversacion_id` o ambos
  - fijar el identificador en el schema de tools y en el prompt para evitar ambigüedades

- **Endurecer el manejo del contexto de embudo**
  - si `etapa_actual` viene `null` pero la metadata indica una etapa válida, definir una regla de reconciliación explícita

## Prioridad media

- **Agregar tests automatizados**
  - tests de schemas Pydantic
  - tests de rutas HTTP (con mocks)
  - tests de construcción del grafo
  - test de carga de tools MCP con mocks
  - test end-to-end del flujo Kapso + multi-agente

- **Persistir historial del funnel debug**
  - hoy el buffer se pierde al reiniciar
  - agregar tabla `funnel_debug_runs` en BD
  - modificar `add_funnel_debug_run()` para guardar en BD opcionalmente

- **Alinear la documentación con ejemplos reales de negocio**
  - requests de embudo con datos reales anonimizados
  - requests con MCP reales
  - ejemplos de trazabilidad con tool calls

## Prioridad baja

- **Mover benchmarks históricos a archive**
  - `benchmarks/` ya no se usa activamente
  - mover a `archive/benchmarks/` para no contaminar la raíz del proyecto

- **Separar utilidades operativas de benchmarks**
  - `scripts/` solo para utilidades del stack principal
  - `benchmarks/` solo para investigación histórica

- **Preparar runbooks operativos**
  - reinicio local del bridge y FastAPI
  - validación de variables de entorno
  - troubleshooting MCP (tool no responde, timeout)
  - troubleshooting OpenRouter (rate limit, modelo no disponible)
  - troubleshooting Kapso (webhook no llega, mensajes atascados)

## Orden de ejecución sugerido

1. Tests automatizados básicos (schemas + rutas)
2. Tracing por nodo (persistencia en BD)
3. Estrategia de errores y retries por tool
4. Persistir historial funnel debug
5. Runbooks operativos
6. Archivar benchmarks cuando ya no sean necesarios
