# AG ENTE DE EMBUDO (Funnel Agent) - Documentación de Implementación

## 📋 Resumen

Se ha implementado un **Agente de Embudo** que ejecuta **de manera asincrónica** y carga el contexto completo del prospecto en paralelo, similar a la arquitectura Deno que proporcionaste.

---

## 🎯 Características Principales

### 1. **Carga de Contexto en Paralelo**
- Contacto + Etapas del embudo + Conversación + Mensajes
- Todo se carga con `asyncio.gather()` para máxima velocidad
- Ubicado en: [app/db/queries.py](app/db/queries.py) → `load_funnel_context()`

### 2. **Agente Asincrónico con LangGraph** 
- Ejecuta análisis del estado del contacto
- Herramientas disponibles:
  - `update_etapa_embudo`: Cambiar la etapa actual
  - `update_metadata`: Registrar información capturada
- Ubicado en: [app/agents/funnel.py](app/agents/funnel.py)

### 3. **Contexto Inteligente**
- **System Prompt**: Incluye identidad, datos claves, historial de conversación y checklist
- **User Message**: Análisis diferenciado por contacto
- Respuesta orientada al **equipo interno** (máx 3 líneas)

### 4. **Endpoint REST**
- **POST** `/api/v1/funnel/analyze`
- Request: `FunnelAgentRequest` (contacto_id, empresa_id, agente_id, etc)
- Response: `FunnelAgentResponse` (respuesta, etapa_anterior, etapa_nueva, timing, agent_runs)

---

## 📁 Archivos Creados/Modificados

### Nuevos Archivos

1. **[app/agents/funnel.py](app/agents/funnel.py)**
   - Agente principal con lógica de embudo
   - Funciones helpers para formatear contexto
   - Grafo LangGraph con nodos: `agent` → `tools`
   - Función principal: `run_funnel_agent()`

2. **[app/schemas/funnel.py](app/schemas/funnel.py)**
   - Schemas Pydantic para request/response
   - `FunnelAgentRequest`, `FunnelAgentResponse`
   - `FunnelContextResponse`, `FunnelStageInfo`, etc

3. **[app/api/funnel_routes.py](app/api/funnel_routes.py)**
   - Endpoint FastAPI para ejecutar agente
   - Ruta: `POST /api/v1/funnel/analyze`

### Archivos Modificados

1. **[app/db/queries.py](app/db/queries.py)**
   - `actualizar_etapa_contacto()` - Actualiza etapa en BD
   - `actualizar_metadata_contacto()` - Actualiza metadata con merge
   - `get_conversacion_con_mensajes()` - Carga en paralelo
   - `load_funnel_context()` - Orquestador principal

2. **[main.py](main.py)**
   - Import del router: `from app.api.funnel_routes import router as funnel_router`
   - Registro en FastAPI: `app.include_router(funnel_router)`
   - Actualización de documentación

---

## 🔄 Flujo de Ejecución

```
POST /api/v1/funnel/analyze
         ↓
FunnelAgentRequest {contacto_id, empresa_id, agente_id, conversacion_id}
         ↓
load_funnel_context() [PARALELO]
  ├─ get_contacto()
  ├─ get_empresa_embudo()
  └─ get_conversacion_con_mensajes()
         ↓
_build_graph() construye LangGraph
  ├─ Nodo "agent": Invoca LLM con contexto
  └─ Nodo "tools": Ejecuta update_etapa_embudo / update_metadata
         ↓
Respuesta: max 3 líneas orientadas al equipo
         ↓
FunnelAgentResponse {respuesta, etapa_anterior, etapa_nueva, tools_used, timing}
```

---

## 🛠️ Herramientas del Agente

### 1. `update_etapa_embudo`
```python
@tool
async def update_etapa_embudo(nueva_etapa_orden: int, razon: str = "...") -> str:
    """Actualiza la etapa del embudo del contacto."""
```
**Usa:** ID de orden de la nueva etapa
**Retorna:** Confirmación del cambio
**IMPORTANTE:** La etapa anterior se rastrea automáticamente

### 2. `update_metadata`
```python
@tool
async def update_metadata(campos: dict, descripcion: str = "...") -> str:
    """Actualiza la metadata del contacto (merge con datos existentes)."""
```
**Usa:** Diccionario con campos a actualizar
**Retorna:** Confirmación de actualización
**SMART MERGE:** Preserva datos existentes, agrega nuevos

