import crypto from 'node:crypto';
import 'dotenv/config';
import cors from 'cors';
import express from 'express';
import { WhatsAppClient } from '@kapso/whatsapp-cloud-api';

const requiredEnvs = ['KAPSO_API_KEY'];
for (const name of requiredEnvs) {
  if (!process.env[name]) {
    throw new Error(`Falta variable de entorno requerida: ${name}`);
  }
}

const PORT = Number(process.env.PORT || process.env.KAPSO_BRIDGE_PORT || 3001);
const KAPSO_API_KEY = process.env.KAPSO_API_KEY;
const KAPSO_BASE_URL = process.env.KAPSO_BASE_URL || 'https://api.kapso.ai/meta/whatsapp';
const KAPSO_WEBHOOK_SECRET = process.env.KAPSO_WEBHOOK_SECRET || '';
const INTERNAL_AGENT_API_URL = process.env.INTERNAL_AGENT_API_URL || 'http://127.0.0.1:8000/api/v1/kapso/inbound';
const KAPSO_INTERNAL_TOKEN = process.env.KAPSO_INTERNAL_TOKEN || '';

const client = new WhatsAppClient({
  baseUrl: KAPSO_BASE_URL,
  kapsoApiKey: KAPSO_API_KEY,
});

const app = express();
const threadQueues = new Map();
const processedMessageIds = new Map();
const bridgeDebugEvents = [];
const MAX_BRIDGE_DEBUG_EVENTS = 200;
const PROCESSED_MESSAGE_TTL_MS = 10 * 60 * 1000;
const PROCESS_TIMEOUT_MS = 180 * 1000;
const PROCESSING_MESSAGE_TTL_MS = PROCESS_TIMEOUT_MS + 30 * 1000;
const MAX_SEND_RETRIES = 3;
const RATE_LIMIT_BASE_DELAY_MS = 2000;
const IN_FLIGHT_DELAY_MS = 1500;
const MEDIA_TYPES = ['image', 'audio', 'video', 'document', 'sticker'];

app.use(cors());
app.use(express.json({
  limit: '5mb',
  verify: (req, _res, buf) => {
    req.rawBody = buf;
  },
}));

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function addBridgeDebugEvent(stage, payload = {}) {
  bridgeDebugEvents.unshift({
    timestamp: new Date().toISOString(),
    source: 'bridge',
    stage,
    payload,
  });
  if (bridgeDebugEvents.length > MAX_BRIDGE_DEBUG_EVENTS) {
    bridgeDebugEvents.length = MAX_BRIDGE_DEBUG_EVENTS;
  }
}

function maskSecret(value) {
  if (!value) return null;
  if (String(value).length <= 8) return '***';
  return `${String(value).slice(0, 4)}...${String(value).slice(-4)}`;
}

function getBridgeDebugConfig() {
  return {
    port: PORT,
    kapso_base_url: KAPSO_BASE_URL,
    internal_agent_api_url: INTERNAL_AGENT_API_URL,
    kapso_api_key: maskSecret(KAPSO_API_KEY),
    kapso_webhook_secret: maskSecret(KAPSO_WEBHOOK_SECRET),
    kapso_internal_token: maskSecret(KAPSO_INTERNAL_TOKEN),
  };
}

async function fetchFastApiDebugJson(pathname) {
  const targetUrl = new URL(pathname, INTERNAL_AGENT_API_URL).toString();
  const response = await fetch(targetUrl, {
    headers: KAPSO_INTERNAL_TOKEN ? { 'x-kapso-internal-token': KAPSO_INTERNAL_TOKEN } : {},
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`FastAPI debug respondió ${response.status}: ${body}`);
  }
  return response.json();
}

