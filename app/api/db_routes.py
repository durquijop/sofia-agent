"""Endpoints para consultas a Supabase."""
import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.db import queries as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/db", tags=["database"])


@router.get("/health")
async def db_health():
    """Verifica conexión a Supabase."""
    try:
        from app.db.client import get_supabase
        sb = await get_supabase()
        res = await sb.query("wp_empresa_perfil", select="id", count=True, limit=1)
        return {
            "status": "ok",
            "supabase": "connected",
            "empresas_count": res["count"] if isinstance(res, dict) else 0,
        }
    except Exception as e:
        logger.error(f"DB health check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")


@router.get("/empresa/{empresa_id}")
async def get_empresa(empresa_id: int):
    """Obtiene perfil de empresa."""
    data = await db.get_empresa(empresa_id)
    if not data:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    return data


@router.get("/empresa/{empresa_id}/agentes")
async def get_agentes(empresa_id: int):
    """Lista agentes activos de una empresa."""
    return await db.get_agentes_por_empresa(empresa_id)


@router.get("/empresa/{empresa_id}/embudo")
async def get_embudo(empresa_id: int):
    """Obtiene las etapas del embudo de una empresa."""
    return await db.get_empresa_embudo(empresa_id)


@router.get("/empresa/{empresa_id}/team")
async def get_team(empresa_id: int):
    """Lista miembros activos del equipo."""
    return await db.get_team_disponible(empresa_id)


@router.get("/agente/{agente_id}")
async def get_agente(agente_id: int):
    """Obtiene configuración completa de un agente."""
    data = await db.get_agente(agente_id)
    if not data:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    return data


@router.get("/agente/{agente_id}/tools")
async def get_agente_tools(agente_id: int):
    """Obtiene herramientas MCP de un agente."""
    return await db.get_agente_tools(agente_id)


@router.get("/contacto/{contacto_id}")
async def get_contacto(contacto_id: int):
    """Obtiene un contacto con contexto (notas, citas)."""
    data = await db.get_contacto_con_contexto(contacto_id)
    if not data:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
    return data


@router.get("/contacto/buscar/telefono")
async def buscar_contacto_telefono(
    telefono: str = Query(..., description="Teléfono del contacto"),
    empresa_id: int = Query(..., description="ID de la empresa"),
):
    """Busca contacto por teléfono dentro de una empresa."""
    data = await db.get_contacto_por_telefono(telefono, empresa_id)
    if not data:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
    return data


@router.get("/conversacion/{conversacion_id}/mensajes")
async def get_mensajes(
    conversacion_id: int,
    limit: int = Query(default=20, le=100, description="Máximo de mensajes"),
):
    """Obtiene los últimos mensajes de una conversación."""
    return await db.get_mensajes_recientes(conversacion_id, limit)


@router.get("/numero/{numero_id}")
async def get_numero(numero_id: int):
    """Obtiene configuración de un número/canal."""
    data = await db.get_numero(numero_id)
    if not data:
        raise HTTPException(status_code=404, detail="Número no encontrado")
    return data


# ─── Debug Events (persistidos en Supabase) ──────────────────────────────


@router.get("/debug/events")
async def get_debug_events(
    source: str | None = Query(default=None, description="Filtrar por source: kapso, funnel, bridge"),
    limit: int = Query(default=50, le=500, description="Máximo de eventos"),
):
    """Retorna los últimos debug events persistidos en Supabase."""
    try:
        from app.db.client import get_supabase

        sb = await get_supabase()
        filters = {}
        if source:
            filters["source"] = source

        result = await sb.query(
            "debug_events",
            select="*",
            filters=filters,
            order="created_at",
            order_desc=True,
            limit=limit,
        )
        return {"events": result if isinstance(result, list) else [], "source_filter": source}
    except Exception as e:
        logger.error("Error fetching debug_events: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/debug/realtime", response_class=HTMLResponse)
