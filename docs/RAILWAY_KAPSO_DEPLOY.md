# Railway + Kapso Deployment

## Objetivo

Desplegar el proyecto como un solo servicio de Railway con esta arquitectura:

- **Proceso público**: `kapso-bridge/server.mjs`
- **Proceso interno**: `FastAPI + LangGraph` en `127.0.0.1:8000`

Kapso debe apuntar su webhook a la URL pública de Railway terminando en:

```text
/webhook/kapso
```

Adicionalmente, el bridge expone públicamente:

```text
/openapi.json
/api/v1/scheduling/disponibilidad
/api/v1/scheduling/crear-evento
/api/v1/scheduling/reagendar-evento
/api/v1/scheduling/eliminar-evento
```

## Arquitectura desplegada

```text
Kapso Webhook
  -> Railway public URL
  -> Node Kapso Bridge
  -> Internal FastAPI endpoint http://127.0.0.1:8000/api/v1/kapso/inbound
  -> LangGraph agent
  -> Node Kapso Bridge
  -> WhatsApp reply via Kapso SDK
```

```text
n8n / Postman / clientes HTTP
  -> Railway public URL
  -> Node Kapso Bridge
  -> Internal FastAPI scheduling routes
  -> Nylas + Supabase
```

## Variables de entorno requeridas en Railway

### Core Python

- `OPENROUTER_API_KEY`
- `OPENROUTER_BASE_URL`
- `DEFAULT_MODEL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `APP_NAME`
- `DEBUG`

### Kapso

- `KAPSO_API_KEY`
- `KAPSO_WEBHOOK_SECRET`
- `KAPSO_INTERNAL_TOKEN`
- `KAPSO_BASE_URL`
- `INTERNAL_AGENT_API_URL`
- `PYTHON_SERVICE_PORT`

## Valores recomendados

```env
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
DEFAULT_MODEL=x-ai/grok-4.1-fast
DEBUG=false

KAPSO_BASE_URL=https://api.kapso.ai/meta/whatsapp
KAPSO_INTERNAL_TOKEN=kapso-internal-prod-urpe
INTERNAL_AGENT_API_URL=http://127.0.0.1:8000/api/v1/kapso/inbound
PYTHON_SERVICE_PORT=8000
```

## Archivos relevantes para Railway

- `nixpacks.toml`
- `railway-start.sh`
- `kapso-bridge/server.mjs`
- `main.py`
- `app/api/kapso_routes.py`
- `app/api/scheduling_routes.py`

## Webhook de Kapso

Cuando Railway te entregue una URL pública, en Kapso debes configurar:

```text
https://TU-SERVICIO.up.railway.app/webhook/kapso
```

No debes usar:

```text
/api/v1/kapso/inbound
```

porque ese endpoint es interno entre el bridge y FastAPI.

## Scheduling público

Las integraciones externas como n8n deben llamar la URL pública del bridge, por ejemplo:

```text
https://TU-SERVICIO.up.railway.app/api/v1/scheduling/disponibilidad
```

No es necesario exponer FastAPI por separado mientras el bridge esté desplegado correctamente.

## Eventos recomendados para pruebas

Activar solo:

- `Message received`

## Requisitos de base de datos

Debe existir un registro en `wp_numeros` con:

- `id_kapso = <phone_number_id real del canal Kapso>`
- `agente_id = <agente configurado para responder>`
- `activo = true`

## Verificación rápida post deploy

- Railway responde en `/health` del bridge
- Railway responde en `/docs` del backend FastAPI
- Railway responde en `/openapi.json` vía bridge
- Railway responde en `/api/v1/scheduling/disponibilidad` vía bridge
- Kapso webhook queda en estado activo
- un mensaje desde WhatsApp dispara respuesta del agente