---

## 📊 System Prompt Template

El sistema construye dinámicamente el system prompt con:

```
# IDENTIDAD Y MISIÓN
Eres un analista conversacional...

# Datos claves
El contacto {nombre} se encuentra en la etapa {etapa}

# CONTEXTO DEL EMBUDO
[Todas las etapas + etapa actual + metadata]

# HISTORIAL DE CONVERSACIÓN
[Últimos mensajes con timestamps]

# CHECKLIST FINAL
- ¿Cambió de etapa? → update_etapa_embudo
- ¿Registré información? → update_metadata
- ¿Respuesta máximo 3 líneas?
```

---

## 🔌 Cómo Usar

### Ejemplo de Request

```bash
curl -X POST "http://localhost:8080/api/v1/funnel/analyze" \
  -H "Content-Type: application/json" \
  -d '{
    "contacto_id": 1234,
    "empresa_id": 5,
    "agente_id": 10,
    "conversacion_id": 999,
    "model": "x-ai/grok-4.1-fast",
    "max_tokens": 512,
    "temperature": 0.5
  }'
```

### Ejemplo de Response

```json
{
  "success": true,
  "respuesta": "Lead escalado a 'Calificado' por alto presupuesto. Requiere demo técnica. Asignar al equipo gris.",
  "etapa_anterior": "Interesado",
  "etapa_nueva": 3,
  "metadata_actualizada": {
    "presupuesto": "50000-100000 USD",
    "urgencia": "esta_semana"
  },
  "tools_used": [
    {
      "tool_name": "update_etapa_embudo",
      "status": "ok",
      "duration_ms": 45.2
    },
    {
      "tool_name": "update_metadata",
      "status": "ok",
      "duration_ms": 38.7
    }
  ],
  "timing": {
    "total_ms": 1250.3,
    "llm_ms": 890.5,
    "tool_execution_ms": 84.0,
    "graph_build_ms": 45.1
  }
}
```

---

## ⚡ Rendimiento

- **Carga de contexto**: Paralela (3-4 queries simultáneas)
- **Típicas métricas**:
  - Contexto: 100-300ms
  - LLM: 800-1500ms
  - Herramientas: 50-200ms
  - **Total**: 1-2.5 segundos

---

## 🔐 Validaciones

- ✅ Contacto debe existir en empresa
- ✅ Empresa debe tener embudo configurado
- ✅ Metadata usa merge (no sobrescribe datos existentes)
- ✅ Etapa nueva debe ser válida en el embudo
- ✅ Respuesta limitada a 3 líneas

---

## 📚 Diagrama de Clases

```
FunnelAgentRequest
  ├─ contacto_id (int)
  ├─ empresa_id (int)
  ├─ agente_id (int)
  ├─ conversacion_id (int, optional)
  ├─ model (str, optional)
  ├─ max_tokens (int, optional)
  └─ temperature (float, optional)

FunnelContextResponse
  ├─ contacto: ContactInfo
  ├─ etapa_actual: FunnelCurrentStage
  ├─ todas_etapas: List[FunnelStageInfo]
  ├─ tiene_embudo: bool
  ├─ conversacion_resumen: str
  └─ ultimos_mensajes: List[dict]

FunnelAgentResponse
  ├─ success: bool
  ├─ respuesta: str
  ├─ etapa_anterior: str
  ├─ etapa_nueva: int
  ├─ metadata_actualizada: dict
  ├─ tools_used: List[ToolCall]
  ├─ timing: TimingInfo
  ├─ agent_runs: List[AgentRunTrace]
  └─ error: str (optional)
```

---

## 🚀 Próximas Mejoras (Opcionales)

- [ ] Integración con Kapso para análisis automático de leads
- [ ] Historial de cambios de etapa (audit log)
- [ ] Recomendaciones inteligentes por etapa
- [ ] Metricas de conversión por embudo
- [ ] Webhooks para notificar cambios de etapa

---

## 📞 Endpoints Disponibles

Ahora la aplicación tiene:

```
GET  /                                  → Info y endpoints
POST /api/v1/chat                      → Agente Conversacional
POST /api/v1/funnel/analyze ⭐ NUEVO  → Agente de Embudo
POST /api/v1/kapso/inbound            → Kapso WhatsApp
GET  /docs                             → Swagger UI
```

---

✅ **Implementación Completa y Lista para Usar**
