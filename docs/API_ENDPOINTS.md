# URPE AI Lab - Documentación de Endpoints

**Base URL:** `http://localhost:8080`
**Swagger UI:** `http://localhost:8080/docs`
**ReDoc:** `http://localhost:8080/redoc`

**Documentos relacionados:**

- `docs/PROJECT_CONTEXT.md`
- `docs/architecture/OVERVIEW.md`
- `docs/FUNNEL_AGENT.md`
- `docs/RAILWAY_KAPSO_DEPLOY.md`

---

## 1. GET `/`

**Descripción:** Información general del servicio.

### Response `200 OK`

```json
{
    "service": "URPE AI Lab - Multi-Agent System",
    "version": "1.0.0",
    "docs": "/docs",
    "endpoints": {
        "chat": "/api/v1/chat",
        "health": "/api/v1/health",
        "funnel": "/api/v1/funnel/analyze",
        "funnel_debug": "/api/v1/funnel/debug",
        "kapso_inbound": "/api/v1/kapso/inbound",
        "db_health": "/api/v1/db/health"
    }
}
```

---

## 2. GET `/api/v1/health`

**Descripción:** Health check del servicio.

### Response `200 OK`

```json
{
    "status": "ok",
    "service": "urpe-multiagent",
    "cache_size": 0
}
```

---

## 3. POST `/api/v1/chat`

**Descripción:** Agente conversacional genérico. Recibe system prompt y mensaje, ejecuta el agente LangGraph con herramientas MCP opcionales.

**Content-Type:** `application/json`

### Request Body

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `system_prompt` | `string` | Sí | System prompt que define el comportamiento del agente |
| `message` | `string` | Sí | Mensaje del usuario |
| `model` | `string` | No | Modelo via OpenRouter. Default: `x-ai/grok-4.1-fast` |
| `mcp_servers` | `array[MCPServerConfig]` | No | Servidores MCP para herramientas dinámicas |
| `conversation_id` | `string` | No | ID de conversación para mantener contexto |
| `max_tokens` | `integer` | No | Máximo tokens en la respuesta. Default: `1024` |
| `temperature` | `float` | No | Temperatura del modelo (0-2). Default: `0.7` |

#### MCPServerConfig

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `url` | `string` | Sí | URL del MCP server |
| `name` | `string` | No | Nombre identificador del server |

### Response `200 OK`

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `response` | `string` | Respuesta del agente |
| `conversation_id` | `string` | ID de la conversación |
| `model_used` | `string` | Modelo LLM utilizado |
| `tools_used` | `array[ToolCall]` | Herramientas ejecutadas |
| `timing` | `TimingInfo` | Métricas de tiempo por fase |
| `agent_runs` | `array[AgentRunTrace]` | Trazas de ejecución del agente |

#### ToolCall

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `tool_name` | `string` | Nombre de la herramienta |
| `tool_input` | `object` | Parámetros enviados |
| `tool_output` | `string` | Resultado (truncado a 500 chars) |
| `duration_ms` | `float` | Duración de la ejecución |
| `status` | `string` | `ok` o `error` |

#### TimingInfo

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `total_ms` | `float` | Tiempo total |
| `llm_ms` | `float` | Tiempo de inferencia LLM |
| `mcp_discovery_ms` | `float` | Tiempo de descubrimiento MCP |
| `graph_build_ms` | `float` | Tiempo de compilación del grafo |
| `tool_execution_ms` | `float` | Tiempo de ejecución de tools |

### Response `500 Internal Server Error`

```json
{ "detail": "Error procesando la solicitud: <descripción>" }
```

### Ejemplos

#### Chat simple

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres un asistente de ventas de TechCorp.",
    "message": "¿Cuáles son los planes de precios?"
  }'
```

#### Chat con MCP servers

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres el asistente de Marketia. Tienes acceso al CRM.",
    "message": "Busca los clientes activos del último mes",
    "mcp_servers": [
      {
        "url": "https://marketia.app.n8n.cloud/mcp/aa0f6b46-ba2f-urpe-Monica",
        "name": "marketia-crm"
      }
    ]
  }'
```

