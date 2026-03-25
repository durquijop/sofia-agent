"""
Debug Dashboard — HTML visual para /debug/kapso/

Renderiza un dashboard completo con eventos, configuración y
trazas de agente directamente desde el backend FastAPI.

Fuente de datos: tabla ``debug_events`` en Supabase (persistente)
+ eventos en memoria del proceso actual (para los más recientes).
"""

import json
import logging
import os
from html import escape

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.core.kapso_debug import get_kapso_debug_events, mask_secret
from app.db.client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(tags=["debug"])

def _get_debug_config() -> dict:
    settings = get_settings()

    return {
        "app_name": settings.APP_NAME,
        "default_model": settings.DEFAULT_MODEL,
        "python_service_port": os.getenv("PYTHON_SERVICE_PORT", "8000"),
        "internal_agent_api_url": os.getenv(
            "INTERNAL_AGENT_API_URL",
            "http://127.0.0.1:8000/api/v1/kapso/inbound",
        ),
        "kapso_internal_token": mask_secret(settings.KAPSO_INTERNAL_TOKEN),
        "supabase_url": mask_secret(settings.SUPABASE_URL),
        "fallback_phone": "(eliminado — error directo si no se resuelve)",
        "fallback_agent_id": "(eliminado — error directo si no se resuelve)",
    }


async def _load_events_from_supabase(limit: int = 200) -> list[dict]:
    """Load debug events from the persistent Supabase table."""
    try:
        db = await get_supabase()
        rows = await db.query(
            "debug_events",
            select="*",
            filters={"source": "kapso"},
            order="created_at",
            order_desc=True,
            limit=limit,
        )
        if not rows or not isinstance(rows, list):
            return []
        # Normalize to the same shape as in-memory events
        events = []
        for row in rows:
            events.append({
                "timestamp": row.get("created_at") or row.get("timestamp"),
                "source": row.get("source", "kapso"),
                "stage": row.get("stage", ""),
                "payload": row.get("payload") or {},
            })
        return events
    except Exception as exc:
        logger.warning("Failed to load debug_events from Supabase: %s", exc)
        return []


async def _get_merged_events(limit: int = 200) -> list[dict]:
    """Merge in-memory events with Supabase persisted events (dedup by timestamp+stage)."""
    memory_events = get_kapso_debug_events(limit)
    db_events = await _load_events_from_supabase(limit)

    # Build a dedup set from memory events
    seen = set()
    for ev in memory_events:
        key = (ev.get("timestamp", ""), ev.get("stage", ""))
        seen.add(key)

    # Merge: memory first (freshest), then DB events not already present
    merged = list(memory_events)
    for ev in db_events:
        key = (ev.get("timestamp", ""), ev.get("stage", ""))
        if key not in seen:
            merged.append(ev)
            seen.add(key)

    # Sort by timestamp descending
    merged.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return merged[:limit]


def _esc(value) -> str:
    """HTML-escape any value."""
    return escape(str(value)) if value is not None else "—"


