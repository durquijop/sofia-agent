"""Dynamic graph schema endpoint — introspects agents, tools, and orchestration at runtime."""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Query

from app.agents.conversational import MAX_CONVERSATIONAL_LLM_ITERATIONS
from app.agents.contact_update import (
    ALLOWED_CONTACT_FIELDS,
    CONTACT_UPDATE_MODEL,
    MAX_CONTACT_UPDATE_ITERATIONS,
)
from app.db import queries as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


# ---------------------------------------------------------------------------
# Parse mcp_url into list of URLs (same logic as kapso_routes._build_mcp_servers)
# ---------------------------------------------------------------------------

def _parse_mcp_urls(raw) -> list[dict]:
    """Parse mcp_url field into list of {url, name} dicts."""
    if not raw:
        return []
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return []
        if value.startswith("["):
            try:
                parsed = json.loads(value)
                results = []
                for item in parsed:
                    if isinstance(item, dict) and item.get("url"):
                        results.append({"url": item["url"], "name": item.get("name", "")})
                    elif isinstance(item, str) and item.strip():
                        results.append({"url": item.strip(), "name": ""})
                return results
            except json.JSONDecodeError:
                pass
        if "," in value:
            return [{"url": u.strip(), "name": ""} for u in value.split(",") if u.strip()]
        return [{"url": value, "name": ""}]
    return []


# ---------------------------------------------------------------------------
# Graph introspection helpers
# ---------------------------------------------------------------------------