function renderKapsoDebugHtml() {
  return `<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kapso Debug</title>
  <style>
    body { font-family: Arial, sans-serif; background: #111827; color: #e5e7eb; margin: 0; padding: 16px; }
    h1, h2 { margin: 0 0 12px; }
    .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
    .card { background: #1f2937; border: 1px solid #374151; border-radius: 10px; padding: 16px; }
    pre { white-space: pre-wrap; word-break: break-word; background: #0b1220; border-radius: 8px; padding: 12px; overflow: auto; }
    .muted { color: #9ca3af; font-size: 12px; }
    .event { border-top: 1px solid #374151; padding: 10px 0; }
    .event:first-child { border-top: 0; padding-top: 0; }
    .stage { color: #93c5fd; font-weight: bold; }
    .source { color: #86efac; }
    .toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
    button { background: #2563eb; color: white; border: 0; padding: 8px 12px; border-radius: 8px; cursor: pointer; }
  </style>
</head>
<body>
  <div class="toolbar">
    <h1>Kapso Debug Dashboard</h1>
    <button onclick="loadData()">Refrescar</button>
    <span class="muted" id="status">cargando...</span>
  </div>
  <div class="grid">
    <section class="card">
      <h2>Bridge config</h2>
      <pre id="bridge-config"></pre>
    </section>
    <section class="card">
      <h2>FastAPI config</h2>
      <pre id="fastapi-config"></pre>
    </section>
  </div>
  <div class="grid" style="margin-top: 16px;">
    <section class="card">
      <h2>Bridge events</h2>
      <div id="bridge-events"></div>
    </section>
    <section class="card">
      <h2>FastAPI events</h2>
      <div id="fastapi-events"></div>
    </section>
  </div>
  <script>
    function renderEvents(containerId, events) {
      const container = document.getElementById(containerId);
      if (!events || !events.length) {
        container.innerHTML = '<div class="muted">Sin eventos</div>';
        return;
      }
      container.innerHTML = events.map(event => 
        '<div class="event">' +
          '<div><span class="source">' + (event.source || '') + '</span> · <span class="stage">' + (event.stage || '') + '</span></div>' +
          '<div class="muted">' + (event.timestamp || '') + '</div>' +
          '<pre>' + JSON.stringify(event.payload || {}, null, 2) + '</pre>' +
        '</div>'
      ).join('');
    }

    async function loadData() {
      const status = document.getElementById('status');
      status.textContent = 'cargando...';
      try {
        const response = await fetch('/debug/kapso/data');
        const data = await response.json();
        document.getElementById('bridge-config').textContent = JSON.stringify(data.bridge_config || {}, null, 2);
        document.getElementById('fastapi-config').textContent = JSON.stringify(data.fastapi_config || {}, null, 2);
        renderEvents('bridge-events', data.bridge_events || []);
        renderEvents('fastapi-events', data.fastapi_events || []);
        status.textContent = 'actualizado ' + new Date().toLocaleTimeString();
      } catch (error) {
        status.textContent = 'error cargando datos';
      }
    }

    loadData();
    setInterval(loadData, 3000);
  </script>
</body>
</html>`;
}

function normalizeTimestamp(raw) {
  if (typeof raw === 'number') return String(raw);
  if (typeof raw === 'string' && /^\d+$/.test(raw)) return raw;
  const parsed = raw ? new Date(raw).getTime() : Date.now();
  return String(Math.floor(parsed / 1000));
}

function extractDataArray(body) {
  if (Array.isArray(body)) {
    if (body.length === 0) return [];
    const first = body[0];
    if (first?.body && typeof first.body === 'object' && 'data' in first.body) {
      return body.flatMap(item => Array.isArray(item?.body?.data) ? item.body.data : []);
    }
    if ('message' in first && 'conversation' in first) {
      return body;
    }
    if ('data' in first && Array.isArray(first.data)) {
      return body.flatMap(item => Array.isArray(item?.data) ? item.data : []);
    }
    return [];
  }

  if (body && typeof body === 'object') {
    if ('data' in body && Array.isArray(body.data)) {
      return body.data;
    }
    if (body.body && typeof body.body === 'object' && Array.isArray(body.body?.data)) {
      return body.body.data;
    }
    if ('message' in body && 'conversation' in body) {
      return [body];
    }
  }

  return [];
}

