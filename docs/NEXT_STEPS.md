# Próximos Pasos Recomendados

## Prioridad alta

- **Medir `tool_execution_ms` real en producción**
  - hoy existe en el schema, pero no se calcula de forma explícita en `run_agent()`
  - conviene medirlo para mejorar observabilidad

- **Definir una estrategia clara de errores por tool**
  - timeout por tool
  - retries controlados
  - fallback cuando una tool falle
  - mensajes internos más accionables

- **Unificar identificadores internos para tools**
  - definir si las tools internas deben trabajar con `contacto_id`, `conversacion_id` o ambos
  - evitar ambigüedades entre contextos y prompts

- **Endurecer el manejo del contexto de embudo**
  - si `etapa_actual` viene `null` pero la metadata indica una etapa válida, definir una regla de reconciliación

## Prioridad media

- **Agregar tracing por nodo de LangGraph**
  - tiempo por nodo
  - inputs y outputs resumidos
  - errores por transición

- **Crear un flujo multi-agente productivo explícito**
  - separar formalmente agente de embudo y agente procesador
  - compartir estado de manera controlada

- **Mover benchmarks históricos a una zona de research o archive**
  - hoy siguen en `benchmarks/`
  - si ya no se usan activamente, pueden reubicarse luego

- **Alinear la documentación con ejemplos reales de negocio**
  - requests de embudo
  - requests con MCP reales
  - ejemplos de trazabilidad

## Prioridad baja

- **Separar utilidades operativas de benchmarks**
  - `scripts/` para utilidades Python del stack principal
  - `benchmarks/` solo para investigación

- **Agregar tests automáticos básicos**
  - schemas
  - rutas
  - construcción del grafo
  - tool loading con mocks

- **Preparar carpeta de runbooks operativos**
  - reinicio local
  - validación de variables
  - troubleshooting MCP
  - troubleshooting OpenRouter

## Recomendación de ejecución

Orden sugerido:

1. medir `tool_execution_ms`
2. robustecer errores/retries
3. formalizar flujo multi-agente productivo
4. agregar tracing fino
5. archivar comparativos históricos cuando ya no hagan falta
