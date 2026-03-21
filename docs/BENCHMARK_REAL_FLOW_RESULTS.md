# Benchmark final documentado: flujo real de embudo + procesador

## Parámetros de la prueba

- **`contacto_id`**: `328159`
- **`empresa_id`**: `2`
- **`agente_id`**: `4`
- **`conversacion_id`**: `63380`
- **`limite_mensajes`**: `20`
- **`modelo`**: `x-ai/grok-4.1-fast`

## Objetivo

Comparar `LangGraph` vs `Vercel AI SDK` en un flujo real y documentado con estas condiciones:

- **Agente de embudo real**
  - sin tools
  - `system prompt` construido con la Edge Function `obtener-contexto-completo-v1`
  - `user message` construido con el historial completo de la conversación

- **Agente procesador normalizado**
  - con tools async
  - ejecución concurrente con el agente de embudo
  - mismas 3 tools en ambos frameworks
  - misma intención de salida

## Archivos base generados

- **`artifacts/benchmark_documented_langgraph.json`**
  - contiene prompts completos, contexto completo, respuestas y trazas de `LangGraph`

- **`artifacts/benchmark_documented_vercel.json`**
  - contiene prompts completos, contexto completo, respuestas y trazas de `Vercel AI SDK`

## Resumen del contexto real recuperado

La Edge Function devolvió contexto suficiente para inferir el estado del contacto:

- **Contacto**
  - `Oscar Eduardo Cumbe Tribiño`
  - teléfono `573123944451`
  - email `edcumbe79@gmail.com`
  - ciudad `Chía Cundinamarca`

- **Contexto comercial relevante**
  - deuda total aprox: `113 millones COP`
  - cita virtual agendada para `2026-03-20 20:30`
  - cita no realizada
  - reacción final del prospecto:
    - `Si así de serios son para una cita como serán para llevar el caso, gracias no me agenden`

- **Metadata relevante ya registrada**
  - etapa 4: cita no realizada por impaciencia / percepción de falta de seriedad
  - etapa 8: razón de pérdida registrada

## Hallazgo importante del contexto

- **Inconsistencia en el payload de embudo**
  - `contexto_embudo.data.etapa_actual` vino como `null`
  - pero `informacion_contacto.etapa_actual_orden` vino como `223`
  - y la metadata ya sugiere claramente un estado de **Prospecto Perdido**

Esto explica por qué el prompt dice `Sin etapa asignada`, pero los agentes igual logran inferir correctamente que el contacto está perdido por el historial y la metadata.

## Qué lleva el `system prompt` del agente de embudo

El prompt completo exacto está guardado en ambos JSON de artifacts. Su estructura real incluye:

- **`# IDENTIDAD Y MISIÓN`**
  - analista conversacional de etapas del embudo

- **`## Objetivos`**
  - identificar etapa actual
  - actualizar etapa con `actualizar-etapa-embudo`
  - registrar información con `actualizar-metadata-v1`

- **`# Datos claves`**
  - nombre del contacto
  - etapa actual renderizada desde contexto

- **`# CONTEXTO DEL EMBUDO`**
  - JSON completo de etapas disponibles
  - JSON completo de metadata/contexto actual del embudo

- **`# HERRAMIENTAS`**
  - instrucciones de uso para `update_etapa_embudo`
  - instrucciones de uso para `update-metadata`
  - reglas para `informacion_capturada`
  - reglas de merge
  - reglas JSON

- **`## CHECKLIST FINAL`**
  - validaciones antes de responder

- **`Contexto temporal`**
  - fecha/hora actual
  - semana, mes y año
  - próximos 7 días

- **`Output esperado`**
  - respuesta interna de máximo 3 líneas
  - no responder al prospecto

## Qué lleva el `user message` del agente de embudo

El `user message` también quedó guardado íntegro en los artifacts. Su contenido real es:

- **`Historial de la conversación`**
  - `id`, `empresa_id`, `agente_id`, `contacto_id`
  - canal
  - total de mensajes
  - últimos 20 mensajes completos
  - alternancia agente/usuario
  - texto literal del rechazo final del prospecto

## Prompt del procesador normalizado

El procesador se normalizó para ambos frameworks con estas reglas:

- analizar el contacto y la situación actual
- llamar exactamente una vez a:
  - `get_processing_policy`
  - `get_conversation_summary`
  - `get_followup_constraints`
- responder en máximo 3 líneas para el equipo interno

## Tools del procesador

Las 3 tools async usadas en ambos lados fueron:

- **`get_processing_policy`**
  - prioridad `P1`
  - acción preferida `ofrecer llamada`
  - escalamiento si no responde en `4h`

- **`get_conversation_summary`**
  - interés alto
  - pidió precios
  - posible demostración
  - objeciones: `tiempo`, `comparación con competencia`
  - sentiment `positivo`

- **`get_followup_constraints`**
  - canales permitidos: `whatsapp`, `llamada`
  - ventana preferida: `09:00-12:00`

## Respuesta del agente de embudo

### LangGraph

```text
Prospecto Oscar Eduardo Cumbe Tribiño permanece en Prospecto Perdido (ID 223) tras rechazo explícito por impaciencia en cita.
Metadata ya registrada en etapa_4 y etapa_8 (razón: percepción de falta de seriedad).
No hay nuevos datos ni cambio de etapa; ok.
```

### Vercel AI SDK

```text
ok. Prospecto permanece en "Prospecto Perdido" (ID 223). Metadata ya capturada con rechazo por impaciencia y duda de seriedad; sin cambios nuevos.
```

## Respuesta del agente procesador

### LangGraph