def _build_interactions(events: list[dict]) -> list[dict]:
    """Group raw kapso debug events into interaction objects (like the bridge does)."""
    interactions_map: dict[str, dict] = {}

    for ev in events:
        payload = ev.get("payload") or {}
        stage = ev.get("stage", "")
        msg_id = payload.get("message_id") or payload.get("interaction_id")
        if not msg_id:
            continue

        if msg_id not in interactions_map:
            interactions_map[msg_id] = {
                "message_id": msg_id,
                "started_at": ev.get("timestamp"),
                "status": "processing",
                "events": [],
            }
        interaction = interactions_map[msg_id]
        interaction["events"].append(ev)

        if stage == "inbound_received":
            interaction["from_phone"] = payload.get("from_phone") or payload.get("from")
            interaction["message_type"] = payload.get("message_type")
            interaction["message_text"] = payload.get("text", payload.get("message_text"))
            interaction["contact_name"] = payload.get("contact_name")

        if stage == "inbound_entities_resolved":
            interaction["contact_name"] = interaction.get("contact_name") or payload.get("contact_name")
            interaction["conversation_id"] = payload.get("conversacion_id") or payload.get("conversation_db_id")
            interaction["from_phone"] = interaction.get("from_phone") or payload.get("normalized_from_phone")

        if stage == "run_agent_start":
            interaction["agent_name"] = interaction.get("agent_name") or payload.get("agent_name")
            interaction["model_used"] = interaction.get("model_used") or payload.get("model")

        if stage == "run_agent_done":
            interaction["status"] = "ok"
            interaction["agent_name"] = payload.get("agent_name") or interaction.get("agent_name")
            interaction["model_used"] = payload.get("model_used") or interaction.get("model_used")
            timing = payload.get("timing") or {}
            interaction["duration_ms"] = payload.get("total_ms") or timing.get("total_ms")
            interaction["reaction_emoji"] = payload.get("reaction_emoji")
            interaction["response_preview"] = (payload.get("response_preview") or payload.get("reply_text") or "")[:200]
            interaction["reply_type"] = payload.get("reply_type", "text")
            interaction["timing"] = timing
            interaction["tools_used"] = payload.get("tools_used") or []
            interaction["agent_runs"] = payload.get("agent_runs") or []

        if stage == "run_funnel_done":
            interaction["funnel_etapa_nueva"] = payload.get("etapa_nueva")
            interaction["funnel_metadata_actualizada"] = payload.get("metadata_actualizada")
            interaction["funnel_error"] = payload.get("error")

        if stage == "run_contact_update_done":
            interaction["contact_update_fields"] = payload.get("updated_fields")

        if stage in ("inbound_error", "error", "exception", "http_error"):
            interaction["status"] = "error"
            interaction["error"] = payload.get("error") or payload.get("detail")

    return sorted(
        interactions_map.values(),
        key=lambda x: x.get("started_at") or "",
        reverse=True,
    )