async def _build_graph_schema(empresa_id: int | None = None) -> dict:
    """Build the full graph schema by introspecting actual agent definitions.
    
    If empresa_id is provided, fetches agent data from Supabase to enrich
    the MCP and LLM details dynamically.
    """

    nodes: list[dict] = []
    edges: list[dict] = []

    # ── Fetch live agent data from Supabase if possible ──
    agent_data: dict | None = None
    mcp_urls: list[dict] = []
    llm_model = "grok-4.1-fast"
    manejo_herramientas = ""

    if empresa_id:
        try:
            agentes = await db.get_agentes_por_empresa(empresa_id)
            if agentes:
                # Use first agent (primary) — fetch full details
                agent_data = await db.get_agente(agentes[0]["id"])
                if agent_data:
                    llm_model = agent_data.get("llm") or "grok-4.1-fast"
                    mcp_urls = _parse_mcp_urls(agent_data.get("mcp_url"))
                    manejo_herramientas = (agent_data.get("manejo_herramientas") or "").strip()
        except Exception as e:
            logger.warning("Could not fetch agent data for graph: %s", e)

    # ── External services ──
    nodes.append({
        "id": "whatsapp", "label": "WhatsApp", "kind": "external",
        "desc": "WhatsApp via Kapso Bridge",
        "detail": "Bridge: kapso-bridge/server.mjs\nFunciones: envío texto, reacciones,\nbotones, listas, media\nTyping keepalive cada 20s",
    })

    openrouter_detail = f"Base URL: openrouter.ai/api/v1\nModelo activo: {llm_model}\nProvee inferencia LLM para los agentes"
    nodes.append({
        "id": "openrouter", "label": "OpenRouter", "kind": "external",
        "desc": "OpenRouter LLM API",
        "detail": openrouter_detail,
    })

    # MCP Servers — show actual URLs if available
    mcp_srv_detail = "Configurados por agente en BD\nDescubrimiento dinámico de herramientas\nConexión vía StreamableHTTPTransport"
    if mcp_urls:
        url_lines = "\n".join(f"· {m['url']}" for m in mcp_urls)
        mcp_srv_detail = f"URLs activas:\n{url_lines}\n\nConexión vía StreamableHTTPTransport"
    nodes.append({
        "id": "mcp_srv", "label": "MCP Servers", "kind": "external",
        "desc": "Servidores MCP externos",
        "detail": mcp_srv_detail,
    })

    # ── Database ──
    nodes.append({
        "id": "supabase", "label": "Supabase", "kind": "database",
        "desc": "Supabase (PostgreSQL + REST)",
        "detail": "Tablas principales:\n· wp_contactos (perfil + metadata)\n· agent_memory (memoria conversacional)\n· wp_conversaciones\n· wp_mensajes\n· wp_citas\n· wp_contactos_nota\n· wp_multimedia\n· debug_events (realtime)",
    })

    # ── Supabase Storage ──
    nodes.append({
        "id": "storage", "label": "Storage", "kind": "database",
        "desc": "Supabase Storage (bucket multimedia)",
        "detail": "Bucket: multimedia (público)\nAlmacena audio, imágenes y documentos\nsubidos desde WhatsApp\nURL pública para acceso directo",
    })

    # ── Vision Model ──
    nodes.append({
        "id": "vision", "label": "Vision", "kind": "external",
        "desc": "Gemini 2.5 Flash (Vision)",
        "detail": "Modelo: google/gemini-2.5-flash\nVía OpenRouter API\nDescribe imágenes enviadas por el usuario\nResultado sincrónico para el agente",
    })

    # ── Edge Functions ──
    nodes.append({
        "id": "edge_fn", "label": "Edge Functions", "kind": "external",
        "desc": "Supabase Edge Functions",
        "detail": "Funciones:\n· crear-multimedia-inicial-v1\n  → Registra en wp_multimedia\n· guardar-multimedia-v4\n  → Procesa y almacena contenido\nEjecución async (fire-and-forget)",
    })

    # ── Orchestrator ──
    nodes.append({
        "id": "orch", "label": "Orquestador", "kind": "orchestrator",
        "desc": "Kapso Inbound Handler",
        "detail": "POST /api/v1/kapso/inbound\nPhase 0: Multimedia (audio/img/doc)\nPhase 1: Funnel + Contact Update (paralelo)\nPhase 2: Enriquecimiento del prompt\nPhase 3: Agente Conversacional\nPhase 4: Merge de resultados",
    })
    edges.append({"from": "whatsapp", "to": "orch", "label": "mensaje entrante"})

    # Multimedia pipeline edges
    edges.append({"from": "orch", "to": "storage", "label": "upload media"})
    edges.append({"from": "orch", "to": "vision", "label": "describe imagen", "dash": True})
    edges.append({"from": "vision", "to": "openrouter", "label": "Gemini 2.5 Flash"})
    edges.append({"from": "orch", "to": "edge_fn", "label": "multimedia pipeline", "dash": True})
    edges.append({"from": "edge_fn", "to": "supabase", "label": "wp_multimedia"})

    # ── Conversational Agent — with live LLM model ──
    conv_detail = (
        f"Modelo: {llm_model}\nTemp: 0.7 · Max tokens: 1024\n"
        f"Iteraciones LLM: hasta {MAX_CONVERSATIONAL_LLM_ITERATIONS}\n"
        f"Memoria: agent_memory (8 turnos)\nRecibe prompt enriquecido del funnel"
    )
    nodes.append({
        "id": "conv", "label": "Conversacional", "kind": "agent",
        "desc": "Agente Conversacional",
        "detail": conv_detail,
    })
    edges.append({"from": "orch", "to": "conv", "label": "Phase 3 (prompt enriquecido)"})
    edges.append({"from": "conv", "to": "whatsapp", "label": "respuesta final"})
    edges.append({"from": "conv", "to": "openrouter", "label": "LLM"})
    edges.append({"from": "conv", "to": "supabase", "label": "agent_memory", "dash": True})

    # Conversational tools
    nodes.append({
        "id": "t_reaction", "label": "send_reaction", "kind": "tool",
        "desc": "Herramienta: send_reaction",
        "detail": "Envía emoji de reacción al mensaje\nParámetro: emoji (❤️ 🙏 😂 🎉 👍 🔥)\nActiva en mensajes emotivos",
    })
    edges.append({"from": "conv", "to": "t_reaction", "label": ""})

    # MCP Tools — with live instructions
    mcp_detail = "Descubrimiento vía JSON-RPC\nProtocolo: initialize → tools/list → tools/call\nTimeout discovery: 15s\nCache por servidor"
    if mcp_urls:
        mcp_detail += f"\n\nServidores: {len(mcp_urls)}"
        for m in mcp_urls:
            name_part = f" ({m['name']})" if m.get("name") else ""
            mcp_detail += f"\n· {m['url']}{name_part}"
    if manejo_herramientas:
        # Truncate to keep tooltip readable
        instrucciones_preview = manejo_herramientas[:500]
        if len(manejo_herramientas) > 500:
            instrucciones_preview += "…"
        mcp_detail += f"\n\n📋 Instrucciones:\n{instrucciones_preview}"
    nodes.append({
        "id": "t_mcp", "label": "MCP Tools", "kind": "tool",
        "desc": "Herramientas MCP (dinámicas)",
        "detail": mcp_detail,
    })
    edges.append({"from": "conv", "to": "t_mcp", "label": ""})
    edges.append({"from": "t_mcp", "to": "mcp_srv", "label": "JSON-RPC"})

    # Built-in conversational tools: guardar_nota + marcar_prospecto_calificado
    nodes.append({
        "id": "t_nota", "label": "guardar_nota", "kind": "tool",
        "desc": "Herramienta: guardar_nota",
        "detail": "Memoria persistente del contacto\nAPPEND-only en wp_contactos.notas\nGuarda acuerdos, contexto, resultados\nFormato: [FECHA] CATEGORÍA: detalles",
    })
    edges.append({"from": "conv", "to": "t_nota", "label": ""})
    edges.append({"from": "t_nota", "to": "supabase", "label": "PATCH notas"})

    nodes.append({
        "id": "t_calificado", "label": "marcar_calificado", "kind": "tool",
        "desc": "Herramienta: marcar_prospecto_calificado",
        "detail": "Marca si el prospecto es calificado\nActualiza wp_contactos.es_calificado\nValores: 'si' o 'no'\nAfecta seguimiento y remarketing",
    })
    edges.append({"from": "conv", "to": "t_calificado", "label": ""})
    edges.append({"from": "t_calificado", "to": "supabase", "label": "PATCH calificado"})

    # Built-in: ejecutar_comando (multimedia sending)
    nodes.append({
        "id": "t_comandos", "label": "ejecutar_comando", "kind": "tool",
        "desc": "Herramienta: ejecutar_comando",
        "detail": "Envía multimedia al contacto vía Kapso\nComandos: image, audio, video\nParámetros: comando, solicitud (URL), extra (caption)\nLee URLs de manejo_herramientas e instrucciones",
    })
    edges.append({"from": "conv", "to": "t_comandos", "label": ""})
    edges.append({"from": "t_comandos", "to": "whatsapp", "label": "multimedia"})

    # Built-in: desactivar_contacto_spam
    nodes.append({
        "id": "t_spam", "label": "desactivar_spam", "kind": "tool",
        "desc": "Herramienta: desactivar_contacto_spam",
        "detail": "Marca contacto como spam y lo desactiva\nLlama Edge Function: apagar-contacto-spam-v1\nActualiza wp_contactos vía Supabase\nEvita seguimiento y mensajes futuros",
    })
    edges.append({"from": "conv", "to": "t_spam", "label": ""})
    edges.append({"from": "t_spam", "to": "supabase", "label": "desactivar spam"})

    # ── Funnel Agent ──
    nodes.append({
        "id": "funnel", "label": "Embudo", "kind": "agent",
        "desc": "Agente de Embudo",
        "detail": f"Modelo: {llm_model}\nTemp: 0.5 · Max tokens: 512\nIteraciones LLM: hasta 2\nTimeout: 25s\nAnaliza etapa del contacto en el funnel",
    })
    edges.append({"from": "orch", "to": "funnel", "label": "Phase 1 (paralelo)"})
    edges.append({"from": "funnel", "to": "orch", "label": "resultado embudo", "dash": True})
    edges.append({"from": "funnel", "to": "openrouter", "label": "LLM"})

    nodes.append({
        "id": "t_metadata", "label": "update_metadata", "kind": "tool",
        "desc": "Herramienta: update_metadata",
        "detail": "Registra información capturada\nParámetros: informacion_capturada, seccion,\nid_etapa (opcional), razon_etapa\nEscribe en wp_contactos.metadata",
    })
    edges.append({"from": "funnel", "to": "t_metadata", "label": ""})
    edges.append({"from": "t_metadata", "to": "supabase", "label": "PATCH metadata"})

    # ── Contact Update Agent ──
    fields_str = ", ".join(sorted(ALLOWED_CONTACT_FIELDS))
    nodes.append({
        "id": "contact", "label": "Contacto", "kind": "agent",
        "desc": "Agente de Actualización de Contacto",
        "detail": f"Modelo: {CONTACT_UPDATE_MODEL}\nTemp: 0.2 · Max tokens: 512\nIteraciones LLM: hasta {MAX_CONTACT_UPDATE_ITERATIONS}\nTimeout: 20s\nCaptura nombre, email, teléfono, etc.",
    })
    edges.append({"from": "orch", "to": "contact", "label": "Phase 1 (paralelo)"})
    edges.append({"from": "contact", "to": "openrouter", "label": "LLM"})

    nodes.append({
        "id": "t_update", "label": "update_contact", "kind": "tool",
        "desc": "Herramienta: update_contact_info",
        "detail": f"Actualiza columnas de wp_contactos\nCampos: {fields_str}",
    })
    edges.append({"from": "contact", "to": "t_update", "label": ""})
    edges.append({"from": "t_update", "to": "supabase", "label": "PATCH contacto"})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# Layout: assign default positions + visual properties per kind
