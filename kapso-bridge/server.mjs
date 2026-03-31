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

const DEFAULT_EMPRESA_ID = process.env.DEFAULT_EMPRESA_ID || '1';

const DEBUG_FLAG = /^(1|true|yes|on)$/i;

const KAPSO_PUBLIC_DEBUG = DEBUG_FLAG.test(String(process.env.KAPSO_PUBLIC_DEBUG || ''));

const KAPSO_DEBUG_TOKEN = process.env.KAPSO_DEBUG_TOKEN || '';



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

const DEFAULT_EMPTY_REPLY_TEXT = 'Hola, te leo. ¿En qué puedo ayudarte?';

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



function appendDebugToken(pathname, token) {

  if (!token) return pathname;

  const url = new URL(pathname, 'http://localhost');

  url.searchParams.set('token', token);

  return `${url.pathname}${url.search}`;

}



function extractAccessToken(req) {

  const headerToken = req.headers['x-kapso-debug-token'] ?? req.headers['x-kapso-internal-token'];

  if (typeof headerToken === 'string' && headerToken.trim()) return headerToken.trim();

  if (Array.isArray(headerToken) && headerToken[0]) return String(headerToken[0]).trim();

  const authHeader = req.headers.authorization;

  if (typeof authHeader === 'string' && authHeader.toLowerCase().startsWith('bearer ')) {

    return authHeader.slice(7).trim();

  }

  const queryToken = req.query?.token;

  if (typeof queryToken === 'string' && queryToken.trim()) return queryToken.trim();

  if (Array.isArray(queryToken) && queryToken[0]) return String(queryToken[0]).trim();

  return '';

}



function isLoopbackRequest(req) {

  const candidates = [req.ip, req.socket?.remoteAddress];

  return candidates.some(value => value === '127.0.0.1' || value === '::1' || value === '::ffff:127.0.0.1');

}



function requireDebugAccess(req, res) {

  if (KAPSO_PUBLIC_DEBUG || DEBUG_FLAG.test(String(process.env.DEBUG || '')) || isLoopbackRequest(req)) {

    return true;

  }

  const allowedTokens = new Set([KAPSO_DEBUG_TOKEN, KAPSO_INTERNAL_TOKEN].filter(Boolean));

  if (!allowedTokens.size) {

    res.status(503).json({ error: 'debug_disabled' });

    return false;

  }

  if (!allowedTokens.has(extractAccessToken(req))) {

    res.status(401).json({ error: 'unauthorized' });

    return false;

  }

  return true;

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



function getFastApiBaseUrl() {

  return INTERNAL_AGENT_API_URL.replace(/\/api\/v1\/kapso\/inbound$/, '');

}



async function proxyFastApiRequest(req, res, pathname) {

  const headers = {};

  if (KAPSO_INTERNAL_TOKEN) {

    headers['x-kapso-internal-token'] = KAPSO_INTERNAL_TOKEN;

  }

  if (req.headers['content-type']) {

    headers['content-type'] = req.headers['content-type'];

  }



  const targetUrl = new URL(pathname, `${getFastApiBaseUrl()}/`).toString();

  const requestInit = {

    method: req.method,

    headers,

  };



  if (req.method !== 'GET' && req.method !== 'HEAD') {

    requestInit.body = JSON.stringify(req.body ?? {});

  }



  try {

    const response = await fetch(targetUrl, requestInit);

    const contentType = response.headers.get('content-type') || 'application/json; charset=utf-8';

    const bodyText = await response.text();

    res.status(response.status);

    res.set('Content-Type', contentType);

    res.send(bodyText);

  } catch (error) {

    res.status(502).json({

      error: 'fastapi_proxy_error',

      detail: String(error?.message || error),

      target: pathname,

    });

  }

}

function buildKapsoInteractions(bridgeEvents = [], fastapiEvents = []) {

  const allEvents = [...bridgeEvents, ...fastapiEvents]

    .filter(event => event && event.timestamp && event.stage)

    .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));



  const interactionMap = new Map();



  for (const event of allEvents) {

    const payload = event.payload || {};

    const messageId = payload.message_id || payload.wa_id || payload.id;

    if (!messageId) continue;



    if (!interactionMap.has(messageId)) {

      interactionMap.set(messageId, {

        id: messageId,

        message_id: messageId,

        started_at: event.timestamp,

        status: 'processing',

        tools_used: [],

        agent_runs: [],

        mcp_servers: [],

      });

    }



    const interaction = interactionMap.get(messageId);



    if (event.source === 'fastapi') {

      if (event.stage === 'inbound_received') {

        if (payload.from) interaction.from_phone = payload.from;

        if (payload.contact_name) interaction.contact_name = payload.contact_name;

        if (payload.message_type) interaction.message_type = payload.message_type;

        if (payload.text) interaction.message_text = payload.text;

        if (payload.phone_number_id) interaction.phone_number_id = payload.phone_number_id;
        if (payload.empresa_id != null) interaction.empresa_id = payload.empresa_id;

      }



      if (event.stage === 'inbound_entities_resolved') {
        if (payload.empresa_id != null) interaction.empresa_id = payload.empresa_id;
      }

      if (event.stage === 'run_agent_start') {

        interaction.agent_id = payload.agent_id;

        interaction.memory_session_id = payload.memory_session_id;

        if (payload.conversation_id) interaction.conversation_id = payload.conversation_id;

        if (payload.model) interaction.model_used = payload.model;

        if (payload.mcp_servers) interaction.mcp_servers = [`${payload.mcp_servers} servers`];

      }



      if (event.stage === 'run_agent_done') {

        interaction.agent_id = payload.agent_id;
        if (payload.empresa_id != null) interaction.empresa_id = payload.empresa_id;

        if (payload.conversation_id) interaction.conversation_id = payload.conversation_id;

        if (payload.agent_name) interaction.agent_name = payload.agent_name;

        if (payload.model_used) interaction.model_used = payload.model_used;

        if (payload.response_chars !== undefined) interaction.response_chars = payload.response_chars;

        if (payload.response_preview) interaction.response_preview = payload.response_preview;

        if (payload.reaction_emoji) interaction.reaction_emoji = payload.reaction_emoji;

        if (Array.isArray(payload.tools_used)) interaction.tools_used = payload.tools_used;

        if (Array.isArray(payload.agent_runs)) interaction.agent_runs = payload.agent_runs;

        if (payload.timing) interaction.timing = Object.assign(interaction.timing || {}, payload.timing);

      }



      if (event.stage === 'run_funnel_done') {

        if (payload.timing) interaction.funnel_timing = Object.assign(interaction.funnel_timing || {}, payload.timing);

        if (payload.etapa_nueva !== undefined) interaction.funnel_etapa_nueva = payload.etapa_nueva;

        if (payload.metadata_actualizada) interaction.funnel_metadata_actualizada = payload.metadata_actualizada;

        if (payload.error) interaction.funnel_error = payload.error;

      }



      if (event.stage === 'run_contact_update_done') {

        if (payload.timing) interaction.contact_timing = Object.assign(interaction.contact_timing || {}, payload.timing);

        if (payload.updated_fields) interaction.contact_updated_fields = payload.updated_fields;

        if (payload.error) interaction.contact_error = payload.error;

      }



      if (event.stage === 'http_error' || event.stage === 'exception') {

        interaction.status = 'error';

        interaction.error = payload.error || payload.detail || 'Error en FastAPI';

        interaction.finished_at = event.timestamp;

      }



      if (event.stage === 'slash_command_done') {

        interaction.status = 'ok';

        interaction.finished_at = event.timestamp;

        if (payload.reply_text) interaction.response_preview = payload.reply_text;

        if (payload.command) interaction.message_text = payload.command;

      }

    }



    if (event.source === 'bridge') {

      if (event.stage === 'message_processing_start') {

        if (payload.from) interaction.from_phone = payload.from;

        if (payload.contact_name) interaction.contact_name = payload.contact_name;

        if (payload.message_type) interaction.message_type = payload.message_type;

        if (payload.text) interaction.message_text = payload.text;

        if (payload.phone_number_id) interaction.phone_number_id = payload.phone_number_id;

      }



      if (event.stage === 'call_fastapi_done') {

        if (payload.reply_type) interaction.reply_type = payload.reply_type;

        if (payload.agent_id) interaction.agent_id = payload.agent_id;

        if (payload.conversation_id) interaction.conversation_id = payload.conversation_id;

        if (payload.agent_name) interaction.agent_name = payload.agent_name;

        if (payload.model_used) interaction.model_used = payload.model_used;

        if (payload.response_chars !== undefined) interaction.response_chars = payload.response_chars;

        if (payload.response_preview) interaction.response_preview = payload.response_preview;

        if (payload.reaction_emoji) interaction.reaction_emoji = payload.reaction_emoji;

        if (Array.isArray(payload.tools_used)) interaction.tools_used = payload.tools_used;

        if (Array.isArray(payload.agent_runs)) interaction.agent_runs = payload.agent_runs;

        if (payload.timing) interaction.timing = Object.assign(interaction.timing || {}, payload.timing);

      }



      if (event.stage === 'kapso_send_start') {

        if (payload.to) interaction.from_phone = payload.to;

        if (payload.reply_type) interaction.reply_type = payload.reply_type;

        if (payload.has_reaction && !interaction.reaction_emoji) interaction.reaction_emoji = 'sent';

      }



      if (event.stage === 'kapso_send_reaction_with_text') {

        if (payload.emoji) interaction.reaction_emoji = payload.emoji;

      }



      if (event.stage === 'message_processing_done') {

        interaction.finished_at = event.timestamp;

        interaction.status = (payload.error || payload.send_result?.error) ? 'error' : 'ok';

        if (payload.send_result) interaction.send_result = payload.send_result;

      }



      if (event.stage === 'message_processing_error' || event.stage === 'kapso_presence_error') {

        interaction.status = 'error';

        interaction.error = payload.error || payload.detail || 'Error en bridge';

        interaction.finished_at = event.timestamp;

      }

    }



    if (interaction.started_at && interaction.finished_at) {

      interaction.duration_ms = new Date(interaction.finished_at) - new Date(interaction.started_at);

    }

  }



  // Second pass: infer status & timing from FastAPI events when bridge events are missing

  for (const interaction of interactionMap.values()) {

    // If we never got a bridge "message_processing_done", derive from FastAPI data

    if (interaction.status === 'processing') {

      // If run_agent_done fired, the request completed successfully

      if (interaction.response_preview || interaction.agent_name) {

        interaction.status = 'ok';

      }

    }

    // If no duration_ms yet, use timing.total_ms from run_agent_done payload

    if (interaction.duration_ms == null && interaction.timing?.total_ms != null) {

      interaction.duration_ms = Math.round(interaction.timing.total_ms);

    }

  }



  return Array.from(interactionMap.values()).sort((a, b) => new Date(b.started_at) - new Date(a.started_at));

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



function escapeHtml(value) {

  return String(value ?? '')

    .replace(/&/g, '&amp;')

    .replace(/</g, '&lt;')

    .replace(/>/g, '&gt;')

    .replace(/"/g, '&quot;')

    .replace(/'/g, '&#39;');

}



async function collectKapsoDebugPayload() {

  const [fastapiEventsResult, fastapiConfigResult] = await Promise.allSettled([

    fetchFastApiDebugJson('/api/v1/kapso/debug/events?limit=100'),

    fetchFastApiDebugJson('/api/v1/kapso/debug/config'),

  ]);



  const fastapiEvents = fastapiEventsResult.status === 'fulfilled' ? fastapiEventsResult.value.events : [{

    timestamp: new Date().toISOString(),

    source: 'bridge',

    stage: 'fastapi_debug_error',

    payload: { error: String(fastapiEventsResult.reason) },

  }];



  return {

    bridge_config: getBridgeDebugConfig(),

    bridge_events: bridgeDebugEvents,

    fastapi_config: fastapiConfigResult.status === 'fulfilled' ? fastapiConfigResult.value : { error: String(fastapiConfigResult.reason) },

    fastapi_events: fastapiEvents,

    interactions: buildKapsoInteractions(bridgeDebugEvents, fastapiEvents),

  };

}



const PUBLIC_VISUAL_NODE_IDS = {

  whatsapp: 'n1',

  orch: 'n2',

  conv: 'n3',

  funnel: 'n4',

  contact: 'n5',

  supabase: 'n6',

  openrouter: 'n7',

  storage: 'n8',

  edge_fn: 'n9',

  vision: 'n10',

  t_mcp: 'n11',

  mcp_srv: 'n12',

  t_reaction: 'n13',

  t_nota: 'n14',

  t_calificado: 'n15',

  t_comandos: 'n16',

  t_metadata: 'n17',

  t_update: 'n18',

  t_spam: 'n19',

};



const PUBLIC_VISUAL_ALLOWED_STAGES = new Set([

  'inbound_received',

  'fallback_numero',

  'fallback_agent',

  'inbound_entities_resolved',

  'inbound_messages_persisted',

  'memory_session_resolved',

  'prompt_context_built',

  'run_agent_start',

  'run_agent_done',

  'run_funnel_done',

  'run_contact_update_done',

  'audio_processing',

  'image_processing',

  'document_processing',

  'slash_command_done',

  'call_fastapi_done',

  'kapso_send_done',

  'kapso_send_reaction_with_text',

  'http_error',

  'exception',

]);



const PUBLIC_VISUAL_NODE_META = {

  whatsapp: { label: 'WhatsApp', desc: 'Canal de mensajería', detail: 'Canal de entrada y salida de la conversación.', kind: 'external' },

  orch: { label: 'Orquestador', desc: 'Coordinación central', detail: 'Coordina el flujo entre agentes, memoria, herramientas y canal.', kind: 'orchestrator' },

  conv: { label: 'Conversacional', desc: 'Agente conversacional', detail: 'Gestiona razonamiento y respuesta general del sistema.', kind: 'agent' },

  funnel: { label: 'Embudo', desc: 'Agente de clasificación', detail: 'Evalúa etapa y señales del flujo comercial sin exponer reglas internas.', kind: 'agent' },

  contact: { label: 'Contacto', desc: 'Agente de actualización', detail: 'Actualiza y normaliza información del contacto de forma abstracta.', kind: 'agent' },

  supabase: { label: 'Base de datos', desc: 'Persistencia', detail: 'Guarda estado conversacional y contexto operativo.', kind: 'database' },

  openrouter: { label: 'Motor LLM', desc: 'Proveedor de modelo', detail: 'Procesa inferencia de lenguaje mediante un servicio externo.', kind: 'external' },

  storage: { label: 'Storage', desc: 'Archivos y media', detail: 'Almacena adjuntos y recursos del flujo.', kind: 'database' },

  edge_fn: { label: 'Edge Functions', desc: 'Procesamiento auxiliar', detail: 'Ejecuta lógica desacoplada del flujo principal.', kind: 'external' },

  vision: { label: 'Visión', desc: 'Análisis multimodal', detail: 'Procesa imágenes y contenido visual.', kind: 'external' },

  t_mcp: { label: 'MCP Tools', desc: 'Herramientas dinámicas', detail: 'Activa capacidades externas por protocolo de herramientas.', kind: 'tool' },

  mcp_srv: { label: 'MCP Servers', desc: 'Servidores de herramientas', detail: 'Proveen herramientas integradas al flujo.', kind: 'external' },

  t_reaction: { label: 'Reacciones', desc: 'Acción de canal', detail: 'Envía señales rápidas de interacción al canal.', kind: 'tool' },

  t_nota: { label: 'Notas', desc: 'Registro interno', detail: 'Genera registro operativo sin exponer estructura interna.', kind: 'tool' },

  t_calificado: { label: 'Calificación', desc: 'Actualización de estado', detail: 'Marca el estado comercial del contacto de forma abstracta.', kind: 'tool' },

  t_comandos: { label: 'Comandos', desc: 'Acciones operativas', detail: 'Ejecuta acciones operativas controladas del sistema.', kind: 'tool' },

  t_metadata: { label: 'Metadata', desc: 'Actualización contextual', detail: 'Ajusta datos contextuales sin revelar estructuras sensibles.', kind: 'tool' },

  t_update: { label: 'Actualización', desc: 'Datos de contacto', detail: 'Refresca información del contacto sin exponer campos internos.', kind: 'tool' },

  t_spam: { label: 'Control spam', desc: 'Protección de canal', detail: 'Aplica controles de seguridad y supresión del canal.', kind: 'tool' },

};



function sanitizePublicConstellationGraph(graphData) {

  if (!graphData || !Array.isArray(graphData.nodes)) return null;



  const idMap = new Map();

  const nodes = graphData.nodes.map((node, index) => {

    const publicId = PUBLIC_VISUAL_NODE_IDS[node.id] || `x${index + 1}`;

    const meta = PUBLIC_VISUAL_NODE_META[node.id] || {};

    const kind = String(meta.kind || node.kind || 'external');

    idMap.set(node.id, publicId);

    return {

      id: publicId,

      kind,

      x: typeof node.x === 'number' ? node.x : Math.random(),

      y: typeof node.y === 'number' ? node.y : Math.random(),

      hx: typeof node.x === 'number' ? node.x : Math.random(),

      hy: typeof node.y === 'number' ? node.y : Math.random(),

      r: typeof node.r === 'number' ? node.r : 12,

      color: typeof node.color === 'string' ? node.color : '#818cf8',

      glow: typeof node.glow === 'string' ? node.glow : 'rgba(129,140,248,.25)',

      label: meta.label || (kind === 'tool' ? 'Herramienta' : kind === 'database' ? 'Datos' : kind === 'agent' ? 'Agente' : kind === 'orchestrator' ? 'Orquestador' : 'Servicio'),

      desc: meta.desc || 'Componente del sistema',

      detail: meta.detail || 'Componente abstracto del flujo multiagente.',

    };

  });



  const edges = Array.isArray(graphData.edges)

    ? graphData.edges.map(edge => ({

        from: idMap.get(edge.from),

        to: idMap.get(edge.to),

        dash: Boolean(edge.dash),

      })).filter(edge => edge.from && edge.to)

    : [];



  return { nodes, edges };

}



function matchesPublicVisualEmpresa(event, empresaId = '') {

  if (!empresaId) return true;

  const payload = event?.payload || {};

  if (payload.empresa_id != null) return String(payload.empresa_id) === String(empresaId);

  return true;

}



function sanitizePublicVisualEvent(event) {

  if (!event || !event.timestamp || !PUBLIC_VISUAL_ALLOWED_STAGES.has(event.stage)) return null;

  return {

    timestamp: event.timestamp,

    source: event.source === 'bridge' ? 'bridge' : 'fastapi',

    stage: event.stage,

  };

}



async function collectKapsoPublicVisualPayload(empresaId = '') {

  const fastapiEventsResult = await Promise.allSettled([

    fetchFastApiDebugJson('/api/v1/kapso/debug/events?limit=100'),

  ]);



  const fastapiEvents = fastapiEventsResult[0].status === 'fulfilled' ? fastapiEventsResult[0].value.events : [];



  const events = [...bridgeDebugEvents, ...fastapiEvents]

    .filter(event => matchesPublicVisualEmpresa(event, empresaId))

    .map(sanitizePublicVisualEvent)

    .filter(Boolean)

    .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp))

    .slice(-100);



  return { events };

}



