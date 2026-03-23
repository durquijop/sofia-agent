import json
import logging
from typing import Any

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


class MCPClient:
    """Cliente para conectarse a MCP servers via Streamable HTTP y descubrir herramientas dinámicamente."""

    def __init__(self, server_url: str, server_name: str = ""):
        self.server_url = server_url.rstrip("/")
        self.server_name = server_name or self.server_url.split("/")[-1][:12]
        self._tools_cache: list[dict] | None = None
        self._session_id: str | None = None
        self._request_id: int = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send_jsonrpc(self, method: str, params: dict | None = None) -> dict:
        """Envía una request JSON-RPC al MCP server, manteniendo session ID."""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self.server_url, json=payload, headers=headers)
            response.raise_for_status()

            # Capture session ID from server
            new_session = response.headers.get("mcp-session-id")
            if new_session:
                self._session_id = new_session

            content_type = response.headers.get("content-type", "")

            if "text/event-stream" in content_type:
                return self._parse_sse_response(response.text)

            return response.json()

    def _parse_sse_response(self, sse_text: str) -> dict:
        """Parsea respuesta SSE del MCP server."""
        last_data = None
        for line in sse_text.strip().split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str:
                    try:
                        last_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

        if last_data:
            return last_data

        raise ValueError(f"No se pudo parsear respuesta SSE del MCP server: {sse_text[:200]}")

    async def initialize(self) -> dict:
        """Inicializa la conexión con el MCP server."""
        try:
            result = await self._send_jsonrpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "urpe-multiagent", "version": "1.0.0"},
            })
            logger.info(f"MCP server '{self.server_name}' inicializado: {result.get('result', {}).get('serverInfo', {})}")
            return result
        except Exception as e:
            logger.error(f"Error inicializando MCP server '{self.server_name}': {e}")
            raise

    async def discover_tools(self) -> list[dict]:
        """Descubre las herramientas disponibles en el MCP server."""
        if self._tools_cache is not None:
            return self._tools_cache

        try:
            await self.initialize()
            result = await self._send_jsonrpc("tools/list")
            tools = result.get("result", {}).get("tools", [])
            self._tools_cache = tools
            logger.info(f"MCP server '{self.server_name}': {len(tools)} herramientas descubiertas")
            return tools
        except Exception as e:
            logger.error(f"Error descubriendo herramientas en '{self.server_name}': {e}")
            return []

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Ejecuta una herramienta en el MCP server. Re-initializes session on 400."""
        for attempt in range(2):
            try:
                result = await self._send_jsonrpc("tools/call", {
                    "name": tool_name,
                    "arguments": arguments,
                })
                tool_result = result.get("result", {})
                content = tool_result.get("content", [])

                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        text_parts.append(item)

                return "\n".join(text_parts) if text_parts else json.dumps(tool_result)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400 and attempt == 0:
                    logger.warning(f"MCP session expired for '{tool_name}', re-initializing...")
                    self._session_id = None
                    await self.initialize()
                    continue
                raise
            except Exception as e:
                error_msg = f"Error ejecutando herramienta '{tool_name}': {e}"
                logger.error(error_msg)
                return error_msg
        return f"Error: tool '{tool_name}' failed after retry"


def _build_tool_schema(tool_def: dict) -> dict:
    """Construye el schema de parámetros desde la definición MCP."""
    input_schema = tool_def.get("inputSchema", {})
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])

    schema = {}
    for prop_name, prop_def in properties.items():
        prop_type = prop_def.get("type", "string")
        python_type = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }.get(prop_type, str)
        schema[prop_name] = (
            python_type,
            prop_def.get("description", f"Parámetro {prop_name}"),
            ... if prop_name in required else None,
        )
    return schema


async def mcp_tools_to_langchain(mcp_client: MCPClient) -> list[StructuredTool]:
    """Convierte herramientas MCP en herramientas LangChain compatibles con LangGraph."""
    mcp_tools_defs = await mcp_client.discover_tools()
    langchain_tools = []

    for tool_def in mcp_tools_defs:
        tool_name = tool_def.get("name", "unknown")
        tool_description = tool_def.get("description", f"Herramienta MCP: {tool_name}")
        input_schema = tool_def.get("inputSchema", {})

        captured_client = mcp_client
        captured_name = tool_name

        async def _invoke_tool(captured_c=captured_client, captured_n=captured_name, **kwargs) -> str:
            result = await captured_c.call_tool(captured_n, kwargs)
            return str(result)

        from pydantic import create_model, Field as PydanticField
        fields = {}
        properties = input_schema.get("properties", {})
        required_fields = input_schema.get("required", [])

        for prop_name, prop_def in properties.items():
            prop_type = prop_def.get("type", "string")
            python_type = {
                "string": str, "integer": int, "number": float,
                "boolean": bool, "array": list, "object": dict,
            }.get(prop_type, str)

            description = prop_def.get("description", f"Parameter {prop_name}")
            if prop_name in required_fields:
                fields[prop_name] = (python_type, PydanticField(description=description))
            else:
                fields[prop_name] = (python_type, PydanticField(default=None, description=description))

        if fields:
            ArgsModel = create_model(f"{tool_name}_args", **fields)
        else:
            ArgsModel = create_model(f"{tool_name}_args")

        tool = StructuredTool.from_function(
            coroutine=_invoke_tool,
            name=tool_name,
            description=tool_description,
            args_schema=ArgsModel,
        )
        langchain_tools.append(tool)

    return langchain_tools
