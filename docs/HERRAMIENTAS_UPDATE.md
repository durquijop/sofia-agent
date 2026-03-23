# Actualización: Herramientas del Agente de Embudo

## 🔄 Cambios Realizados

El agente de embudo ha sido actualizado con las siguientes mejoras:

### 1. **Modelo LLM Fijo**
- ✅ Usa **`x-ai/grok-4.1-fast`** exclusivamente (optimizado para análisis de embudos)
- Modelo seleccionado automáticamente, no configurable por request

### 2. **Herramienta: `update_etapa_embudo`**

#### Antes:
```python
@tool
async def update_etapa_embudo(nueva_etapa_orden: int, razon: str) -> str:
    """Actualiza la etapa del embudo del contacto."""
```

#### Ahora:
```python
@tool
async def update_etapa_embudo(id_etapa: int, razon: str = "Cambio identificado por agente") -> str:
    """
    Actualiza la etapa del embudo del contacto.
    
    Args:
        id_etapa: ID de la etapa (número entero válido del contexto)
        razon: Razón del cambio
    """
```

#### Cambios Técnicos:
- **Parámetro:** `nueva_etapa_orden` → `id_etapa`
- **Función DB:** `db.actualizar_etapa_contacto(contacto_id, nueva_etapa_orden=id_etapa)`
- **Validación:** Verifica que `id_etapa` esté en `context.todas_etapas`
- **Update SQL:** 
  ```sql
  UPDATE wp_contactos 
  SET etapa_embudo = {id_etapa} 
  WHERE id = {contacto_id}
  ```

#### Ejemplo de Uso:
```json
{
  "id_etapa": 2,
  "razon": "Lead mostró presupuesto confirmado"
}
```

---

### 3. **Herramienta: `update_metadata`**

#### Antes:
```python
@tool
async def update_metadata(campos: dict, descripcion: str) -> str:
    """Actualiza la metadata del contacto."""
```

#### Ahora:
```python
@tool
async def update_metadata(informacion_capturada: dict, seccion: str = "etapa_actual") -> str:
    """
    Registra TODA la información capturada en la conversación según la etapa actual.
    
    Args:
        informacion_capturada: Diccionario con los campos capturados
        seccion: Sección de metadata donde guardar (default: "etapa_actual")
    """
```

#### Cambios Técnicos:
- **Parámetro principal:** `campos` → `informacion_capturada`
- **Ya no usa:** `db.actualizar_metadata_contacto()`
- **Ahora usa:** **POST al endpoint Supabase**
  ```
  POST https://vecspltvmyopwbjzerow.supabase.co/functions/v1/actualizar-metadata-v3
  ```

#### Payload Enviado:
```python
{
    "contacto_id": int,
    "empresa_id": int,
    "agente_id": int,
    "metadata": str  # JSON string
}
```

#### Formato de Metadata Enviada:
```python
metadata_json = {
    "etapa_actual": {  # o la seccion especificada
        "informacion_capturada": {
            "info_reg_1": "2025-01-27 10:30 AM",
            "info_reg_2": "¿Cuánto cuesta el tratamiento?",
            "info_reg_3": "Presupuesto: 50000-100000 USD"
        },
        "actualizado_en": "1704067800.5432"
    }
}
```

#### Ejemplo de Uso:
```json
{
  "informacion_capturada": {
    "info_reg_1": "Fecha contacto: 2025-01-27 10:30 AM",
    "info_reg_2": "Pregunta inicial: ¿Cuánto cuesta?",
    "info_reg_3": "Presupuesto: 50000-100000 USD",
    "info_reg_4": "Urgencia: Esta semana"
  },
  "seccion": "etapa_actual"
}
```

---

## 🎯 Flujo de Ejecución Actualizado