# ---------------------------------------------------------------------------

_KIND_STYLE = {
    "orchestrator": {"color": "#a78bfa", "glow": "rgba(167,139,250,.4)", "r": 38},
    "agent":        {"color": "#fb923c", "glow": "rgba(251,146,60,.35)", "r": 30},
    "tool":         {"color": "#34d399", "glow": "rgba(52,211,153,.3)",  "r": 20},
    "external":     {"color": "#60a5fa", "glow": "rgba(96,165,250,.35)", "r": 22},
    "database":     {"color": "#f472b6", "glow": "rgba(244,114,182,.35)","r": 28},
}

# Default positions (normalized 0-1), keyed by node id.
# New nodes without a position get auto-placed.
_DEFAULT_POSITIONS: dict[str, tuple[float, float]] = {
    "whatsapp":   (0.50, 0.10),
    "orch":       (0.50, 0.22),
    "openrouter": (0.10, 0.28),
    "supabase":   (0.90, 0.28),
    "storage":    (0.90, 0.50),
    "vision":     (0.10, 0.50),
    "edge_fn":    (0.90, 0.72),
    "funnel":     (0.24, 0.45),
    "conv":       (0.50, 0.52),
    "contact":    (0.76, 0.45),
    "t_reaction": (0.64, 0.67),
    "t_mcp":      (0.36, 0.67),
    "t_nota":     (0.50, 0.72),
    "t_calificado":(0.78, 0.60),
    "t_comandos":  (0.42, 0.82),
    "t_spam":      (0.58, 0.82),
    "t_metadata": (0.12, 0.60),
    "t_update":   (0.88, 0.40),
    "mcp_srv":    (0.22, 0.80),
}

