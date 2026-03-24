"""Test rápido: verifica que debug_events funciona en Supabase."""
import asyncio
from app.db.client import get_supabase


async def main():
    db = await get_supabase()

    # 1. Insert
    print("1. Insertando evento de prueba...")
    result = await db.insert("debug_events", {
        "source": "test",
        "stage": "connectivity_check",
        "payload": {"msg": "Test desde Python", "ok": True},
        "empresa_id": 1,
        "contacto_id": None,
        "message_id": None,
    })
    row_id = result.get("id")
    print(f"   OK -> id={row_id}, created_at={result.get('created_at')}")

    # 2. Query
    print("2. Consultando eventos...")
    rows = await db.query(
        "debug_events", select="*",
        order="created_at", order_desc=True, limit=5,
    )
    print(f"   {len(rows)} evento(s) encontrados")
    for r in rows:
        print(f"   - [{r['source']}] {r['stage']} @ {r['created_at']}")

    # 3. Cleanup
    print("3. Limpiando evento de prueba...")
    await db.delete("debug_events", {"source": "test"})
    print("   OK")

    print("\n✅ debug_events funciona correctamente.")


if __name__ == "__main__":
    asyncio.run(main())
