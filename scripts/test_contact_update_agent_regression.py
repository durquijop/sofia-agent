#!/usr/bin/env python3
"""Regression tests for the contact update agent."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.agents.contact_update import run_contact_update_agent
from app.schemas.contact_update import ContactUpdateAgentRequest


CONTEXT_SNAPSHOT = {
    "contexto_embudo": {
        "success": True,
        "data": {
            "informacion_contacto": {
                "contacto_id": 133678,
                "nombre": "Agustin",
                "apellido": "Peralta",
                "nombre_completo": "Agustin Peralta",
                "telefono": "573133043991",
                "email": None,
                "etapa_emocional": None,
                "timezone": None,
                "es_calificado": None,
                "estado": "prospecto",
            },
        },
    },
    "conversacion_memoria": {
        "success": True,
        "data": {
            "id": 64368,
            "mensajes": [
                {"hora": "08:20:00", "remitente": "agente", "mensaje": "¿Me compartes tu nombre y correo?"},
                {"hora": "08:26:23", "remitente": "usuario", "mensaje": "Soy Agustin Peralta"},
                {"hora": "08:46:47", "remitente": "usuario", "mensaje": "apg@urpeailab.com"},
                {"hora": "08:50:39", "remitente": "usuario", "mensaje": "Soy de Colombia"},
            ],
        },
    },
}


async def test_contact_update_agent_noop_when_no_new_data() -> None:
    request = ContactUpdateAgentRequest(contacto_id=133678, empresa_id=4, agente_id=7, conversacion_id=64368)

    class DummyLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="⚪ OK Sin acción - no hay datos nuevos")

    with patch("app.agents.contact_update.db.load_contexto_completo_local", return_value=CONTEXT_SNAPSHOT), patch(
        "app.agents.contact_update.db.get_citas_contacto_detalladas", return_value=[]
    ), patch("app.agents.contact_update._create_llm", return_value=DummyLLM()):
        response = await run_contact_update_agent(request)

    assert response.success is True
    assert response.updated_fields == []
    assert response.contact_updates is None
    assert response.respuesta.startswith("⚪ OK Sin acción")
    assert response.tools_used == []


async def test_contact_update_agent_updates_allowed_contact_fields() -> None:
    request = ContactUpdateAgentRequest(contacto_id=133678, empresa_id=4, agente_id=7, conversacion_id=64368)
    recorded_updates: list[tuple[int, dict]] = []

    class DummyLLM:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "update_contact_info",
                            "args": {
                                "nombre": "Agustin",
                                "apellido": "Peralta",
                                "email": "apg@urpeailab.com",
                                "timezone": "America/Bogota",
                                "estado": "prospecto",
                            },
                            "id": "tool-contact-update",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="✅ OK Guardado - email, timezone")

    async def fake_actualizar_campos_contacto(contacto_id: int, cambios: dict):
        recorded_updates.append((contacto_id, cambios))
        return {"id": contacto_id, **cambios}

    with patch("app.agents.contact_update.db.load_contexto_completo_local", return_value=CONTEXT_SNAPSHOT), patch(
        "app.agents.contact_update.db.get_citas_contacto_detalladas", return_value=[]
    ), patch("app.agents.contact_update._create_llm", return_value=DummyLLM()), patch(
        "app.agents.contact_update.db.actualizar_campos_contacto", side_effect=fake_actualizar_campos_contacto
    ):
        response = await run_contact_update_agent(request)

    assert response.success is True
    assert response.updated_fields == ["email", "timezone"]
    assert response.contact_updates == {
        "email": "apg@urpeailab.com",
        "timezone": "America/Bogota",
    }
    assert recorded_updates == [
        (
            133678,
            {
                "email": "apg@urpeailab.com",
                "timezone": "America/Bogota",
            },
        )
    ]
    assert {tool.tool_name for tool in response.tools_used} == {"update_contact_info"}
    assert all(tool.status == "ok" for tool in response.tools_used)


async def main() -> int:
    await test_contact_update_agent_noop_when_no_new_data()
    await test_contact_update_agent_updates_allowed_contact_fields()
    print("OK - contact update regression tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))