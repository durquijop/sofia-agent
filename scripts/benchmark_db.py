"""Benchmark de velocidad de queries Supabase - sync vs async client."""
import asyncio
import httpx
import time

BASE = "http://localhost:8080"

QUERIES = [
    ("Health (cold)", "/api/v1/db/health"),
    ("Agente 5", "/api/v1/db/agente/5"),
    ("Empresa 1", "/api/v1/db/empresa/1"),
    ("Agentes emp 1", "/api/v1/db/empresa/1/agentes"),
    ("Health (warm)", "/api/v1/db/health"),
    ("Agente 5 (2nd)", "/api/v1/db/agente/5"),
    ("Empresa 1 (2nd)", "/api/v1/db/empresa/1"),
]


def bench_sync():
    """Cada request crea nueva conexión TCP."""
    print("=== Sync httpx (new conn each) ===")
    for name, url in QUERIES:
        t = time.perf_counter()
        r = httpx.get(f"{BASE}{url}", timeout=20)
        ms = (time.perf_counter() - t) * 1000
        print(f"  {name:25s} -> {ms:7.0f}ms  [{r.status_code}]")


async def bench_async_pooled():
    """Reutiliza conexión TCP con httpx.AsyncClient."""
    print("=== Async httpx (pooled conn) ===")
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as client:
        for name, url in QUERIES:
            t = time.perf_counter()
            r = await client.get(url)
            ms = (time.perf_counter() - t) * 1000
            print(f"  {name:25s} -> {ms:7.0f}ms  [{r.status_code}]")


async def bench_direct_supabase():
    """Directo a Supabase sin pasar por el servidor."""
    print("=== Directo a Supabase (httpx pooled) ===")
    url = "https://vecspltvmyopwbjzerow.supabase.co"
    key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZlY3NwbHR2bXlvcHdianplcm93Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0NDA1ODk3OSwiZXhwIjoyMDU5NjM0OTc5fQ.ufyhBSe09pvA7232vdGAdRve5n-izUqXvHlCXjBHKu0"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(base_url=f"{url}/rest/v1", headers=headers, timeout=15) as client:
        # warmup
        await client.get("/wp_empresa_perfil?select=id&limit=1")
        supabase_queries = [
            ("Empresa 1", "/wp_empresa_perfil?select=*&id=eq.1"),
            ("Agente 5", "/wp_agentes?select=*&id=eq.5&archivado=eq.false"),
            ("Empresa 1 (2nd)", "/wp_empresa_perfil?select=*&id=eq.1"),
        ]
        for name, q_url in supabase_queries:
            t = time.perf_counter()
            r = await client.get(q_url)
            ms = (time.perf_counter() - t) * 1000
            print(f"  {name:25s} -> {ms:7.0f}ms  [{r.status_code}]")


async def main():
    await bench_direct_supabase()
    print()
    await bench_async_pooled()
    print()
    bench_sync()


asyncio.run(main())