_AUTO_Y = 0.85  # fallback y for new nodes without a known position
_auto_x_counter = 0.3


def _enrich_node(node: dict) -> dict:
    """Add visual properties (position, color, glow, radius) to a node."""
    nid = node["id"]
    kind = node["kind"]
    style = _KIND_STYLE.get(kind, _KIND_STYLE["tool"])

    if nid in _DEFAULT_POSITIONS:
        x, y = _DEFAULT_POSITIONS[nid]
    else:
        global _auto_x_counter
        x, y = _auto_x_counter, _AUTO_Y
        _auto_x_counter = (_auto_x_counter + 0.15) % 0.9
        if _auto_x_counter < 0.1:
            _auto_x_counter = 0.1

    return {
        **node,
        "x": x, "y": y,
        "r": style["r"],
        "color": style["color"],
        "glow": style["glow"],
    }


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@router.get("/schema")
async def get_graph_schema(empresa_id: Optional[int] = Query(None, description="ID de empresa para obtener datos reales del agente")):
    """Return the full graph schema for the constellation visualization."""
    schema = await _build_graph_schema(empresa_id=empresa_id)
    schema["nodes"] = [_enrich_node(n) for n in schema["nodes"]]
    # Ensure every edge has a dash field
    for e in schema["edges"]:
        e.setdefault("dash", False)
        e.setdefault("label", "")
    return schema
