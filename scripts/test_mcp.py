"""Test MCP connectivity and tool discovery."""
import asyncio
import json
from app.db import queries as db
from app.mcp_client.client import MCPClient, mcp_tools_to_langchain


async def main():
    # 1. List all agents and their MCP URLs
    print("=" * 60)
    print("1. AGENTS FROM DATABASE")
    print("=" * 60)
    agents = await db.get_agentes_por_empresa(1)
    for a in agents:
        aid = a.get("id")
        name = a.get("nombre_agente")
        mcp = a.get("mcp_url")
        llm = a.get("llm")
        print(f"  Agent {aid}: {name}")
        print(f"    LLM: {llm}")
        print(f"    MCP URL: {mcp!r}")
        print()

    # 2. Get full agent details (agent 1 or first found)
    target_id = agents[0]["id"] if agents else 1
    agent = await db.get_agente(target_id)
    if agent:
        print("=" * 60)
        print(f"2. FULL AGENT DETAILS (ID={target_id})")
        print("=" * 60)
        for k in ["id", "nombre_agente", "mcp_url", "llm", "instrucciones_tools"]:
            val = agent.get(k)
            if k == "instrucciones_tools" and val:
                val = val[:500] + "..." if len(str(val)) > 500 else val
            print(f"  {k}: {val!r}")
        print()

        # 3. Test MCP connection
        mcp_url = agent.get("mcp_url")
        if mcp_url:
            print("=" * 60)
            print("3. TESTING MCP CONNECTION")
            print("=" * 60)
            # Parse URL (handle multiple formats)
            urls = []
            raw = mcp_url.strip()
            if raw.startswith("["):
                parsed = json.loads(raw)
                for item in parsed:
                    if isinstance(item, dict):
                        urls.append(item.get("url", ""))
                    elif isinstance(item, str):
                        urls.append(item)
            elif "," in raw:
                urls = [u.strip() for u in raw.split(",")]
            else:
                urls = [raw]

            for url in urls:
                if not url:
                    continue
                print(f"\n  Testing: {url}")
                try:
                    client = MCPClient(server_url=url)
                    tools = await asyncio.wait_for(client.discover_tools(), timeout=15)
                    print(f"  ✅ Connected! Found {len(tools)} tools:")
                    for t in tools:
                        name = t.get("name", "?")
                        desc = (t.get("description") or "")[:120]
                        params = list(t.get("inputSchema", {}).get("properties", {}).keys())
                        print(f"    - {name}: {desc}")
                        if params:
                            print(f"      params: {params}")
                    
                    # 4. Convert to LangChain tools
                    print(f"\n  Converting to LangChain tools...")
                    client2 = MCPClient(server_url=url)
                    lc_tools = await asyncio.wait_for(
                        mcp_tools_to_langchain(client2), timeout=15
                    )
                    print(f"  ✅ {len(lc_tools)} LangChain tools created:")
                    for lct in lc_tools:
                        print(f"    - {lct.name}: {lct.description[:100]}")
                except Exception as e:
                    print(f"  ❌ Error: {e}")
        else:
            print("  ⚠️ No MCP URL configured for this agent!")

    # 5. Check agent tools table
    print("\n" + "=" * 60)
    print("5. AGENT TOOLS FROM wp_agente_tools")
    print("=" * 60)
    tools_db = await db.get_agente_tools(target_id)
    if tools_db:
        for t in tools_db:
            print(f"  Tool: {t}")
    else:
        print("  (no tools in wp_agente_tools)")


if __name__ == "__main__":
    asyncio.run(main())
