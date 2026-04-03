# Sofia Agent — Monica Intelligence v2

Sistema de inteligencia artificial multi-agente para **Urpe AI Lab**. Agente de ventas de **Monica Intelligence** (Ecosistemas Comerciales Inteligentes) desplegado en Railway con WhatsApp via Kapso.

**Produccion:** sofia-agent-production-44b2.up.railway.app
**WhatsApp:** +1 205-701-0979
**Supabase:** mkzlcgrqbukwehiulqfn (schema v2 dimensional)

## Stack principal

- **API**: `FastAPI` (Python 3.11)
- **Orquestacion de agentes**: `LangGraph`
- **Provider LLM**: `OpenRouter` (x-ai/grok-4.1-fast)
- **Herramientas dinamicas**: `MCP servers`
- **Base de datos**: `Supabase` (schema v2: dim_*/fact_*/analytics_*)
- **Canal de mensajeria**: `Kapso (WhatsApp)`
- **Email**: `Nylas`
- **Deploy**: `Railway` (auto-deploy desde main)

## Contexto recomendado antes de desarrollar

Leer primero:

1. **`docs/PROJECT_CONTEXT.md`** — fuente de verdad del proyecto
2. **`docs/architecture/OVERVIEW.md`** — arquitectura y componentes
3. **`docs/API_ENDPOINTS.md`** — referencia de endpoints HTTP
4. **`app/agents/conversational.py`** — agente principal
5. **`app/api/kapso_routes.py`** — flujo de mensajes WhatsApp
6. **`app/db/queries.py`** — queries con mappers v2 (get_empresa, get_agente)
7. **`app/core/kapso_prompt.py`** — construccion del system prompt

## Cambios recientes (3 abril 2026)

### Bugs corregidos
- **`is_whatsapp` UnboundLocalError** — Variable usada antes de definirse en `conversational.py:749`. Crasheaba todos los mensajes inbound.
- **401 OpenRouter** — `OPENROUTER_API_KEY` en Railway tenia valor invalido. Actualizada con key correcta `sk-or-v1-...`.
- **`agent_memory` tabla inexistente** — `/borrar` y `/borrar2` crasheaban con 500 porque la tabla no existe en schema v2. `delete_agent_memory()` ahora maneja el error gracefully.

### Mappers schema v2
- **`get_empresa()`** — Aplana `dim_enterprise.settings` JSON a campos planos esperados por `kapso_prompt.py` (informacion_empresarial, servicios_generales, preguntas_frecuentes, embudo_ventas, reglas_negocio).
- **`get_agente()`** — Extrae config de `dim_agent.system_prompt` JSON a campos planos (instrucciones, comportamiento, restricciones, formato_respuesta, etc.).

### Sistema de prompt hash
- Calcula SHA-256 del system prompt en cada mensaje.
- Guarda hash junto a cada turno de memoria.
- Si las instrucciones cambian, descarta la memoria vieja automaticamente.
- Sofia arranca fresco con instrucciones actualizadas sin necesidad de `/borrar2`.

### Datos comerciales
- `dim_enterprise.settings` poblado con info completa de Monica Intelligence: 5 dimensiones, catalogo de servicios con pricing, FAQs, embudo de ventas, reglas de negocio.
- `dim_agent.system_prompt` actualizado con restricciones, battle cards, manejo de objeciones, resultados reales de clientes.

## Arquitectura

```text
Kapso (WhatsApp)
  -> kapso-bridge (Node.js)
  -> FastAPI
       -> Agente Conversacional (LangGraph + MCP)   [principal]
       -> Agente de Embudo     (LangGraph)           [paralelo]
       -> Agente Contacto      (LangGraph)           [paralelo]
  -> Supabase (BD)
  -> OpenRouter (LLM)
```

## Agentes disponibles

| Agente | Archivo | Endpoint |
|--------|---------|----------|
| Conversacional | `app/agents/conversational.py` | `POST /api/v1/chat` |
| Embudo (Funnel) | `app/agents/funnel.py` | `POST /api/v1/funnel/analyze` |
| Actualización de Contacto | `app/agents/contact_update.py` | Interno (desde Kapso) |

## Estructura del Proyecto

