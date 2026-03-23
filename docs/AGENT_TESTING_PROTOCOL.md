# Protocolo de Testeo y Estado de Agentes

## Objetivo

Este documento sirve como guía rápida para cualquier agente o desarrollador que necesite:

- entender qué está implementado hoy
- saber qué validar antes de probar en vivo
- identificar qué ya fue corregido
- distinguir qué sigue pendiente o requiere observación

## Estado actual resumido

### Hecho

- existe el agente conversacional principal
- existe el agente de embudo
- Kapso inbound ejecuta ambos agentes en paralelo
- se agregaron comandos slash `/borrar` y `/borrar2`
- se corrigió el riesgo de que el agente conversacional quedara colgado por loops del grafo
- se agregó timeout al discovery de herramientas MCP
- se agregó timeout defensivo a la ejecución del grafo conversacional
- el flujo de embudo ya vuelve del nodo de tools al agente para análisis final
- se alineó `orden_etapa` como valor operativo del funnel agent

### No hecho o no confirmado

- no está confirmada una prueba end-to-end completa del flujo Kapso + MCP + funnel en todos los escenarios
- no está confirmado que todas las tools MCP externas respondan estable bajo carga
- no está confirmado que todos los prompts eviten tool loops en escenarios complejos
- no está definido un test automatizado formal para Kapso inbound y multi-agent

## Riesgo principal ya corregido

### Síntoma

Los mensajes quedaban en estado `processing` en Kapso Debug y no aparecía `run_agent_done`.

### Causa raíz

El agente conversacional podía quedar en ciclo:

- `agent -> tools -> agent -> tools`

sin límite de iteraciones ni timeout del grafo.

### Corrección aplicada

En `app/agents/conversational.py`:

- límite de iteraciones del LLM
- timeout de ejecución del grafo
- timeout en discovery de MCP
- fallback para no usar `ToolMessage` crudo como respuesta final válida

## Archivos críticos a revisar antes de tocar algo

- `app/api/kapso_routes.py`
- `app/agents/conversational.py`
- `app/agents/funnel.py`
- `app/db/queries.py`
- `kapso-bridge/server.mjs`

## Flujo real que debe entenderse

### 1. Bridge

`kapso-bridge/server.mjs`

Responsable de:

- recibir webhook de Kapso
- agrupar mensajes
- marcar presencia/typing
- llamar al FastAPI interno
- enviar respuesta final a Kapso
- registrar eventos debug

### 2. FastAPI inbound

`app/api/kapso_routes.py`

Responsable de:

- validar token interno
- resolver número, empresa, agente y contacto
- persistir mensajes entrantes
- construir prompt/contexto
- correr agente conversacional y funnel agent en paralelo
- persistir mensaje saliente
- devolver respuesta interna al bridge

### 3. Agente conversacional

`app/agents/conversational.py`

Responsable de:

- cargar memoria
- descubrir tools MCP
- ejecutar grafo LangGraph
- devolver respuesta para el usuario final

### 4. Funnel agent

`app/agents/funnel.py`

Responsable de:

- analizar etapa del embudo
- registrar metadata
- actualizar etapa si corresponde
- devolver trazas para debug interno

## Protocolo mínimo de testeo

### Paso 1. Validar sintaxis

Ejecutar:

```powershell
python -m py_compile app\agents\conversational.py app\agents\funnel.py app\api\kapso_routes.py app\db\queries.py
```

### Paso 2. Validar imports críticos

Ejecutar:

```powershell
python -c "import app.agents.conversational as c; import app.agents.funnel as f; import app.api.kapso_routes as k; print('imports_ok')"
```

### Paso 3. Confirmar que el bridge y FastAPI estén arriba

Validar que:

- bridge esté escuchando correctamente
- FastAPI esté respondiendo
- `INTERNAL_AGENT_API_URL` apunte al puerto correcto

### Paso 4. Probar mensaje simple por Kapso

Enviar:

```text
Hola
```

Resultado esperado:

- aparece `inbound_received`
- aparece `run_agent_start`
- aparece `run_agent_done`
- el mensaje deja de estar en `processing`
- el bridge registra `call_fastapi_done`
- el usuario recibe una respuesta de texto

### Paso 5. Probar slash commands

Enviar:

```text
/borrar
```

y luego:

```text
/borrar2
```

Resultado esperado:

- no pasan por el agente
- responden de inmediato
- no quedan como mensajes normales del flujo de conversación

### Paso 6. Probar flujo con tool MCP

Enviar un mensaje que obligue uso de herramienta real.

Resultado esperado:

- `tools_used` muestra la tool
- `run_agent_done` aparece
- no se excede el timeout
- no queda la interacción en `processing`

### Paso 7. Probar funnel agent manualmente

Consumir:

```text
POST /api/v1/funnel/analyze
```

Validar:

- responde `success=true` o `success=false` controlado
- incluye `timing`
- incluye `tools_used`
- incluye `agent_runs`
- no rompe el flujo conversacional aunque falle

## Señales de que algo volvió a romperse

- aparece `run_agent_start` pero no `run_agent_done`
- aparece `call_fastapi_start` pero no `call_fastapi_done`
- en debug la interacción queda en `processing`
- `tools_used` crece sin respuesta final
- respuestas vacías o solo con output de tools
- timeouts frecuentes al cargar MCP

## Qué revisar si vuelve a colgarse

### Revisar primero

- `app/agents/conversational.py`
- tools MCP recién agregadas
- prompts que estén forzando uso excesivo de tools
- latencia o caída del MCP server
- si se removió accidentalmente el timeout del grafo
- si se removió accidentalmente el límite de iteraciones

### Revisar en debug

Comparar estos eventos:

- `call_fastapi_start`
- `run_agent_start`
- `run_funnel_done`
- `run_agent_done`
- `call_fastapi_done`

Si falta `run_agent_done`, el problema casi siempre está dentro del agente conversacional o en una tool llamada por él.

## Criterio de “OK para usar”

Se considera estable si se cumple todo esto:

- mensajes simples responden sin quedarse en `processing`
- mensajes con tools MCP responden dentro de tiempo razonable
- slash commands responden inmediato
- funnel agent no bloquea la respuesta conversacional
- no hay loops visibles en tools
- debug muestra cierre completo de la interacción

## Nota operativa

Si se agregan nuevas tools o nueva lógica multi-agente, repetir este protocolo antes de hacer push.