function fmtMs(v) { return v != null ? (v / 1000).toFixed(1) + ' s' : '—'; }



function computeInfraMs(item) {

  const t = item.timing;

  if (!t || t.total_ms == null) return null;

  const infra = t.total_ms - (t.llm_ms || 0) - (t.tool_execution_ms || 0) - (t.mcp_discovery_ms || 0) - (t.graph_build_ms || 0);

  return Math.max(0, Math.round(infra));

}



function timingColorClass(ms) {

  if (ms == null) return '';

  if (ms <= 20000) return 'color:#34d399';

  if (ms <= 30000) return 'color:#fbbf24';

  return 'color:#f87171';

}



function renderTimingCells(item) {

  const t = item.timing || {};

  const totalMs = item.duration_ms != null ? item.duration_ms : (t.total_ms != null ? Math.round(t.total_ms) : null);

  const infraMs = computeInfraMs(item);

  const llmMs = t.llm_ms != null ? Math.round(t.llm_ms) : null;

  const toolMs = t.tool_execution_ms != null ? Math.round(t.tool_execution_ms) : null;



  // Per-agent breakdown

  const runs = Array.isArray(item.agent_runs) ? item.agent_runs : [];

  const agentParts = runs.map(r => {

    const name = escapeHtml(r.agent_name || r.agent_key || '?');

    const ms = r.timing?.total_ms != null ? Math.round(r.timing.total_ms) : null;

    return `<span style="white-space:nowrap">${name}: <b>${fmtMs(ms)}</b></span>`;

  });

  // Also show funnel + contact if separate

  if (item.funnel_timing?.total_ms != null && !runs.some(r => (r.agent_key || '').includes('funnel'))) {

    agentParts.push(`<span style="white-space:nowrap">Funnel: <b>${fmtMs(item.funnel_timing.total_ms)}</b></span>`);

  }

  if (item.contact_timing?.total_ms != null && !runs.some(r => (r.agent_key || '').includes('contact'))) {

    agentParts.push(`<span style="white-space:nowrap">Contact: <b>${fmtMs(item.contact_timing.total_ms)}</b></span>`);

  }



  return `<td style="${timingColorClass(totalMs)}"><b>${fmtMs(totalMs)}</b></td>`

    + `<td>${fmtMs(infraMs)}</td>`

    + `<td>${fmtMs(llmMs)}</td>`

  if (!Array.isArray(items) || !items.length) {

    return '<div style="color:#94a3b8">Sin herramientas.</div>';

  }



  const rows = items.map(item => {

    const durationText = item.duration_ms != null ? (item.duration_ms / 1000).toFixed(1) + ' s' : '—';

    const errorHtml = item.error
      ? '<div style="margin-top:8px;color:#fca5a5"><strong>Error:</strong> ' + escapeHtml(item.error) + '</div>'
      : '';

    return ''
      + '<tr>'
      + '<td>' + escapeHtml(item.tool_name || '—') + '</td>'
      + '<td>' + escapeHtml(item.source || '—') + '</td>'
      + '<td>' + escapeHtml(item.status || 'ok') + '</td>'
      + '<td>' + escapeHtml(durationText) + '</td>'
      + '<td>' + escapeHtml(item.description || '—') + '</td>'
      + '</tr>'
      + '<tr>'
      + '<td colspan="5">'
      + '<div style="margin-bottom:8px"><strong>Input</strong></div>'
      + '<pre>' + escapeHtml(JSON.stringify(item.tool_input || {}, null, 2)) + '</pre>'
      + '<div style="margin:8px 0 8px"><strong>Output</strong></div>'
      + '<pre>' + escapeHtml(item.tool_output || '—') + '</pre>'
      + errorHtml
      + '</td>'
      + '</tr>';

  }).join('');


  return `

    <table style="margin-top:8px">

      <thead>

        <tr>

          <th>Tool</th>

          <th>Source</th>

          <th>Estado</th>

          <th>Tiempo</th>

          <th>Descripción</th>

        </tr>

      </thead>

      <tbody>

        ${rows}

      </tbody>

    </table>`;
  }



  return `

    <table style="margin-top:8px">

      <thead>

        <tr>

          <th>Tool</th>

          <th>Source</th>

          <th>Descripción</th>

        </tr>

      </thead>

      <tbody>

        ${items.map(item => `

          <tr>

            <td>${escapeHtml(item.tool_name || '—')}</td>

            <td>${escapeHtml(item.source || '—')}</td>

            <td>${escapeHtml(item.description || '—')}</td>

          </tr>`).join('')}

      </tbody>

    </table>`;

}



function renderTimingTable(timing = {}) {

  const infraMs = timing.total_ms != null

    ? Math.max(0, Math.round(timing.total_ms - (timing.llm_ms || 0) - (timing.tool_execution_ms || 0) - (timing.mcp_discovery_ms || 0) - (timing.graph_build_ms || 0)))

    : null;

  return `

    <table style="margin-top:8px">

      <thead>

        <tr>

          <th>Total</th>

          <th>Infra</th>

          <th>LLM</th>

          <th>MCP</th>

          <th>Graph</th>

          <th>Tools</th>

        </tr>

      </thead>

      <tbody>

        <tr>

          <td style="${timing.total_ms != null ? (timing.total_ms <= 20000 ? 'color:#34d399' : timing.total_ms <= 30000 ? 'color:#fbbf24' : 'color:#f87171') : ''}"><b>${fmtMs(timing.total_ms)}</b></td>

          <td>${fmtMs(infraMs)}</td>

          <td>${fmtMs(timing.llm_ms)}</td>

          <td>${fmtMs(timing.mcp_discovery_ms)}</td>

          <td>${fmtMs(timing.graph_build_ms)}</td>

          <td>${fmtMs(timing.tool_execution_ms)}</td>

        </tr>

      </tbody>

    </table>`;

}



function renderOverviewGrid(items = []) {

  const validItems = items.filter(item => item && (item.label || item.value));

  if (!validItems.length) {

    return '';

  }



  return `

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:12px 0">

      ${validItems.map(item => `

        <div class="card" style="padding:10px 12px">

          <div class="label">${escapeHtml(item.label || 'Dato')}</div>

          <div style="font-size:14px;font-weight:700;margin-top:6px;word-break:break-word">${escapeHtml(item.value || '—')}</div>

        </div>`).join('')}

    </div>`;

}



function buildExecutionRows(item = {}) {

  const rows = [];

  const toolsCount = Array.isArray(item.tools_used) ? item.tools_used.length : 0;



  if (item.agent_name || item.agent_id) {

    rows.push({

      stage: 'Kapso',

      name: item.agent_name || `Agente ${item.agent_id}`,

      type: 'Agente resuelto',

      model: item.model_used || '—',

      iterations: '—',

      tools: toolsCount,

      conversation: item.conversation_id || '—',

    });

  }



  const agentRuns = Array.isArray(item.agent_runs) ? item.agent_runs : [];

  for (const [index, run] of agentRuns.entries()) {

    rows.push({

      stage: 'LangGraph',

      name: run.agent_name || run.agent_key || `Agente ${index + 1}`,

      type: run.agent_kind || 'agent',

      model: run.model_used || item.model_used || '—',

      iterations: run.llm_iterations != null ? String(run.llm_iterations) : '—',

      tools: Array.isArray(run.tools_used) ? run.tools_used.length : 0,

      conversation: run.conversation_id || item.conversation_id || '—',

    });

  }



  return rows;

}



function renderExecutionSummary(item = {}) {

  const agentRuns = Array.isArray(item.agent_runs) ? item.agent_runs : [];

  const toolsUsed = Array.isArray(item.tools_used) ? item.tools_used : [];

  const summaryCards = [

    { label: 'Agente Kapso', value: item.agent_name || (item.agent_id ? `ID ${item.agent_id}` : '—') },

    { label: 'Conversación', value: item.conversation_id || '—' },

    { label: 'Memoria', value: item.memory_session_id || '—' },

    { label: 'Reply', value: item.reply_type || 'text' },

    { label: 'Trazas LangGraph', value: String(agentRuns.length) },

    { label: 'Herramientas', value: String(toolsUsed.length) },

  ];

  const executionRows = buildExecutionRows(item);



  return `

    ${renderOverviewGrid(summaryCards)}

    <table style="margin-top:8px">

      <thead>

        <tr>

          <th>Etapa</th>

          <th>Nombre</th>

          <th>Tipo</th>

          <th>Modelo</th>

          <th>Iteraciones</th>

          <th>Tools</th>

          <th>Conversation</th>

        </tr>

      </thead>

      <tbody>

        ${executionRows.length ? executionRows.map(row => `

          <tr>

            <td>${escapeHtml(row.stage)}</td>

            <td>${escapeHtml(row.name)}</td>

            <td>${escapeHtml(row.type)}</td>

            <td>${escapeHtml(row.model)}</td>

            <td>${escapeHtml(row.iterations)}</td>

            <td>${escapeHtml(String(row.tools))}</td>

            <td>${escapeHtml(row.conversation)}</td>

          </tr>`).join('') : `

          <tr>

            <td colspan="7" style="padding:16px;color:#94a3b8">Sin datos de ejecución todavía.</td>

          </tr>`}

      </tbody>

    </table>`;

}



function renderAgentRuns(agentRuns = []) {

  if (!Array.isArray(agentRuns) || !agentRuns.length) {

    return '<div style="color:#94a3b8">Esta interacción no tiene trazas detalladas de agentes todavía.</div>';

  }



  return agentRuns.map((run, index) => `

    <details style="margin-top:12px">

      <summary>${escapeHtml(run.agent_name || run.agent_key || `Agente ${index + 1}`)} · ${escapeHtml(run.agent_kind || 'agent')} · ${escapeHtml(run.model_used || '—')}</summary>

      <div style="margin-top:12px">

        <div style="margin-bottom:10px"><strong>Agent key:</strong> ${escapeHtml(run.agent_key || '—')}</div>

        <div style="margin-bottom:10px"><strong>Conversation:</strong> ${escapeHtml(run.conversation_id || '—')}</div>

        <div style="margin-bottom:10px"><strong>Memory session:</strong> ${escapeHtml(run.memory_session_id || '—')}</div>

        <div style="margin-bottom:10px"><strong>LLM iterations:</strong> ${escapeHtml(run.llm_iterations ?? 0)}</div>

        <div style="margin:12px 0 6px"><strong>Timing</strong></div>

        ${renderTimingTable(run.timing || {})}

        <div style="margin:12px 0 6px"><strong>Herramientas disponibles</strong></div>

        ${renderAvailableToolList(run.available_tools || [])}

        <div style="margin:12px 0 6px"><strong>Herramientas ejecutadas</strong></div>

        ${renderToolList(run.tools_used || [])}

        <details style="margin-top:12px">

          <summary>Prompts</summary>

          <div style="margin-top:12px">

            <div style="margin:0 0 6px"><strong>System prompt</strong></div>

            <pre>${escapeHtml(run.system_prompt || '')}</pre>

            <div style="margin:12px 0 6px"><strong>User prompt</strong></div>

            <pre>${escapeHtml(run.user_prompt || '')}</pre>

          </div>

        </details>

      </div>

    </details>`).join('');

}



