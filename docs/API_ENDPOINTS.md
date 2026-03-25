# URPE AI Lab - Documentación de Endpoints

**Base URL:** `http://localhost:8080`
**Versión:** 1.0.0
**Swagger UI:** `http://localhost:8080/docs`
**ReDoc:** `http://localhost:8080/redoc`

**Documentos relacionados:**

- `docs/PROJECT_CONTEXT.md`
- `docs/architecture/OVERVIEW.md`
- `docs/NEXT_STEPS.md`

---

## 1. GET `/`

**Descripción:** Información general del servicio.

**Autenticación:** No requerida

### Response `200 OK`

```json
{
    "service": "URPE AI Lab - Multi-Agent System",
    "version": "1.0.0",
    "docs": "/docs",
    "endpoints": {
        "chat": "/api/v1/chat",
        "health": "/api/v1/health",
        "db_health": "/api/v1/db/health",
        "db_docs": "/docs#/database"
    }
}
```

---

## 2. GET `/api/v1/health`

**Descripción:** Health check del servicio. Útil para monitoreo y load balancers.

**Autenticación:** No requerida

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

**Descripción:** Endpoint principal del sistema multi-agente. Envía un mensaje al agente configurado con el system prompt de la empresa via OpenRouter. Opcionalmente conecta MCP servers para herramientas dinámicas.

**Autenticación:** No requerida (pendiente de implementar)
**Content-Type:** `application/json`

### Request Body

| Campo | Tipo | Requerido | Descripción |
| ----- | ---- | --------- | ----------- |
| `system_prompt` | `string` | Si | System prompt que define el comportamiento del agente para la empresa |
| `message` | `string` | Si | Mensaje del usuario |
| `model` | `string` | No | Modelo LLM via OpenRouter (ej: `x-ai/grok-4.1-fast`). Default: config en `.env` |
| `mcp_servers` | `array[MCPServerConfig]` | No | Lista de MCP servers para herramientas dinámicas. Default: `[]` |
| `conversation_id` | `string` | No | ID de conversación para mantener contexto. Si no se envía, se genera uno automáticamente |
| `max_tokens` | `integer` | No | Máximo de tokens en la respuesta. Menor = más rápido. Default: `1024` |
| `temperature` | `float` | No | Temperatura del modelo (0-2). Default: `0.7` |

#### Objeto MCPServerConfig

| Campo | Tipo | Requerido | Descripción |
| ----- | ---- | --------- | ----------- |
| `url` | `string` | Si | URL completa del MCP server (ej: `https://marketia.app.n8n.cloud/mcp/aa0f6b46-...`) |
| `name` | `string` | No | Nombre identificador del MCP server. Default: `""` |

### Response 200 OK

| Campo | Tipo | Descripción |
| ----- | ---- | ----------- |
| `response` | `string` | Respuesta generada por el agente |
| `conversation_id` | `string` | ID de la conversación (generado o el enviado en el request) |
| `model_used` | `string` | Modelo LLM que se utilizó |
| `tools_used` | `array[ToolCall]` | Lista de herramientas que el agente usó durante la generación |
| `timing` | `TimingInfo` | Métricas de tiempo de cada fase del procesamiento |

#### Objeto ToolCall

| Campo | Tipo | Descripción |
| ----- | ---- | ----------- |
| `tool_name` | `string` | Nombre de la herramienta ejecutada |
| `tool_input` | `object` | Parámetros enviados a la herramienta |
| `tool_output` | `string` | Resultado devuelto por la herramienta (truncado a 500 chars) |

#### Objeto TimingInfo

| Campo | Tipo | Descripción |
| ----- | ---- | ----------- |
| `total_ms` | `float` | Tiempo total de procesamiento en milisegundos |
| `llm_ms` | `float` | Tiempo de la llamada al LLM en milisegundos |
| `mcp_discovery_ms` | `float` | Tiempo descubriendo herramientas MCP en milisegundos |
| `graph_build_ms` | `float` | Tiempo construyendo el grafo LangGraph en milisegundos |
| `tool_execution_ms` | `float` | Tiempo ejecutando herramientas en milisegundos |

### Response 500 Internal Server Error