def _render_dashboard_html(config: dict) -> str:
    """Render SPA shell — all data loaded via AJAX from /debug/kapso/data."""
    config_json = json.dumps(config, ensure_ascii=False, indent=2)

    return """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kapso Debug — FastAPI</title>
  <style>
    body{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}
    .top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
    .title{font-size:20px;font-weight:700}
    .actions a,.actions button{color:#93c5fd;text-decoration:none;margin-left:12px;background:none;border:none;cursor:pointer;font-size:14px}
    .actions button:hover{text-decoration:underline}
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
    .auto-refresh{font-size:11px;color:#94a3b8;margin-left:12px}
    .loading{text-align:center;padding:40px;color:#94a3b8;font-size:14px}
    .pulse{animation:pulse 1.5s ease-in-out infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
    .source-badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600}
    .source-db{background:#1e3a5f;color:#7dd3fc}
    .source-mem{background:#3b1f5e;color:#c4b5fd}
    .new-row{animation:highlightNew 2.5s ease-out}
    @keyframes highlightNew{0%{background:#854d0e}40%{background:#713f12}100%{background:transparent}}
    .new-badge{display:inline-block;background:#f59e0b;color:#000;font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;margin-left:10px;animation:badgePop .4s ease-out}
    @keyframes badgePop{0%{transform:scale(0)}60%{transform:scale(1.2)}100%{transform:scale(1)}}
  </style>
</head>
<body>
  <div class="top">
    <div class="title">🔍 Kapso Debug — FastAPI Backend</div>
    <div class="actions">
      <button onclick="loadData()">⟳ Refrescar</button>
      <a href="/debug/kapso/data" target="_blank">Ver JSON</a>
      <a href="/docs" target="_blank">API Docs</a>
      <span class="auto-refresh" id="autoLabel"></span>
      <span id="newBadge" style="display:none"></span>
    </div>
  </div>

  <div class="stats" id="statsGrid">
    <div class="card"><div class="label">Interacciones</div><div class="value" id="statTotal">—</div></div>
    <div class="card"><div class="label">OK</div><div class="value" style="color:#4ade80" id="statOk">—</div></div>
    <div class="card"><div class="label">Errores</div><div class="value" style="color:#f87171" id="statErrors">—</div></div>
    <div class="card"><div class="label">Tiempo avg</div><div class="value" id="statAvg">—</div></div>
  </div>

  <div class="section">
    <table>
      <thead>
        <tr>
          <th>Hora</th><th>Contacto</th><th>Teléfono</th><th>Tipo</th>
          <th>Mensaje</th><th>Agente</th><th>Modelo</th><th>Reply</th>
          <th>Rx</th><th>Tiempo</th><th>Status</th><th>Detalle</th>
        </tr>
      </thead>
      <tbody id="interactionRows">
        <tr><td colspan="12" class="loading pulse">Cargando datos desde Supabase...</td></tr>
      </tbody>
    </table>
  </div>

  <div id="interactionDetails"></div>

  <details class="section">
    <summary id="eventsTitle">Eventos raw (0)</summary>
    <table>
      <thead><tr><th>Timestamp</th><th>Source</th><th>Stage</th><th>Payload</th></tr></thead>
      <tbody id="eventRows">
        <tr><td colspan="4" class="loading pulse">Cargando...</td></tr>
      </tbody>
    </table>
  </details>

  <details class="section">
    <summary>FastAPI Config</summary>
    <pre>""" + escape(config_json) + """</pre>
  </details>

  <script>
  const E = s => {
    const d = document.createElement('div');
    d.textContent = s != null ? String(s) : '—';
    return d.innerHTML;
  };

  function timingTable(t) {
    if (!t) t = {};
    const f = v => v != null ? E(v + ' ms') : '—';
    return `<table style="margin-top:8px">
      <thead><tr><th>Total</th><th>LLM</th><th>MCP</th><th>Graph</th><th>Tools</th></tr></thead>
      <tbody><tr><td>${f(t.total_ms)}</td><td>${f(t.llm_ms)}</td><td>${f(t.mcp_discovery_ms)}</td><td>${f(t.graph_build_ms)}</td><td>${f(t.tool_execution_ms)}</td></tr></tbody>
    </table>`;
  }

  function toolList(items) {
    if (!items || !items.length) return '<div style="color:#94a3b8">Sin herramientas.</div>';
    let rows = '';
    for (const it of items) {
      if (typeof it !== 'object') { rows += `<tr><td colspan="5">${E(it)}</td></tr>`; continue; }
      const dur = it.duration_ms != null ? it.duration_ms + ' ms' : '—';
      const errHtml = it.error ? `<div style="margin-top:8px;color:#fca5a5"><strong>Error:</strong> ${E(it.error)}</div>` : '';
      rows += `<tr><td>${E(it.tool_name||'—')}</td><td>${E(it.source||'—')}</td><td>${E(it.status||'ok')}</td><td>${E(dur)}</td><td>${E(it.description||'—')}</td></tr>
        <tr><td colspan="5">
          <div style="margin-bottom:8px"><strong>Input</strong></div><pre>${E(JSON.stringify(it.tool_input||{},null,2))}</pre>
          <div style="margin:8px 0"><strong>Output</strong></div><pre>${E(it.tool_output||'—')}</pre>${errHtml}
        </td></tr>`;
    }
    return `<table style="margin-top:8px"><thead><tr><th>Tool</th><th>Source</th><th>Estado</th><th>Tiempo</th><th>Descripción</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function agentRuns(runs) {
    if (!runs || !runs.length) return '<div style="color:#94a3b8">Sin trazas detalladas de agentes.</div>';
    return runs.map((r, i) => {
      const nm = E(r.agent_name || r.agent_key || 'Agente '+(i+1));
      const availRows = (r.available_tools||[]).map(t =>
        typeof t === 'object'
          ? `<tr><td>${E(t.tool_name||'—')}</td><td>${E(t.source||'—')}</td><td>${E(t.description||'—')}</td></tr>`
          : `<tr><td colspan="3">${E(t)}</td></tr>`
      ).join('') || '<tr><td colspan="3" style="color:#94a3b8">Sin herramientas disponibles.</td></tr>';
      return `<details style="margin-top:12px">
        <summary>${nm} · ${E(r.agent_kind||'agent')} · ${E(r.model_used||'—')}</summary>
        <div style="margin-top:12px">
          <div style="margin-bottom:10px"><strong>Agent key:</strong> ${E(r.agent_key||'—')}</div>
          <div style="margin-bottom:10px"><strong>Conversation:</strong> ${E(r.conversation_id||'—')}</div>
          <div style="margin-bottom:10px"><strong>Memory session:</strong> ${E(r.memory_session_id||'—')}</div>
          <div style="margin-bottom:10px"><strong>LLM iterations:</strong> ${E(r.llm_iterations??0)}</div>
          <div style="margin:12px 0 6px"><strong>Timing</strong></div>${timingTable(r.timing)}
          <div style="margin:12px 0 6px"><strong>Herramientas disponibles</strong></div>
          <table style="margin-top:8px"><thead><tr><th>Tool</th><th>Source</th><th>Descripción</th></tr></thead><tbody>${availRows}</tbody></table>
          <div style="margin:12px 0 6px"><strong>Herramientas ejecutadas</strong></div>${toolList(r.tools_used)}
          <details style="margin-top:12px"><summary>Prompts</summary><div style="margin-top:12px">
            <div style="margin:0 0 6px"><strong>System prompt</strong></div><pre>${E(r.system_prompt||'')}</pre>
            <div style="margin:12px 0 6px"><strong>User prompt</strong></div><pre>${E(r.user_prompt||'')}</pre>
          </div></details>
        </div>
      </details>`;
    }).join('');
  }

  function renderInteractions(interactions, newIds) {
    if (!newIds) newIds = new Set();
    const tbody = document.getElementById('interactionRows');
    const detailsDiv = document.getElementById('interactionDetails');

    if (!interactions || !interactions.length) {
      tbody.innerHTML = '<tr><td colspan="12" style="padding:20px;color:#94a3b8">Sin interacciones todavía.</td></tr>';
      detailsDiv.innerHTML = '';
      return;
    }

    // Stats
    const okCount = interactions.filter(i => i.status === 'ok').length;
    const errCount = interactions.filter(i => i.status === 'error').length;
    const durs = interactions.filter(i => i.duration_ms != null).map(i => i.duration_ms);
    const avg = durs.length ? Math.round(durs.reduce((a,b) => a+b, 0) / durs.length) : null;
    document.getElementById('statTotal').textContent = interactions.length;
    document.getElementById('statOk').textContent = okCount;
    document.getElementById('statErrors').textContent = errCount;
    document.getElementById('statAvg').textContent = avg != null ? avg + ' ms' : '—';

    // Table rows
    tbody.innerHTML = interactions.map((item, idx) => {
      const isNew = newIds.has(item.message_id);
      return `<tr class="${isNew ? 'new-row' : ''}">
      <td>${E(item.started_at||'—')}</td>
      <td>${E(item.contact_name||'—')}</td>
      <td>${E(item.from_phone||'—')}</td>
      <td>${E(item.message_type||'text')}</td>
      <td style="white-space:pre-wrap;max-width:320px">${E(item.message_text||'—')}</td>
      <td>${E(item.agent_name||'—')}</td>
      <td>${E(item.model_used||'—')}</td>
      <td>${E(item.reply_type||'text')}</td>
      <td>${E(item.reaction_emoji||'—')}</td>
      <td>${E(item.duration_ms != null ? item.duration_ms + ' ms' : '—')}</td>
      <td>${E(item.status||'processing')}</td>
      <td><a href="#interaction-${idx}" style="color:#93c5fd" onclick="document.getElementById('interaction-${idx}').open=true">Ver detalle</a></td>
    </tr>`;
    }).join('');

    // Details
    detailsDiv.innerHTML = interactions.map((item, idx) => {
      const label = E(item.contact_name || item.from_phone || item.message_id || 'Interacción '+(idx+1));
      const dur = item.duration_ms != null ? item.duration_ms + ' ms' : '—';
      const funnelJson = JSON.stringify({
        etapa_nueva: item.funnel_etapa_nueva ?? null,
        metadata_actualizada: item.funnel_metadata_actualizada ?? null,
        error: item.funnel_error ?? null
      }, null, 2);
      return `<details class="section" id="interaction-${idx}">
        <summary>${label} · ${E(item.status||'processing')} · ${E(dur)}</summary>
        <div style="margin-top:12px">
          <div style="margin-bottom:8px"><strong>Message ID:</strong> ${E(item.message_id||'—')}</div>
          <div style="margin:12px 0 6px"><strong>Error</strong></div><pre>${E(item.error||'—')}</pre>
          <div style="margin-bottom:8px"><strong>Mensaje:</strong></div><pre>${E(item.message_text||'—')}</pre>
          <div style="margin:12px 0 6px"><strong>Respuesta preview</strong></div><pre>${E(item.response_preview||'—')}</pre>
          <div style="margin:12px 0 6px"><strong>Embudo en metadata</strong></div><pre>${E(funnelJson)}</pre>
          <div style="margin:12px 0 6px"><strong>Timing global</strong></div>${timingTable(item.timing)}
          <div style="margin:12px 0 6px"><strong>Tools globales</strong></div>${toolList(item.tools_used)}
          <div style="margin:12px 0 6px"><strong>Trazas detalladas del agente</strong></div>${agentRuns(item.agent_runs)}
        </div>
      </details>`;
    }).join('');
  }

  function renderEvents(events) {
    const tbody = document.getElementById('eventRows');
    const title = document.getElementById('eventsTitle');
    title.textContent = 'Eventos raw (' + (events ? events.length : 0) + ')';

    if (!events || !events.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="padding:16px;color:#94a3b8">Sin eventos.</td></tr>';
      return;
    }

    tbody.innerHTML = events.slice(0, 150).map(ev => {
      const payload = ev.payload || {};
      return `<tr>
        <td>${E(ev.timestamp||'—')}</td>
        <td>${E(ev.source||'—')}</td>
        <td>${E(ev.stage||'—')}</td>
        <td style="max-width:400px;word-break:break-word"><pre style="margin:0;font-size:11px">${E(JSON.stringify(payload).substring(0, 500))}</pre></td>
      </tr>`;
    }).join('');
  }

  let refreshInterval = 5;
  let countdown = refreshInterval;
  const label = document.getElementById('autoLabel');
  const newBadge = document.getElementById('newBadge');
  let knownMessageIds = new Set();
  let isFirstLoad = true;

  async function loadData() {
    countdown = refreshInterval;
    try {
      const r = await fetch('/debug/kapso/data', {cache: 'no-store'});
      const data = await r.json();
      const interactions = data.interactions || [];

      // Detect new messages
      const currentIds = new Set(interactions.map(i => i.message_id).filter(Boolean));
      let newIds = new Set();
      if (!isFirstLoad) {
        currentIds.forEach(id => { if (!knownMessageIds.has(id)) newIds.add(id); });
      }
      knownMessageIds = currentIds;
      isFirstLoad = false;

      renderInteractions(interactions, newIds);
      renderEvents(data.fastapi_events || []);

      // Show badge for new messages
      if (newIds.size > 0) {
        newBadge.textContent = '+' + newIds.size + ' nuevo' + (newIds.size > 1 ? 's' : '');
        newBadge.className = 'new-badge';
        newBadge.style.display = 'inline-block';
        setTimeout(() => { newBadge.style.display = 'none'; }, 4000);
      }
    } catch (err) {
      console.error('Debug data load error:', err);
      document.getElementById('interactionRows').innerHTML =
        '<tr><td colspan="12" style="padding:20px;color:#fca5a5">Error cargando datos: ' + E(err.message) + '</td></tr>';
    }
  }

  setInterval(() => {
    countdown--;
    if (label) label.textContent = 'Auto-refresh en ' + countdown + 's';
    if (countdown <= 0) loadData();
  }, 1000);

  // Initial load
  loadData();
  </script>
</body>
</html>"""


@router.get("/debug/kapso/", response_class=HTMLResponse)
@router.get("/debug/kapso", response_class=HTMLResponse)
async def debug_kapso_dashboard():
    """Dashboard visual de debug para Kapso."""
    config = _get_debug_config()
    return _render_dashboard_html(config)


@router.get("/debug/kapso/data")
async def debug_kapso_data(limit: int = Query(default=200, ge=1, le=500)):
    """JSON con eventos merged (memoria + Supabase persistido)."""
    events = await _get_merged_events(limit)
    config = _get_debug_config()
    interactions = _build_interactions(events)
    return {
        "fastapi_config": config,
        "fastapi_events": events,
        "interactions": interactions,
    }