#### Chat con modelo y tokens específicos

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres un asistente técnico experto en Python.",
    "message": "¿Cómo uso async/await?",
    "model": "openai/gpt-4o",
    "max_tokens": 512
  }'
```

---

## 4. POST `/api/v1/funnel/analyze`

**Descripción:** Ejecuta el funnel agent. Analiza el estado del contacto en el embudo comercial y actualiza etapa y/o metadata si corresponde.

**Content-Type:** `application/json`

### Request Body

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `contacto_id` | `integer` | Sí | ID del contacto en Supabase |
| `empresa_id` | `integer` | Sí | ID de la empresa |
| `agente_id` | `integer` | Sí | ID del agente |
| `conversacion_id` | `integer` | No | ID de conversación específica |
| `model` | `string` | No | Ignorado: el agente usa `x-ai/grok-4.1-fast` fijo |
| `max_tokens` | `integer` | No | Default: `512` |
| `temperature` | `float` | No | Default: `0.5` |

### Response `200 OK`

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `success` | `boolean` | Resultado de la ejecución |
| `respuesta` | `string` | Análisis del agente (máx 3 líneas) |
| `etapa_anterior` | `string` | Nombre de la etapa antes del análisis |
| `etapa_nueva` | `integer` | Número de la nueva etapa (si cambió) |
| `metadata_actualizada` | `object` | Metadata registrada (si aplica) |
| `tools_used` | `array[ToolCall]` | Herramientas ejecutadas |
| `timing` | `TimingInfo` | Métricas de tiempo |
| `agent_runs` | `array[AgentRunTrace]` | Trazas del agente |
| `error` | `string` | Descripción del error (si `success=false`) |

### Ejemplo

```bash
curl -X POST http://localhost:8080/api/v1/funnel/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "contacto_id": 1234,
    "empresa_id": 5,
    "agente_id": 10,
    "conversacion_id": 999
  }'
```

**Response:**

```json
{
  "success": true,
  "respuesta": "Lead escalado a Calificado por presupuesto confirmado. Próximo paso: agendar demo técnica.",
  "etapa_anterior": "Interesado",
  "etapa_nueva": 3,
  "metadata_actualizada": { "info_reg_1": "2026-03-31", "info_reg_2": "Presupuesto: USD 50k+" },
  "tools_used": [
    { "tool_name": "update_etapa_embudo", "status": "ok", "duration_ms": 45.2 },
    { "tool_name": "update_metadata", "status": "ok", "duration_ms": 198.7 }
  ],
  "timing": { "total_ms": 1450.2, "llm_ms": 950.0, "tool_execution_ms": 241.2, "graph_build_ms": 45.3 }
}
```

---

## 5. GET `/api/v1/funnel/debug`

**Descripción:** Dashboard HTML con las últimas 50 ejecuciones del funnel agent. Muestra estadísticas, timing, herramientas usadas y cambios de etapa.

**Acceso directo:** `http://localhost:8080/api/v1/funnel/debug`

---

## 6. GET `/api/v1/funnel/debug/events`

**Descripción:** Eventos de debug del funnel agent en formato JSON.

**Query params:** `?limit=20` (default: 50)

### Response `200 OK`

```json
{
  "runs": [
    {
      "timestamp": "2026-03-31T15:30:00+00:00",
      "contacto_id": 1234,
      "empresa_id": 5,
      "success": true,
      "respuesta": "...",
      "etapa_anterior": "Interesado",
      "etapa_nueva": 3,
      "timing": { "total_ms": 1450.2 },
      "tools_used": [...]
    }
  ],
  "stats": {
    "total_runs": 45,
    "successful": 43,
    "failed": 2,
    "avg_duration_ms": 2850.5
  }
}
```

---

## 7. POST `/api/v1/kapso/inbound`

**Descripción:** Endpoint interno para el bridge de Kapso. Recibe mensajes de WhatsApp, ejecuta los agentes en paralelo y devuelve la respuesta al bridge.