```
1. Request llega a POST /api/v1/funnel/analyze
   ↓
2. load_funnel_context() en paralelo
   ├─ get_contacto()
   ├─ get_empresa_embudo()
   └─ get_conversacion_con_mensajes()
   ↓
3. LLM instancia con x-ai/grok-4.1-fast
   ├─ Herramienta 1: update_etapa_embudo(id_etapa, razon)
   └─ Herramienta 2: update_metadata(informacion_capturada, seccion)
   ↓
4. Si el LLM detecta cambios:
   
   a) update_etapa_embudo:
      → db.actualizar_etapa_contacto() 
      → UPDATE wp_contactos SET etapa_embudo = {id_etapa}
      
   b) update_metadata:
      → POST a https://vecspltvmyopwbjzerow.supabase.co/functions/v1/actualizar-metadata-v3
      → Headers: Content-Type: application/json
      → Body: {contacto_id, empresa_id, agente_id, metadata (string JSON)}
   ↓
5. Response al cliente con timing y tool_used
```

---

## 🧪 Ejemplo de Request/Response

### Request:
```bash
curl -X POST "http://localhost:8080/api/v1/funnel/analyze" \
  -H "Content-Type: application/json" \
  -d '{
    "contacto_id": 328159,
    "empresa_id": 1,
    "agente_id": 1,
    "conversacion_id": 999
  }'
```

### Response Esperado:
```json
{
  "success": true,
  "respuesta": "Lead escalado a Calificado por presupuesto confirmado. Próximo paso: agendar demo técnica.",
  "etapa_anterior": "Interesado",
  "etapa_nueva": 3,
  "metadata_actualizada": {
    "info_reg_1": "2025-01-27 10:30 AM",
    "info_reg_2": "¿Cuánto cuesta?",
    "info_reg_3": "USD 50000-100000"
  },
  "tools_used": [
    {
      "tool_name": "update_etapa_embudo",
      "tool_input": {
        "id_etapa": 3,
        "razon": "Presupuesto confirmado"
      },
      "tool_output": "✓ Etapa actualizada a 3: Presupuesto confirmado",
      "duration_ms": 42.5,
      "status": "ok",
      "source": "funnel"
    },
    {
      "tool_name": "update_metadata",
      "tool_input": {
        "informacion_capturada": {
          "info_reg_1": "2025-01-27 10:30 AM",
          "info_reg_2": "¿Cuánto cuesta?",
          "info_reg_3": "USD 50000-100000"
        },
        "seccion": "etapa_actual"
      },
      "tool_output": "✓ Metadata registrada: 3 campos capturados",
      "duration_ms": 198.7,
      "status": "ok",
      "source": "funnel"
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

## ⚡ Validaciones Implementadas

✅ **Etapa válida:** Verifica que `id_etapa` existe en `context.todas_etapas`  
✅ **Metadata JSON:** Construye JSON válido antes de enviar a Supabase  
✅ **POST async:** Usa httpx.AsyncClient con timeout 15s  
✅ **Manejo de errores:** Captura excepciones y retorna mensajes claros  
✅ **Status codes:** Valida respuesta (200, 201) del endpoint Supabase  

---

## 🔐 Seguridad

- ✅ Endpoint Supabase validado: `https://vecspltvmyopwbjzerow.supabase.co/...`
- ✅ Parámetros validados antes de enviar
- ✅ Timeout de 15s configurable
- ✅ Logging detallado de todas las operaciones
- ✅ No se envían credenciales en el payload (usa endpoint de Supabase)

---

## 📊 Cambios de Parámetros Resumidos

| Aspecto | Antes | Ahora |
|---------|-------|-------|
| **Modelo** | Configurable | Fijo: `x-ai/grok-4.1-fast` |
| **update_etapa_embudo param** | `nueva_etapa_orden` | `id_etapa` |
| **update_metadata param** | `campos` | `informacion_capturada` |
| **update_metadata backend** | `db.actualizar_metadata_contacto()` | POST a Supabase |
| **Endpoint metadata** | Local BD | `https://vecspltvmyopwbjzerow.supabase.co/functions/v1/actualizar-metadata-v3` |

---

## ✅ Testing

Para probar las nuevas herramientas:

```bash
python scripts/test_funnel_agent.py
```

O usar Swagger: `http://localhost:8080/docs` → `/api/v1/funnel/analyze`

---

**Fecha de Implementación:** 22 de Marzo, 2026  
**Status:** ✅ Completado y validado
