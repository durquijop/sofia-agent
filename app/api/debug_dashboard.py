"""
Debug Dashboard — HTML visual para /debug/kapso/

Renderiza un dashboard completo con eventos, configuración y
trazas de agente directamente desde el backend FastAPI.
"""

import json
from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.core.kapso_debug import get_kapso_debug_events, mask_secret

router = APIRouter(tags=["debug"])

DEFAULT_KAPSO_FALLBACK_PHONE = "14705500109"
DEFAULT_KAPSO_FALLBACK_AGENT_ID = 4


def _get_debug_config() -> dict:
    settings = get_settings()
    import os

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
        "fallback_phone": DEFAULT_KAPSO_FALLBACK_PHONE,
        "fallback_agent_id": DEFAULT_KAPSO_FALLBACK_AGENT_ID,
    }


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
            interaction["from_phone"] = payload.get("from_phone")
            interaction["message_type"] = payload.get("message_type")
            interaction["message_text"] = payload.get("text", payload.get("message_text"))
            interaction["contact_name"] = payload.get("contact_name")

        if stage == "inbound_entities_resolved":
            interaction["agent_name"] = payload.get("agent_name")
            interaction["contact_name"] = interaction.get("contact_name") or payload.get("contact_name")
            interaction["conversation_id"] = payload.get("conversacion_id")

        if stage == "run_agent_done":
            interaction["status"] = "ok"
            interaction["model_used"] = payload.get("model_used")
            interaction["duration_ms"] = payload.get("total_ms")
            interaction["reaction_emoji"] = payload.get("reaction_emoji")
            interaction["response_preview"] = (payload.get("reply_text") or "")[:200]
            interaction["reply_type"] = payload.get("reply_type", "text")
            interaction["timing"] = payload.get("timing") or {}
            interaction["tools_used"] = payload.get("tools_used") or []
            interaction["agent_runs"] = payload.get("agent_runs") or []

        if stage == "run_funnel_done":
            interaction["funnel_etapa_nueva"] = payload.get("etapa_nueva")
            interaction["funnel_metadata_actualizada"] = payload.get("metadata_actualizada")
            interaction["funnel_error"] = payload.get("error")

        if stage == "run_contact_update_done":
            interaction["contact_update_fields"] = payload.get("updated_fields")

        if stage in ("inbound_error", "error"):
            interaction["status"] = "error"
            interaction["error"] = payload.get("error") or payload.get("detail")

    return sorted(
        interactions_map.values(),
        key=lambda x: x.get("started_at") or "",
        reverse=True,
    )


def _render_timing_table(timing: dict) -> str:
    return f"""<table style="margin-top:8px">
      <thead><tr><th>Total</th><th>LLM</th><th>MCP</th><th>Graph</th><th>Tools</th></tr></thead>
      <tbody><tr>
        <td>{_esc(f"{timing.get('total_ms')} ms" if timing.get('total_ms') is not None else '—')}</td>
        <td>{_esc(f"{timing.get('llm_ms')} ms" if timing.get('llm_ms') is not None else '—')}</td>
        <td>{_esc(f"{timing.get('mcp_discovery_ms')} ms" if timing.get('mcp_discovery_ms') is not None else '—')}</td>
        <td>{_esc(f"{timing.get('graph_build_ms')} ms" if timing.get('graph_build_ms') is not None else '—')}</td>
        <td>{_esc(f"{timing.get('tool_execution_ms')} ms" if timing.get('tool_execution_ms') is not None else '—')}</td>
      </tr></tbody>
    </table>"""