function accumulateMessage(record, groupedPayloads) {
  const message = record?.message;
  const conversation = record?.conversation;
  if (!message || !conversation) return;

  const msgType = message.type || 'text';
  const hasMediaByKapso = message.kapso?.has_media === true;
  const hasMediaByType = MEDIA_TYPES.includes(msgType) && !!message[msgType];
  const hasMedia = hasMediaByKapso || hasMediaByType;
  const mediaCaption = MEDIA_TYPES.includes(msgType) ? message[msgType]?.caption : undefined;
  const textPart = message.text?.body ?? mediaCaption ?? message.kapso?.content ?? '';
  const from = String(message.from);
  const timestamp = normalizeTimestamp(message.timestamp);

  if (!groupedPayloads.has(from)) {
    groupedPayloads.set(from, {
      from,
      contact_name: conversation.contact_name ?? null,
      phone_number_id: record.phone_number_id ?? conversation.phone_number_id,
      kapso_conversation_id: conversation.id,
      message_id: message.id,
      message_type: msgType,
      text: textPart || null,
      timestamp,
      has_media: hasMedia,
      media_raw: hasMedia ? message : null,
    });
    return;
  }

  const existing = groupedPayloads.get(from);
  if (textPart) {
    existing.text = existing.text ? `${existing.text}\n${textPart}` : textPart;
  }
  existing.timestamp = String(Math.max(Number(existing.timestamp), Number(timestamp)));
  if (hasMedia) {
    existing.has_media = true;
    existing.media_raw = message;
    if (msgType && msgType !== 'text') {
      existing.message_type = msgType;
    }
  }
}

function cleanupProcessedMessages(now) {
  for (const [messageId, state] of processedMessageIds.entries()) {
    const ttlMs = state.status === 'processing' ? PROCESSING_MESSAGE_TTL_MS : PROCESSED_MESSAGE_TTL_MS;
    if (now - state.updatedAt > ttlMs) {
      processedMessageIds.delete(messageId);
    }
  }
}

async function withTimeout(promise, timeoutMs) {
  let timeoutHandle;
  const timeoutPromise = new Promise((_, reject) => {
    timeoutHandle = setTimeout(() => reject(new Error(`Timeout tras ${timeoutMs}ms`)), timeoutMs);
  });
  try {
    return await Promise.race([promise, timeoutPromise]);
  } finally {
    clearTimeout(timeoutHandle);
  }
}

function isRateLimitError(err) {
  return err?.code === 131056 || err?.category === 'throttling';
}

function isInFlightError(err) {
  return err?.httpStatus === 409 || (typeof err?.raw?.error === 'string' && err.raw.error.includes('in-flight'));
}

function isServerError(err) {
  return err?.httpStatus >= 500 || err?.code === 500 || err?.category === 'server';
}

async function withKapsoRetry(fn, label) {
  for (let attempt = 1; attempt <= MAX_SEND_RETRIES; attempt += 1) {
    try {
      return await fn();
    } catch (error) {
      const isLast = attempt === MAX_SEND_RETRIES;
      if (isRateLimitError(error)) {
        if (isLast) throw error;
        await sleep(RATE_LIMIT_BASE_DELAY_MS * attempt);
        continue;
      }
      if (isInFlightError(error)) {
        if (isLast) throw error;
        await sleep(IN_FLIGHT_DELAY_MS);
        continue;
      }
      if (isServerError(error)) {
        if (isLast) throw error;
        await sleep(RATE_LIMIT_BASE_DELAY_MS * attempt);
        continue;
      }
      throw error;
    }
  }
  throw new Error(`No se pudo completar ${label}`);
}

