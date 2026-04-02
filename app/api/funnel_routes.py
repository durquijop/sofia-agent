"""Rutas para el agente de embudo."""
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.agents.funnel import run_funnel_agent
from app.core.funnel_debug import get_funnel_debug_runs, get_funnel_debug_stats
from app.schemas.funnel import FunnelAgentRequest, FunnelAgentResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/funnel", tags=["funnel"])


def escape_html(text):
    """Escapa caracteres HTML especiales."""
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


def render_timing_table(timing):
    """Renderiza tabla de timing."""
    if not timing:
        return "<div style='color:#94a3b8'>Sin información de timing</div>"
    
    html = '<table style="border-collapse:collapse;width:100%">'
    html += '<tr style="border-bottom:1px solid #e2e8f0">'
    html += '<td style="padding:8px;text-align:left"><strong>Métrica</strong></td>'
    html += '<td style="padding:8px;text-align:right"><strong>ms</strong></td>'
    html += '</tr>'
    
    for key, value in sorted(timing.items()):
        if key.endswith('_ms'):
            label = key[:-3].replace('_', ' ').title()
            html += f'<tr style="border-bottom:1px solid #f1f5f9">'
            html += f'<td style="padding:8px">{escape_html(label)}</td>'
            html += f'<td style="padding:8px;text-align:right;font-family:monospace">{value:.1f}</td>'
            html += '</tr>'
    
    html += '</table>'
    return html


def render_tool_list(tools):
    """Renderiza lista de herramientas ejecutadas."""
    if not tools:
        return '<div style="color:#94a3b8">Sin herramientas ejecutadas</div>'
    
    html = '<ul style="margin:0;padding-left:20px">'
    for tool in tools:
        status_color = '#10b981' if tool.get('status') == 'ok' else '#ef4444'
        status = tool.get('status', 'unknown')
        name = escape_html(tool.get('tool_name', 'unknown'))
        duration = tool.get('duration_ms', 0)
        output = escape_html(str(tool.get('tool_output', ''))[:100])
        
        html += f'<li style="margin:8px 0"><strong style="color:{status_color}">{status.upper()}</strong> {name} ({duration:.0f}ms)<br/>'
        html += f'<small style="color:#64748b">{output}</small></li>'
    
    html += '</ul>'
    return html


def render_agent_runs(agent_runs):
    """Renderiza trazas de agentes."""
    if not agent_runs:
        return '<div style="color:#94a3b8">Sin trazas de agentes</div>'
    
    html = ''
    for i, run in enumerate(agent_runs):
        agent_name = escape_html(run.get('agent_name', f'Agente {i+1}'))
        agent_kind = escape_html(run.get('agent_kind', 'agent'))
        model = escape_html(run.get('model_used', '—'))
        
        html += f'<details style="margin:12px 0;border:1px solid #e2e8f0;border-radius:6px;padding:12px">'
        html += f'<summary style="cursor:pointer;font-weight:bold">{agent_name} · {agent_kind} · {model}</summary>'
        html += f'<div style="margin-top:12px;padding-top:12px;border-top:1px solid #f1f5f9">'
        
        html += f'<div style="margin-bottom:10px"><strong>Agent key:</strong> {escape_html(run.get("agent_key", "—"))}</div>'
        html += f'<div style="margin-bottom:10px"><strong>LLM iterations:</strong> {run.get("llm_iterations", 0)}</div>'
        
        html += f'<div style="margin:12px 0 6px"><strong>Timing</strong></div>'
        html += render_timing_table(run.get('timing', {}))
        
        html += f'<div style="margin:12px 0 6px"><strong>Herramientas ejecutadas</strong></div>'
        html += render_tool_list(run.get('tools_used', []))
        
        html += f'</div></details>'
    
    return html