def _render_tool_list(items: list) -> str:
    if not items:
        return '<div style="color:#94a3b8">Sin herramientas.</div>'
    rows = ""
    for item in items:
        if isinstance(item, dict):
            tool_name = item.get("tool_name", "—")
            source = item.get("source", "—")
            status = item.get("status", "ok")
            duration = f"{item['duration_ms']} ms" if item.get("duration_ms") is not None else "—"
            desc = item.get("description", "—")
            tool_input = json.dumps(item.get("tool_input", {}), indent=2, ensure_ascii=False)
            tool_output = item.get("tool_output", "—")
            error = item.get("error", "")
        else:
            tool_name = str(item)
            source = status = duration = desc = tool_input = tool_output = error = "—"
        error_html = f'<div style="margin-top:8px;color:#fca5a5"><strong>Error:</strong> {_esc(error)}</div>' if error and error != "—" else ""
        rows += f"""<tr>
          <td>{_esc(tool_name)}</td><td>{_esc(source)}</td><td>{_esc(status)}</td>
          <td>{_esc(duration)}</td><td>{_esc(desc)}</td>
        </tr>
        <tr><td colspan="5">
          <div style="margin-bottom:8px"><strong>Input</strong></div>
          <pre>{_esc(tool_input)}</pre>
          <div style="margin:8px 0"><strong>Output</strong></div>
          <pre>{_esc(tool_output)}</pre>
          {error_html}
        </td></tr>"""
    return f"""<table style="margin-top:8px">
      <thead><tr><th>Tool</th><th>Source</th><th>Estado</th><th>Tiempo</th><th>Descripción</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _render_agent_runs(agent_runs: list) -> str:
    if not agent_runs:
        return '<div style="color:#94a3b8">Sin trazas detalladas de agentes.</div>'
    html = ""
    for idx, run in enumerate(agent_runs):
        if not isinstance(run, dict):
            continue
        name = run.get("agent_name") or run.get("agent_key") or f"Agente {idx + 1}"
        kind = run.get("agent_kind", "agent")
        model = run.get("model_used", "—")
        conv_id = run.get("conversation_id", "—")
        mem_id = run.get("memory_session_id", "—")
        iters = run.get("llm_iterations", 0)
        timing_html = _render_timing_table(run.get("timing") or {})
        tools_html = _render_tool_list(run.get("tools_used") or [])
        sys_prompt = run.get("system_prompt", "")
        user_prompt = run.get("user_prompt", "")
        avail_tools = run.get("available_tools") or []
        avail_html = ""
        if avail_tools:
            at_rows = "".join(
                f'<tr><td>{_esc(t.get("tool_name","—") if isinstance(t,dict) else t)}</td>'
                f'<td>{_esc(t.get("source","—") if isinstance(t,dict) else "—")}</td>'
                f'<td>{_esc(t.get("description","—") if isinstance(t,dict) else "—")}</td></tr>'
                for t in avail_tools
            )
            avail_html = f'<table style="margin-top:8px"><thead><tr><th>Tool</th><th>Source</th><th>Descripción</th></tr></thead><tbody>{at_rows}</tbody></table>'
        else:
            avail_html = '<div style="color:#94a3b8">Sin herramientas disponibles.</div>'

        html += f"""<details style="margin-top:12px">
          <summary>{_esc(name)} · {_esc(kind)} · {_esc(model)}</summary>
          <div style="margin-top:12px">
            <div style="margin-bottom:10px"><strong>Agent key:</strong> {_esc(run.get("agent_key","—"))}</div>
            <div style="margin-bottom:10px"><strong>Conversation:</strong> {_esc(conv_id)}</div>
            <div style="margin-bottom:10px"><strong>Memory session:</strong> {_esc(mem_id)}</div>
            <div style="margin-bottom:10px"><strong>LLM iterations:</strong> {_esc(iters)}</div>
            <div style="margin:12px 0 6px"><strong>Timing</strong></div>
            {timing_html}
            <div style="margin:12px 0 6px"><strong>Herramientas disponibles</strong></div>
            {avail_html}
            <div style="margin:12px 0 6px"><strong>Herramientas ejecutadas</strong></div>
            {tools_html}
            <details style="margin-top:12px">
              <summary>Prompts</summary>
              <div style="margin-top:12px">
                <div style="margin:0 0 6px"><strong>System prompt</strong></div>
                <pre>{_esc(sys_prompt)}</pre>
                <div style="margin:12px 0 6px"><strong>User prompt</strong></div>
                <pre>{_esc(user_prompt)}</pre>
              </div>
            </details>
          </div>
        </details>"""
    return html


def _render_dashboard_html(events: list[dict], config: dict) -> str:
    interactions = _build_interactions(events)
    ok_count = sum(1 for i in interactions if i.get("status") == "ok")
    error_count = sum(1 for i in interactions if i.get("status") == "error")
    durations = [i["duration_ms"] for i in interactions if i.get("duration_ms") is not None]
    avg_duration = round(sum(durations) / len(durations)) if durations else None

    # --- Interaction rows ---
    interaction_rows = ""
    if interactions:
        for idx, item in enumerate(interactions):
            interaction_rows += f"""<tr>
              <td>{_esc(item.get("started_at","—"))}</td>
              <td>{_esc(item.get("contact_name","—"))}</td>
              <td>{_esc(item.get("from_phone","—"))}</td>
              <td>{_esc(item.get("message_type","text"))}</td>
              <td style="white-space:pre-wrap;max-width:320px">{_esc(item.get("message_text","—"))}</td>
              <td>{_esc(item.get("agent_name","—"))}</td>
              <td>{_esc(item.get("model_used","—"))}</td>
              <td>{_esc(item.get("reply_type","text"))}</td>
              <td>{_esc(item.get("reaction_emoji","—"))}</td>
              <td>{_esc(f"{item['duration_ms']} ms" if item.get("duration_ms") is not None else "—")}</td>
              <td>{_esc(item.get("status","processing"))}</td>
              <td><a href="#interaction-{idx}" style="color:#93c5fd">Ver detalle</a></td>
            </tr>"""
    else:
        interaction_rows = '<tr><td colspan="12" style="padding:20px;color:#94a3b8">Sin interacciones todavía.</td></tr>'

    # --- Interaction details ---
    interaction_details = ""
    for idx, item in enumerate(interactions):
        funnel_json = json.dumps(
            {
                "etapa_nueva": item.get("funnel_etapa_nueva"),
                "metadata_actualizada": item.get("funnel_metadata_actualizada"),
                "error": item.get("funnel_error"),
            },
            indent=2,
            ensure_ascii=False,
        )
        timing_html = _render_timing_table(item.get("timing") or {})
        tools_html = _render_tool_list(item.get("tools_used") or [])
        agent_runs_html = _render_agent_runs(item.get("agent_runs") or [])
        label = item.get("contact_name") or item.get("from_phone") or item.get("message_id") or f"Interacción {idx + 1}"
        status = item.get("status", "processing")
        dur = f"{item['duration_ms']} ms" if item.get("duration_ms") is not None else "—"

        interaction_details += f"""<details class="section" id="interaction-{idx}">
          <summary>{_esc(label)} · {_esc(status)} · {_esc(dur)}</summary>
          <div style="margin-top:12px">
            <div style="margin-bottom:8px"><strong>Message ID:</strong> {_esc(item.get("message_id","—"))}</div>
            <div style="margin:12px 0 6px"><strong>Error</strong></div>
            <pre>{_esc(item.get("error","—"))}</pre>
            <div style="margin-bottom:8px"><strong>Mensaje:</strong></div>
            <pre>{_esc(item.get("message_text","—"))}</pre>
            <div style="margin:12px 0 6px"><strong>Respuesta preview</strong></div>
            <pre>{_esc(item.get("response_preview","—"))}</pre>
            <div style="margin:12px 0 6px"><strong>Embudo en metadata</strong></div>
            <pre>{_esc(funnel_json)}</pre>
            <div style="margin:12px 0 6px"><strong>Timing global</strong></div>
            {timing_html}
            <div style="margin:12px 0 6px"><strong>Tools globales</strong></div>
            {tools_html}
            <div style="margin:12px 0 6px"><strong>Trazas detalladas del agente</strong></div>
            {agent_runs_html}
          </div>
        </details>"""

    # --- Raw events table ---
    event_rows = ""
    for ev in events[:100]:
        payload = ev.get("payload") or {}
        event_rows += f"""<tr>
          <td>{_esc(ev.get("timestamp","—"))}</td>
          <td>{_esc(ev.get("source","—"))}</td>
          <td>{_esc(ev.get("stage","—"))}</td>
          <td style="max-width:400px;word-break:break-word"><pre style="margin:0;font-size:11px">{_esc(json.dumps(payload, ensure_ascii=False)[:500])}</pre></td>
        </tr>"""

    avg_text = f"{avg_duration} ms" if avg_duration is not None else "—"

    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kapso Debug — FastAPI</title>
  <style>
    body{{font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}}
    .top{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
    .title{{font-size:20px;font-weight:700}}
    .actions a{{color:#93c5fd;text-decoration:none;margin-left:12px}}
    .stats{{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:12px;margin-bottom:16px}}
    .card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px}}
    .label{{font-size:11px;color:#94a3b8;text-transform:uppercase}}
    .value{{font-size:22px;font-weight:700;margin-top:6px}}
    table{{width:100%;border-collapse:collapse;background:#111827;border:1px solid #334155}}
    th,td{{padding:10px;border-bottom:1px solid #334155;text-align:left;vertical-align:top;font-size:12px}}
    th{{background:#1e293b;color:#93c5fd}}
    .section{{margin-top:18px}}
    details{{margin-top:12px;background:#111827;border:1px solid #334155;border-radius:8px;padding:12px}}
    summary{{cursor:pointer;font-weight:700}}
    pre{{white-space:pre-wrap;word-break:break-word;color:#cbd5e1;font-size:12px}}
    .auto-refresh{{font-size:11px;color:#94a3b8;margin-left:12px}}
  </style>
</head>
<body>
  <div class="top">
    <div class="title">🔍 Kapso Debug — FastAPI Backend</div>
    <div class="actions">
      <a href="/debug/kapso/">Refrescar</a>
      <a href="/debug/kapso/data" target="_blank">Ver JSON</a>
      <a href="/docs" target="_blank">API Docs</a>
      <span class="auto-refresh" id="autoLabel"></span>
    </div>
  </div>

  <div class="stats">
    <div class="card"><div class="label">Interacciones</div><div class="value">{len(interactions)}</div></div>
    <div class="card"><div class="label">OK</div><div class="value" style="color:#4ade80">{ok_count}</div></div>
    <div class="card"><div class="label">Errores</div><div class="value" style="color:#f87171">{error_count}</div></div>
    <div class="card"><div class="label">Tiempo avg</div><div class="value">{_esc(avg_text)}</div></div>
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
      <tbody>{interaction_rows}</tbody>
    </table>
  </div>

  {interaction_details}

  <details class="section">
    <summary>Eventos raw ({len(events)})</summary>
    <table>
      <thead><tr><th>Timestamp</th><th>Source</th><th>Stage</th><th>Payload</th></tr></thead>
      <tbody>{event_rows if event_rows else '<tr><td colspan="4" style="padding:16px;color:#94a3b8">Sin eventos.</td></tr>'}</tbody>
    </table>
  </details>

  <details class="section">
    <summary>FastAPI Config</summary>
    <pre>{_esc(json.dumps(config, indent=2, ensure_ascii=False))}</pre>
  </details>

  <script>
    // Auto-refresh every 15 seconds
    let countdown = 15;
    const label = document.getElementById('autoLabel');
    setInterval(() => {{
      countdown--;
      if (label) label.textContent = 'Auto-refresh en ' + countdown + 's';
      if (countdown <= 0) {{ window.location.reload(); }}
    }}, 1000);
  </script>
</body>
</html>"""


@router.get("/debug/kapso/", response_class=HTMLResponse)
@router.get("/debug/kapso", response_class=HTMLResponse)
async def debug_kapso_dashboard():
    """Dashboard visual de debug para Kapso."""
    events = get_kapso_debug_events(200)
    config = _get_debug_config()
    return _render_dashboard_html(events, config)


@router.get("/debug/kapso/data")
async def debug_kapso_data():
    """JSON raw con todos los datos de debug."""
    events = get_kapso_debug_events(200)
    config = _get_debug_config()
    interactions = _build_interactions(events)
    return {
        "fastapi_config": config,
        "fastapi_events": events,
        "interactions": interactions,
    }
