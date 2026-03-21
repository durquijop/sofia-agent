"""Benchmark: SDK vs httpx directo a Supabase REST API."""
import asyncio
import time
import httpx

SUPABASE_URL = "https://vecspltvmyopwbjzerow.supabase.co"
SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZlY3NwbHR2bXlvcHdianplcm93Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0NDA1ODk3OSwiZXhwIjoyMDU5NjM0OTc5fQ.ufyhBSe09pvA7232vdGAdRve5n-izUqXvHlCXjBHKu0"
HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
}


async def test_httpx_pooled():
    """httpx.AsyncClient con connection pooling."""
    async with httpx.AsyncClient(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers=HEADERS,
        timeout=15,
        http2=True,
    ) as client:
        # Warm up
        await client.get("/wp_empresa_perfil?select=id&limit=1")

        queries = [
            ("Empresa 1", "/wp_empresa_perfil?select=*&id=eq.1"),
            ("Agente 5", "/wp_agentes?select=*&id=eq.5&archivado=eq.false"),
            ("Agentes emp1", "/wp_agentes?select=id,nombre_agente,rol,llm,mcp_url&empresa_id=eq.1&archivado=eq.false"),
            ("Contacto 1", "/wp_contactos?select=*&id=eq.1"),
            ("Empresa 1 (2nd)", "/wp_empresa_perfil?select=*&id=eq.1"),
            ("Agente 5 (2nd)", "/wp_agentes?select=*&id=eq.5&archivado=eq.false"),
        ]

        for name, url in queries:
            t = time.perf_counter()
            r = await client.get(url)
            ms = (time.perf_counter() - t) * 1000
            rows = len(r.json()) if isinstance(r.json(), list) else 1
            print(f"{name:25s} -> {ms:7.0f}ms  [{r.status_code}] ({rows} rows)")


async def test_sdk_async():
    """Supabase SDK async."""
    from supabase._async.client import create_client
    sb = await create_client(SUPABASE_URL, SERVICE_KEY)

    # Warm up
    await sb.table("wp_empresa_perfil").select("id").limit(1).execute()

    queries = [
        ("SDK Empresa 1", lambda: sb.table("wp_empresa_perfil").select("*").eq("id", 1).maybe_single().execute()),
        ("SDK Agente 5", lambda: sb.table("wp_agentes").select("*").eq("id", 5).eq("archivado", False).maybe_single().execute()),
        ("SDK Empresa 1 (2nd)", lambda: sb.table("wp_empresa_perfil").select("*").eq("id", 1).maybe_single().execute()),
    ]

    for name, query_fn in queries:
        t = time.perf_counter()
        res = await query_fn()
        ms = (time.perf_counter() - t) * 1000
        print(f"{name:25s} -> {ms:7.0f}ms")


async def main():
    print("=== httpx.AsyncClient (pooled, HTTP/2) ===")
    await test_httpx_pooled()
    print()
    print("=== Supabase SDK Async ===")
    await test_sdk_async()


asyncio.run(main())