```json
{
    "detail": "Error procesando la solicitud: <descripción del error>"
}
```

### Ejemplos

#### Chat simple (sin herramientas)

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres un asistente de ventas de TechCorp. Responde de forma profesional y concisa.",
    "message": "¿Cuáles son los planes de precios?"
  }'
```

**Response:**

```json
{
    "response": "En TechCorp ofrecemos tres planes...",
    "conversation_id": "80715e22-6c6c-4b69-8eff-cf9294cff385",
    "model_used": "x-ai/grok-4.1-fast",
    "tools_used": [],
    "timing": {
        "total_ms": 4200.5,
        "llm_ms": 4050.3,
        "mcp_discovery_ms": 0.0,
        "graph_build_ms": 3.2,
        "tool_execution_ms": 0.0
    }
}
```

#### Chat con modelo específico y max_tokens

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

#### Chat con MCP servers (herramientas dinámicas)

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres el asistente de Marketia. Tienes acceso a herramientas del CRM.",
    "message": "Busca los clientes activos del último mes",
    "mcp_servers": [
      {
        "url": "https://marketia.app.n8n.cloud/mcp/aa0f6b46-ba2f-urpe-Monica",
        "name": "marketia-crm"
      }
    ]
  }'
```

#### Chat con múltiples MCP servers

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres el asistente integral de la empresa.",
    "message": "¿Cuánto debe el cliente Juan Pérez?",
    "mcp_servers": [
      {
        "url": "https://empresa.n8n.cloud/mcp/crm-server",
        "name": "crm"
      },
      {
        "url": "https://empresa.n8n.cloud/mcp/billing-server",
        "name": "facturacion"
      }
    ]
  }'
```

---

## 4. GET `/api/v1/db/health`

**Descripción:** Verifica la conectividad a Supabase.

### Response `200 OK`

```json
{
    "status": "ok",
    "supabase": "connected",
    "empresas_count": 28
}
```

### Response `500 Internal Server Error`

```json
{
    "detail": "Supabase error: <descripción del error>"
}
```

---

## 5. Endpoints de consulta `/api/v1/db/*`

Estos endpoints exponen consultas auxiliares de lectura sobre Supabase.

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

### Códigos comunes

- `200` solicitud exitosa
- `404` recurso no encontrado
- `500` error interno o error de Supabase

---

## 6. DELETE `/api/v1/cache`

**Descripción:** Limpia el cache de respuestas en memoria.

### Response 200 OK

```json
{
    "status": "ok",
    "message": "Cache limpiado"
}
```

---

## 7. Modelos disponibles (OpenRouter)

El campo `model` acepta cualquier modelo disponible en [OpenRouter](https://openrouter.ai/models). Ejemplos:

| Modelo | ID |
| ------ | -- |
| Grok 4.1 Fast | `x-ai/grok-4.1-fast` |
| GPT-4o | `openai/gpt-4o` |
| GPT-4o Mini | `openai/gpt-4o-mini` |
| Claude 3.5 Sonnet | `anthropic/claude-3.5-sonnet` |
| Llama 3.1 70B | `meta-llama/llama-3.1-70b-instruct` |
| Gemini Pro 1.5 | `google/gemini-pro-1.5` |

---

## 8. Variables de Entorno

| Variable | Requerida | Descripción |
| -------- | --------- | ----------- |
| `OPENROUTER_API_KEY` | Si | API key de OpenRouter |
| `OPENROUTER_BASE_URL` | No | URL base de OpenRouter. Default: `https://openrouter.ai/api/v1` |
| `DEFAULT_MODEL` | No | Modelo por defecto. Default: `x-ai/grok-4.1-fast` |
| `SUPABASE_URL` | Si | URL base del proyecto Supabase |
| `SUPABASE_SERVICE_KEY` | Si | Service role key usada por endpoints y benchmarks que consumen contexto |
| `APP_NAME` | No | Nombre visible del servicio FastAPI |
| `DEBUG` | No | Activa comportamiento de depuración |

---

## 9. Códigos de Estado HTTP

| Código | Descripción |
| ------ | ----------- |
| `200` | Solicitud exitosa |
| `422` | Error de validación (campos requeridos faltantes o formato inválido) |
| `500` | Error interno del servidor |
