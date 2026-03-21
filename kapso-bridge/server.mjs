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
const INTERNAL_AGENT_API_URL = process.env.INTERNAL_AGENT_API_URL || 'http://127.0.0.1:8080/api/v1/kapso/inbound';
const KAPSO_INTERNAL_TOKEN = process.env.KAPSO_INTERNAL_TOKEN || '';

const client = new WhatsAppClient({
  baseUrl: KAPSO_BASE_URL,
  kapsoApiKey: KAPSO_API_KEY,
});

const app = express();
const threadQueues = new Map();
const processedMessageIds = new Map();
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

  const response = await fetch(INTERNAL_AGENT_API_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify(sqlPayload),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Backend FastAPI respondió ${response.status}: ${body}`);
  }

  return response.json();
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

app.post('/webhook/kapso', async (req, res) => {
  try {
    if (!validateWebhook(req, res)) return;

    const dataArray = extractDataArray(req.body);
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
          await markKapsoAsRead(sqlPayload.phone_number_id, sqlPayload.message_id);
          const reply = await withTimeout(callInternalAgent(sqlPayload), PROCESS_TIMEOUT_MS);
          await dispatchKapsoResponse(reply);
          processedOk = true;
        })
        .catch(error => {
          console.error('[KapsoBridge] Error procesando mensaje:', error);
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