async def debug_realtime_dashboard():
    """Dashboard con Supabase Realtime para debug_events."""
    from app.core.config import get_settings

    settings = get_settings()
    return _render_realtime_debug_html(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


def _escape_html(text: str | None) -> str:
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _render_realtime_debug_html(supabase_url: str, supabase_key: str) -> str:
    esc_url = _escape_html(supabase_url)
    esc_key = _escape_html(supabase_key)

    return (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>Debug Realtime</title>\n'
        '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>\n'
        "<style>\n"
        "*{box-sizing:border-box;margin:0;padding:0}\n"
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#e2e8f0;padding:16px}\n'
        ".header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:8px}\n"
        "h1{font-size:20px;color:#f8fafc}\n"
        ".status{display:flex;align-items:center;gap:8px;font-size:13px}\n"
        ".dot{width:10px;height:10px;border-radius:50%;background:#ef4444;display:inline-block}\n"
        ".dot.connected{background:#22c55e;animation:pulse 2s infinite}\n"
        "@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}\n"
        ".filters{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}\n"
        ".filters button{padding:6px 14px;border:1px solid #334155;border-radius:6px;background:#1e293b;color:#94a3b8;cursor:pointer;font-size:12px;transition:all .15s}\n"
        ".filters button:hover,.filters button.active{background:#334155;color:#f8fafc;border-color:#60a5fa}\n"
        ".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}\n"
        ".stat{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px}\n"
        ".stat-val{font-size:22px;font-weight:700;color:#f8fafc}\n"
        ".stat-lbl{font-size:11px;color:#64748b;text-transform:uppercase;margin-top:2px}\n"
        "#events{display:flex;flex-direction:column;gap:6px;max-height:calc(100vh - 240px);overflow-y:auto;padding-right:4px}\n"
        "#events::-webkit-scrollbar{width:6px}\n"
        "#events::-webkit-scrollbar-thumb{background:#334155;border-radius:3px}\n"
        ".evt{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px;cursor:pointer;transition:border-color .15s}\n"
        ".evt:hover{border-color:#60a5fa}\n"
        ".evt.new{animation:flash .6s ease-out}\n"
        "@keyframes flash{0%{background:#1e3a5f;border-color:#3b82f6}100%{background:#1e293b;border-color:#334155}}\n"
        ".evt-top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}\n"
        ".evt-left{display:flex;gap:8px;align-items:center}\n"
        ".badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}\n"
        ".badge-kapso{background:#312e81;color:#a5b4fc}\n"
        ".badge-funnel{background:#1e3a2f;color:#6ee7b7}\n"
        ".badge-bridge{background:#3b1e1e;color:#fca5a5}\n"
        ".stage{font-size:13px;color:#cbd5e1}\n"
        ".time{font-size:11px;color:#64748b;white-space:nowrap}\n"
        ".evt-detail{display:none;margin-top:10px;padding-top:10px;border-top:1px solid #334155}\n"
        ".evt.open .evt-detail{display:block}\n"
        "pre{background:#0f172a;color:#94a3b8;padding:10px;border-radius:6px;font-size:11px;overflow-x:auto;max-height:300px;overflow-y:auto}\n"
        ".empty{text-align:center;color:#475569;padding:40px;font-size:14px}\n"
        "</style>\n</head>\n<body>\n\n"
        '<div class="header">\n'
        "  <h1>&#128752; Debug Realtime</h1>\n"
        '  <div class="status">\n'
        '    <span class="dot" id="statusDot"></span>\n'
        '    <span id="statusText">Conectando...</span>\n'
        '    <span style="color:#475569;margin-left:8px" id="countText">0 eventos</span>\n'
        "  </div>\n</div>\n\n"
        '<div class="filters">\n'
        '  <button class="active" data-filter="all">Todos</button>\n'
        '  <button data-filter="kapso">Kapso</button>\n'
        '  <button data-filter="funnel">Funnel</button>\n'
        '  <button data-filter="bridge">Bridge</button>\n'
        "</div>\n\n"
        '<div class="stats">\n'
        '  <div class="stat"><div class="stat-val" id="sTotal">0</div><div class="stat-lbl">Total</div></div>\n'
        '  <div class="stat"><div class="stat-val" id="sKapso">0</div><div class="stat-lbl">Kapso</div></div>\n'
        '  <div class="stat"><div class="stat-val" id="sFunnel">0</div><div class="stat-lbl">Funnel</div></div>\n'
        '  <div class="stat"><div class="stat-val" id="sOk">0</div><div class="stat-lbl">OK</div></div>\n'
        '  <div class="stat"><div class="stat-val" id="sErr">0</div><div class="stat-lbl">Errores</div></div>\n'
        "</div>\n\n"
        '<div id="events"><div class="empty">Esperando eventos...</div></div>\n\n'
        "<script>\n"
        'const SUPABASE_URL = "' + esc_url + '";\n'
        'const SUPABASE_KEY = "' + esc_key + '";\n'
        "const sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_KEY);\n\n"
        "let events = [];\n"
        'let filter = "all";\n'
        "let stats = {total:0, kapso:0, funnel:0, ok:0, err:0};\n\n"
        'const $events  = document.getElementById("events");\n'
        'const $dot     = document.getElementById("statusDot");\n'
        'const $status  = document.getElementById("statusText");\n'
        'const $count   = document.getElementById("countText");\n'
        'const $sTotal  = document.getElementById("sTotal");\n'
        'const $sKapso  = document.getElementById("sKapso");\n'
        'const $sFunnel = document.getElementById("sFunnel");\n'
        'const $sOk     = document.getElementById("sOk");\n'
        'const $sErr    = document.getElementById("sErr");\n\n'
        'document.querySelectorAll(".filters button").forEach(btn => {\n'
        '  btn.addEventListener("click", () => {\n'
        '    document.querySelectorAll(".filters button").forEach(b => b.classList.remove("active"));\n'
        '    btn.classList.add("active");\n'
        "    filter = btn.dataset.filter;\n"
        "    renderEvents();\n"
        "  });\n"
        "});\n\n"
        "function fmtTime(iso) {\n"
        '  if (!iso) return "\\u2014";\n'
        "  const d = new Date(iso);\n"
        '  return d.toLocaleTimeString("es", {hour:"2-digit",minute:"2-digit",second:"2-digit"});\n'
        "}\n\n"
        "function escHtml(s) {\n"
        '  const d = document.createElement("div");\n'
        '  d.textContent = String(s ?? "");\n'
        "  return d.innerHTML;\n"
        "}\n\n"
        "function updateStats() {\n"
        "  $sTotal.textContent  = stats.total;\n"
        "  $sKapso.textContent  = stats.kapso;\n"
        "  $sFunnel.textContent = stats.funnel;\n"
        "  $sOk.textContent     = stats.ok;\n"
        "  $sErr.textContent    = stats.err;\n"
        '  $count.textContent   = stats.total + " eventos";\n'
        "}\n\n"
        "function renderEvents() {\n"
        '  const filtered = filter === "all" ? events : events.filter(e => e.source === filter);\n'
        "  if (!filtered.length) {\n"
        "    $events.innerHTML = '<div class=\"empty\">Sin eventos' + "
        '(filter !== "all" ? " para " + filter : "") + \'</div>\';\n'
        "    return;\n"
        "  }\n"
        "  $events.innerHTML = filtered.map((e, i) => {\n"
        '    const badgeClass = "badge-" + (e.source || "kapso");\n'
        "    const payload = e.payload || {};\n"
        '    const stage = escHtml(e.stage || "\\u2014");\n'
        '    const isErr = (e.stage && e.stage.includes("error")) || (e.stage && e.stage.includes("unauthorized")) || payload.success === false;\n'
        '    const stageColor = isErr ? "color:#f87171" : "";\n'
        "    return '<div class=\"evt ' + (i === 0 && e._new ? 'new' : '') + '\" onclick=\"this.classList.toggle(\\'open\\')\">' +\n"
        "      '<div class=\"evt-top\">' +\n"
        "        '<div class=\"evt-left\">' +\n"
        "          '<span class=\"badge ' + badgeClass + '\">' + escHtml(e.source) + '</span>' +\n"
        "          '<span class=\"stage\" style=\"' + stageColor + '\">' + stage + '</span>' +\n"
        '          (payload.contacto_id ? \'<span style="font-size:11px;color:#64748b">Contacto #\' + payload.contacto_id + \'</span>\' : \'\') +\n'
        "        '</div>' +\n"
        "        '<span class=\"time\">' + fmtTime(e.created_at) + '</span>' +\n"
        "      '</div>' +\n"
        "      '<div class=\"evt-detail\"><pre>' + escHtml(JSON.stringify(payload, null, 2)) + '</pre></div>' +\n"
        "    '</div>';\n"
        '  }).join("");\n'
        "}\n\n"
        "function addEvent(evt, isNew) {\n"
        "  if (isNew) evt._new = true;\n"
        "  events.unshift(evt);\n"
        "  if (events.length > 500) events.pop();\n"
        "  stats.total++;\n"
        '  if (evt.source === "kapso") stats.kapso++;\n'
        '  if (evt.source === "funnel") stats.funnel++;\n'
        "  const payload = evt.payload || {};\n"
        '  const isErr = (evt.stage && evt.stage.includes("error")) || (evt.stage && evt.stage.includes("unauthorized")) || payload.success === false;\n'
        "  if (isErr) stats.err++; else stats.ok++;\n"
        "  updateStats();\n"
        "  renderEvents();\n"
        "}\n\n"
        "async function loadHistory() {\n"
        "  try {\n"
        '    const res = await fetch("/api/v1/db/debug/events?limit=200");\n'
        "    const data = await res.json();\n"
        "    (data.events || []).forEach(e => addEvent(e, false));\n"
        "  } catch (err) {\n"
        '    console.warn("No se pudo cargar historial:", err);\n'
        "  }\n"
        "}\n\n"
        "function subscribe() {\n"
        '  const channel = sb.channel("debug_events_realtime")\n'
        '    .on("postgres_changes",\n'
        '      { event: "INSERT", schema: "public", table: "debug_events" },\n'
        "      (payload) => { addEvent(payload.new, true); }\n"
        "    )\n"
        "    .subscribe((status) => {\n"
        '      if (status === "SUBSCRIBED") {\n'
        '        $dot.classList.add("connected");\n'
        '        $status.textContent = "Conectado (realtime)";\n'
        '      } else if (status === "CLOSED" || status === "CHANNEL_ERROR") {\n'
        '        $dot.classList.remove("connected");\n'
        '        $status.textContent = "Desconectado";\n'
        "      }\n"
        "    });\n"
        "}\n\n"
        "loadHistory().then(subscribe);\n"
        "</script>\n</body>\n</html>"
    )