def render_debug_html():
    """Renderiza el dashboard de debug del funnel agent."""
    runs = get_funnel_debug_runs(50)
    stats = get_funnel_debug_stats()
    
    html = '''<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Funnel Agent Debug Dashboard</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #f8fafc;
            color: #1e293b;
            margin: 0;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { margin: 0 0 20px; color: #0f172a; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .stat-card {
            background: white;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 16px;
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }
        .stat-value { font-size: 24px; font-weight: bold; color: #0f172a; }
        .stat-label { font-size: 12px; color: #64748b; text-transform: uppercase; margin-top: 4px; }
        .runs-list { background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; }
        .run-item {
            border-bottom: 1px solid #f1f5f9;
            padding: 16px 0;
        }
        .run-item:last-child { border-bottom: none; }
        .run-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .run-primary {
            display: flex;
            gap: 16px;
            align-items: center;
            flex-wrap: wrap;
        }
        .run-meta {
            font-size: 12px;
            color: #64748b;
        }
        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }
        .badge-success { background: #dcfce7; color: #166534; }
        .badge-error { background: #fee2e2; color: #991b1b; }
        .response-text {
            background: #f8fafc;
            border-left: 4px solid #0ea5e9;
            padding: 12px;
            margin: 8px 0;
            border-radius: 4px;
            font-size: 13px;
        }
        pre {
            background: #1e293b;
            color: #e2e8f0;
            padding: 12px;
            border-radius: 6px;
            overflow-x: auto;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Funnel Agent Debug Dashboard</h1>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">''' + str(stats['total_runs']) + '''</div>
                <div class="stat-label">Total de ejecuciones</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #16a34a;">''' + str(stats['successful']) + '''</div>
                <div class="stat-label">Exitosas</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color: #dc2626;">''' + str(stats['failed']) + '''</div>
                <div class="stat-label">Con error</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">''' + f"{stats['avg_duration_ms']:.0f}" + '''</div>
                <div class="stat-label">Duración promedio (ms)</div>
            </div>
        </div>
        
        <div class="runs-list">
            <h2 style="margin-top:0">Últimas ejecuciones</h2>
    '''
    
    if not runs:
        html += '<div style="color:#94a3b8;padding:20px;text-align:center">Sin ejecuciones registradas aún</div>'
    else:
        for run in runs:
            timestamp = escape_html(run.get('timestamp', ''))
            person_id = run.get('person_id', '?')
            enterprise_id = run.get('enterprise_id', '?')
            success = run.get('success', True)
            respuesta = escape_html(run.get('respuesta', '')[:150])
            etapa_anterior = escape_html(run.get('etapa_anterior', '—') or '—')
            etapa_nueva = run.get('etapa_nueva')
            tools_count = len(run.get('tools_used', []))
            timing = run.get('timing', {})
            total_ms = timing.get('total_ms', 0)
            
            badge_class = 'badge-success' if success else 'badge-error'
            badge_text = '✓ OK' if success else '✗ ERROR'
            
            html += f'''
            <div class="run-item">
                <div class="run-header">
                    <div class="run-primary">
                        <span class="badge {badge_class}">{badge_text}</span>
                        <span class="run-meta">Contacto #{person_id}</span>
                        <span class="run-meta">Empresa #{enterprise_id}</span>
                        <span class="run-meta">{total_ms:.0f}ms</span>
            '''
            
            if etapa_anterior or etapa_nueva:
                html += f'<span class="run-meta">{etapa_anterior} → {etapa_nueva or "—"}</span>'
            
            if tools_count > 0:
                html += f'<span class="run-meta">🔧 {tools_count} tool{"s" if tools_count != 1 else ""}</span>'
            
            html += f'''
                    </div>
                    <span class="run-meta">{timestamp}</span>
                </div>
                <div class="response-text"><strong>Respuesta:</strong><br/>{respuesta}</div>
            '''
            
            if run.get('agent_runs'):
                html += '<details style="margin-top:8px"><summary style="cursor:pointer">Ver trazas de agente</summary>'
                html += render_agent_runs(run.get('agent_runs', []))
                html += '</details>'
            
            html += '</div>'
    
    html += '''
        </div>
    </div>
</body>
</html>
    '''
    
    return html


@router.post("/analyze", response_model=FunnelAgentResponse)
async def analyze_funnel_status(request: FunnelAgentRequest):
    """
    Ejecuta el agente de embudo para analizar el estado de un contacto.
    
    Retorna:
    - Análisis del estado actual (máx 3 líneas)
    - Cambios de etapa realizados (si aplica)
    - Metadata actualizada (si aplica)
    - Métricas de ejecución
    """
    try:
        logger.info(f"Funnel analysis request - contacto: {request.person_id}, empresa: {request.enterprise_id}")
        response = await run_funnel_agent(request)
        logger.info(f"Funnel analysis completed - success: {response.success}, tools_used: {len(response.tools_used)}")
        return response
    except Exception as e:
        logger.error(f"Error en /funnel/analyze: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error procesando análisis de embudo: {str(e)}")


@router.get("/debug", response_class=HTMLResponse)
async def debug_dashboard():
    """
    Retorna el dashboard de debug del agente de embudo.
    Muestra las últimas 50 ejecuciones con trazas de agentes, timing y herramientas.
    """
    return render_debug_html()


@router.get("/debug/events")
async def debug_events(limit: int = 50):
    """
    Retorna los últimos eventos de debug del agente de embudo en formato JSON.
    
    Parámetros:
    - limit: Número máximo de eventos a retornar (máx 50)
    """
    return {"runs": get_funnel_debug_runs(limit), "stats": get_funnel_debug_stats()}
