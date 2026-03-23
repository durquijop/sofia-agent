# Funnel Agent Debug Dashboard - Implementation

## Overview

Se ha agregado un dashboard de debug completo para el **Agente de Embudo** similar al existente para **Agentes Conversacionales**. Ahora puedes ver:

- ✅ Todas las ejecuciones del funnel agent (últimas 50)
- ✅ Trazas detalladas de cada ejecución
- ✅ Timing y métricas de rendimiento
- ✅ Herramientas ejecutadas y sus resultados
- ✅ Cambios de etapa realizados
- ✅ Estadísticas globales

---

## Endpoints Nuevos

### 1. Dashboard HTML (Interfaz Visual)
```
GET /api/v1/funnel/debug
```
**Retorna:** Página HTML con dashboard visual
- Estadísticas en tiempo real
- Lista de últimas 50 ejecuciones
- Detalles expandibles de cada ejecución
- Información de trazas de agentes

**Acceso:**
```
http://localhost:8080/api/v1/funnel/debug
```

### 2. Debug Events JSON
```
GET /api/v1/funnel/debug/events?limit=50
```
**Retorna:** JSON con eventos de debug
```json
{
  "runs": [
    {
      "timestamp": "2025-01-27T15:30:00+00:00",
      "contacto_id": 123,
      "empresa_id": 456,
      "success": true,
      "respuesta": "Contacto en etapa de Negociación...",
      "etapa_anterior": "Interesado",
      "etapa_nueva": 3,
      "agent_runs": [...],
      "timing": {...},
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

## Componentes Implementados

### 1. **app/core/funnel_debug.py** (Nuevo)
Módulo de debug en memoria que almacena las últimas 50 ejecuciones del funnel agent.

**Funciones principales:**
- `add_funnel_debug_run()` - Registra una ejecución
- `get_funnel_debug_runs()` - Recupera ejecuciones (últimas N)
- `get_funnel_debug_stats()` - Estadísticas generales
- `clear_funnel_debug_runs()` - Limpia historial

**Almacenamiento:** Circular buffer en memoria (no persiste entre reinicios)

### 2. **app/agents/funnel.py** (Modificado)
Ahora registra cada ejecución en el debug.

**Cambios:**
- Importa `add_funnel_debug_run` de `funnel_debug.py`
- Después de cada ejecución exitosa, llama a `add_funnel_debug_run()`
- Pasa: timing, agent_runs, tools_used, cambios de etapa, etc.

### 3. **app/api/funnel_routes.py** (Modificado)
Agregó 4 nuevas funciones de rendering + 2 nuevos endpoints.

**Nuevas funciones:**
- `escape_html()` - Sanitización de HTML
- `render_timing_table()` - Tabla de timing en HTML
- `render_tool_list()` - Lista de herramientas en HTML
- `render_agent_runs()` - Trazas de agentes en HTML
- `render_debug_html()` - Página completa del dashboard

**Nuevos endpoints:**
- `GET /api/v1/funnel/debug` → Dashboard HTML
- `GET /api/v1/funnel/debug/events` → JSON con eventos

---

## Visualización en el Dashboard

El dashboard muestra:

### **Stats (Arriba)**
```
┌─────────────────────────────────────────┐
│ Total Runs  │  Exitosas  │  Con Error  │  Duracion Prom
│     50      │     48     │      2      │      2,850ms
└─────────────────────────────────────────┘
```

### **Ejecuciones (Lista)**
Cada ejecución muestra:
- Badge de estado (✓ OK / ✗ ERROR)
- ID de contacto y empresa
- Duración total
- Cambio de etapa (⇢ nueva)
- Número de herramientas ejecutadas
- Timestamp
- Preview de respuesta (150 chars)
- Expandible: Trazas de agentes con timing y tools

**Ejemplo:**
```
✓ OK  Contacto #12345  Empresa #678  2,850ms  Interesado → Calificado  🔧 2 tools
Respuesta: El contacto mostró interés claro en features avanzadas. Presupuesto estimado USD...
  [Expandible] Ver trazas de agente
    - Timing tabla (total, llm, tools, graph)
    - Herramientas ejecutadas (update_etapa_embudo, update_metadata)
```

---

## Flujo de Datos

```
1. POST /api/v1/funnel/analyze
   └─> app/agents/funnel.py::run_funnel_agent()
       └─> Ejecuta agente
       └─> add_funnel_debug_run()  ← Registra en memoria
       └─> Retorna FunnelAgentResponse

2. GET /api/v1/funnel/debug
   └─> app/api/funnel_routes.py::debug_dashboard()
       └─> get_funnel_debug_runs()  ← Lee últimas 50
       └─> render_debug_html()
       └─> Retorna HTML

3. GET /api/v1/funnel/debug/events
   └─> Retorna JSON con runs + stats
```

---

## Características

✅ **Historial en Memoria**
- Últimas 50 ejecuciones
- Se limpia al reiniciar servidor
- Thread-safe (usa Lock)

✅ **Estadísticas**
- Total de ejecuciones
- Exitosas vs fallidas
- Duración promedio
- Capacidad máxima

✅ **Visualización HTML**
- Responsive design
- Grid de stats
- Detalles expandibles
- Syntax highlighting para JSON

✅ **Detalles por Ejecución**
- Contacto y empresa
- Estado (OK/ERROR)
- Respuesta del agente
- Cambios de etapa
- Timing breakdown
- Herramientas ejecutadas
- Trazas completas de agente

---

## Comparación con Dashboard Conversacional

| Aspecto | Conversacional | Funnel |
|---------|---|---|
| Almacenamiento | Kapso events + BD | Circular buffer en memoria |
| Endpoints | `/api/v1/kapso/debug/*` | `/api/v1/funnel/debug*` |
| UI | Integrada en kapso-bridge | HTML dedicado en FastAPI |
| Eventos | Múltiples stages | 1 simple por run |
| Historial | Filtrable por mensaje | Últimas 50 runs |

---

## Uso

### Ver Dashboard Completo
```
http://localhost:8080/api/v1/funnel/debug
```

### Ver Últimos 20 Eventos (JSON)
```
http://localhost:8080/api/v1/funnel/debug/events?limit=20
```

### Programáticamente
```python
from app.core.funnel_debug import get_funnel_debug_runs, get_funnel_debug_stats

# Obtener últimas 10 ejecuciones
runs = get_funnel_debug_runs(10)

# Obtener estadísticas
stats = get_funnel_debug_stats()
print(f"Total: {stats['total_runs']}, Exitosas: {stats['successful']}")
```

---

## Notas Técnicas

- **Thread-safe:** Usa `threading.Lock` para acceso concurrente
- **Memory-efficient:** Circular buffer limita a 50 entries máximo
- **No persistence:** El historial se limpia al reiniciar
- **HTML Sanitized:** Todas las entradas escapadas para evitar XSS
- **Responsive:** Funciona en desktop y mobile

---

## Próximos Pasos (Opcional)

Si quieres persistencia:
1. Agregar tabla `funnel_debug_runs` en BD
2. Modificar `add_funnel_debug_run()` para guardar en BD
3. Agregar endpoint de limpieza/archivar para no llenar la BD

---

**Status:** ✅ Implementado y funcional
**Fecha:** 2025-01-27
**Archivos:** 
- ✨ `app/core/funnel_debug.py` (nuevo)
- 📝 `app/agents/funnel.py` (modificado)
- 📝 `app/api/funnel_routes.py` (modificado)