```text
Prioridad: P1 (alta). Acción: Ofrecer llamada reactivación disculpándonos por cita fallida, resaltar calificación (113M deudas) y congelamiento intereses; proponer lunes 09:00-12:00. Canal: WhatsApp/llamada (prioridad WhatsApp).
```

### Vercel AI SDK

```text
Contacto: Oscar Eduardo Cumbe Tribiño (ID:328159, etapa: Sin etapa asignada).
Prioridad: P1 (interés alto, objeciones: tiempo). Sentiment positivo pese a frustración por cita fallida.
Acción operativa: Ofrecer llamada reactivadora vía WhatsApp hoy 09:00-12:00 (canal pref.), enfatizar seriedad y congelamiento intereses para recuperar confianza. Escalada si no reply en 4h.
```

## Qué hizo cada framework

### LangGraph

- **Embudo**
  - no llamó tools
  - 1 paso
  - resolvió el caso solo con prompt + historial + metadata

- **Procesador**
  - 2 pasos
  - llamó exactamente 1 vez cada tool
  - tools llamadas:
    - `get_processing_policy`
    - `get_conversation_summary`
    - `get_followup_constraints`

### Vercel AI SDK

- **Embudo**
  - no llamó tools
  - 1 paso
  - resolvió correctamente el estado perdido

- **Procesador**
  - 2 fases normalizadas
    - fase 1: tool calling
    - fase 2: respuesta final
  - llamó exactamente 1 vez cada tool
  - tools llamadas:
    - `get_processing_policy`
    - `get_conversation_summary`
    - `get_followup_constraints`

## Tiempos de la corrida documentada

### LangGraph

- **Workflow total**: `8973.5ms`
- **Embudo**: `5493.9ms`
- **Procesador**: `8955.8ms`

### Vercel AI SDK

- **Workflow total**: `7671.3ms`
- **Embudo**: `5715.5ms`
- **Procesador**: `7670.9ms`

## Promedios medidos

### LangGraph

- **Workflow avg**: `9499.8ms`
- **Embudo avg**: `5552.4ms`
- **Procesador avg**: `9472.5ms`
- **Tools procesador avg**: `745.2ms`

### Vercel AI SDK

- **Workflow avg**: `9392.5ms`
- **Embudo avg**: `6635.1ms`
- **Procesador avg**: `9392.1ms`
- **Tools procesador avg**: `738.8ms`

## Lectura comparativa

- **Workflow total**
  - `Vercel AI SDK` quedó apenas más rápido en promedio por aproximadamente `107ms`
  - la diferencia es pequeña y está cerca del ruido normal de red/modelo

- **Embudo**
  - `LangGraph` fue más rápido en promedio
  - delta aprox a favor de `LangGraph`: `~1082.7ms`

- **Procesador**
  - ambos quedaron prácticamente empatados
  - delta aprox: `~80ms` a favor de `Vercel AI SDK`

- **Tools async**
  - el tiempo puro de tools fue casi idéntico entre frameworks
  - esto sugiere que el costo fuerte sigue estando en la inferencia del modelo, no en el runtime local

## Hallazgos principales

- **El embudo real sí clasifica correctamente el lead como perdido**
  - aunque `etapa_actual` vino `null`, ambos agentes concluyeron que el contacto sigue en **Prospecto Perdido**

- **La metadata ya venía suficientemente rica**
  - no había necesidad real de tools en el embudo para esta prueba
  - por eso la respuesta correcta fue esencialmente `ok / sin cambios`

- **El procesador quedó consistente en ambos frameworks**
  - ambos priorizaron reactivación
  - ambos recomendaron contacto por `WhatsApp/llamada`
  - ambos enfatizaron el problema de confianza por la cita fallida

- **`Vercel AI SDK` necesitó una normalización explícita para el procesador**
  - sin esa normalización, el flujo podía quedarse cerrando en `tool-calls`
  - se resolvió separando la ejecución en:
    - fase de tools
    - fase final de respuesta

- **`LangGraph` fue más robusto para el loop agente -> tools -> respuesta final**
  - en esta prueba no necesitó esa normalización adicional

- **El tiempo total final quedó prácticamente empatado**
  - con ventaja marginal para `Vercel AI SDK` en esta versión normalizada
  - pero la diferencia no es lo suficientemente grande como para justificar por sí sola un cambio de stack

## Observaciones finas

- **Discrepancia de identificador en el procesador**
  - `LangGraph` llamó sus tools con `lead_id = 63380`
  - `Vercel AI SDK` llamó sus tools con `leadId = 328159`
  - esto pasó porque el contexto contiene tanto `contacto_id` como `conversacion_id`
  - para producción conviene fijar explícitamente el identificador correcto en el prompt o en el schema de tools

- **El mejor insight funcional no fue de velocidad sino de consistencia de contexto**
  - si el payload de embudo entregara `etapa_actual` correctamente resuelta, el embudo sería todavía más confiable y simple de interpretar

## Recomendación

- **Si tu prioridad es control fino del loop de agentes y tools**
  - `LangGraph` sigue siendo una muy buena base

- **Si tu prioridad es una implementación compacta y directa**
  - `Vercel AI SDK` también funciona bien, siempre que normalices el cierre del flujo con tools

- **Para producción**
  - no decidiría el stack solo por estos milisegundos
  - decidiría por:
    - ergonomía del control de agentes
    - trazabilidad
    - facilidad para tools reales
    - facilidad de observabilidad

## Estado final

- **Benchmark documentado real**: completado
- **Resultados crudos JSON**: generados
- **Reporte de hallazgos**: generado
