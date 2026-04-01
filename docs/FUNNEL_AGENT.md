# Agente de Embudo (Funnel Agent)

## Resumen

El **Funnel Agent** analiza el estado del contacto en el embudo comercial y ejecuta acciones automáticamente:

- Detecta si el contacto cambió de etapa y actualiza la BD
- Registra información capturada en la conversación como metadata
- Responde con un resumen de máximo 3 líneas para uso interno del equipo

Se ejecuta en paralelo con el agente conversacional cada vez que entra un mensaje de WhatsApp.

---

## Endpoint

```
POST /api/v1/funnel/analyze
```

### Request

```json
{
  "contacto_id": 1234,
  "empresa_id": 5,
  "agente_id": 10,
  "conversacion_id": 999,
  "max_tokens": 512,
  "temperature": 0.5
}
```

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `contacto_id` | `int` | Sí | ID del contacto |
| `empresa_id` | `int` | Sí | ID de la empresa |
| `agente_id` | `int` | Sí | ID del agente |
| `conversacion_id` | `int` | No | ID de conversación específica |
| `max_tokens` | `int` | No | Default: 512 |
| `temperature` | `float` | No | Default: 0.5 |

> El modelo LLM está fijo en `x-ai/grok-4.1-fast`. El campo `model` en el request es ignorado.

### Response

```json
{
  "success": true,
  "respuesta": "Lead escalado a Calificado por presupuesto confirmado. Próximo paso: agendar demo técnica.",
  "etapa_anterior": "Interesado",
  "etapa_nueva": 3,
  "metadata_actualizada": {
    "info_reg_1": "2026-03-31 10:30 AM",
    "info_reg_2": "¿Cuánto cuesta el tratamiento?",
    "info_reg_3": "Presupuesto: 50000-100000 USD"
  },
  "tools_used": [
    {
      "tool_name": "update_etapa_embudo",
      "tool_input": { "orden_etapa": 3, "razon": "Presupuesto confirmado" },
      "tool_output": "✓ Etapa actualizada a 3: Presupuesto confirmado",
      "duration_ms": 42.5,
      "status": "ok"
    },
    {
      "tool_name": "update_metadata",
      "tool_input": {
        "informacion_capturada": { "info_reg_1": "...", "info_reg_2": "..." },
        "seccion": "etapa_actual"
      },
      "tool_output": "✓ Metadata registrada: 3 campos capturados",
      "duration_ms": 198.7,
      "status": "ok"
    }
  ],
  "timing": {
    "total_ms": 1450.2,
    "llm_ms": 950.0,
    "tool_execution_ms": 241.2,
    "graph_build_ms": 45.3
  }
}
```

---

## Herramientas del agente

### 1. `update_etapa_embudo`

Actualiza la etapa del embudo del contacto en la base de datos.

```python
update_etapa_embudo(orden_etapa: int, razon: str = "Cambio identificado por agente") -> str
```

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `orden_etapa` | `int` | Número de orden de la nueva etapa (ej: 1, 2, 3). Debe ser un valor válido del contexto |
| `razon` | `str` | Razón del cambio de etapa |

**Importante:** `orden_etapa` es el número de orden de la etapa (no su ID en BD). Los valores válidos se extraen del contexto del embudo cargado antes de la ejecución.

**Efecto:** `UPDATE wp_contactos SET etapa_embudo = {orden_etapa} WHERE id = {contacto_id}`

### 2. `update_metadata`

Registra información capturada en la conversación como metadata del contacto.

```python
update_metadata(informacion_capturada: dict, seccion: str = "etapa_actual") -> str
```

| Parámetro | Tipo | Descripción |
|-----------|------|-------------|
| `informacion_capturada` | `dict` | Campos capturados (claves: `info_reg_1`, `info_reg_2`, etc.) |
| `seccion` | `str` | Sección de metadata donde guardar. Default: `"etapa_actual"` |

**Efecto:** POST a Supabase Edge Function `actualizar-metadata-v3` con merge inteligente (preserva datos existentes).

**Formato del payload enviado:**

```json
{
  "contacto_id": 1234,
  "empresa_id": 5,
  "agente_id": 10,
  "metadata": "{\"etapa_actual\": {\"informacion_capturada\": {...}, \"actualizado_en\": \"...\"}}"
}
```

---

## Flujo de ejecución

```text
POST /api/v1/funnel/analyze
         ↓
load_funnel_context() [en paralelo]
  ├─ get_contacto()
  ├─ get_empresa_embudo()
  └─ get_conversacion_con_mensajes()
         ↓
_build_graph() → LangGraph con 2 nodos
         ↓
agent_node → analiza contexto con LLM
         ↓ (si necesita tools)
tool_node → ejecuta update_etapa_embudo y/o update_metadata
         ↓
agent_node → análisis final (máx 2 iteraciones LLM total)
         ↓
FunnelAgentResponse
```

---

## Grafo LangGraph

```text
START → agent
  ├─ si hay tool_calls y < 2 iteraciones → tools → agent
  └─ si no hay tool_calls o >= 2 iteraciones → END
```

---

## Context del sistema (system prompt)

El agente recibe dinámicamente:

- Identidad y misión del analista
- Datos del contacto (nombre, etapa actual)
- Contexto completo del embudo (todas las etapas con nombres y órdenes)
- Metadata existente del contacto
- Historial de la conversación (últimos mensajes)
- Checklist de validaciones antes de responder
- Contexto temporal (fecha, hora)

---

## Validaciones implementadas

- Contacto debe existir en la empresa
- Empresa debe tener embudo configurado
- `orden_etapa` debe ser un valor válido del contexto del embudo
- Metadata usa merge (no sobrescribe datos existentes)
- Respuesta limitada a 3 líneas
- Máximo 2 iteraciones LLM para evitar loops

---

## Rendimiento esperado

| Fase | Tiempo típico |
|------|---------------|
| Carga de contexto (paralela) | 100–300 ms |
| Construcción del grafo | 40–80 ms |
| Inferencia LLM | 800–1500 ms |
| Ejecución de tools | 50–250 ms |
| **Total** | **1–2.5 segundos** |

---

## Debug

Ver las últimas 50 ejecuciones del funnel agent:

```
GET http://localhost:8080/api/v1/funnel/debug
```

Ver eventos en JSON:

```
GET http://localhost:8080/api/v1/funnel/debug/events?limit=20
```

Ver detalles en: `docs/FUNNEL_DEBUG_DASHBOARD.md`

---

## Archivos relevantes

| Archivo | Descripción |
|---------|-------------|
| `app/agents/funnel.py` | Implementación del agente |
| `app/schemas/funnel.py` | Schemas Pydantic de request/response |
| `app/api/funnel_routes.py` | Endpoints FastAPI + debug dashboard |
| `app/db/queries.py` | Funciones de carga de contexto y actualización |
| `app/core/funnel_debug.py` | Buffer de debug en memoria |

---

## Próximas mejoras

- [ ] Integración directa con Kapso para disparo automático por nuevos mensajes
- [ ] Historial de cambios de etapa persistente (audit log en BD)
- [ ] Métricas de conversión por embudo
- [ ] Webhooks para notificar cambios de etapa a sistemas externos