function renderKapsoBasicHtml(debugData, debugToken = '') {

  const interactions = Array.isArray(debugData?.interactions) ? debugData.interactions : [];

  const okCount = interactions.filter(item => item.status === 'ok').length;

  const errorCount = interactions.filter(item => item.status === 'error').length;

  const withTiming = interactions.filter(item => item.duration_ms != null || item.timing?.total_ms != null);

  const avgDuration = withTiming.length

    ? Math.round(withTiming.reduce((acc, item) => acc + (item.duration_ms || item.timing?.total_ms || 0), 0) / withTiming.length)

    : null;

  const avgLlm = withTiming.length

    ? Math.round(withTiming.reduce((acc, item) => acc + (item.timing?.llm_ms || 0), 0) / withTiming.length)

    : null;

  const avgInfra = withTiming.length

    ? Math.round(withTiming.reduce((acc, item) => {

        const t = item.timing;

        if (!t || t.total_ms == null) return acc;

        return acc + Math.max(0, t.total_ms - (t.llm_ms || 0) - (t.tool_execution_ms || 0) - (t.mcp_discovery_ms || 0) - (t.graph_build_ms || 0));

      }, 0) / withTiming.length)

    : null;



  const interactionRows = interactions.length

    ? interactions.map((item, index) => `

        <tr>

          <td>${escapeHtml(item.started_at ? new Date(item.started_at).toLocaleString() : '—')}</td>

          <td>${escapeHtml(item.contact_name || '—')}</td>

          <td>${escapeHtml(item.from_phone || '—')}</td>

          <td>${escapeHtml(item.message_type || 'text')}</td>

          <td style="max-width:220px;overflow:hidden;word-break:break-word">${(()=>{const t=item.message_text||'—';if(t.length<=120)return `<span style="white-space:pre-wrap">${escapeHtml(t)}</span>`;const id=`msg-${index}`;return `<span id="${id}-short" style="white-space:pre-wrap">${escapeHtml(t.slice(0,120))}<span>... </span><a href="#" style="color:#93c5fd;font-size:11px" onclick="document.getElementById('${id}-short').style.display='none';document.getElementById('${id}-full').style.display='';return false">leer más</a></span><span id="${id}-full" style="display:none;white-space:pre-wrap">${escapeHtml(t)} <a href="#" style="color:#93c5fd;font-size:11px" onclick="document.getElementById('${id}-full').style.display='none';document.getElementById('${id}-short').style.display='';return false">ver menos</a></span>`;})()}</td>

          <td>${escapeHtml(item.agent_name || '—')}</td>

          <td>${escapeHtml(item.model_used || '—')}</td>

          <td>${escapeHtml(item.reply_type || 'text')}</td>

          <td>${escapeHtml(item.reaction_emoji || '—')}</td>

          ${renderTimingCells(item)}

          <td>${escapeHtml(item.status || 'processing')}</td>

           <td><a href="#interaction-${index}" style="color:#93c5fd">Ver detalle</a></td>

        </tr>`).join('')

    : '<tr><td colspan="16" style="padding:20px;color:#94a3b8">Sin interacciones todavía.</td></tr>';



  const interactionDetails = interactions.length

    ? interactions.map((item, index) => `

      <details class="section" id="interaction-${index}">

        <summary>${escapeHtml(item.contact_name || item.from_phone || item.message_id || `Interacción ${index + 1}`)} · ${escapeHtml(item.status || 'processing')} · ${escapeHtml(item.duration_ms != null ? `${(item.duration_ms / 1000).toFixed(1)} s` : '—')}</summary>

        <div style="margin-top:12px">

          <div style="margin-bottom:8px"><strong>Message ID:</strong> ${escapeHtml(item.message_id || '—')}</div>

          <div style="margin:12px 0 6px"><strong>Error</strong></div>

          <pre>${escapeHtml(item.error || '—')}</pre>

          <div style="margin-bottom:8px"><strong>Mensaje:</strong></div>

          <pre>${escapeHtml(item.message_text || '—')}</pre>

          <div style="margin:12px 0 6px"><strong>Respuesta preview</strong></div>

          <pre>${escapeHtml(item.response_preview || '—')}</pre>

          <div style="margin:12px 0 6px"><strong>Embudo en metadata</strong></div>

          <pre>${escapeHtml(JSON.stringify({

            etapa_nueva: item.funnel_etapa_nueva ?? null,

            metadata_actualizada: item.funnel_metadata_actualizada ?? null,

            error: item.funnel_error ?? null,

          }, null, 2))}</pre>

          <div style="margin:12px 0 6px"><strong>Timing global</strong></div>

          ${renderTimingTable(item.timing || {})}

          <div style="margin:12px 0 6px"><strong>Resumen de ejecución</strong></div>

          ${renderExecutionSummary(item)}

          <div style="margin:12px 0 6px"><strong>Tools globales</strong></div>

          ${renderToolList(item.tools_used || [])}

          <div style="margin:12px 0 6px"><strong>Trazas detalladas del agente</strong></div>

          ${renderAgentRuns(item.agent_runs || [])}

        </div>

      </details>`).join('')

    : '';



  return `<!doctype html>

<html lang="es">

<head>

  <meta charset="utf-8">

  <meta name="viewport" content="width=device-width, initial-scale=1">

  <title>Kapso Debug Básico</title>

  <style>

    body{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}

    .top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}

    .title{font-size:20px;font-weight:700}

    .actions a{color:#93c5fd;text-decoration:none;margin-left:12px}

    .stats{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:12px;margin-bottom:16px}

    .card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px}

    .label{font-size:11px;color:#94a3b8;text-transform:uppercase}

    .value{font-size:22px;font-weight:700;margin-top:6px}

    table{width:100%;border-collapse:collapse;background:#111827;border:1px solid #334155}

    th,td{padding:10px;border-bottom:1px solid #334155;text-align:left;vertical-align:top;font-size:12px}

    th{background:#1e293b;color:#93c5fd}

    .section{margin-top:18px}

    details{margin-top:12px;background:#111827;border:1px solid #334155;border-radius:8px;padding:12px}

    summary{cursor:pointer;font-weight:700}

    pre{white-space:pre-wrap;word-break:break-word;color:#cbd5e1;font-size:12px}

  </style>

</head>

<body>

  <div class="top">

    <div class="title">Kapso Debug Básico</div>

    <div class="actions">

      <span id="last-update" style="color:#94a3b8;font-size:11px"></span>

      <button id="toggle-auto" style="background:#16a34a;color:#fff;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px">⏸ Pausar</button>

      <a href="${appendDebugToken('/debug/kapso', debugToken)}">Refrescar</a>

      <a href="${appendDebugToken('/debug/kapso/data', debugToken)}" target="_blank" rel="noreferrer">Ver JSON</a>

      <a href="${appendDebugToken('/debug/kapso/visual', debugToken)}" style="background:#6366f1;color:#fff;padding:4px 10px;border-radius:6px;text-decoration:none;font-size:12px">Ver visual</a>

    </div>

  </div>



  <div class="stats">

    <div class="card"><div class="label">Total</div><div class="value">${interactions.length}</div></div>

    <div class="card"><div class="label">OK</div><div class="value">${okCount}</div></div>

    <div class="card"><div class="label">Errores</div><div class="value">${errorCount}</div></div>

    <div class="card"><div class="label">Tiempo AVG</div><div class="value">${avgDuration != null ? `${(avgDuration / 1000).toFixed(1)} s` : '—'}</div></div>

    <div class="card"><div class="label">LLM AVG</div><div class="value">${avgLlm != null ? `${(avgLlm / 1000).toFixed(1)} s` : '—'}</div></div>

    <div class="card"><div class="label">Infra AVG</div><div class="value">${avgInfra != null ? `${(avgInfra / 1000).toFixed(1)} s` : '—'}</div></div>

  </div>



  <div class="section">

    <table>

      <thead>

        <tr>

          <th>Hora</th>

          <th>Contacto</th>

          <th>Teléfono</th>

          <th>Tipo</th>

          <th>Mensaje</th>

          <th>Agente</th>

          <th>Modelo</th>

          <th>Reply</th>

          <th>Rx</th>

          <th style="min-width:60px">Total</th>

          <th style="min-width:50px">Infra</th>

          <th style="min-width:50px">LLM</th>

          <th style="min-width:50px">Tools</th>

          <th>Agentes</th>

          <th>Status</th>

          <th>Detalle</th>

        </tr>

      </thead>

      <tbody>${interactionRows}</tbody>

    </table>

  </div>



  <div id="interaction-details">${interactionDetails}</div>



  <details class="section">

    <summary>Bridge Config</summary>

    <pre id="bridge-config">${escapeHtml(JSON.stringify(debugData.bridge_config, null, 2))}</pre>

  </details>



  <details class="section">

    <summary>FastAPI Config</summary>

    <pre id="fastapi-config">${escapeHtml(JSON.stringify(debugData.fastapi_config, null, 2))}</pre>

  </details>



  <details class="section">

    <summary>JSON completo</summary>

    <pre id="json-completo">${escapeHtml(JSON.stringify(debugData, null, 2))}</pre>

  </details>

<script>

(function(){

  const DEBUG_TOKEN = new URLSearchParams(window.location.search).get('token') || ${JSON.stringify(debugToken || '')};
  function debugPath(path){
    if(!DEBUG_TOKEN)return path;
    var u=new URL(path,window.location.origin);
    u.searchParams.set('token',DEBUG_TOKEN);
    return u.pathname+u.search;
  }
  function fetchDebug(path,init){
    return fetch(debugPath(path),init);
  }

  const POLL_INTERVAL = 5000;

  let autoRefresh = true;

  let timer = null;



  function esc(v){ return String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  function fms(v){ return v!=null?(v/1000).toFixed(1)+' s':'—'; }

  function tcls(ms){ if(ms==null)return ''; if(ms<=20000)return 'color:#34d399'; if(ms<=30000)return 'color:#fbbf24'; return 'color:#f87171'; }

  function infraMs(item){

    var t=item.timing; if(!t||t.total_ms==null)return null;

    return Math.max(0,Math.round(t.total_ms-(t.llm_ms||0)-(t.tool_execution_ms||0)-(t.mcp_discovery_ms||0)-(t.graph_build_ms||0)));

  }

  function agentBreakdown(item){

    var runs=Array.isArray(item.agent_runs)?item.agent_runs:[];

    var parts=runs.map(function(r){

      var name=esc(r.agent_name||r.agent_key||'?');

      var ms=r.timing&&r.timing.total_ms!=null?Math.round(r.timing.total_ms):null;

      return '<span style="white-space:nowrap">'+name+': <b>'+fms(ms)+'</b></span>';

    });

    if(item.funnel_timing&&item.funnel_timing.total_ms!=null&&!runs.some(function(r){return (r.agent_key||'').indexOf('funnel')>=0})){

      parts.push('<span style="white-space:nowrap">Funnel: <b>'+fms(item.funnel_timing.total_ms)+'</b></span>');

    }

    if(item.contact_timing&&item.contact_timing.total_ms!=null&&!runs.some(function(r){return (r.agent_key||'').indexOf('contact')>=0})){

      parts.push('<span style="white-space:nowrap">Contact: <b>'+fms(item.contact_timing.total_ms)+'</b></span>');

    }

    return parts.length?parts.join('<br>'):'—';

  }



  function renderRow(item, idx){

    var t=item.timing||{};

    var totalMs=item.duration_ms!=null?item.duration_ms:(t.total_ms!=null?Math.round(t.total_ms):null);

    var inf=infraMs(item);

    var llm=t.llm_ms!=null?Math.round(t.llm_ms):null;

    var tools=t.tool_execution_ms!=null?Math.round(t.tool_execution_ms):null;

    return '<tr>'

      +'<td>'+esc(item.started_at?new Date(item.started_at).toLocaleString():'—')+'</td>'

      +'<td>'+esc(item.contact_name||'—')+'</td>'

      +'<td>'+esc(item.from_phone||'—')+'</td>'

      +'<td>'+esc(item.message_type||'text')+'</td>'

      +(function(){var t=item.message_text||'—';if(t.length<=120)return '<td style="max-width:220px;overflow:hidden;word-break:break-word;white-space:pre-wrap">'+esc(t)+'</td>';var id='msg-dyn-'+idx;return '<td style="max-width:220px;overflow:hidden;word-break:break-word">'+'<span id="'+id+'-short" style="white-space:pre-wrap">'+esc(t.slice(0,120))+'<span>... </span><a href="#" style="color:#93c5fd;font-size:11px" onclick="document.getElementById(\''+id+'-short\').style.display=\'none\';document.getElementById(\''+id+'-full\').style.display=\'\';return false">leer más</a></span>'+'<span id="'+id+'-full" style="display:none;white-space:pre-wrap">'+esc(t)+' <a href="#" style="color:#93c5fd;font-size:11px" onclick="document.getElementById(\''+id+'-full\').style.display=\'none\';document.getElementById(\''+id+'-short\').style.display=\'\';return false">ver menos</a></span>'+'</td>';})()

      +'<td>'+esc(item.agent_name||'—')+'</td>'

      +'<td>'+esc(item.model_used||'—')+'</td>'

      +'<td>'+esc(item.reply_type||'text')+'</td>'

      +'<td>'+esc(item.reaction_emoji||'—')+'</td>'

      +'<td style="'+tcls(totalMs)+'"><b>'+fms(totalMs)+'</b></td>'

      +'<td>'+fms(inf)+'</td>'

      +'<td>'+fms(llm)+'</td>'

      +'<td>'+fms(tools)+'</td>'

      +'<td style="font-size:11px">'+agentBreakdown(item)+'</td>'

      +'<td>'+esc(item.status||'processing')+'</td>'

      +'<td><a href="#interaction-'+idx+'" style="color:#93c5fd">Ver detalle</a></td>'

      +'</tr>';

  }



  function renderTimingTbl(t){

    if(!t)return '<div style="color:#94a3b8">—</div>';

    var inf=t.total_ms!=null?Math.max(0,Math.round(t.total_ms-(t.llm_ms||0)-(t.tool_execution_ms||0)-(t.mcp_discovery_ms||0)-(t.graph_build_ms||0))):null;

    return '<table style="margin-top:8px"><thead><tr><th>Total</th><th>Infra</th><th>LLM</th><th>MCP</th><th>Graph</th><th>Tools</th></tr></thead><tbody><tr>'

      +'<td style="'+tcls(t.total_ms)+'"><b>'+fms(t.total_ms)+'</b></td>'

      +'<td>'+fms(inf)+'</td>'

      +'<td>'+fms(t.llm_ms)+'</td>'

      +'<td>'+fms(t.mcp_discovery_ms)+'</td>'

      +'<td>'+fms(t.graph_build_ms)+'</td>'

      +'<td>'+fms(t.tool_execution_ms)+'</td>'

      +'</tr></tbody></table>';

  }



  function renderTools(items){

    if(!Array.isArray(items)||!items.length) return '<div style="color:#94a3b8">Sin herramientas.</div>';

    return '<table style="margin-top:8px"><thead><tr><th>Tool</th><th>Source</th><th>Estado</th><th>Tiempo</th><th>Descripción</th></tr></thead><tbody>'

      +items.map(function(it){

        return '<tr><td>'+esc(it.tool_name||'—')+'</td><td>'+esc(it.source||'—')+'</td><td>'+esc(it.status||'ok')+'</td><td>'+esc(it.duration_ms!=null?it.duration_ms+' ms':'—')+'</td><td>'+esc(it.description||'—')+'</td></tr>'

          +'<tr><td colspan="5"><div style="margin-bottom:8px"><strong>Input</strong></div><pre>'+esc(JSON.stringify(it.tool_input||{},null,2))+'</pre>'

          +'<div style="margin:8px 0"><strong>Output</strong></div><pre>'+esc(it.tool_output||'—')+'</pre>'

          +(it.error?'<div style="margin-top:8px;color:#fca5a5"><strong>Error:</strong> '+esc(it.error)+'</div>':'')

          +'</td></tr>';

      }).join('')+'</tbody></table>';

  }



  function renderAvailableTools(items){

    if(!Array.isArray(items)||!items.length) return '<div style="color:#94a3b8">Sin herramientas disponibles.</div>';

    return '<table style="margin-top:8px"><thead><tr><th>Tool</th><th>Source</th><th>Descripción</th></tr></thead><tbody>'

      +items.map(function(it){

        return '<tr><td>'+esc(it.tool_name||'—')+'</td><td>'+esc(it.source||'—')+'</td><td>'+esc(it.description||'—')+'</td></tr>';

      }).join('')+'</tbody></table>';

  }



  function renderAgentRuns(agentRuns, interactionIdx){

    if(!Array.isArray(agentRuns)||!agentRuns.length)

      return '<div style="color:#94a3b8">Sin trazas detalladas.</div>';

    return agentRuns.map(function(r,i){

      var runId='run-'+interactionIdx+'-'+i;

      return '<details id="'+runId+'" style="margin-top:12px">'

        +'<summary>'+esc(r.agent_name||r.agent_key||'Agente '+(i+1))+' · '+esc(r.agent_kind||'agent')+' · '+esc(r.model_used||'—')+'</summary>'

        +'<div style="margin-top:12px">'

        +'<div style="margin-bottom:10px"><strong>Agent key:</strong> '+esc(r.agent_key||'—')+'</div>'

        +'<div style="margin-bottom:10px"><strong>Conversation:</strong> '+esc(r.conversation_id||'—')+'</div>'

        +'<div style="margin-bottom:10px"><strong>Memory session:</strong> '+esc(r.memory_session_id||'—')+'</div>'

        +'<div style="margin-bottom:10px"><strong>LLM iterations:</strong> '+esc(r.llm_iterations??0)+'</div>'

        +'<div style="margin:12px 0 6px"><strong>Timing</strong></div>'+renderTimingTbl(r.timing||{})

        +'<div style="margin:12px 0 6px"><strong>Herramientas disponibles</strong></div>'+renderAvailableTools(r.available_tools||[])

        +'<div style="margin:12px 0 6px"><strong>Herramientas ejecutadas</strong></div>'+renderTools(r.tools_used||[])

        +'<details id="'+runId+'-prompts" style="margin-top:12px"><summary>Prompts</summary>'

        +'<div style="margin-top:12px">'

        +'<div style="margin:0 0 6px"><strong>System prompt</strong></div>'

        +'<pre>'+esc(r.system_prompt||'')+'</pre>'

        +'<div style="margin:12px 0 6px"><strong>User prompt</strong></div>'

        +'<pre>'+esc(r.user_prompt||'')+'</pre>'

        +'</div></details>'

        +'</div></details>';

    }).join('');

  }



  function renderDetail(item, idx){

    var funnel=JSON.stringify({etapa_nueva:item.funnel_etapa_nueva??null,metadata_actualizada:item.funnel_metadata_actualizada??null,error:item.funnel_error??null},null,2);

    var agentRuns=Array.isArray(item.agent_runs)?item.agent_runs:[];



    return '<details class="section" id="interaction-'+idx+'">'

      +'<summary>'+esc(item.contact_name||item.from_phone||item.message_id||'Interacción '+(idx+1))+' · '+esc(item.status||'processing')+' · '+esc(item.duration_ms!=null?item.duration_ms+' ms':'—')+'</summary>'

      +'<div style="margin-top:12px">'

      +'<div style="margin-bottom:8px"><strong>Message ID:</strong> '+esc(item.message_id||'—')+'</div>'

      +'<div style="margin:12px 0 6px"><strong>Error</strong></div><pre>'+esc(item.error||'—')+'</pre>'

      +'<div style="margin-bottom:8px"><strong>Mensaje:</strong></div><pre>'+esc(item.message_text||'—')+'</pre>'

      +'<div style="margin:12px 0 6px"><strong>Respuesta preview</strong></div><pre>'+esc(item.response_preview||'—')+'</pre>'

      +'<div style="margin:12px 0 6px"><strong>Embudo en metadata</strong></div><pre>'+esc(funnel)+'</pre>'

      +'<div style="margin:12px 0 6px"><strong>Timing global</strong></div>'+renderTimingTbl(item.timing||{})

      +'<div style="margin:12px 0 6px"><strong>Tools globales</strong></div>'+renderTools(item.tools_used||[])

      +'<div style="margin:12px 0 6px"><strong>Trazas detalladas del agente</strong></div>'+renderAgentRuns(agentRuns, idx)

      +'</div></details>';

  }



  function update(data){

    var items=Array.isArray(data.interactions)?data.interactions:[];

    var ok=items.filter(function(i){return i.status==='ok'}).length;

    var err=items.filter(function(i){return i.status==='error'}).length;

    var wt=items.filter(function(i){return i.duration_ms!=null||(i.timing&&i.timing.total_ms!=null)});

    var avg=wt.length?Math.round(wt.reduce(function(a,i){return a+(i.duration_ms||i.timing&&i.timing.total_ms||0)},0)/wt.length):null;

    var avgLlm=wt.length?Math.round(wt.reduce(function(a,i){return a+(i.timing&&i.timing.llm_ms||0)},0)/wt.length):null;

    var avgInf=wt.length?Math.round(wt.reduce(function(a,i){

      var t=i.timing; if(!t||t.total_ms==null)return a;

      return a+Math.max(0,t.total_ms-(t.llm_ms||0)-(t.tool_execution_ms||0)-(t.mcp_discovery_ms||0)-(t.graph_build_ms||0));

    },0)/wt.length):null;



    var cards=document.querySelectorAll('.card .value');

    if(cards[0])cards[0].textContent=items.length;

    if(cards[1])cards[1].textContent=ok;

    if(cards[2])cards[2].textContent=err;

    if(cards[3])cards[3].textContent=avg!=null?avg+' ms':'—';

    if(cards[4])cards[4].textContent=avgLlm!=null?avgLlm+' ms':'—';

    if(cards[5])cards[5].textContent=avgInf!=null?avgInf+' ms':'—';



    var tbody=document.querySelector('table tbody');

    if(tbody){

      tbody.innerHTML=items.length

        ?items.map(renderRow).join('')

        :'<tr><td colspan="16" style="padding:20px;color:#94a3b8">Sin interacciones todavía.</td></tr>';

    }



    // Preserve open state of ALL details with IDs (interaction + run + prompts)

    var detailsContainer=document.getElementById('interaction-details');

    if(detailsContainer){

      var openSet=new Set();

      detailsContainer.querySelectorAll('details[open][id]').forEach(function(d){

        openSet.add(d.id);

      });

      detailsContainer.innerHTML=items.map(renderDetail).join('');

      openSet.forEach(function(id){

        var el=document.getElementById(id);

        if(el)el.setAttribute('open','');

      });

    }



    var bridgePre=document.getElementById('bridge-config');

    if(bridgePre)bridgePre.textContent=JSON.stringify(data.bridge_config||{},null,2);

    var fastapiPre=document.getElementById('fastapi-config');

    if(fastapiPre)fastapiPre.textContent=JSON.stringify(data.fastapi_config||{},null,2);

    var jsonPre=document.getElementById('json-completo');

    if(jsonPre)jsonPre.textContent=JSON.stringify(data,null,2);



    var ts=document.getElementById('last-update');

    if(ts)ts.textContent='Última actualización: '+new Date().toLocaleTimeString();

  }



  function poll(){

    var scrollY=window.scrollY;

    fetch(debugPath('/debug/kapso/data')).then(function(r){return r.json()}).then(function(data){

      update(data);

      requestAnimationFrame(function(){ window.scrollTo(0,scrollY); });

    }).catch(function(e){console.warn('poll error',e)});

  }



  function toggleAuto(){

    autoRefresh=!autoRefresh;

    var btn=document.getElementById('toggle-auto');

    if(autoRefresh){

      btn.textContent='⏸ Pausar';

      btn.style.background='#16a34a';

      timer=setInterval(poll,POLL_INTERVAL);

    }else{

      btn.textContent='▶ Reanudar';

      btn.style.background='#dc2626';

      clearInterval(timer);

    }

  }



  document.getElementById('toggle-auto').addEventListener('click',toggleAuto);

  timer=setInterval(poll,POLL_INTERVAL);

})();

</script>

</body>

</html>`;





function renderConstellationHtml(graphData, empresasList = [], debugToken = '') {

  const injectedData = graphData ? JSON.stringify(graphData) : 'null';
  const injectedEmpresas = JSON.stringify(empresasList);

  return `<!doctype html>

<html lang="es">

<head>

<meta charset="utf-8">

<meta name="viewport" content="width=device-width, initial-scale=1">

<title>Monica Brain — Neural Map</title>

<style>

@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');

*{margin:0;padding:0;box-sizing:border-box}

html,body{width:100%;height:100%;overflow:hidden;background:#020010;font-family:'Outfit',system-ui,sans-serif;color:#e2e8f0}

canvas{display:block;position:absolute;top:0;left:0}

#back{position:fixed;top:20px;left:20px;z-index:20;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);color:rgba(167,139,250,.8);padding:8px 18px;border-radius:10px;font-size:13px;cursor:pointer;text-decoration:none;backdrop-filter:blur(12px);transition:all .2s}

#back:hover{background:rgba(167,139,250,.12);border-color:rgba(167,139,250,.3);color:#c4b5fd}

#header{position:fixed;top:18px;left:50%;transform:translateX(-50%);z-index:20;text-align:center;pointer-events:none}

#header h1{font-size:20px;font-weight:600;letter-spacing:6px;text-transform:uppercase;background:linear-gradient(135deg,#a78bfa 0%,#6366f1 40%,#818cf8 70%,#c4b5fd 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}

#header p{font-size:11px;letter-spacing:3px;color:rgba(255,255,255,.18);margin-top:2px;text-transform:uppercase}

#tooltip{position:fixed;display:none;z-index:30;background:rgba(8,4,28,.94);border:1px solid rgba(139,92,246,.3);border-radius:14px;padding:16px 20px;max-width:360px;font-size:13px;line-height:1.7;backdrop-filter:blur(16px);box-shadow:0 0 40px rgba(99,102,241,.15),0 12px 40px rgba(0,0,0,.6);pointer-events:none}

#tooltip h3{font-size:15px;margin-bottom:6px;font-weight:600;color:#fff}

#tooltip .tag{display:inline-block;padding:3px 10px;border-radius:6px;font-size:10px;font-weight:600;letter-spacing:.8px;margin-bottom:10px;text-transform:uppercase}

#tooltip .detail{color:rgba(203,213,225,.75);font-size:12px}

#tooltip .detail b{color:#e2e8f0}

#legend{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:20;display:flex;gap:20px;font-size:12px;color:rgba(255,255,255,.35);background:rgba(8,4,28,.6);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:10px 24px;backdrop-filter:blur(12px)}

#legend span{display:flex;align-items:center;gap:6px}

#legend i{display:inline-block;width:10px;height:10px;border-radius:50%;box-shadow:0 0 6px currentColor}

#loader{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:25;color:rgba(167,139,250,.6);font-size:14px;letter-spacing:2px;text-transform:uppercase;pointer-events:none}

#speed-ctrl{position:fixed;top:20px;right:20px;z-index:20;display:none;align-items:center;gap:6px;background:rgba(8,4,28,.7);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:6px 10px;backdrop-filter:blur(12px)}

#speed-ctrl span{font-size:11px;color:rgba(255,255,255,.35);letter-spacing:1px;text-transform:uppercase;margin-right:4px}

#speed-ctrl button{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);color:rgba(203,213,225,.7);font-size:11px;font-weight:500;padding:3px 10px;border-radius:6px;cursor:pointer;transition:all .15s;font-family:inherit}

#speed-ctrl button.active{background:rgba(167,139,250,.25);border-color:rgba(167,139,250,.5);color:#c4b5fd}

#realtime-badge{position:fixed;top:56px;right:20px;z-index:20;font-size:10px;color:rgba(52,211,153,.7);letter-spacing:1px;text-transform:uppercase;display:flex;align-items:center;gap:5px}
#empresa-filter{position:fixed;top:20px;left:120px;z-index:20;display:flex;align-items:center;gap:8px;background:rgba(8,4,28,.7);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:6px 12px;backdrop-filter:blur(12px)}
#empresa-filter label{font-size:11px;color:rgba(255,255,255,.35);letter-spacing:1px;text-transform:uppercase}
#empresa-filter select{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);color:#c4b5fd;font-size:12px;padding:4px 8px;border-radius:6px;cursor:pointer;font-family:inherit;outline:none}
#empresa-filter select:focus{border-color:rgba(167,139,250,.5)}
#empresa-filter select option{background:#1e1b3a;color:#e2e8f0}

#realtime-badge i{width:6px;height:6px;border-radius:50%;background:#34d399;box-shadow:0 0 8px #34d399;animation:pulse-rt 1.5s infinite}

@keyframes pulse-rt{0%,100%{opacity:1}50%{opacity:.3}}

</style>

</head>

<body>

<a href="/debug/kapso" id="back">Panel</a>

<div id="empresa-filter">
  <label>Empresa</label>
  <select id="empresa-sel">
    <option value="">Todas</option>
  </select>
</div>

<div id="header">

  <h1>Monica Brain</h1>

  <p>Neural Architecture Map</p>

</div>

<canvas id="c"></canvas>

<div id="loader">Cargando grafo…</div>

<div id="tooltip"></div>

<div id="legend">

  <span><i style="background:#a78bfa"></i> Orquestador</span>

  <span><i style="background:#fb923c"></i> Agente</span>

  <span><i style="background:#34d399"></i> Herramienta</span>

  <span><i style="background:#60a5fa"></i> Externo</span>

  <span><i style="background:#f472b6"></i> Base de datos</span>

</div>

<script>

const DEBUG_TOKEN=new URLSearchParams(window.location.search).get('token')||${JSON.stringify(debugToken || '')};
function debugPath(path){
  if(!DEBUG_TOKEN)return path;
  const u=new URL(path,window.location.origin);
  u.searchParams.set('token',DEBUG_TOKEN);
  return u.pathname+u.search;
}
const C=document.getElementById('c'),X=C.getContext('2d'),TT=document.getElementById('tooltip');

const LOADER=document.getElementById('loader');

let W,H,mx=-1,my=-1,hovered=null,dragging=null,dragOff={x:0,y:0},t=0,dpr=1;

/* ── Empresa filter ── */
const _empresas = ${injectedEmpresas};
let selectedEmpresaId = '';
(function initEmpresaFilter(){
  const sel=document.getElementById('empresa-sel');
  if(!sel)return;
  _empresas.forEach(function(e){
    const opt=document.createElement('option');
    opt.value=String(e.id);
    opt.textContent=e.nombre;
    sel.appendChild(opt);
  });
  sel.addEventListener('change',function(){
    selectedEmpresaId=sel.value;
  });
})();
function _matchesEmpresaFilter(e){
  if(!selectedEmpresaId)return true;
  var p=e&&e.payload;
  if(!p)return true; // no payload = allow (bridge events etc.)
  if(p.empresa_id!=null) return String(p.empresa_id)===selectedEmpresaId;
  return true; // events without empresa_id pass through
}

let prevMx=0,prevMy=0,dragVx=0,dragVy=0;

const DAMPING=0.97,BOUNCE_MARGIN=0.05;



let NODES=[], EDGES=[];



/* ── Speed control ── */

const SPEED_MULT=2;



/* ── Flow particles ── */

// Each particle: { edgeFrom, edgeTo, progress (0→1), speed, color, r, label, trail[] }

const flowParticles=[];

const PARTICLE_BASE_DURATION=2200; // ms at x1 to travel full edge



/* Map of stage name → list of edges to animate (each edge: [fromId, toId, color]) */

/* Static stages — always animate the same edges */

const STAGE_FLOWS={

  'inbound_received':      [['whatsapp','orch','#60a5fa']],

  'fallback_numero':       [['orch','orch','#f59e0b']],

  'fallback_agent':        [['orch','orch','#f59e0b']],

  'inbound_entities_resolved': [['orch','supabase','#f472b6'],['supabase','orch','#f472b6']],

  'inbound_messages_persisted':[['orch','supabase','#f472b6']],

  'memory_session_resolved':   [['orch','supabase','#f472b6'],['supabase','orch','#f472b6']],

  'prompt_context_built':  [['orch','conv','#a78bfa']],

  'run_agent_start':       [['orch','conv','#a78bfa'],['orch','funnel','#fb923c'],['orch','contact','#fb923c']],

  'slash_command_done':    [['orch','whatsapp','#34d399']],

  'audio_processing':      [['orch','storage','#f472b6'],['orch','edge_fn','#60a5fa']],

  'image_processing':      [['orch','storage','#f472b6'],['orch','vision','#60a5fa'],['vision','openrouter','#60a5fa']],

  'document_processing':   [['orch','storage','#f472b6'],['orch','edge_fn','#60a5fa']],

  'http_error':            [['orch','whatsapp','#ef4444']],

  'exception':             [['orch','whatsapp','#ef4444']],

};



/* Map tool_name → graph node id for dynamic flow building */

const TOOL_NODE_MAP={

  'send_reaction':              't_reaction',

  'guardar_nota':               't_nota',

  'marcar_prospecto_calificado':'t_calificado',

  'ejecutar_comando':           't_comandos',

  'update_metadata':            't_metadata',

  'update_contact_info':        't_update',

  'desactivar_contacto_spam':   't_spam',

};

/* Tools that come from MCP (not in TOOL_NODE_MAP) route to t_mcp */

function toolToNodeId(toolName){ return TOOL_NODE_MAP[toolName] || 't_mcp'; }



/* Build dynamic flows for stages that depend on tools_used */

function buildDynamicFlows(stage, payload){

  const tools = (payload && Array.isArray(payload.tools_used)) ? payload.tools_used : [];

  const toolNames = tools.map(function(t){ return t.tool_name || t.name || ''; });



  if(stage === 'run_agent_done'){

    const flows = [['conv','openrouter','#60a5fa']]; // LLM always runs

    const seen = new Set();

    toolNames.forEach(function(name){

      if(!name) return;

      const nodeId = toolToNodeId(name);

      if(seen.has(nodeId)) return;

      seen.add(nodeId);

      flows.push(['conv', nodeId, '#34d399']);

      // If MCP tool, also animate mcp_srv

      if(nodeId === 't_mcp') flows.push(['t_mcp','mcp_srv','#60a5fa']);

      // If ejecutar_comando, also animate to whatsapp

      if(nodeId === 't_comandos') flows.push(['t_comandos','whatsapp','#fb923c']);

      // If guardar_nota or marcar_calificado, animate to supabase

      if(nodeId === 't_nota' || nodeId === 't_calificado') flows.push([nodeId,'supabase','#f472b6']);

      // If desactivar_spam, animate to supabase

      if(nodeId === 't_spam') flows.push(['t_spam','supabase','#f472b6']);

    });

    // Always end with response to whatsapp

    flows.push(['conv','whatsapp','#a78bfa']);

    return flows;

  }



  if(stage === 'run_funnel_done'){

    const flows = [['funnel','openrouter','#60a5fa']];

    if(toolNames.includes('update_metadata')){

      flows.push(['funnel','t_metadata','#34d399']);

      flows.push(['t_metadata','supabase','#f472b6']);

    }

    flows.push(['funnel','orch','#fb923c']);

    return flows;

  }



  if(stage === 'run_contact_update_done'){

    const flows = [['contact','openrouter','#60a5fa']];

    if(toolNames.includes('update_contact_info') || toolNames.length > 0){

      flows.push(['contact','t_update','#34d399']);

      flows.push(['t_update','supabase','#f472b6']);

    }

    return flows;

  }



  return null; // not a dynamic stage, use STAGE_FLOWS

}




/* Node pulse: when a stage hits, briefly light up nodes */

const nodePulse={}; // nodeId -> { until: timestamp, color }



function triggerFlows(stage, payload){

  const flows = buildDynamicFlows(stage, payload) || STAGE_FLOWS[stage];

  if(!flows)return;

  const duration=PARTICLE_BASE_DURATION/SPEED_MULT;

  flows.forEach(function(f,i){

    const fromId=f[0],toId=f[1],color=f[2];

    // Stagger multiple particles slightly

    setTimeout(function(){

      flowParticles.push({

        fromId:fromId,toId:toId,

        progress:0,

        speed:1/duration,

        color:color,

        r:5,

        trail:[],

        label:stage.replace(/_/g,' '),

      });

      // Pulse both endpoints

      nodePulse[fromId]={until:Date.now()+800,color:color};

      nodePulse[toId]={until:Date.now()+800+duration,color:color};

    },i*180/SPEED_MULT);

  });

}



/* ── Real-time event tracking via SSE + polling fallback ── */

const seenEventKeys=new Set();

let lastPollAt=0;

const POLL_INTERVAL=10000; // Fallback polling (longer since SSE is primary)

let sseConnected=false;



function processSingleEvent(e){

  if(!e||!e.stage)return;
  if(!_matchesEmpresaFilter(e))return;

  const DYNAMIC_STAGES=['run_agent_done','run_funnel_done','run_contact_update_done'];

  const key=(e.timestamp||'')+'|'+(e.stage||'')+'|'+(e.source||'');

  if(seenEventKeys.has(key))return;

  seenEventKeys.add(key);

  if(STAGE_FLOWS[e.stage] || DYNAMIC_STAGES.includes(e.stage)){

    triggerFlows(e.stage, e.payload);

  }

}



function processNewEvents(events){

  if(!Array.isArray(events))return;

  const DYNAMIC_STAGES=['run_agent_done','run_funnel_done','run_contact_update_done'];

  const fresh=[];

  for(let i=0;i<events.length;i++){

    const e=events[i];

    if(!e||!e.stage)continue;
    if(!_matchesEmpresaFilter(e))continue;

    const key=(e.timestamp||'')+'|'+(e.stage||'')+'|'+(e.source||'');

    if(seenEventKeys.has(key))continue;

    seenEventKeys.add(key);

    if(STAGE_FLOWS[e.stage] || DYNAMIC_STAGES.includes(e.stage))fresh.push(e);

  }

  if(!fresh.length)return;

  fresh.sort(function(a,b){return new Date(a.timestamp)-new Date(b.timestamp);});

  let acc=0;

  fresh.forEach(function(ev){

    setTimeout(function(){triggerFlows(ev.stage, ev.payload);},acc);

    acc+=Math.max(180,450/SPEED_MULT);

  });

}



function pollDebugData(){

  if(sseConnected)return; // SSE is handling real-time, skip polling

  const now=Date.now();

  if(now-lastPollAt<POLL_INTERVAL)return;

  lastPollAt=now;

  fetch(debugPath('/debug/kapso/data')).then(function(r){return r.json()}).then(function(data){

    processNewEvents(data.fastapi_events);

  }).catch(function(){});

}


/* ── SSE connection ── */

function connectSSE(){

  const badge=document.getElementById('realtime-badge');

  const badgeIcon=badge?badge.querySelector('i'):null;

  const badgeText=badge;

  

  function setBadge(text,color,glowColor){

    if(badgeIcon){badgeIcon.style.background=color;badgeIcon.style.boxShadow='0 0 8px '+glowColor;}

    if(badgeText)badgeText.lastChild.textContent=text;

  }

  

  const es=new EventSource(debugPath('/debug/kapso/stream'));

  

  es.onopen=function(){

    sseConnected=true;

    setBadge(' Real-time','#34d399','#34d399');

    console.log('[Visual] SSE connected — real-time mode');

  };

  

  es.onmessage=function(msg){

    try{

      const ev=JSON.parse(msg.data);

      if(ev.error){console.warn('[Visual] SSE error:',ev.error);return;}

      processSingleEvent(ev);

    }catch(e){console.warn('[Visual] SSE parse error:',e);}

  };

  

  es.onerror=function(){

    sseConnected=false;

    setBadge(' Polling','#f59e0b','#f59e0b');

    console.warn('[Visual] SSE disconnected — falling back to polling');

    es.close();

    // Reconnect after 5s

    setTimeout(connectSSE,5000);

  };

}


// Start SSE + initial load of existing events

connectSSE();

fetch(debugPath('/debug/kapso/data')).then(function(r){return r.json();}).then(function(data){

  processNewEvents(data.fastapi_events);

}).catch(function(){});



/* ── Load graph schema (injected server-side) ── */

const _injected = ${injectedData};

if(_injected && _injected.nodes){

  NODES=_injected.nodes;

  EDGES=_injected.edges||[];

  NODES.forEach(n=>{n.vx=0;n.vy=0;});

  if(LOADER)LOADER.style.display='none';



  // Seed seen events so existing data doesn't replay as particles

  fetch(debugPath('/debug/kapso/data')).then(function(r){return r.json();}).then(function(data){

    const evts=data.fastapi_events;

    if(Array.isArray(evts)){

      evts.forEach(function(e){

        if(!e||!e.stage)continue;
        if(!_matchesEmpresaFilter(e))continue;

        seenEventKeys.add((e.timestamp||'')+'|'+(e.stage||'')+'|'+(e.source||''));

      });

    }

  }).catch(function(){});



  // Initial demo burst after 800ms (shows MCP + nota flow)

  setTimeout(function(){triggerFlows('inbound_received');},800);

  setTimeout(function(){triggerFlows('run_agent_start');},1600);

  setTimeout(function(){triggerFlows('run_agent_done',_demoPayload);},2600);

}else{

  if(LOADER)LOADER.textContent='Grafo no disponible — reinicia el servidor Python';

}


/* ── Nebula & stars ── */

const stars=Array.from({length:400},()=>({x:Math.random(),y:Math.random(),s:Math.random()*1.2+.3,b:Math.random(),sp:Math.random()*.5+.5}));

const nebulae=[

  {x:.3,y:.35,rx:220,ry:140,color:'rgba(99,102,241,.04)'},

  {x:.7,y:.45,rx:180,ry:120,color:'rgba(244,114,182,.03)'},

  {x:.5,y:.2,rx:250,ry:100,color:'rgba(96,165,250,.03)'},

  {x:.5,y:.7,rx:200,ry:130,color:'rgba(52,211,153,.025)'},

];


function resize(){

  dpr=window.devicePixelRatio||1;

  W=window.innerWidth;H=window.innerHeight;

  C.width=W*dpr;C.height=H*dpr;

  C.style.width=W+'px';C.style.height=H+'px';

  X.setTransform(dpr,0,0,dpr,0,0);

}

window.addEventListener('resize',resize);resize();


function nodePos(n){return{x:n.x*W,y:n.y*H}}


function physics(){

  // Node-to-node collision: repel overlapping nodes
  for(let i=0;i<NODES.length;i++){
    for(let j=i+1;j<NODES.length;j++){
      const a=NODES[i],b=NODES[j];
      const ax=a.x*W,ay=a.y*H,bx=b.x*W,by=b.y*H;
      const dx=bx-ax,dy=by-ay;
      const dist=Math.sqrt(dx*dx+dy*dy)||1;
      const minDist=(a.r+b.r)*1.6;
      if(dist<minDist){
        const overlap=(minDist-dist)/dist*0.02;
        const ox=dx*overlap/W,oy=dy*overlap/H;
        if(a!==dragging){a.x-=ox;a.y-=oy;a.vx-=ox*.3;a.vy-=oy*.3;}
        if(b!==dragging){b.x+=ox;b.y+=oy;b.vx+=ox*.3;b.vy+=oy*.3;}
      }
    }
  }

  for(const n of NODES){

    if(n===dragging)continue;

    if(Math.abs(n.vx)<0.00001&&Math.abs(n.vy)<0.00001)continue;

    n.vx*=DAMPING; n.vy*=DAMPING;

    n.x+=n.vx; n.y+=n.vy;

    if(n.x<BOUNCE_MARGIN){n.x=BOUNCE_MARGIN;n.vx=Math.abs(n.vx)*.4;}

    if(n.x>1-BOUNCE_MARGIN){n.x=1-BOUNCE_MARGIN;n.vx=-Math.abs(n.vx)*.4;}

    if(n.y<BOUNCE_MARGIN){n.y=BOUNCE_MARGIN;n.vy=Math.abs(n.vy)*.4;}

    if(n.y>1-BOUNCE_MARGIN){n.y=1-BOUNCE_MARGIN;n.vy=-Math.abs(n.vy)*.4;}

    if(Math.abs(n.vx)<0.00001)n.vx=0;

    if(Math.abs(n.vy)<0.00001)n.vy=0;

  }

}


let lastFrameTime=performance.now();

function draw(){

  const now=performance.now();

  const dt=(now-lastFrameTime)/1000; // seconds

  lastFrameTime=now;


  t+=.002;

  physics();

  pollDebugData();


  X.clearRect(0,0,W,H);


  // Deep space gradient

  const bg=X.createRadialGradient(W/2,H/2,0,W/2,H/2,Math.max(W,H)*.7);

  bg.addColorStop(0,'#0a0520');bg.addColorStop(.5,'#050214');bg.addColorStop(1,'#020010');

  X.fillStyle=bg;X.fillRect(0,0,W,H);


  // Nebulae

  for(const nb of nebulae){

    const g=X.createRadialGradient(nb.x*W,nb.y*H,0,nb.x*W,nb.y*H,nb.rx);

    const pulse=1+.15*Math.sin(t*1.5+nb.x*4);

    g.addColorStop(0,nb.color.replace(/[\\d.]+\\)$/,(parseFloat(nb.color.match(/[\\d.]+\\)$/)[0])*pulse)+')'));

    g.addColorStop(1,'transparent');

    X.fillStyle=g;

    X.beginPath();X.ellipse(nb.x*W,nb.y*H,nb.rx*pulse,nb.ry*pulse,0,0,6.28);X.fill();

  }


  // Stars

  for(const s of stars){

    const bri=.12+.18*Math.sin(t*s.sp*2+s.b*6.28);

    X.fillStyle='rgba(200,210,255,'+bri+')';

    X.beginPath();X.arc(s.x*W,s.y*H,s.s,0,6.28);X.fill();

  }


  // Update + draw flow particles

  for(let i=flowParticles.length-1;i>=0;i--){

    const p=flowParticles[i];

    const fromNode=NODES.find(n=>n.id===p.fromId);

    const toNode=NODES.find(n=>n.id===p.toId);

    if(!fromNode||!toNode){flowParticles.splice(i,1);continue;}

    p.progress+=p.speed*dt*1000*SPEED_MULT;

    if(p.progress>=1){flowParticles.splice(i,1);continue;}


    const fp=nodePos(fromNode);

    const tp=nodePos(toNode);

    const px=fp.x+(tp.x-fp.x)*p.progress;

    const py=fp.y+(tp.y-fp.y)*p.progress;


    // Trail

    p.trail.push({x:px,y:py});

    if(p.trail.length>18)p.trail.shift();


    // Draw trail

    for(let j=1;j<p.trail.length;j++){

      const alpha=(j/p.trail.length)*0.45;

      const tr=p.trail[j-1],tr2=p.trail[j];

      X.strokeStyle=p.color.replace(')',','+alpha+')').replace('rgb','rgba').replace('rgba','rgba');

      // Use a simpler approach: set globalAlpha

      X.save();

      X.globalAlpha=alpha;

      X.strokeStyle=p.color;

      X.lineWidth=2*(j/p.trail.length);

      X.beginPath();X.moveTo(tr.x,tr.y);X.lineTo(tr2.x,tr2.y);X.stroke();

      X.restore();

    }


    // Draw particle head

    X.save();

    X.shadowColor=p.color;X.shadowBlur=16;

    X.fillStyle=p.color;

    X.beginPath();X.arc(px,py,p.r,0,6.28);X.fill();

    X.shadowBlur=0;X.restore();

  }


  // Edges

  for(const e of EDGES){

    const a=NODES.find(n=>n.id===e.from),b=NODES.find(n=>n.id===e.to);

    if(!a||!b)continue;

    const p1=nodePos(a),p2=nodePos(b);

    const isHov=hovered&&(hovered.id===a.id||hovered.id===b.id);

    const isConnected=hovered&&EDGES.some(ed=>(ed.from===hovered.id||ed.to===hovered.id)&&(ed.from===a.id||ed.to===a.id||ed.from===b.id||ed.to===b.id));


    // Check if any active flow particle is on this edge

    const hasFlow=flowParticles.some(fp=>fp.fromId===e.from&&fp.toId===e.to);


    X.save();


    if(isHov){

      X.shadowColor=a.color;X.shadowBlur=8;

      X.strokeStyle='rgba(255,255,255,.5)';

      X.lineWidth=2;

    }else if(hasFlow){

      X.strokeStyle='rgba(255,255,255,.25)';

      X.lineWidth=1.5;

    }else if(hovered&&!isConnected){

      X.strokeStyle='rgba(255,255,255,.03)';

      X.lineWidth=.5;

    }else{

      X.strokeStyle='rgba(255,255,255,.1)';

      X.lineWidth=.8;

    }

    if(e.dash){X.setLineDash([5,8]);}else{X.setLineDash([]);}

    X.beginPath();X.moveTo(p1.x,p1.y);X.lineTo(p2.x,p2.y);X.stroke();

    X.restore();


    // Ambient edge particle

    if(!hovered||isHov){

      const speed=(t*(.3+a.x*.2))%1;

      const epx=p1.x+(p2.x-p1.x)*speed;

      const epy=p1.y+(p2.y-p1.y)*speed;


      X.fillStyle=isHov?'rgba(255,255,255,.6)':'rgba(255,255,255,.12)';


      X.beginPath();X.arc(epx,epy,isHov?2.5:1.5,0,6.28);X.fill();

    }


    if(e.label&&isHov){

      const mx2=(p1.x+p2.x)/2,my2=(p1.y+p2.y)/2;

      X.font='500 11px Outfit,system-ui,sans-serif';

      X.fillStyle='rgba(255,255,255,.55)';

      X.textAlign='center';X.textBaseline='middle';

      X.fillText(e.label,mx2,my2-10);

    }

  }


  // Nodes

  const nowTs=Date.now();

  for(const n of NODES){

    const p=nodePos(n);

    const isHov=hovered&&hovered.id===n.id;

    const isConn=hovered&&EDGES.some(e=>(e.from===hovered.id&&e.to===n.id)||(e.to===hovered.id&&e.from===n.id));

    const dimmed=hovered&&!isHov&&!isConn;

    const pulse=1+.06*Math.sin(t*2.5+n.x*8+n.y*5);


    // Check pulse from flow

    const np=nodePulse[n.id];

    const isPulsing=np&&nowTs<np.until;

    const pulseExtra=isPulsing?1+.25*Math.sin((nowTs-np.until+800)/800*Math.PI):0;

    const R=n.r*pulse*(isHov?1.2:1)*(isPulsing?1+pulseExtra*.15:1);


    // Outer glow

    const glowColor=isPulsing?np.color:n.glow;

    const g=X.createRadialGradient(p.x,p.y,R*.2,p.x,p.y,R*(isHov?3:2.5));

    g.addColorStop(0,isPulsing?(np.color+'88'):glowColor);g.addColorStop(1,'transparent');


    X.globalAlpha=dimmed?.2:1;

    X.fillStyle=g;X.beginPath();X.arc(p.x,p.y,R*(isHov?3:2.5),0,6.28);X.fill();


    if(isHov||isPulsing){


      X.strokeStyle=isPulsing?np.color:n.color;


      X.lineWidth=isPulsing?2:1.5;


      X.globalAlpha=isPulsing?.5:.3;


      X.beginPath();X.arc(p.x,p.y,R*1.6,0,6.28);X.stroke();

      X.globalAlpha=1;

    }

    const cg=X.createRadialGradient(p.x-R*.2,p.y-R*.25,R*.1,p.x,p.y,R);

    cg.addColorStop(0,'rgba(255,255,255,.25)');cg.addColorStop(.4,n.color);cg.addColorStop(1,n.color+'99');

    X.fillStyle=cg;


    X.globalAlpha=dimmed?.25:(isHov?1:.8);

    X.beginPath();X.arc(p.x,p.y,R,0,6.28);X.fill();

    X.globalAlpha=1;


    X.strokeStyle=dimmed?'rgba(255,255,255,.04)':(isHov?'rgba(255,255,255,.6)':isPulsing?'rgba(255,255,255,.35)':'rgba(255,255,255,.1)');


    X.lineWidth=isHov?2:isPulsing?1.5:1;

    X.beginPath();X.arc(p.x,p.y,R+1,0,6.28);X.stroke();

    const fontSize=n.kind==='orchestrator'?15:n.kind==='agent'?14:12;

    X.font=(n.kind==='orchestrator'||n.kind==='agent'?'600 ':'400 ')+fontSize+'px Outfit,system-ui,sans-serif';

    X.fillStyle=dimmed?'rgba(255,255,255,.15)':'rgba(255,255,255,.85)';

    X.textAlign='center';X.textBaseline='middle';

    X.fillText(n.label,p.x,p.y+R+18);

  }


  requestAnimationFrame(draw);

}

requestAnimationFrame(draw);


/* ── Interaction: drag & hover ── */


function hitTest(ex,ey){

  for(const n of NODES){

    const p=nodePos(n);

    const dx=ex-p.x,dy=ey-p.y;

    if(dx*dx+dy*dy<(n.r+14)*(n.r+14))return n;

  }

  return null;

}


C.addEventListener('mousedown',e=>{

  const hit=hitTest(e.clientX,e.clientY);

  if(hit){

    dragging=hit;

    hit.vx=0;hit.vy=0;

    const p=nodePos(hit);

    dragOff.x=e.clientX-p.x;

    dragOff.y=e.clientY-p.y;

    prevMx=e.clientX;prevMy=e.clientY;

    dragVx=0;dragVy=0;

    TT.style.display='none';

  }

});


C.addEventListener('mousemove',e=>{

  mx=e.clientX;my=e.clientY;

  if(dragging){

    dragging.x=(mx-dragOff.x)/W;

    dragging.y=(my-dragOff.y)/H;

    dragVx=0.7*dragVx+0.3*(mx-prevMx)/W;

    dragVy=0.7*dragVy+0.3*(my-prevMy)/H;

    prevMx=mx;prevMy=my;

    C.style.cursor='grabbing';

    TT.style.display='none';

    return;

  }

  hovered=hitTest(mx,my);

  if(hovered){

    C.style.cursor='grab';

    const n=hovered;

    const colors={orchestrator:'#a78bfa',agent:'#fb923c',tool:'#34d399',external:'#60a5fa',database:'#f472b6'};

    const labels={orchestrator:'ORQUESTADOR',agent:'AGENTE',tool:'HERRAMIENTA',external:'SERVICIO EXTERNO',database:'BASE DE DATOS'};

    TT.innerHTML='<h3>'+n.desc+'</h3>'

      +'<span class="tag" style="background:'+colors[n.kind]+'18;color:'+colors[n.kind]+';border:1px solid '+colors[n.kind]+'44">'+labels[n.kind]+'</span>'

      +'<div class="detail">'+n.detail.replace(/\\n/g,'<br>')+'</div>';

    TT.style.display='block';

    let tx=mx+18,ty=my+18;

    if(tx+370>W)tx=mx-380;

    if(ty+220>H)ty=my-230;

    TT.style.left=tx+'px';TT.style.top=ty+'px';

  }else{

    C.style.cursor='default';

    TT.style.display='none';

  }

});

window.addEventListener('mouseup',()=>{

  if(dragging){

    dragging.vx=dragVx*.35;

    dragging.vy=dragVy*.35;

  }

  dragging=null;

});

C.addEventListener('mouseleave',()=>{

  hovered=null;

  if(dragging){dragging.vx=dragVx*.25;dragging.vy=dragVy*.25;}

  dragging=null;

  TT.style.display='none';

});

const _injected=${injectedData};

if(_injected&&_injected.nodes){

  NODES=_injected.nodes;

  EDGES=_injected.edges||[];


  NODES.forEach(function(n){n.vx=0;n.vy=0;n.hx=n.hx??n.x;n.hy=n.hy??n.y;});

  if(LOADER)LOADER.style.display='none';

  setTimeout(function(){triggerFlows('inbound_received');},800);

  setTimeout(function(){triggerFlows('run_agent_start');},1600);

  setTimeout(function(){triggerFlows('run_agent_done');},2600);

}else if(LOADER){

  LOADER.textContent='Animación no disponible';

}

poll();

setInterval(poll,10000);

requestAnimationFrame(draw);

</script>

</body>

</html>`;





function renderPublicConstellationHtml(graphData, publicDataPath) {

  const injectedData = graphData ? JSON.stringify(graphData) : 'null';
  const injectedEmpresas = JSON.stringify(empresasList);

  return `<!doctype html>

<html lang="es">

<head>

<meta charset="utf-8">

<meta name="viewport" content="width=device-width, initial-scale=1">

<title>Kapso Visual</title>

<style>

@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap');

*{margin:0;padding:0;box-sizing:border-box}

html,body{width:100%;height:100%;overflow:hidden;background:#020010;font-family:'Outfit',system-ui,sans-serif}

canvas{display:block;position:absolute;top:0;left:0}

#loader{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:10;color:rgba(167,139,250,.55);font-size:14px;letter-spacing:2px;text-transform:uppercase;pointer-events:none}

#header{position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:20;text-align:center;pointer-events:none}

#header h1{font-size:22px;letter-spacing:8px;font-weight:600;background:linear-gradient(90deg,#a78bfa,#60a5fa,#f472b6);-webkit-background-clip:text;background-clip:text;color:transparent;text-transform:uppercase}

#header p{font-size:11px;letter-spacing:3px;color:rgba(255,255,255,.18);margin-top:2px;text-transform:uppercase}

#tooltip{position:fixed;display:none;z-index:30;background:rgba(8,4,28,.94);border:1px solid rgba(139,92,246,.3);border-radius:14px;padding:16px 20px;max-width:360px;font-size:13px;line-height:1.7;backdrop-filter:blur(16px);box-shadow:0 0 40px rgba(99,102,241,.15),0 12px 40px rgba(0,0,0,.6);pointer-events:none}

#tooltip h3{font-size:15px;margin-bottom:6px;font-weight:600;color:#fff}

#tooltip .tag{display:inline-block;padding:3px 10px;border-radius:6px;font-size:10px;font-weight:600;letter-spacing:.8px;margin-bottom:10px;text-transform:uppercase}

#tooltip .detail{color:rgba(203,213,225,.75);font-size:12px}

#tooltip .detail b{color:#e2e8f0}

#legend{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:20;display:flex;gap:20px;font-size:12px;color:rgba(255,255,255,.35);background:rgba(8,4,28,.6);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:10px 24px;backdrop-filter:blur(12px)}

#legend span{display:flex;align-items:center;gap:6px}

#legend i{width:10px;height:10px;border-radius:50%;display:inline-block}

</style>

</head>

<body>

<a href="/debug/kapso" id="back">Panel</a>

<div id="header">

  <h1>Monica Brain</h1>

  <p>Neural Architecture Map</p>

</div>

<canvas id="c"></canvas>

<div id="loader">Cargando animación…</div>

<div id="tooltip"></div>

<div id="legend">

  <span><i style="background:#a78bfa"></i> Orquestador</span>

  <span><i style="background:#fb923c"></i> Agente</span>

  <span><i style="background:#34d399"></i> Herramienta</span>

  <span><i style="background:#60a5fa"></i> Externo</span>

  <span><i style="background:#f472b6"></i> Base de datos</span>

</div>

<script>

const DATA_PATH=${JSON.stringify(publicDataPath)};

const C=document.getElementById('c'),X=C.getContext('2d'),TT=document.getElementById('tooltip');

const LOADER=document.getElementById('loader');

let W,H,mx=-1,my=-1,hovered=null,dragging=null,dragOff={x:0,y:0},prevMx=0,prevMy=0,dragVx=0,dragVy=0,t=0,dpr=1;

let NODES=[],EDGES=[];

const flowParticles=[];

const seenEventKeys=new Set();

const SPEED_MULT=2;

const PARTICLE_BASE_DURATION=2200;

const STAGE_FLOWS={

  inbound_received:[['n1','n2','#60a5fa']],

  fallback_numero:[['n2','n2','#f59e0b']],

  fallback_agent:[['n2','n2','#f59e0b']],

  inbound_entities_resolved:[['n2','n6','#f472b6'],['n6','n2','#f472b6']],

  inbound_messages_persisted:[['n2','n6','#f472b6']],

  memory_session_resolved:[['n2','n6','#f472b6'],['n6','n2','#f472b6']],

  prompt_context_built:[['n2','n3','#a78bfa']],

  run_agent_start:[['n2','n3','#a78bfa'],['n2','n4','#fb923c'],['n2','n5','#fb923c']],

  run_agent_done:[['n3','n7','#60a5fa'],['n3','n2','#a78bfa'],['n2','n1','#34d399']],

  run_funnel_done:[['n4','n7','#60a5fa'],['n4','n2','#fb923c']],

  run_contact_update_done:[['n5','n7','#60a5fa'],['n5','n6','#f472b6']],

  slash_command_done:[['n2','n1','#34d399']],

  audio_processing:[['n2','n8','#f472b6'],['n2','n9','#60a5fa']],

  image_processing:[['n2','n8','#f472b6'],['n2','n10','#60a5fa'],['n10','n7','#60a5fa']],

  document_processing:[['n2','n8','#f472b6'],['n2','n9','#60a5fa']],

  call_fastapi_done:[['n2','n1','#34d399']],

  kapso_send_done:[['n2','n1','#34d399']],

  kapso_send_reaction_with_text:[['n2','n1','#34d399']],

  http_error:[['n2','n1','#ef4444']],

  exception:[['n2','n1','#ef4444']],

};

const nodePulse={};

const stars=Array.from({length:400},()=>({x:Math.random(),y:Math.random(),s:Math.random()*1.2+.3,b:Math.random(),sp:Math.random()*.5+.5}));

const nebulae=[

  {x:.3,y:.35,rx:220,ry:140,color:'rgba(99,102,241,.04)'},

  {x:.7,y:.6,rx:260,ry:170,color:'rgba(139,92,246,.05)'},

  {x:.55,y:.2,rx:180,ry:110,color:'rgba(236,72,153,.035)'}

];

function resize(){

  dpr=Math.min(window.devicePixelRatio||1,2);

  W=window.innerWidth;H=window.innerHeight;

  C.width=W*dpr;C.height=H*dpr;C.style.width=W+'px';C.style.height=H+'px';

  X.setTransform(dpr,0,0,dpr,0,0);

}

window.addEventListener('resize',resize);resize();

function nodePos(n){return{x:n.x*W,y:n.y*H};}

function physics(){

  for(let i=0;i<NODES.length;i++){

    for(let j=i+1;j<NODES.length;j++){

      const a=NODES[i],b=NODES[j];

      const ax=a.x*W,ay=a.y*H,bx=b.x*W,by=b.y*H;

      const dx=bx-ax,dy=by-ay;

      const dist=Math.sqrt(dx*dx+dy*dy)||1;

      const min=((a.r||12)+(b.r||12))*2.3;

      if(dist<min){

        const f=(min-dist)/min*0.00012;

        const nx=dx/dist,ny=dy/dist;

        a.vx=(a.vx||0)-nx*f;b.vx=(b.vx||0)+nx*f;

        a.vy=(a.vy||0)-ny*f;b.vy=(b.vy||0)+ny*f;

      }

    }

  }

  for(const n of NODES){

    if(n===dragging)continue;

    n.vx=(n.vx||0)*0.9;

    n.vy=(n.vy||0)*0.9;

    n.vx+=((n.hx??n.x)-n.x)*0.0035;

    n.vy+=((n.hy??n.y)-n.y)*0.0035;

    n.x+=n.vx;

    n.y+=n.vy;

    n.x=Math.max(0.08,Math.min(0.92,n.x));

    n.y=Math.max(0.1,Math.min(0.9,n.y));

  }

}

function hitTest(ex,ey){

  for(const n of NODES){

    const p=nodePos(n);

    const dx=ex-p.x,dy=ey-p.y;

    if(dx*dx+dy*dy<(n.r+14)*(n.r+14))return n;

  }

  return null;

}

function triggerFlows(stage){

  const flows=STAGE_FLOWS[stage];

  if(!flows)return;

  const duration=PARTICLE_BASE_DURATION/SPEED_MULT;

  flows.forEach(function(f,i){

    setTimeout(function(){

      flowParticles.push({fromId:f[0],toId:f[1],progress:0,speed:1/duration,color:f[2],r:5,trail:[]});

      nodePulse[f[0]]={until:Date.now()+800,color:f[2]};

      nodePulse[f[1]]={until:Date.now()+800+duration,color:f[2]};

    },i*180/SPEED_MULT);

  });

}

function processEvents(events){

  if(!Array.isArray(events)||!events.length)return;

  const fresh=[];

  for(const e of events){

    if(!e||!e.stage||!e.timestamp)continue;

    const key=(e.timestamp||'')+'|'+(e.stage||'')+'|'+(e.source||'');

    if(seenEventKeys.has(key))continue;

    seenEventKeys.add(key);

    if(STAGE_FLOWS[e.stage])fresh.push(e);

  }

  fresh.sort(function(a,b){return new Date(a.timestamp)-new Date(b.timestamp);});

  let acc=0;

  fresh.forEach(function(ev){setTimeout(function(){triggerFlows(ev.stage);},acc);acc+=Math.max(180,450/SPEED_MULT);});

}

function poll(){

  fetch(DATA_PATH).then(function(r){return r.json();}).then(function(data){processEvents(data.events||[]);}).catch(function(){});

}

function drawBg(){

  const bg=X.createLinearGradient(0,0,W,H);

  bg.addColorStop(0,'#020010');bg.addColorStop(.5,'#05031c');bg.addColorStop(1,'#020010');

  X.fillStyle=bg;X.fillRect(0,0,W,H);

  nebulae.forEach(function(n){

    const g=X.createRadialGradient(n.x*W,n.y*H,0,n.x*W,n.y*H,n.rx);

    g.addColorStop(0,n.color);g.addColorStop(1,'transparent');

    X.fillStyle=g;

    X.beginPath();X.ellipse(n.x*W,n.y*H,n.rx,n.ry,0,0,Math.PI*2);X.fill();

  });

  stars.forEach(function(s){

    const a=.15+.55*((Math.sin(t*s.sp+s.b*6)+1)/2);

    X.fillStyle='rgba(255,255,255,'+a+')';

    X.beginPath();X.arc(s.x*W,s.y*H,s.s,0,Math.PI*2);X.fill();

  });

}

function drawParticles(){

  for(let i=flowParticles.length-1;i>=0;i--){

    const p=flowParticles[i];

    const fromNode=NODES.find(n=>n.id===p.fromId),toNode=NODES.find(n=>n.id===p.toId);

    if(!fromNode||!toNode){flowParticles.splice(i,1);continue;}

    p.progress+=p.speed*16.666*SPEED_MULT;

    if(p.progress>=1){flowParticles.splice(i,1);continue;}

    const a=nodePos(fromNode),b=nodePos(toNode);

    const x=a.x+(b.x-a.x)*p.progress;

    const y=a.y+(b.y-a.y)*p.progress;

    p.trail.push({x,y});

    if(p.trail.length>18)p.trail.shift();

    for(let j=0;j<p.trail.length;j++){

      const tp=p.trail[j];

      X.globalAlpha=(j+1)/p.trail.length*.25;

      X.fillStyle=p.color;

      X.beginPath();X.arc(tp.x,tp.y,p.r*(j+1)/p.trail.length*.9,0,Math.PI*2);X.fill();

    }

    X.globalAlpha=1;

    X.shadowColor=p.color;X.shadowBlur=18;

    X.fillStyle=p.color;

    X.beginPath();X.arc(x,y,p.r,0,Math.PI*2);X.fill();

    X.shadowBlur=0;

  }

}

function draw(){

  t+=0.016*SPEED_MULT;

  physics();

  drawBg();

  for(const e of EDGES){

    const a=NODES.find(n=>n.id===e.from),b=NODES.find(n=>n.id===e.to);

    if(!a||!b)continue;

    const p1=nodePos(a),p2=nodePos(b);

    const isHov=hovered&&(hovered.id===a.id||hovered.id===b.id);

    const isConnected=hovered&&EDGES.some(ed=>(ed.from===hovered.id||ed.to===hovered.id)&&(ed.from===a.id||ed.to===a.id||ed.from===b.id||ed.to===b.id));

    const hasFlow=flowParticles.some(fp=>fp.fromId===e.from&&fp.toId===e.to);

    X.save();


    if(isHov){

      X.shadowColor=a.color;X.shadowBlur=8;

      X.strokeStyle='rgba(255,255,255,.5)';

      X.lineWidth=2;

    }else if(hasFlow){

      X.strokeStyle='rgba(255,255,255,.25)';

      X.lineWidth=1.5;

    }else if(hovered&&!isConnected){

      X.strokeStyle='rgba(255,255,255,.03)';

      X.lineWidth=.5;

    }else{

      X.strokeStyle='rgba(255,255,255,.1)';

      X.lineWidth=.8;

    }

    if(e.dash){X.setLineDash([5,8]);}else{X.setLineDash([]);}

    X.beginPath();X.moveTo(p1.x,p1.y);X.lineTo(p2.x,p2.y);X.stroke();

    X.restore();

    const speed=(t*(.3+a.x*.2))%1;

    const epx=p1.x+(p2.x-p1.x)*speed;

    const epy=p1.y+(p2.y-p1.y)*speed;


    X.fillStyle=isHov?'rgba(255,255,255,.6)':'rgba(255,255,255,.12)';


    X.beginPath();X.arc(epx,epy,isHov?2.5:1.5,0,6.28);X.fill();

  }

  drawParticles();

  const nowTs=Date.now();

  for(const n of NODES){

    const p=nodePos(n);

    const isHov=hovered&&hovered.id===n.id;

    const isConn=hovered&&EDGES.some(e=>(e.from===hovered.id&&e.to===n.id)||(e.to===hovered.id&&e.from===n.id));

    const dimmed=hovered&&!isHov&&!isConn;

    const pulse=1+.06*Math.sin(t*2.5+n.x*8+n.y*5);

    const np=nodePulse[n.id];

    const isPulsing=np&&nowTs<np.until;

    const pulseExtra=isPulsing?1+.25*Math.sin((nowTs-np.until+800)/800*Math.PI):0;

    const R=n.r*pulse*(isHov?1.2:1)*(isPulsing?1+pulseExtra*.15:1);

    const glowColor=isPulsing?np.color:n.glow;

    const g=X.createRadialGradient(p.x,p.y,R*.2,p.x,p.y,R*(isHov?3:2.5));

    g.addColorStop(0,isPulsing?(np.color+'88'):glowColor);g.addColorStop(1,'transparent');


    X.globalAlpha=dimmed?.2:1;

    X.fillStyle=g;X.beginPath();X.arc(p.x,p.y,R*(isHov?3:2.5),0,6.28);X.fill();


    if(isHov||isPulsing){


      X.strokeStyle=isPulsing?np.color:n.color;


      X.lineWidth=isPulsing?2:1.5;


      X.globalAlpha=isPulsing?.5:.3;


      X.beginPath();X.arc(p.x,p.y,R*1.6,0,6.28);X.stroke();

      X.globalAlpha=1;

    }

    const cg=X.createRadialGradient(p.x-R*.2,p.y-R*.25,R*.1,p.x,p.y,R);

    cg.addColorStop(0,'rgba(255,255,255,.25)');cg.addColorStop(.4,n.color);cg.addColorStop(1,n.color+'99');

    X.fillStyle=cg;


    X.globalAlpha=dimmed?.25:(isHov?1:.8);

    X.beginPath();X.arc(p.x,p.y,R,0,6.28);X.fill();

    X.globalAlpha=1;


    X.strokeStyle=dimmed?'rgba(255,255,255,.04)':(isHov?'rgba(255,255,255,.6)':isPulsing?'rgba(255,255,255,.35)':'rgba(255,255,255,.1)');


    X.lineWidth=isHov?2:isPulsing?1.5:1;

    X.beginPath();X.arc(p.x,p.y,R+1,0,6.28);X.stroke();

    const fontSize=n.kind==='orchestrator'?15:n.kind==='agent'?14:12;

    X.font=(n.kind==='orchestrator'||n.kind==='agent'?'600 ':'400 ')+fontSize+'px Outfit,system-ui,sans-serif';

    X.fillStyle=dimmed?'rgba(255,255,255,.15)':'rgba(255,255,255,.85)';

    X.textAlign='center';X.textBaseline='middle';

    X.fillText(n.label,p.x,p.y+R+18);

  }

  requestAnimationFrame(draw);

}

requestAnimationFrame(draw);

/* ── Interaction: drag & hover ── */


C.addEventListener('mousedown',e=>{

  const hit=hitTest(e.clientX,e.clientY);

  if(hit){

    dragging=hit;

    hit.vx=0;hit.vy=0;

    const p=nodePos(hit);

    dragOff.x=e.clientX-p.x;

    dragOff.y=e.clientY-p.y;

    prevMx=e.clientX;prevMy=e.clientY;

    dragVx=0;dragVy=0;

    TT.style.display='none';

  }

});

C.addEventListener('mousemove',e=>{

  mx=e.clientX;my=e.clientY;

  if(dragging){

    dragging.x=(mx-dragOff.x)/W;

    dragging.y=(my-dragOff.y)/H;

    dragVx=0.7*dragVx+0.3*(mx-prevMx)/W;

    dragVy=0.7*dragVy+0.3*(my-prevMy)/H;

    prevMx=mx;prevMy=my;

    C.style.cursor='grabbing';

    TT.style.display='none';

    return;

  }

  hovered=hitTest(mx,my);

  if(hovered){

    C.style.cursor='grab';

    const n=hovered;

    const colors={orchestrator:'#a78bfa',agent:'#fb923c',tool:'#34d399',external:'#60a5fa',database:'#f472b6'};

    const labels={orchestrator:'ORQUESTADOR',agent:'AGENTE',tool:'HERRAMIENTA',external:'SERVICIO EXTERNO',database:'BASE DE DATOS'};

    TT.innerHTML='<h3>'+n.desc+'</h3>'

      +'<span class="tag" style="background:'+colors[n.kind]+'18;color:'+colors[n.kind]+';border:1px solid '+colors[n.kind]+'44">'+labels[n.kind]+'</span>'

      +'<div class="detail">'+n.detail.replace(/\\n/g,'<br>')+'</div>';

    TT.style.display='block';

    let tx=mx+18,ty=my+18;

    if(tx+370>W)tx=mx-380;

    if(ty+220>H)ty=my-230;

    TT.style.left=tx+'px';TT.style.top=ty+'px';

  }else{

    C.style.cursor='default';

    TT.style.display='none';

  }

});

window.addEventListener('mouseup',()=>{

  if(dragging){

    dragging.vx=dragVx*.35;

    dragging.vy=dragVy*.35;

  }

  dragging=null;

});

C.addEventListener('mouseleave',()=>{

  hovered=null;

  if(dragging){dragging.vx=dragVx*.25;dragging.vy=dragVy*.25;}

  dragging=null;

  TT.style.display='none';

});

const _injected=${injectedData};

if(_injected&&_injected.nodes){

  NODES=_injected.nodes;

  EDGES=_injected.edges||[];


  NODES.forEach(function(n){n.vx=0;n.vy=0;n.hx=n.hx??n.x;n.hy=n.hy??n.y;});

  if(LOADER)LOADER.style.display='none';

  setTimeout(function(){triggerFlows('inbound_received');},800);

  setTimeout(function(){triggerFlows('run_agent_start');},1600);

  setTimeout(function(){triggerFlows('run_agent_done');},2600);

}else if(LOADER){

  LOADER.textContent='Animación no disponible';

}

poll();

setInterval(poll,10000);

requestAnimationFrame(draw);


</script>

</body>

</html>`;

}

function renderKapsoDebugHtml() {
  return `<!doctype html>

<html lang="es">

<head>

  <meta charset="utf-8">

  <meta name="viewport" content="width=device-width, initial-scale=1">

  <title>Kapso Debug</title>

  <style>

    *{box-sizing:border-box;margin:0;padding:0}

    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;font-size:13px;height:100vh;display:flex;flex-direction:column;overflow:hidden}

    /* Header */

    .hdr{background:#1e293b;border-bottom:1px solid #334155;padding:10px 18px;display:flex;align-items:center;gap:12px;flex-shrink:0;z-index:10}

    .hdr h1{font-size:15px;font-weight:700;color:#f1f5f9;letter-spacing:-0.3px}

    .hdr .pill{background:#1d4ed8;color:#bfdbfe;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:600}

    .hdr-r{margin-left:auto;display:flex;align-items:center;gap:8px}

    .btn{background:#2563eb;color:#fff;border:none;padding:5px 13px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:500;transition:background .15s}

    .btn:hover{background:#1d4ed8}

    .btn-g{background:transparent;border:1px solid #475569;color:#94a3b8}

    .btn-g:hover{background:#1e293b;color:#e2e8f0}

    .muted{color:#64748b;font-size:11px}

    /* Layout */

    .layout{display:flex;flex:1;overflow:hidden}

    .sidebar{width:260px;min-width:260px;background:#1e293b;border-right:1px solid #334155;overflow-y:auto;padding:14px;flex-shrink:0}

    .main{flex:1;overflow-y:auto;padding:16px}

    /* Sidebar */

    .sb-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#64748b;margin-bottom:8px;margin-top:16px}

    .sb-title:first-child{margin-top:0}

    .cfg-row{margin-bottom:5px}

    .cfg-k{color:#64748b;font-size:10px}

    .cfg-v{color:#e2e8f0;font-size:11px;word-break:break-all}

    /* Stats */

    .stats{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:14px}

    .stat{background:#0f172a;border-radius:8px;padding:10px 12px}

    .stat-n{font-size:18px;font-weight:700;color:#f1f5f9}

    .stat-l{font-size:10px;color:#64748b;margin-top:2px}

    .s-ok .stat-n{color:#34d399}.s-err .stat-n{color:#f87171}.s-avg .stat-n{color:#60a5fa}

    /* Table area */

    .tbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}

    .tbar h2{font-size:14px;font-weight:600;color:#f1f5f9}

    .fi{margin-left:auto;background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:5px 10px;border-radius:6px;font-size:12px;width:170px}

    .fi:focus{outline:none;border-color:#3b82f6}

    .fi::placeholder{color:#475569}

    table{width:100%;border-collapse:collapse}

    thead th{text-align:left;padding:7px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#64748b;background:#1e293b;border-bottom:1px solid #334155;white-space:nowrap}

    tbody tr{border-bottom:1px solid #172033;cursor:pointer;transition:background .1s}

    tbody tr:hover{background:#1a2540}

    tbody tr.sel{background:#1e3a5f}

    td{padding:8px 10px;vertical-align:middle}

    .nd{text-align:center;padding:52px;color:#475569;font-size:13px}

    /* Badges */

    .bok{color:#34d399;display:inline-flex;align-items:center;gap:4px;font-size:11px}

    .berr{color:#f87171;display:inline-flex;align-items:center;gap:4px;font-size:11px}

    .bprc{color:#fbbf24;display:inline-flex;align-items:center;gap:4px;font-size:11px}

    .dot{width:6px;height:6px;border-radius:50%;background:currentColor;display:inline-block}

    .bm{background:#1e3a5f;color:#60a5fa;padding:2px 5px;border-radius:4px;font-size:10px;white-space:nowrap;max-width:90px;overflow:hidden;text-overflow:ellipsis;display:inline-block;vertical-align:middle}

    .bt{background:#2d1b69;color:#a78bfa;padding:2px 6px;border-radius:4px;font-size:10px}

    .tp{font-family:monospace;font-size:11px;font-weight:700}

    .tf{color:#34d399}.tm{color:#fbbf24}.ts{color:#f87171}

    @keyframes sp{to{transform:rotate(360deg)}}

    .sp{width:12px;height:12px;border:2px solid #334155;border-top-color:#fbbf24;border-radius:50%;animation:sp .7s linear infinite;display:inline-block}

    .msgw{display:flex;flex-direction:column;align-items:flex-start;gap:6px}

    .msgc{color:#e2e8f0;white-space:pre-wrap;word-break:break-word}

    .msgc.is-collapsed{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:4;overflow:hidden}

    .msg-toggle{appearance:none;background:none;border:none;padding:0;color:#60a5fa;font-size:11px;font-weight:600;cursor:pointer}

    .msg-toggle:hover{text-decoration:underline}

    /* Modal */

    .ov{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;display:none;align-items:flex-start;justify-content:center;padding:20px;overflow-y:auto}

    .ov.open{display:flex}

    .modal{background:#1e293b;border-radius:12px;border:1px solid #334155;width:100%;max-width:840px;margin:auto;max-height:90vh;display:flex;flex-direction:column}

    .mhdr{padding:14px 18px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px;flex-shrink:0}

    .mttl{font-size:14px;font-weight:700;color:#f1f5f9}

    .mttl span{color:#64748b;font-size:12px;font-weight:400;margin-left:6px}

    .mcls{margin-left:auto;background:none;border:none;color:#64748b;cursor:pointer;font-size:20px;line-height:1;padding:2px 6px}

    .mcls:hover{color:#e2e8f0}

    /* Tabs */

    .tabs{display:flex;border-bottom:1px solid #334155;padding:0 18px;flex-shrink:0}

    .tab{padding:9px 14px;font-size:12px;font-weight:500;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .1s}

    .tab:hover{color:#e2e8f0}

    .tab.a{color:#60a5fa;border-bottom-color:#3b82f6}

    .tc{display:none;padding:18px;overflow-y:auto;flex:1}

    .tc.a{display:block}

    /* Detail cards */

    .dg{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}

    .dc{background:#0f172a;border-radius:8px;padding:13px}

    .dct{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#64748b;margin-bottom:8px}

    .dr{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:5px;gap:8px}

    .dk{color:#64748b;font-size:11px;white-space:nowrap;flex-shrink:0}

    .dv{color:#e2e8f0;font-size:11px;text-align:right;word-break:break-all}

    .msgbox{background:#0f172a;border-radius:8px;padding:13px;margin-bottom:12px}

    .msgl{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-weight:700;margin-bottom:7px}

    .msgt{color:#f1f5f9;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto;padding-right:4px}

    .resp{background:#0a1628;border-radius:8px;padding:13px;margin-top:12px;border-left:3px solid #3b82f6}

    /* Timing */

    .tbr{margin-bottom:10px}

    .tbh{display:flex;justify-content:space-between;margin-bottom:4px}

    .tbl{font-size:11px;color:#94a3b8}

    .tbv{font-family:monospace;font-size:11px;color:#f1f5f9;font-weight:600}

    .tbt{height:7px;background:#1e293b;border-radius:100px;overflow:hidden}

    .tbf{height:100%;border-radius:100px;transition:width .4s}

    .c1{background:#3b82f6}.c2{background:#8b5cf6}.c3{background:#06b6d4}.c4{background:#f59e0b}

    /* Tools */

    .tl{background:#0f172a;border-radius:8px;padding:13px;margin-bottom:10px;border-left:3px solid #7c3aed}

    .tln{font-size:12px;font-weight:700;color:#a78bfa;margin-bottom:10px}

    .tllbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#64748b;margin-bottom:4px}

    pre.cd{background:#0a1628;border-radius:6px;padding:10px;font-size:11px;color:#94a3b8;overflow-x:auto;white-space:pre-wrap;word-break:break-word;margin-bottom:8px;max-height:180px;overflow-y:auto}

    ::-webkit-scrollbar{width:5px;height:5px}

    ::-webkit-scrollbar-thumb{background:#334155;border-radius:5px}

  </style>

</head>

<body>

  <div class="hdr">

    <h1>Kapso Debug</h1>

    <span class="pill">LIVE</span>

    <div class="hdr-r">

      <span class="muted" id="upd">cargando...</span>

      <button class="btn btn-g" id="ar-btn">⏸ Pausar</button>

      <button class="btn" id="refresh-btn">↻ Refrescar</button>

      <button class="btn" id="visual-btn" style="background:#6366f1;color:#fff;">Ver visual</button>

    </div>

  </div>

  <div class="layout">

    <div class="sidebar">

      <div class="stats" id="stats">

        <div class="stat"><div class="stat-n" id="st">0</div><div class="stat-l">Total</div></div>

        <div class="stat s-ok"><div class="stat-n" id="sk">0</div><div class="stat-l">OK</div></div>

        <div class="stat s-err"><div class="stat-n" id="se">0</div><div class="stat-l">Errores</div></div>

        <div class="stat s-avg"><div class="stat-n" id="sa">—</div><div class="stat-l">Tiempo avg</div></div>

      </div>

      <div class="sb-title">Bridge Config</div>

      <div id="bcfg"></div>

      <div class="sb-title">FastAPI Config</div>

      <div id="fcfg"></div>

    </div>

    <div class="main">

      <div class="tbar">

        <h2>Interacciones</h2>
        <select class="fi" id="empresa-fi" style="width:auto;min-width:140px;margin-left:8px">
          <option value="">Todas las empresas</option>
        </select>

        <input class="fi" id="fi" placeholder="Filtrar por teléfono o nombre...">

      </div>

      <table>

        <thead><tr>

          <th>Hora</th><th>Contacto</th><th>Tipo</th><th>Mensaje</th>

          <th>Agente</th><th>Modelo</th><th>Tiempo</th><th>Tools</th><th>Rx</th><th>Status</th>

        </tr></thead>

        <tbody id="tbody"></tbody>

      </table>

    </div>

  </div>

  <!-- Detail modal -->

  <div class="ov" id="ov">

    <div class="modal">

      <div class="mhdr">

        <div>

          <div class="mttl" id="mttl">Interacción <span id="msub"></span></div>

        </div>

        <button class="mcls" id="close-btn">&#x2715;</button>

      </div>

      <div class="tabs">

        <div class="tab a" data-t="ov">Overview</div>

        <div class="tab" data-t="tm">Timing</div>

        <div class="tab" data-t="tl">Herramientas</div>

        <div class="tab" data-t="rp">Respuesta</div>

      </div>

      <div class="tc a" id="tc-ov"></div>

      <div class="tc" id="tc-tm"></div>

      <div class="tc" id="tc-tl"></div>

      <div class="tc" id="tc-rp"></div>

    </div>

  </div>

  <script src="${appendDebugToken('/debug/kapso/app.js', debugToken)}"></script>

</body>

</html>`;

}



function renderKapsoDebugScript(debugToken = '') {

  return `let D={},sel=null,ar=true,arT=null,fq='',empresaFq='';

const DEBUG_TOKEN=new URLSearchParams(window.location.search).get('token')||${JSON.stringify(debugToken || '')};

function debugPath(path){
  if(!DEBUG_TOKEN)return path;
  const u=new URL(path,window.location.origin);
  u.searchParams.set('token',DEBUG_TOKEN);
  return u.pathname+u.search;
}

function fetchDebug(path,init){
  return fetch(debugPath(path),init);
}

// Load empresas list for filter
(function loadEmpresas(){
  fetchDebug('/debug/kapso/empresas').then(r=>r.json()).then(data=>{
    const s=document.getElementById('empresa-fi');
    if(!s||!data.empresas)return;
    data.empresas.forEach(e=>{
      const o=document.createElement('option');
      o.value=String(e.id);
      o.textContent=e.nombre;
      s.appendChild(o);
    });
  }).catch(()=>{});
})();



const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

const MESSAGE_PREVIEW_MAX_CHARS=180;

const MESSAGE_PREVIEW_MAX_LINES=4;

function msgNeedsToggle(s){
  const text=String(s||'');
  return text.length>MESSAGE_PREVIEW_MAX_CHARS||text.split(/\r?\n/).length>MESSAGE_PREVIEW_MAX_LINES;
}

function renderMsgPreview(s,key){
  if(!s)return'<span class="muted">—</span>';
  const text=String(s);
  if(!msgNeedsToggle(text))return'<div class="msgc">'+esc(text)+'</div>';
  return '<div class="msgw"><div class="msgc is-collapsed" data-msg-content="'+key+'">'+esc(text)+'</div><button type="button" class="msg-toggle" data-msg-target="'+key+'" aria-expanded="false">Ver más</button></div>';
}

const trunc=(s,n=45)=>!s?'<span class="muted">—</span>':s.length>n?esc(s.slice(0,n))+'&hellip;':esc(s);

function rel(t){if(!t)return'—';const d=Date.now()-new Date(t);if(d<60e3)return Math.round(d/1e3)+'s';if(d<3600e3)return Math.round(d/60e3)+'m ago';return new Date(t).toLocaleTimeString();}

function fms(ms){if(!ms&&ms!==0)return'—';if(ms<1e3)return Math.round(ms)+'ms';return(ms/1e3).toFixed(1)+'s';}

function tcls(ms){if(ms==null)return'';if(ms<25e3)return'tf';if(ms<=30e3)return'tm';return'ts';}

function sBadge(s){if(s==='ok')return'<span class="bok"><span class="dot"></span>OK</span>';if(s==='error')return'<span class="berr"><span class="dot"></span>Error</span>';return'<span class="bprc"><span class="sp"></span></span>';}

function mshort(m){if(!m)return'—';const p=m.split('/');return p[p.length-1];}



function filt(items){
  let r=items;
  if(empresaFq) r=r.filter(i=>i.empresa_id!=null&&String(i.empresa_id)===empresaFq);
  if(fq) r=r.filter(i=>(i.from_phone||'').includes(fq)||(i.contact_name||'').toLowerCase().includes(fq.toLowerCase()));
  return r;
}



function renderCfg(id,obj){document.getElementById(id).innerHTML=Object.entries(obj||{}).map(([k,v])=>'<div class="cfg-row"><div class="cfg-k">'+esc(k)+'</div><div class="cfg-v">'+esc(v??'—')+'</div></div>').join('');}



function renderStats(items){

  const ok=items.filter(i=>i.status==='ok').length;

  const err=items.filter(i=>i.status==='error').length;

  const dms=items.filter(i=>i.duration_ms).map(i=>i.duration_ms);

  const avg=dms.length?dms.reduce((a,b)=>a+b,0)/dms.length:null;

  document.getElementById('st').textContent=items.length;

  document.getElementById('sk').textContent=ok;

  document.getElementById('se').textContent=err;

  document.getElementById('sa').textContent=fms(avg);

}



function renderTable(items){

  const rows=filt(items);

  const tbody=document.getElementById('tbody');

  if(!rows.length){tbody.innerHTML='<tr><td colspan="10" class="nd">'+(items.length?'Sin resultados para ese filtro.':'Sin interacciones aún. Envía un mensaje WhatsApp para ver actividad.')+'</td></tr>';return;}

  tbody.innerHTML=rows.map((it,i)=>

    '<tr class="'+(sel&&sel.id===it.id?'sel':'')+'" data-row-idx="'+i+'">'+

      '<td class="muted">'+rel(it.started_at)+'</td>'+

      '<td><div style="font-weight:600;color:#f1f5f9">'+esc(it.contact_name||'—')+'</div><div class="muted">'+esc(it.from_phone||'')+'</div></td>'+

      '<td class="muted">'+esc(it.message_type||'text')+'</td>'+

      '<td style="max-width:180px">'+renderMsgPreview(it.message_text,'msg-'+i)+'</td>'+

      '<td><div style="color:#e2e8f0">'+esc(it.agent_name||'—')+'</div><div class="muted">#'+(it.agent_id||'?')+'</div></td>'+

      '<td><span class="bm" title="'+esc(it.model_used||'')+'">'+esc(mshort(it.model_used))+'</span></td>'+

      '<td><span class="tp '+tcls(it.duration_ms)+'">'+fms(it.duration_ms)+'</span></td>'+

      '<td>'+((it.tools_used||[]).length?'<span class="bt">'+((it.tools_used||[]).length)+' tool'+((it.tools_used||[]).length>1?'s':'')+'</span>':'<span class="muted">—</span>')+'</td>'+

      '<td style="font-size:14px">'+(it.reaction_emoji||'<span class="muted">—</span>')+'</td>'+

      '<td>'+sBadge(it.status)+'</td>'+

    '</tr>'

  ).join('');

  document.querySelectorAll('#tbody tr[data-row-idx]').forEach(row=>{

    row.addEventListener('click',()=>openM(Number(row.dataset.rowIdx)));

  });

  tbody.querySelectorAll('button[data-msg-target]').forEach(btn=>{

    btn.addEventListener('click',ev=>{

      ev.stopPropagation();

      const target=btn.dataset.msgTarget;

      const content=tbody.querySelector('[data-msg-content="'+target+'"]');

      if(!content)return;

      const expanded=btn.getAttribute('aria-expanded')==='true';

      btn.setAttribute('aria-expanded',expanded?'false':'true');

      btn.textContent=expanded?'Ver más':'Ver menos';

      content.classList.toggle('is-collapsed',expanded);

    });

  });

}



function openM(idx){

  const rows=filt(D.interactions||[]);

  const it=rows[idx];if(!it)return;

  sel=it;

  const tm=it.timing||{};

  const tools=it.tools_used||[];

  const maxMs=tm.total_ms||1;

  document.getElementById('mttl').innerHTML='Interacción <span id="msub">'+esc(it.contact_name||it.from_phone||'')+'</span>';

  document.getElementById('tc-ov').innerHTML=

    '<div class="msgbox"><div class="msgl">Mensaje recibido</div><div class="msgt">'+(esc(it.message_text)||'<em style="color:#64748b">Sin texto</em>')+'</div></div>'+

    '<div class="dg">'+

      '<div class="dc"><div class="dct">Contacto</div>'+

        '<div class="dr"><span class="dk">Nombre</span><span class="dv">'+esc(it.contact_name||'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Teléfono</span><span class="dv">'+esc(it.from_phone||'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Tipo msg</span><span class="dv">'+esc(it.message_type||'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Message ID</span><span class="dv" style="font-size:9px;font-family:monospace">'+esc(it.message_id||'—')+'</span></div>'+

      '</div>'+

      '<div class="dc"><div class="dct">Agente</div>'+

        '<div class="dr"><span class="dk">Nombre</span><span class="dv">'+esc(it.agent_name||'—')+'</span></div>'+

        '<div class="dr"><span class="dk">ID</span><span class="dv">#'+(it.agent_id||'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Modelo</span><span class="dv">'+esc(it.model_used||'—')+'</span></div>'+

        '<div class="dr"><span class="dk">MCP servers</span><span class="dv">'+((it.mcp_servers||[]).length?it.mcp_servers.map(u=>u.split('/').pop()).join(', '):'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Memory session</span><span class="dv" style="font-size:9px">'+esc(it.memory_session_id||'—')+'</span></div>'+

      '</div>'+

    '</div>'+

    '<div class="dg">'+

      '<div class="dc"><div class="dct">Resultado</div>'+

        '<div class="dr"><span class="dk">Status</span><span class="dv">'+sBadge(it.status)+'</span></div>'+

        '<div class="dr"><span class="dk">Duración</span><span class="dv tp '+tcls(it.duration_ms)+'">'+fms(it.duration_ms)+'</span></div>'+

        '<div class="dr"><span class="dk">Tipo respuesta</span><span class="dv">'+esc(it.reply_type||'text')+'</span></div>'+

        '<div class="dr"><span class="dk">Chars respuesta</span><span class="dv">'+(it.response_chars??'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Reacción emoji</span><span class="dv" style="font-size:16px">'+(it.reaction_emoji||'—')+'</span></div>'+

        (it.error?'<div class="dr"><span class="dk">Error</span><span class="dv" style="color:#f87171">'+esc(it.error)+'</span></div>':'')+

      '</div>'+

      '<div class="dc"><div class="dct">Timestamps</div>'+

        '<div class="dr"><span class="dk">Inicio</span><span class="dv">'+(it.started_at?new Date(it.started_at).toLocaleTimeString():'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Fin</span><span class="dv">'+(it.finished_at?new Date(it.finished_at).toLocaleTimeString():'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Fecha</span><span class="dv">'+(it.started_at?new Date(it.started_at).toLocaleDateString():'—')+'</span></div>'+

        '<div class="dr"><span class="dk">Tools usadas</span><span class="dv">'+tools.length+'</span></div>'+

      '</div>'+

    '</div>';

  const bars=[

    {l:'Total',k:'total_ms',c:'c1'},{l:'LLM',k:'llm_ms',c:'c2'},

    {l:'MCP Discovery',k:'mcp_discovery_ms',c:'c3'},{l:'Graph Build',k:'graph_build_ms',c:'c4'},

  ];

  document.getElementById('tc-tm').innerHTML=

    '<div style="background:#0f172a;border-radius:8px;padding:16px">'+

    bars.map(b=>{const v=tm[b.k]||0;const p=maxMs>0?Math.min(100,(v/maxMs)*100):0;return(

      '<div class="tbr"><div class="tbh"><span class="tbl">'+b.l+'</span><span class="tbv">'+fms(v)+'</span></div>'+

      '<div class="tbt"><div class="tbf '+b.c+'" style="width:'+p.toFixed(1)+'%"></div></div></div>'

    );}).join('')+

    '</div>';

  document.getElementById('tc-tl').innerHTML=tools.length

    ?tools.map(t=>(

      '<div class="tl"><div class="tln">'+esc(t.tool_name)+'</div>'+

      '<div class="tllbl">Input</div><pre class="cd">'+esc(JSON.stringify(t.tool_input,null,2))+'</pre>'+

      '<div class="tllbl">Output</div><pre class="cd">'+esc(t.tool_output||'—')+'</pre></div>'

    )).join('')

    :'<div class="nd">No se usaron herramientas externas en esta interacción.</div>';

  document.getElementById('tc-rp').innerHTML=it.response_preview

    ?('<div class="resp"><div class="msgl">Respuesta enviada <span class="muted">('+

      (it.response_chars||0)+' chars)</span></div><div class="msgt">'+esc(it.response_preview)+

      ((it.response_chars||0)>600?'\n\n<em style="color:#64748b">[...respuesta truncada a 600 chars]</em>':'')+

      '</div></div>')

    :'<div class="nd">Sin preview de respuesta disponible.</div>';

  document.getElementById('ov').classList.add('open');

  swTab('ov');

}



function closeM(){document.getElementById('ov').classList.remove('open');sel=null;}

function swTab(n){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('a',t.dataset.t===n));document.querySelectorAll('.tc').forEach(t=>t.classList.toggle('a',t.id==='tc-'+n));}

function onFilter(){fq=document.getElementById('fi').value.trim();renderTable(D.interactions||[]);}



async function loadAll(){

  try{

    const r=await fetch('/debug/kapso/data',{cache:'no-store'});

    D=await r.json();

    D.interactions = Array.isArray(D.interactions) ? D.interactions : [];



    renderCfg('bcfg',D.bridge_config);

    renderCfg('fcfg',D.fastapi_config);

    renderStats(D.interactions);

    renderTable(D.interactions);

    document.getElementById('upd').textContent='actualizado '+new Date().toLocaleTimeString();

  }catch(e){document.getElementById('upd').textContent='error al cargar';}

}



function toggleAR(){

  ar=!ar;

  document.getElementById('ar-btn').textContent=ar?'⏸ Pausar':'▶ Reanudar';

  if(ar){arT=setInterval(loadAll,4000);}else{clearInterval(arT);}

}



function bindEvents(){

  const refreshBtn=document.getElementById('refresh-btn');

  const arBtn=document.getElementById('ar-btn');

  const fi=document.getElementById('fi');

  const ov=document.getElementById('ov');

  const closeBtn=document.getElementById('close-btn');

  if(refreshBtn) refreshBtn.addEventListener('click',loadAll);

  if(arBtn) arBtn.addEventListener('click',toggleAR);

  if(fi) fi.addEventListener('input',onFilter);
  var empresaFi=document.getElementById('empresa-fi');
  if(empresaFi) empresaFi.addEventListener('change',function(){empresaFq=this.value;renderTable(D.interactions||[]);renderStats(filt(D.interactions||[]));});

  if(closeBtn) closeBtn.addEventListener('click',closeM);

  if(ov) ov.addEventListener('click',event=>{if(event.target===ov) closeM();});

  const visualBtn=document.getElementById('visual-btn');

  if(visualBtn) visualBtn.addEventListener('click',()=>{ window.location.href=debugPath('/debug/kapso/visual'); });

  document.querySelectorAll('.tab').forEach(tab=>{

    tab.addEventListener('click',()=>swTab(tab.dataset.t));

  });

}



bindEvents();

loadAll();

arT=setInterval(loadAll,4000);`;

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



function ensureReplyText(input) {

  const normalized = normalizeWhatsAppText(input);

  return normalized || DEFAULT_EMPTY_REPLY_TEXT;

}



async function markKapsoAsRead(phoneNumberId, messageId) {

  if (!phoneNumberId || !messageId) return null;



  addBridgeDebugEvent('kapso_presence_start', {

    phone_number_id: phoneNumberId,

    message_id: messageId,

    seen: true,

    typing: true,

  });

  console.log(

    `[KapsoBridge] -> KapsoPresence phone_number_id=${phoneNumberId} message_id=${messageId} seen=true typing=true`,

  );



  try {

    const result = await withKapsoRetry(

      () => client.messages.markRead({

        phoneNumberId,

        messageId,

        typingIndicator: { type: 'text' },

      }),

      `markRead(${messageId})`,

    );

    addBridgeDebugEvent('kapso_presence_done', {

      phone_number_id: phoneNumberId,

      message_id: messageId,

      result: result ?? null,

    });

    return result;

  } catch (error) {

    addBridgeDebugEvent('kapso_presence_error', {

      phone_number_id: phoneNumberId,

      message_id: messageId,

      error: String(error?.message || error),

    });

    console.error('[KapsoBridge] Error enviando seen/typing:', error?.stack || error);

    return null;

  }

}



const TYPING_KEEPALIVE_INTERVAL_MS = 20_000;



/**

 * Start a periodic typing indicator that re-fires every 20s.

 * Returns an abort controller — call .abort() to stop the loop.

 */

function startTypingKeepalive(phoneNumberId, messageId) {

  const ac = new AbortController();

  (async () => {

    while (!ac.signal.aborted) {

      await sleep(TYPING_KEEPALIVE_INTERVAL_MS);

      if (ac.signal.aborted) break;

      try {

        await client.messages.markRead({

          phoneNumberId,

          messageId,

          typingIndicator: { type: 'text' },

        });

      } catch (err) {

        console.warn('[KapsoBridge] typing keepalive error (non-fatal):', err?.message || err);

      }

    }

  })();

  return ac;

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

    agent_name: reply.agent_name,

    conversation_id: reply.conversation_id,

    reply_type: reply.reply_type,

    message_id: sqlPayload.message_id,

    model_used: reply.model_used,

    response_chars: String(reply.reply_text || '').length,

    response_preview: String(reply.reply_text || '').slice(0, 600),

    timing: reply.timing || null,

    tools_used: reply.tools_used || [],

    agent_runs: reply.agent_runs || [],

    reaction_emoji: reply.reaction?.emoji || null,

  });

  console.log(

    `[KapsoBridge] <- FastAPI agent_id=${reply.agent_id} conversation_id=${reply.conversation_id} reply_type=${reply.reply_type} chars=${String(reply.reply_text || '').length}`,

  );

  return reply;

}



async function sendKapsoText(recipientPhone, phoneNumberId, text) {

  const body = ensureReplyText(text);



  // Bubble splitting: "---" separates text into multiple WhatsApp messages

  const bubbles = body.split(/\n*---\n*/).map(b => b.trim()).filter(Boolean);

  if (bubbles.length <= 1) {

    return withKapsoRetry(

      () => client.messages.textSender.send({ phoneNumberId, to: recipientPhone, body }),

      `sendText(${recipientPhone})`,

    );

  }



  let lastResult = null;

  for (const bubble of bubbles) {

    const normalizedBubble = ensureReplyText(bubble);

    lastResult = await withKapsoRetry(

      () => client.messages.textSender.send({ phoneNumberId, to: recipientPhone, body: normalizedBubble }),

      `sendText(${recipientPhone})`,

    );

  }

  return lastResult;

}



function shouldSuppressKapsoSend(reply) {

  if (reply?.suppress_send === true) return true;

  return String(reply?.reply_text || '').trimStart().startsWith('❌');

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

    has_reaction: !!(reply.reaction?.emoji),

  });

  console.log(

    `[KapsoBridge] -> KapsoSend to=${recipientPhone} phone_number_id=${phoneNumberId} reply_type=${replyType} reaction=${reply.reaction?.emoji || 'none'}`,

  );



  if (shouldSuppressKapsoSend(reply)) {

    addBridgeDebugEvent('kapso_send_suppressed', {

      to: recipientPhone,

      phone_number_id: phoneNumberId,

      reply_type: replyType,

      message_id: reply.message_id,

      reply_preview: String(reply.reply_text || '').slice(0, 300),

    });

    console.log(

      `[KapsoBridge] envío suprimido message_id=${reply.message_id} reply_type=${replyType}`,

    );

    return { suppressed: true, reason: 'kapso_send_suppressed' };

  }



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

      () => client.messages.sendReaction({

        phoneNumberId,

        to: recipientPhone,

        reaction: {

          messageId: reply.reaction.message_id,

          emoji: reply.reaction.emoji,

        },

      }),

      `sendReaction(${recipientPhone})`,

    );

  }



  if (replyType === 'image' && reply.image_url) {

    return withKapsoRetry(

      () => client.messages.imageSender.send({

        phoneNumberId,

        to: recipientPhone,

        image: {

          link: reply.image_url,

          caption: reply.image_caption ? normalizeWhatsAppText(reply.image_caption) : undefined,

        },

      }),

      `sendImage(${recipientPhone})`,

    );

  }



  if (replyType === 'audio' && reply.audio_url) {

    return withKapsoRetry(

      () => client.messages.audioSender.send({

        phoneNumberId,

        to: recipientPhone,

        audio: {

          link: reply.audio_url,

          voice: true,

        },

      }),

      `sendAudio(${recipientPhone})`,

    );

  }



  if (replyType === 'video' && reply.video_url) {

    return withKapsoRetry(

      () => client.messages.videoSender.send({

        phoneNumberId,

        to: recipientPhone,

        video: {

          link: reply.video_url,

          caption: reply.video_caption ? normalizeWhatsAppText(reply.video_caption) : undefined,

        },

      }),

      `sendVideo(${recipientPhone})`,

    );

  }



  if (replyType === 'document' && reply.document?.url && reply.document?.filename) {

    return withKapsoRetry(

      () => client.messages.documentSender.send({

        phoneNumberId,

        to: recipientPhone,

        document: {

          link: reply.document.url,

          filename: reply.document.filename,

          caption: reply.document.caption ? normalizeWhatsAppText(reply.document.caption) : undefined,

        },

      }),

      `sendDocument(${recipientPhone})`,

    );

  }



  // Texto: si también hay reacción, enviarla primero y luego el texto (dual-dispatch)

  if (reply.reaction?.message_id && reply.reaction?.emoji) {

    addBridgeDebugEvent('kapso_send_reaction_with_text', {

      to: recipientPhone,

      emoji: reply.reaction.emoji,

      message_id: reply.reaction.message_id,

    });

    console.log(

      `[KapsoBridge] -> KapsoReaction (dual) to=${recipientPhone} emoji=${reply.reaction.emoji}`,

    );

    try {

      await withKapsoRetry(

        () => client.messages.sendReaction({

          phoneNumberId,

          to: recipientPhone,

          reaction: {

            messageId: reply.reaction.message_id,

            emoji: reply.reaction.emoji,

          },

        }),

        `sendReaction(${recipientPhone})`,

      );

    } catch (reactionError) {

      // No bloqueamos el envío del texto si la reacción falla

      console.warn('[KapsoBridge] Reacción falló (no bloquea texto):', reactionError?.message || reactionError);

    }

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


// Dispatch endpoint: Python retry task sends processed responses here for WhatsApp delivery
app.post('/api/v1/dispatch', async (req, res) => {
  try {
    const token = req.headers['x-kapso-internal-token'];
    if (KAPSO_INTERNAL_TOKEN && token !== KAPSO_INTERNAL_TOKEN) {
      return res.status(401).json({ error: 'unauthorized' });
    }
    const reply = req.body;
    if (!reply || !reply.recipient_phone || !reply.phone_number_id) {
      return res.status(400).json({ error: 'missing recipient_phone or phone_number_id' });
    }
    addBridgeDebugEvent('dispatch_retry', {
      to: reply.recipient_phone,
      phone_number_id: reply.phone_number_id,
      reply_type: reply.reply_type || 'text',
      message_id: reply.message_id,
    });
    console.log(`[KapsoBridge] dispatch retry to=${reply.recipient_phone} type=${reply.reply_type || 'text'}`);
    const sendResult = await dispatchKapsoResponse(reply);
    res.json({ ok: true, result: sendResult ?? null });
  } catch (error) {
    console.error('[KapsoBridge] dispatch error:', error?.message || error);
    res.status(500).json({ error: String(error?.message || error) });
  }
});


app.get('/openapi.json', async (req, res) => {

  await proxyFastApiRequest(req, res, '/openapi.json');

});



app.post('/api/v1/scheduling/disponibilidad', async (req, res) => {

  await proxyFastApiRequest(req, res, '/api/v1/scheduling/disponibilidad');

});



app.post('/api/v1/scheduling/crear-evento', async (req, res) => {

  await proxyFastApiRequest(req, res, '/api/v1/scheduling/crear-evento');

});



app.post('/api/v1/scheduling/reagendar-evento', async (req, res) => {

  await proxyFastApiRequest(req, res, '/api/v1/scheduling/reagendar-evento');

});



app.post('/api/v1/scheduling/eliminar-evento', async (req, res) => {

  await proxyFastApiRequest(req, res, '/api/v1/scheduling/eliminar-evento');

});

app.get('/debug/kapso', async (_req, res) => {

  if (!requireDebugAccess(_req, res)) return;

  try {

    const debugData = await collectKapsoDebugPayload();

    res.set('Cache-Control', 'no-store, max-age=0');

    res.status(200).type('html').send(renderKapsoBasicHtml(debugData, extractAccessToken(_req)));

  } catch (error) {

    res.status(500).type('html').send(`<pre>${escapeHtml(String(error))}</pre>`);

  }

});


/* ── SSE proxy: streams FastAPI debug events in real time ── */
app.get('/debug/kapso/stream', async (req, res) => {
  if (!requireDebugAccess(req, res)) return;
  res.set({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
    'Access-Control-Allow-Origin': '*',
  });
  res.flushHeaders();

  const baseUrl = getFastApiBaseUrl();
  let aborted = false;
  req.on('close', () => { aborted = true; });

  try {
    const upstream = await fetch(`${baseUrl}/api/v1/kapso/debug/stream`, {
      signal: AbortSignal.timeout(3600_000), // 1h max
    });
    if (!upstream.ok || !upstream.body) {
      res.write(`data: {"error":"upstream ${upstream.status}"}\n\n`);
      res.end();
      return;
    }
    const reader = upstream.body.getReader();
    const decoder = new TextDecoder();
    while (!aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      res.write(decoder.decode(value, { stream: true }));
    }
    reader.cancel().catch(() => {});
  } catch (err) {
    if (!aborted) {
      res.write(`data: {"error":"${String(err.message).slice(0, 200)}"}\n\n`);
    }
  }
  res.end();
});


app.get('/debug/kapso/visual', async (req, res) => {

  if (!requireDebugAccess(req, res)) return;

  res.set('Cache-Control', 'no-store, max-age=0');

  // Fetch graph schema + empresas from Python backend
  let graphData = null;
  let empresasList = [];

  try {

    const baseUrl = INTERNAL_AGENT_API_URL.replace(/\/api\/v1\/kapso\/inbound$/, '');

    const eid = req.query.empresa_id || DEFAULT_EMPRESA_ID;

    const empresaParam = eid ? `?empresa_id=${encodeURIComponent(eid)}` : '';

    const [graphRes, empRes] = await Promise.allSettled([
      fetch(`${baseUrl}/api/v1/graph/schema${empresaParam}`),
      fetch(`${baseUrl}/api/v1/kapso/debug/empresas`),
    ]);

    if (graphRes.status === 'fulfilled' && graphRes.value.ok) graphData = await graphRes.value.json();
    if (empRes.status === 'fulfilled' && empRes.value.ok) {
      const empJson = await empRes.value.json();
      empresasList = empJson.empresas || [];
    }

  } catch (err) {

    console.warn('[visual] Could not fetch graph schema:', err.message);

  }

  res.status(200).type('html').send(renderConstellationHtml(graphData, empresasList, extractAccessToken(req)));

});



app.get('/public/kapso/visual', async (req, res) => {

  res.set('Cache-Control', 'no-store, max-age=0');

  let graphData = null;

  try {

    const baseUrl = INTERNAL_AGENT_API_URL.replace(/\/api\/v1\/kapso\/inbound$/, '');

    const eid = req.query.empresa_id || DEFAULT_EMPRESA_ID;

    const empresaParam = eid ? `?empresa_id=${encodeURIComponent(eid)}` : '';

    const graphRes = await fetch(`${baseUrl}/api/v1/graph/schema${empresaParam}`);

    if (graphRes.ok) graphData = sanitizePublicConstellationGraph(await graphRes.json());

  } catch (err) {

    console.warn('[public visual] Could not fetch graph schema:', err.message);

  }



  const dataPathUrl = new URL('/public/kapso/visual/data', 'http://localhost');

  if (req.query.empresa_id) dataPathUrl.searchParams.set('empresa_id', String(req.query.empresa_id));

  res.status(200).type('html').send(renderPublicConstellationHtml(graphData, `${dataPathUrl.pathname}${dataPathUrl.search}`));

});



app.get('/public/kapso/visual/data', async (req, res) => {

  try {

    const payload = await collectKapsoPublicVisualPayload(req.query.empresa_id || '');

    res.set('Cache-Control', 'no-store, max-age=0');

    res.status(200).json(payload);

  } catch (error) {

    res.status(200).json({ events: [] });

  }

});



app.get('/debug/kapso/app.js', (_req, res) => {

  if (!requireDebugAccess(_req, res)) return;

  res.set('Cache-Control', 'no-store, max-age=0');

  res.status(200).type('application/javascript').send(renderKapsoDebugScript(extractAccessToken(_req)));

});



app.get('/debug/kapso/empresas', async (_req, res) => {
  if (!requireDebugAccess(_req, res)) return;
  try {
    const data = await fetchFastApiDebugJson('/api/v1/kapso/debug/empresas');
    res.set('Cache-Control', 'no-store, max-age=0');
    res.status(200).json(data);
  } catch (error) {
    res.status(200).json({ empresas: [] });
  }
});



app.get('/debug/kapso/data', async (_req, res) => {

  if (!requireDebugAccess(_req, res)) return;

  try {

    const debugData = await collectKapsoDebugPayload();

    res.set('Cache-Control', 'no-store, max-age=0');

    res.status(200).json(debugData);

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

            contact_name: sqlPayload.contact_name,

            phone_number_id: sqlPayload.phone_number_id,

            message_id: sqlPayload.message_id,

            message_type: sqlPayload.message_type,

            text: sqlPayload.text,

          });

          console.log(

            `[KapsoBridge] procesando from=${sqlPayload.from} phone_number_id=${sqlPayload.phone_number_id} message_id=${sqlPayload.message_id}`,

          );

          await markKapsoAsRead(sqlPayload.phone_number_id, sqlPayload.message_id);

          const typingKeepalive = startTypingKeepalive(sqlPayload.phone_number_id, sqlPayload.message_id);

          let reply;

          try {

            reply = await withTimeout(callInternalAgent(sqlPayload), PROCESS_TIMEOUT_MS);

          } finally {

            typingKeepalive.abort();

          }

          const sendResult = await dispatchKapsoResponse(reply);

          addBridgeDebugEvent('message_processing_done', {

            from: sqlPayload.from,

            phone_number_id: sqlPayload.phone_number_id,

            message_id: sqlPayload.message_id,

            send_result: sendResult ?? null,

          });

          console.log(

            `[KapsoBridge] mensaje procesado message_id=${sqlPayload.message_id} kapso_response=${JSON.stringify(sendResult ?? null)}`,

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

