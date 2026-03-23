#!/usr/bin/env python3
"""Regression tests for funnel context normalization and error diagnostics."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.agents.funnel import _build_funnel_system_prompt, _build_funnel_user_message, _load_funnel_context, run_funnel_agent
from app.schemas.funnel import FunnelAgentRequest
IMAGE_SNAPSHOT = {
    "contexto_embudo": {
        "success": True,
        "data": {
            "informacion_contacto": {
                "contacto_id": 333358,
                    "nombre": "",
                    "apellido": "",
                "nombre_completo": "",
                "metadata": {},
                "telefono": "573197956965",
                    "email": "",
                    "origen": "Whatsapp",
                "etapa_actual_orden": None,
            },
            "etapa_actual": None,
            "tiene_embudo": True,
            "total_etapas": 8,
            "todas_etapas": [
                {
                    "id": 218,
                    "nombre_etapa": "Primer contacto",
                    "orden_etapa": 1,
                    "descripcion": {
                        "que_es": "Primer contacto con el prospecto interesado en Ley de Insolvencia para identificar su identidad y etapa emocional (Duda, Urgencia, Dolor, Curiosidad) estableciendo empatía inicial.",
                        "senales": [{"id": "senal_1", "texto": "Es su primer mensaje saludando o preguntando por 'Insolvencia'"}],
                    },
                    "es_etapa_actual": False,
                },
                {
                    "id": 219,
                    "nombre_etapa": "Prefiltrado",
                    "orden_etapa": 2,
                    "descripcion": {
                        "que_es": "Filtro estricto de viabilidad legal y financiera para validar que la deuda sea propia y mayor o igual a 40 Millones antes de solicitar datos adicionales.",
                        "senales": [
                            {"id": "senal_1", "texto": "Prospecto indica un monto de deuda específico"},
                            {"id": "senal_2", "texto": "Confirma que las deudas están a su nombre"},
                        ],
                    },
                    "es_etapa_actual": False,
                },
            ],
        },
    },
    "etapas_embudo": {
        "success": True,
        "data": {
            "contacto": {
                "id": 333358,
                "nombre": "",
                "apellido": "",
                "nombre_completo": "",
                "telefono": "573197956965",
                "email": "",
                "origen": "Whatsapp",
                "etapa_actual_orden": None,
            },
            "empresa_id": 4,
            "tiene_embudo": True,
            "total_etapas": 8,
            "etapas": [
                {
                    "id": 218,
                    "nombre_etapa": "Primer contacto",
                    "orden_etapa": 1,
                    "descripcion": {
                        "que_es": "Primer contacto con el prospecto interesado en Ley de Insolvencia para identificar su identidad y etapa emocional (Duda, Urgencia, Dolor, Curiosidad) estableciendo empatía inicial.",
                        "senales": [{"id": "senal_1", "texto": "Es su primer mensaje saludando o preguntando por 'Insolvencia'"}],
                    },
                    "es_etapa_actual": False,
                },
                {
                    "id": 219,
                    "nombre_etapa": "Prefiltrado",
                    "orden_etapa": 2,
                    "descripcion": {
                        "que_es": "Filtro estricto de viabilidad legal y financiera para validar que la deuda sea propia y mayor o igual a 40 Millones antes de solicitar datos adicionales.",
                        "senales": [
                            {"id": "senal_1", "texto": "Prospecto indica un monto de deuda específico"},
                            {"id": "senal_2", "texto": "Confirma que las deudas están a su nombre"},
                        ],
                    },
                    "es_etapa_actual": False,
                },
                {
                    "id": 220,
                    "nombre_etapa": "Agendamiento",
                    "orden_etapa": 3,
                    "descripcion": {
                        "que_es": "Gestión de agenda para consulta legal, determinando la modalidad (Presencial en Barranquilla o Virtual) y asegurando un espacio de 30 minutos con el abogado. Asegurate de tener el numero de telefono, si en la informacion del contacto lo muestra no es necesario pedirlo",
                        "senales": [
                            {"id": "senal_1", "texto": "Prospecto confirma su ciudad de residencia"},
                            {"id": "senal_2", "texto": "Acepta la modalidad propuesta (Virtual o Presencial)"},
                            {"id": "senal_3", "texto": "Selecciona un bloque horario disponible"},
                        ],
                    },
                    "es_etapa_actual": False,
                },
            ],
        },
    },
    "conversacion_memoria": {
        "success": True,
        "data": {
            "id": 64368,
            "mensajes": [
                {
                    "timestamp": "2026-03-23T09:35:40-04:00",
                    "remitente": "usuario",
                    "mensaje": "Pendiente",
                    "tipo": "text",
                }
            ],
        },
    },
}


async def test_load_funnel_context_normalizes_local_snapshot() -> None:
    snapshot = {
        "contexto_embudo": {
            "success": True,
            "data": {
                "informacion_contacto": {
                    "contacto_id": 133678,
                    "nombre": "Agustin",
                    "apellido": "Peralta",
                    "nombre_completo": "Agustin Peralta",
                    "telefono": "573133043991",
                    "email": "apg@urpeailab.com",
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
    assert context.contacto.nombre == "Agustin"
    assert context.contacto.apellido == "Peralta"
    assert context.contacto.email == "apg@urpeailab.com"
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
                    "nombre": "Agustin",
                    "apellido": "Peralta",
                    "nombre_completo": "Agustin Peralta",
                    "telefono": "573133043991",
                    "email": "apg@urpeailab.com",
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
    assert '"nombre": "Agustin"' in prompt
    assert '"apellido": "Peralta"' in prompt
    assert '"email": "apg@urpeailab.com"' in prompt
    assert "// CONTEXTO TEMPORAL COMPLETO" in prompt
    assert "Output esperado:" in prompt
    assert "No le respondas al prospecto. Ese no es tu trabajo." in prompt


async def test_funnel_system_prompt_renders_image_snapshot_data() -> None:
    with patch("app.agents.funnel.db.load_contexto_completo_local", return_value=IMAGE_SNAPSHOT):
        context = await _load_funnel_context(contacto_id=333358, empresa_id=4, conversacion_id=64368)

    prompt = _build_funnel_system_prompt(
        context=context,
        etapas_payload=IMAGE_SNAPSHOT["etapas_embudo"]["data"]["etapas"],
        contexto_embudo_payload=IMAGE_SNAPSHOT["contexto_embudo"]["data"],
    )

    assert "# IDENTIDAD Y MISIÓN" in prompt
    assert "El contacto Contacto sin nombre se encuentra en la etapa Sin etapa asignada" in prompt
    assert '"id": 218' in prompt
    assert '"nombre_etapa": "Primer contacto"' in prompt
    assert '"telefono": "573197956965"' in prompt
    assert '"nombre": ""' in prompt
    assert '"apellido": ""' in prompt
    assert '"email": ""' in prompt
    assert '"etapa_actual_orden": null' in prompt
    assert "// CONTEXTO TEMPORAL COMPLETO" in prompt


async def test_load_funnel_context_accepts_textual_es_calificado() -> None:
    snapshot = {
        "contexto_embudo": {
            "success": True,
            "data": {
                "informacion_contacto": {
                    "contacto_id": 555001,
                    "nombre": "Laura",
                    "apellido": "Diaz",
                    "nombre_completo": "Laura Diaz",
                    "telefono": "573001112233",
                    "email": "laura@example.com",
                    "es_calificado": "evaluando",
                    "etapa_actual_orden": None,
                    "metadata": {},
                },
                "etapa_actual": None,
                "tiene_embudo": True,
            },
        },
        "etapas_embudo": {
            "success": True,
            "data": {
                "contacto": {
                    "id": 555001,
                    "nombre": "Laura",
                    "apellido": "Diaz",
                    "nombre_completo": "Laura Diaz",
                    "telefono": "573001112233",
                    "email": "laura@example.com",
                    "etapa_actual_orden": None,
                },
                "etapas": [],
            },
        },
        "conversacion_memoria": {
            "success": True,
            "data": {
                "id": 90001,
                "mensajes": [],
            },
        },
    }

    with patch("app.agents.funnel.db.load_contexto_completo_local", return_value=snapshot):
        context = await _load_funnel_context(contacto_id=555001, empresa_id=4, conversacion_id=90001)

    assert context.contacto.es_calificado == "evaluando"

    prompt = _build_funnel_system_prompt(
        context=context,
        etapas_payload=snapshot["etapas_embudo"]["data"]["etapas"],
        contexto_embudo_payload=snapshot["contexto_embudo"]["data"],
    )

    assert '"es_calificado": "evaluando"' in prompt


async def test_run_funnel_agent_keeps_full_system_prompt_in_trace() -> None:
    request = FunnelAgentRequest(contacto_id=333358, empresa_id=4, agente_id=7, conversacion_id=64368)

    class DummyLLM:
        def bind_tools(self, tools):
            return self

    class DummyCompiled:
        async def ainvoke(self, initial_state):
            return {
                "messages": list(initial_state["messages"]) + [AIMessage(content="ok")],
                "tools_used": [],
                "etapa_nueva": None,
                "metadata_actualizada": None,
                "tool_execution_ms": 0,
                "llm_elapsed_ms": 0,
                "llm_iterations": 1,
            }

    class DummyGraph:
        def compile(self):
            return DummyCompiled()

    with patch("app.agents.funnel.db.load_contexto_completo_local", return_value=IMAGE_SNAPSHOT), patch(
        "app.agents.funnel._create_llm", return_value=DummyLLM()
    ), patch("app.agents.funnel._build_graph", return_value=DummyGraph()):
        response = await run_funnel_agent(request)

    assert response.success is True
    assert response.agent_runs
    trace = response.agent_runs[0]
    assert "# IDENTIDAD Y MISIÓN" in trace.system_prompt
    assert "# CONTEXTO DEL EMBUDO" in trace.system_prompt
    assert "Output esperado:" in trace.system_prompt
    assert len(trace.system_prompt) > 200
    assert '"telefono": "573197956965"' in trace.system_prompt


async def test_funnel_user_prompt_compacts_transcript_and_keeps_key_data() -> None:
    payload = {
        "id": 64368,
        "empresa_id": 4,
        "agente_id": 7,
        "contacto_id": 133678,
        "canal": "Kapso",
        "total_mensajes": 6,
        "mensajes_retornados": 6,
        "mensajes": [
            {"hora": "08:18:26", "remitente": "usuario", "mensaje": "Hola"},
            {"hora": "08:20:00", "remitente": "agente", "mensaje": "Hola, ¿me compartes tu nombre completo?"},
            {"hora": "08:26:23", "remitente": "usuario", "mensaje": "Agustin Peralta Guarin"},
            {"hora": "08:40:00", "remitente": "agente", "mensaje": "Perfecto, ahora compárteme tu correo."},
            {"hora": "08:46:47", "remitente": "usuario", "mensaje": "apg@urpeailab.com"},
            {"hora": "08:50:39", "remitente": "usuario", "mensaje": "Soy colombiano"},
        ],
    }

    prompt = _build_funnel_user_message(payload)

    assert '"conversacion_id": 64368' in prompt
    assert '- [08:20:00] agente: Hola, ¿me compartes tu nombre completo?' in prompt
    assert '- [08:26:23] usuario: Agustin Peralta Guarin' in prompt
    assert '- Agustin Peralta Guarin' in prompt
    assert '- apg@urpeailab.com' in prompt
    assert '"mensajes": [' not in prompt


async def main() -> int:
    await test_load_funnel_context_normalizes_local_snapshot()
    await test_run_funnel_agent_returns_diagnostic_trace_on_error()
    await test_funnel_system_prompt_includes_operational_sections()
    await test_funnel_system_prompt_renders_image_snapshot_data()
    await test_load_funnel_context_accepts_textual_es_calificado()
    await test_run_funnel_agent_keeps_full_system_prompt_in_trace()
    await test_funnel_user_prompt_compacts_transcript_and_keeps_key_data()
    print("OK - funnel regression tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))