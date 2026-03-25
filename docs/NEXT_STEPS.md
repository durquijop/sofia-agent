# Próximos Pasos Recomendados

## Prioridad alta

- **Fortalecer observabilidad del runtime productivo**
  - medir `tool_execution_ms` real por agente
  - agregar métricas por fase en Kapso inbound y scheduling
  - unificar logs entre bridge y FastAPI con correlación por request o mensaje

- **Definir una estrategia clara de errores y retries**
  - timeout por tool
  - retries controlados
  - fallbacks más explícitos por integración externa
  - mensajes internos más accionables para debug operativo

- **Endurecer el flujo de scheduling con Nylas**
  - exponer mejor telemetría de grants inhabilitados
  - definir política de recuperación manual o automática
  - validar comportamientos al reiniciar el proceso

- **Unificar identificadores internos para tools y flujos**
  - definir si las tools internas trabajan con `contacto_id`, `conversacion_id` o ambos
  - evitar ambigüedades entre prompt, DB y automatizaciones

## Prioridad media

- **Agregar tracing por nodo de LangGraph**
  - tiempo por nodo
  - inputs y outputs resumidos
  - errores por transición

- **Crear runbooks operativos reales**
  - arranque local FastAPI directo
  - arranque local completo con bridge
  - troubleshooting Kapso
  - troubleshooting n8n
  - troubleshooting Nylas

- **Alinear la documentación con ejemplos reales de negocio**
  - requests de embudo
  - requests reales de scheduling
  - ejemplos de debug y trazabilidad

- **Revisar si benchmarks históricos deben pasar a `archive/`**
  - hoy siguen siendo referencia útil
  - pero no deben competir con la documentación operativa principal

## Prioridad baja

- **Agregar tests automáticos básicos**
  - schemas
  - rutas
  - construcción del grafo
  - scheduling con mocks de Nylas
  - tool loading con mocks

- **Separar mejor utilidades operativas y research**
  - `scripts/` para utilidades del runtime principal
  - `benchmarks/` para investigación histórica

- **Formalizar flujos multi-agente más explícitos**
  - separar mejor responsabilidades entre funnel, contact update y conversacional
  - compartir estado de forma controlada

## Recomendación de ejecución

Orden sugerido:

1. observabilidad y correlación de logs
2. robustecer errores/retries/fallbacks
3. endurecer scheduling y Nylas
4. agregar tracing fino por nodo
5. mover research histórica si deja de aportar al día a día
