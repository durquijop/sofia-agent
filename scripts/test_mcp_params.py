"""Test MCP parameter sanitization fix."""
import asyncio
from app.mcp_client.client import MCPClient, mcp_tools_to_langchain, _sanitize_param_name

# Test sanitization
print("Test sanitization:")
print(f"  Virtual-presencial -> {_sanitize_param_name('Virtual-presencial')}")
print(f"  Duracion_minutos -> {_sanitize_param_name('Duracion_minutos')}")
print(f"  contacto_id -> {_sanitize_param_name('contacto_id')}")
print(f"  123bad -> {_sanitize_param_name('123bad')}")
print()


async def test():
    url = "https://marketia.app.n8n.cloud/mcp/aa0f6b46-ba2f-urpe-Monica"
    client = MCPClient(server_url=url)
    tools = await asyncio.wait_for(mcp_tools_to_langchain(client), timeout=20)
    print(f"Tools: {len(tools)}")
    for t in tools:
        name = getattr(t, "name", "?")
        schema = getattr(t, "args_schema", None)
        if schema and hasattr(schema, "model_fields"):
            fields = list(schema.model_fields.keys())
        else:
            fields = []
        print(f"  {name}: params={fields}")

    # Test Crear_Evento_Calendario
    crear = next((t for t in tools if t.name == "Crear_Evento_Calendario"), None)
    if crear:
        print()
        print("Crear_Evento_Calendario fields:")
        for fname, finfo in crear.args_schema.model_fields.items():
            print(f"  {fname}: required={finfo.is_required()}, desc={str(finfo.description)[:80]}")

        # Test that validation now accepts the sanitized name
        print()
        print("Testing validation with Virtual_presencial...")
        try:
            model = crear.args_schema(
                attendeeEmail="test@test.com",
                summary="Test",
                description="Test",
                Virtual_presencial="Virtual",
                contacto_id="123",
                time_zone_contacto="America/Bogota",
                start="2026-03-24T11:00:00",
            )
            print(f"  OK: {model.model_dump()}")
        except Exception as e:
            print(f"  FAIL: {e}")


asyncio.run(test())
