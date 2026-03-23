#!/usr/bin/env python3
"""Regression tests for funnel context normalization and error diagnostics."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.agents.funnel import _build_funnel_system_prompt, _load_funnel_context, run_funnel_agent
from app.schemas.funnel import FunnelAgentRequest


async def test_load_funnel_context_normalizes_local_snapshot() -> None:
    snapshot = {
        "contexto_embudo": {
            "success": True,
            "data": {
                "informacion_contacto": {
                    "contacto_id": 133678,
                    "nombre_completo": "Agustin Peralta",
                    "telefono": "573133043991",
                    "etapa_actual_orden": 2,
                    "metadata": '{"etapa_actual": {"informacion_capturada": {"info_reg_1": "apg@urpeailab.com"}}}',
                },
                "etapa_actual": {
                    "id": 22,
                    "orden": 2,
                    "nombre": "Calificacion",
                    "que_es": "Lead calificado",
                    "senales": [{"id": "senal_1", "texto": "Comparte correo"}],
                },
                "tiene_embudo": True,
            },
        },
        "etapas_embudo": {
            "success": True,
            "data": {
                "etapas": [
                    {
                        "id": 22,
                        "nombre_etapa": "Calificacion",
                        "orden_etapa": 2,
                        "descripcion": '{"que_es": "Lead calificado", "senales": [{"id": "senal_1", "texto": "Comparte correo"}]}',
                        "es_etapa_actual": True,
                    }
                ]
            },
        },
        "conversacion_memoria": {
            "success": True,
            "data": {
                "id": 64368,
                "resumen": "Lead compartio correo",
                "mensajes": [
                    {
                        "timestamp": "2026-03-23T08:44:00+00:00",
                        "remitente": "usuario",
                        "mensaje": "apg@urpeailab.com",
                        "tipo": "text",
                    }
                ],
            },
        },
    }

    with patch("app.agents.funnel.db.load_contexto_completo_local", return_value=snapshot):
        context = await _load_funnel_context(contacto_id=133678, empresa_id=1, conversacion_id=64368)

    assert context.contacto.contacto_id == 133678
    assert context.contacto.metadata["etapa_actual"]["informacion_capturada"]["info_reg_1"] == "apg@urpeailab.com"
    assert context.ultimos_mensajes[0]["contenido"] == "apg@urpeailab.com"
    assert context.etapa_actual is not None
    assert context.etapa_actual.nombre == "Calificacion"
    assert context.etapa_actual.senales == ["Comparte correo"]


async def test_run_funnel_agent_returns_diagnostic_trace_on_error() -> None:
    request = FunnelAgentRequest(contacto_id=1, empresa_id=1, agente_id=1, conversacion_id=10)

    with patch("app.agents.funnel._load_funnel_context", side_effect=RuntimeError("context load failed")):
        response = await run_funnel_agent(request)

    assert response.success is False
    assert response.error == "context load failed"
    assert response.agent_runs
    assert response.agent_runs[0].agent_kind == "analysis_error"
    assert response.tools_used
    assert response.tools_used[0].tool_name == "funnel_error"


async def test_funnel_system_prompt_includes_operational_sections() -> None:
    snapshot = {
        "contexto_embudo": {
            "success": True,
            "data": {
                "informacion_contacto": {
                    "contacto_id": 133678,
                    "nombre_completo": "Agustin Peralta",
                    "telefono": "573133043991",
                    "etapa_actual_orden": 2,
                    "metadata": {"etapa_actual": {"informacion_capturada": {"info_reg_1": "apg@urpeailab.com"}}},
                },
                "etapa_actual": {
                    "id": 22,
                    "orden": 2,
                    "nombre": "Calificacion",
                    "que_es": "Lead calificado",
                    "senales": [{"id": "senal_1", "texto": "Comparte correo"}],
                },
                "tiene_embudo": True,
                "total_etapas": 1,
            },
        },
        "etapas_embudo": {
            "success": True,
            "data": {
                "contacto": {"nombre_completo": "Agustin Peralta"},
                "etapas": [
                    {
                        "id": 22,
                        "nombre_etapa": "Calificacion",
                        "orden_etapa": 2,
                        "descripcion": {"metadata": {"informacion_registrar": [{"id": "info_reg_1", "texto": "Correo"}]}}
                    }
                ],
            },
        },
        "conversacion_memoria": {"success": True, "data": {"id": 64368, "mensajes": []}},
    }

    with patch("app.agents.funnel.db.load_contexto_completo_local", return_value=snapshot):
        context = await _load_funnel_context(contacto_id=133678, empresa_id=1, conversacion_id=64368)

    prompt = _build_funnel_system_prompt(
        context=context,
        etapas_payload=snapshot["etapas_embudo"]["data"]["etapas"],
        contexto_embudo_payload=snapshot["contexto_embudo"]["data"],
    )

    assert "# CONTEXTO DEL EMBUDO" in prompt
    assert "// CONTEXTO TEMPORAL COMPLETO" in prompt
    assert "Output esperado:" in prompt
    assert "No le respondas al prospecto. Ese no es tu trabajo." in prompt


async def main() -> int:
    await test_load_funnel_context_normalizes_local_snapshot()
    await test_run_funnel_agent_returns_diagnostic_trace_on_error()
    await test_funnel_system_prompt_includes_operational_sections()
    print("OK - funnel regression tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))