function normalizeWhatsAppText(input) {
  if (!input) return '';
  return String(input)
    .replace(/\r\n/g, '\n')
    .replace(/\u00A0/g, ' ')
    .replace(/^\s*[•*]\s+/gm, '- ')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/[ \t]{2,}/g, ' ')
    .trim();
}

async function markKapsoAsRead(phoneNumberId, messageId) {
  void phoneNumberId;
  void messageId;
  return null;
}

async function callInternalAgent(sqlPayload) {
  const headers = {
    'Content-Type': 'application/json',
  };
  if (KAPSO_INTERNAL_TOKEN) {
    headers['x-kapso-internal-token'] = KAPSO_INTERNAL_TOKEN;
  }

  addBridgeDebugEvent('call_fastapi_start', {
    phone_number_id: sqlPayload.phone_number_id,
    from: sqlPayload.from,
    message_id: sqlPayload.message_id,
    message_type: sqlPayload.message_type,
  });
  console.log(
    `[KapsoBridge] -> FastAPI phone_number_id=${sqlPayload.phone_number_id} from=${sqlPayload.from} message_id=${sqlPayload.message_id} type=${sqlPayload.message_type}`,
  );

  const response = await fetch(INTERNAL_AGENT_API_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify(sqlPayload),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Backend FastAPI respondió ${response.status}: ${body}`);
  }

  const reply = await response.json();
  addBridgeDebugEvent('call_fastapi_done', {
    agent_id: reply.agent_id,
    conversation_id: reply.conversation_id,
    reply_type: reply.reply_type,
    message_id: sqlPayload.message_id,
  });
  console.log(
    `[KapsoBridge] <- FastAPI agent_id=${reply.agent_id} conversation_id=${reply.conversation_id} reply_type=${reply.reply_type} chars=${String(reply.reply_text || '').length}`,
  );
  return reply;
}

async function sendKapsoText(recipientPhone, phoneNumberId, text) {
  const body = normalizeWhatsAppText(text);
  if (!body) return null;
  return withKapsoRetry(
    () => client.messages.textSender.send({ phoneNumberId, to: recipientPhone, body }),
    `sendText(${recipientPhone})`,
  );
}

async function dispatchKapsoResponse(reply) {
  const recipientPhone = reply.recipient_phone;
  const phoneNumberId = reply.phone_number_id;
  const replyType = reply.reply_type || 'text';

  addBridgeDebugEvent('kapso_send_start', {
    to: recipientPhone,
    phone_number_id: phoneNumberId,
    reply_type: replyType,
    message_id: reply.message_id,
  });
  console.log(
    `[KapsoBridge] -> KapsoSend to=${recipientPhone} phone_number_id=${phoneNumberId} reply_type=${replyType}`,
  );

  if (replyType === 'buttons' && Array.isArray(reply.buttons) && reply.buttons.length > 0) {
    return withKapsoRetry(
      () => client.messages.interactiveSender.sendButtons({
        phoneNumberId,
        to: recipientPhone,
        bodyText: normalizeWhatsAppText(reply.reply_text || ''),
        buttons: reply.buttons.slice(0, 3).map(button => ({
          id: String(button.id),
          title: String(button.title).slice(0, 20),
        })),
      }),
      `sendButtons(${recipientPhone})`,
    );
  }

  if (replyType === 'list' && reply.list_payload?.sections?.length) {
    return withKapsoRetry(
      () => client.messages.interactiveSender.sendList({
        phoneNumberId,
        to: recipientPhone,
        bodyText: normalizeWhatsAppText(reply.reply_text || ''),
        buttonText: String(reply.list_payload.button_text || 'Ver opciones').slice(0, 20),
        sections: reply.list_payload.sections.map(section => ({
          title: String(section.title).slice(0, 24),
          rows: (section.rows || []).map(row => ({
            id: String(row.id),
            title: String(row.title).slice(0, 24),
            description: row.description ? String(row.description).slice(0, 72) : undefined,
          })),
        })),
      }),
      `sendList(${recipientPhone})`,
    );
  }

  if (replyType === 'reaction' && reply.reaction?.message_id && reply.reaction?.emoji) {
    return withKapsoRetry(
      () => client.messages.reactionSender.send({
        phoneNumberId,
        to: recipientPhone,
        messageId: reply.reaction.message_id,
        emoji: reply.reaction.emoji,
      }),
      `sendReaction(${recipientPhone})`,
    );
  }

  if (replyType === 'image' && reply.image_url) {
    return withKapsoRetry(
      () => client.messages.imageSender.send({
        phoneNumberId,
        to: recipientPhone,
        link: reply.image_url,
        caption: reply.image_caption ? normalizeWhatsAppText(reply.image_caption) : undefined,
      }),
      `sendImage(${recipientPhone})`,
    );
  }

  if (replyType === 'document' && reply.document?.url && reply.document?.filename) {
    return withKapsoRetry(
      () => client.messages.documentSender.send({
        phoneNumberId,
        to: recipientPhone,
        link: reply.document.url,
        filename: reply.document.filename,
        caption: reply.document.caption ? normalizeWhatsAppText(reply.document.caption) : undefined,
      }),
      `sendDocument(${recipientPhone})`,
    );
  }

  return sendKapsoText(recipientPhone, phoneNumberId, reply.reply_text || '');
}

function validateWebhook(req, res) {
  if (!KAPSO_WEBHOOK_SECRET) return true;

  const signature = req.headers['x-webhook-signature'];
  const signatureStr = Array.isArray(signature) ? signature[0] : signature;
  const rawBody = req.rawBody;

  if (signatureStr && rawBody) {
    const hmac = crypto.createHmac('sha256', KAPSO_WEBHOOK_SECRET);
    hmac.update(rawBody);
    const computedSignature = hmac.digest('hex');
    if (computedSignature !== signatureStr) {
      res.status(401).json({ error: 'unauthorized', message: 'invalid signature' });
      return false;
    }
    return true;
  }

  const incomingSecret = req.headers['x-webhook-secret'];
  const incomingSecretStr = Array.isArray(incomingSecret) ? incomingSecret[0] : incomingSecret;
  if (!incomingSecretStr || incomingSecretStr !== KAPSO_WEBHOOK_SECRET) {
    res.status(401).json({ error: 'unauthorized' });
    return false;
  }

  return true;
}

app.get('/health', (_req, res) => {
  res.status(200).json({ status: 'ok', bridge: 'kapso', timestamp: new Date().toISOString() });
});

app.get('/debug/kapso', (_req, res) => {
  res.status(200).type('html').send(renderKapsoDebugHtml());
});

app.get('/debug/kapso/data', async (_req, res) => {
  try {
    const [fastapiEventsResult, fastapiConfigResult] = await Promise.allSettled([
      fetchFastApiDebugJson('/api/v1/kapso/debug/events?limit=100'),
      fetchFastApiDebugJson('/api/v1/kapso/debug/config'),
    ]);

    res.status(200).json({
      bridge_config: getBridgeDebugConfig(),
      bridge_events: bridgeDebugEvents,
      fastapi_config: fastapiConfigResult.status === 'fulfilled' ? fastapiConfigResult.value : { error: String(fastapiConfigResult.reason) },
      fastapi_events: fastapiEventsResult.status === 'fulfilled' ? fastapiEventsResult.value.events : [{
        timestamp: new Date().toISOString(),
        source: 'bridge',
        stage: 'fastapi_debug_error',
        payload: { error: String(fastapiEventsResult.reason) },
      }],
    });
  } catch (error) {
    res.status(500).json({ error: String(error) });
  }
});

app.post('/webhook/kapso', async (req, res) => {
  try {
    if (!validateWebhook(req, res)) return;

    const dataArray = extractDataArray(req.body);
    addBridgeDebugEvent('webhook_received', { records: dataArray.length });
    console.log(`[KapsoBridge] webhook recibido records=${dataArray.length}`);
    if (!dataArray.length) {
      res.status(400).json({ error: 'empty_batch' });
      return;
    }

    const groupedPayloads = new Map();
    for (const item of dataArray) {
      accumulateMessage(item, groupedPayloads);
    }

    if (!groupedPayloads.size) {
      res.status(400).json({ error: 'no_valid_messages' });
      return;
    }

    addBridgeDebugEvent('webhook_grouped', { conversations: groupedPayloads.size });
    console.log(`[KapsoBridge] webhook agrupado conversations=${groupedPayloads.size}`);

    res.status(200).json({ status: 'received', groups: groupedPayloads.size });

    const now = Date.now();
    cleanupProcessedMessages(now);

    for (const [_from, sqlPayload] of groupedPayloads.entries()) {
      const messageId = sqlPayload.message_id;
      if (messageId) {
        const existing = processedMessageIds.get(messageId);
        if (existing) {
          if (existing.status === 'processing' && now - existing.updatedAt > PROCESSING_MESSAGE_TTL_MS) {
            processedMessageIds.delete(messageId);
          } else {
            continue;
          }
        }
        processedMessageIds.set(messageId, { status: 'processing', updatedAt: now });
      }

      const queueKey = `contact:${sqlPayload.from}`;
      const previous = threadQueues.get(queueKey) ?? Promise.resolve();
      let processedOk = false;

      const current = previous
        .catch(() => {})
        .then(async () => {
          addBridgeDebugEvent('message_processing_start', {
            from: sqlPayload.from,
            phone_number_id: sqlPayload.phone_number_id,
            message_id: sqlPayload.message_id,
          });
          console.log(
            `[KapsoBridge] procesando from=${sqlPayload.from} phone_number_id=${sqlPayload.phone_number_id} message_id=${sqlPayload.message_id}`,
          );
          await markKapsoAsRead(sqlPayload.phone_number_id, sqlPayload.message_id);
          const reply = await withTimeout(callInternalAgent(sqlPayload), PROCESS_TIMEOUT_MS);
          const sendResult = await dispatchKapsoResponse(reply);
          addBridgeDebugEvent('message_processing_done', {
            from: sqlPayload.from,
            phone_number_id: sqlPayload.phone_number_id,
            message_id: sqlPayload.message_id,
            send_result: sendResult ?? null,
          });
          console.log(
            `[KapsoBridge] mensaje enviado message_id=${sqlPayload.message_id} kapso_response=${JSON.stringify(sendResult ?? null)}`,
          );
          processedOk = true;
        })
        .catch(error => {
          addBridgeDebugEvent('message_processing_error', {
            from: sqlPayload.from,
            phone_number_id: sqlPayload.phone_number_id,
            message_id: sqlPayload.message_id,
            error: String(error?.message || error),
          });
          console.error('[KapsoBridge] Error procesando mensaje:', error?.stack || error);
        })
        .finally(() => {
          if (threadQueues.get(queueKey) === current) {
            threadQueues.delete(queueKey);
          }
          if (messageId) {
            if (processedOk) {
              processedMessageIds.set(messageId, { status: 'done', updatedAt: Date.now() });
            } else {
              processedMessageIds.delete(messageId);
            }
          }
        });

      threadQueues.set(queueKey, current);
      current.catch(error => {
        console.error('[KapsoBridge] Error inesperado en cola:', error);
      });
    }
  } catch (error) {
    console.error('[KapsoBridge] Error en webhook:', error);
    if (!res.headersSent) {
      res.status(500).json({ error: 'internal_server_error' });
    }
  }
});

app.listen(PORT, () => {
  console.log(`[KapsoBridge] escuchando en http://localhost:${PORT}`);
});