```text
├── main.py                              # Punto de entrada FastAPI
├── requirements.txt                     # Dependencias Python
├── package.json                         # Utilidades Node (kapso-bridge + benchmarks)
├── docker-compose.yml                   # Configuración Docker
├── Dockerfile
├── railway-start.sh                     # Script de inicio en Railway
├── nixpacks.toml                        # Config Railway
├── .env.example
│
├── app/
│   ├── api/
│   │   ├── routes.py                   # Endpoint principal de chat
│   │   ├── funnel_routes.py            # Endpoints funnel agent + debug dashboard
│   │   ├── kapso_routes.py             # Endpoints WhatsApp/Kapso
│   │   ├── scheduling_routes.py        # Endpoints de agendamiento
│   │   ├── graph_routes.py             # Endpoints debug del grafo
│   │   ├── db_routes.py                # Endpoints utilitarios de BD
│   │   └── debug_dashboard.py          # Dashboard de debug conversacional
│   │
│   ├── agents/
│   │   ├── conversational.py           # Agente conversacional (LangGraph + MCP)
│   │   ├── funnel.py                   # Agente de embudo
│   │   └── contact_update.py           # Agente de actualización de contacto
│   │
│   ├── core/
│   │   ├── config.py                   # Configuración central (variables de entorno)
│   │   ├── cache.py                    # Cache de respuestas con TTL de 5 min
│   │   ├── error_webhook.py            # Notificaciones de error via webhook
│   │   ├── funnel_debug.py             # Buffer circular de debug del funnel agent
│   │   ├── kapso_debug.py              # Utilidades de debug de Kapso
│   │   └── kapso_prompt.py             # System prompts de Kapso
│   │
│   ├── db/
│   │   ├── client.py                   # Cliente Supabase async con connection pooling
│   │   └── queries.py                  # Funciones de consulta a BD
│   │
│   ├── mcp_client/
│   │   └── client.py                   # Cliente MCP para herramientas dinámicas
│   │
│   ├── nylas_client/
│   │   └── client.py                   # Cliente de email Nylas
│   │
│   ├── schemas/
│   │   ├── chat.py                     # Schemas del agente conversacional
│   │   ├── funnel.py                   # Schemas del funnel agent
│   │   ├── contact_update.py           # Schemas de actualización de contacto
│   │   ├── channel.py                  # Schemas de configuración de canal
│   │   ├── kapso.py                    # Schemas de Kapso/WhatsApp
│   │   └── scheduling.py               # Schemas de agendamiento
│   │
│   └── services/
│       └── channel_adapter.py          # Adaptadores de canal/plataforma
│
├── kapso-bridge/
│   └── server.mjs                      # Bridge Node.js para webhook Kapso
│
├── docs/                               # Documentación
│   ├── PROJECT_CONTEXT.md
│   ├── API_ENDPOINTS.md
│   ├── FUNNEL_AGENT.md
│   ├── FUNNEL_DEBUG_DASHBOARD.md
│   ├── AGENT_TESTING_PROTOCOL.md
│   ├── BENCHMARK_REAL_FLOW_RESULTS.md
│   ├── RAILWAY_KAPSO_DEPLOY.md
│   ├── NEXT_STEPS.md
│   └── architecture/OVERVIEW.md
│
├── scripts/                            # Scripts de benchmark y utilidades
│   ├── benchmark_parallel_langgraph.py
│   ├── documented_real_flow_langgraph.py
│   └── test_funnel_agent.py
│
└── benchmarks/                         # Benchmarks comparativos históricos (Vercel AI SDK)
```

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Crear `.env` a partir de `.env.example`:

```env
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DEFAULT_MODEL=x-ai/grok-4.1-fast

SUPABASE_URL=https://...supabase.co
SUPABASE_SERVICE_KEY=...
SUPABASE_EDGE_FUNCTION_URL=https://...supabase.co/functions/v1
SUPABASE_EDGE_FUNCTION_TOKEN=...   # opcional

KAPSO_API_KEY=...
KAPSO_WEBHOOK_SECRET=...
KAPSO_INTERNAL_TOKEN=...
KAPSO_BASE_URL=https://api.kapso.ai/meta/whatsapp
INTERNAL_AGENT_API_URL=http://127.0.0.1:8000/api/v1/kapso/inbound
```

## Ejecución

```bash
# Solo el backend Python
python main.py

# Con el bridge de Kapso (en otra terminal)
npm run kapso:bridge
```

La API estará disponible en `http://localhost:8080`

- Swagger UI: `http://localhost:8080/docs`
- Documentación de endpoints: `docs/API_ENDPOINTS.md`
- Debug funnel: `http://localhost:8080/api/v1/funnel/debug`

## Ejemplos rápidos

### Chat conversacional

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres un asistente de ventas de la empresa X.",
    "message": "¿Cuáles son los productos disponibles?",
    "max_tokens": 512
  }'
```

### Chat con MCP servers

```bash
curl -X POST http://localhost:8080/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{
    "system_prompt": "Eres el asistente de Marketia.",
    "message": "Busca los clientes activos",
    "mcp_servers": [
      { "url": "https://marketia.app.n8n.cloud/mcp/aa0f6b46-...", "name": "marketia-crm" }
    ]
  }'
```

### Análisis de embudo

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

## Optimizaciones

- **Cache TTL 5 min** → 0.5ms hit vs ~4s full
- **Connection pooling HTTP/2** → 20 conexiones máx, 10 keep-alive
- **Carga paralela MCP tools** → `asyncio.gather`
- **Carga paralela contexto funnel** → 4 queries simultáneas
- **Cache de instancias LLM** por modelo + parámetros
- **Timeout defensivo** en ejecución del grafo y discovery MCP
- **Límite de iteraciones** en agentes para evitar loops infinitos
- **Retry automático** de mensajes atascados cada 10 minutos (background task)

## Despliegue

Ver `docs/RAILWAY_KAPSO_DEPLOY.md` para instrucciones de Railway.

## Benchmarks históricos

Los benchmarks comparativos con `Vercel AI SDK` se conservan en `benchmarks/` como referencia histórica. El stack de producción es `LangGraph`. Ver `docs/BENCHMARK_REAL_FLOW_RESULTS.md` para resultados.