**Autenticación:** Token interno (`KAPSO_INTERNAL_TOKEN` en header)

> No consumir directamente. Este endpoint es llamado por `kapso-bridge/server.mjs`.

---

## 8. GET `/api/v1/db/health`

**Descripción:** Verifica la conectividad a Supabase.

### Response `200 OK`

```json
{ "status": "ok", "supabase": "connected", "empresas_count": 28 }
```

---

## 9. Endpoints de consulta `/api/v1/db/*`

Endpoints utilitarios de lectura sobre Supabase. Útiles para inspección y debugging.

### Empresa

- `GET /api/v1/db/empresa/{empresa_id}`
- `GET /api/v1/db/empresa/{empresa_id}/agentes`
- `GET /api/v1/db/empresa/{empresa_id}/embudo`
- `GET /api/v1/db/empresa/{empresa_id}/team`

### Agente

- `GET /api/v1/db/agente/{agente_id}`
- `GET /api/v1/db/agente/{agente_id}/tools`

### Contacto

- `GET /api/v1/db/contacto/{contacto_id}`
- `GET /api/v1/db/contacto/buscar/telefono?telefono=...&empresa_id=...`

### Conversación

- `GET /api/v1/db/conversacion/{conversacion_id}/mensajes?limit=20`

### Número / canal

- `GET /api/v1/db/numero/{numero_id}`

---

## 10. DELETE `/api/v1/cache`

**Descripción:** Limpia el cache de respuestas en memoria.

### Response `200 OK`

```json
{ "status": "ok", "message": "Cache limpiado" }
```

---

## 11. Modelos disponibles (OpenRouter)

El campo `model` acepta cualquier modelo de [OpenRouter](https://openrouter.ai/models). Ejemplos comunes:

| Modelo | ID |
|--------|----|
| Grok 4.1 Fast (default) | `x-ai/grok-4.1-fast` |
| GPT-4o | `openai/gpt-4o` |
| GPT-4o Mini | `openai/gpt-4o-mini` |
| Claude 3.5 Sonnet | `anthropic/claude-3.5-sonnet` |
| Llama 3.1 70B | `meta-llama/llama-3.1-70b-instruct` |
| Gemini Pro 1.5 | `google/gemini-pro-1.5` |

---

## 12. Variables de entorno

| Variable | Requerida | Descripción |
|----------|-----------|-------------|
| `OPENROUTER_API_KEY` | Sí | API key de OpenRouter |
| `OPENROUTER_BASE_URL` | No | Default: `https://openrouter.ai/api/v1` |
| `DEFAULT_MODEL` | No | Default: `x-ai/grok-4.1-fast` |
| `SUPABASE_URL` | Sí | URL del proyecto Supabase |
| `SUPABASE_SERVICE_KEY` | Sí | Service role key de Supabase |
| `SUPABASE_EDGE_FUNCTION_URL` | Sí | URL base de las Edge Functions |
| `SUPABASE_EDGE_FUNCTION_TOKEN` | No | Token de autorización para Edge Functions |
| `KAPSO_API_KEY` | Sí (Kapso) | API key de Kapso |
| `KAPSO_WEBHOOK_SECRET` | Sí (Kapso) | Secret para validar webhooks de Kapso |
| `KAPSO_INTERNAL_TOKEN` | Sí (Kapso) | Token interno bridge → FastAPI |
| `KAPSO_BASE_URL` | No | Default: `https://api.kapso.ai/meta/whatsapp` |
| `INTERNAL_AGENT_API_URL` | Sí (Kapso) | URL del endpoint interno de Kapso |
| `ERROR_WEBHOOK_URL` | No | URL para notificaciones de errores HTTP 500+ |
| `APP_NAME` | No | Nombre del servicio en FastAPI |
| `DEBUG` | No | Activa modo debug |

---

## 13. Códigos de Estado HTTP

| Código | Descripción |
|--------|-------------|
| `200` | Solicitud exitosa |
| `422` | Error de validación (campos faltantes o formato inválido) |
| `500` | Error interno del servidor |
