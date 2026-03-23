"""Analyze agent memory for session 133678."""
import asyncio
from app.db.client import get_supabase


async def main():
    sb = await get_supabase()
    rows = await sb.query("agent_memory", filters={"session_id": "133678"}, order="id", order_desc=True)
    total = len(rows)
    total_chars = 0
    for r in rows:
        msg = r.get("message", {})
        total_chars += len(str(msg.get("content", "")))

    print(f"Total memory rows: {total}")
    print(f"Total content chars: {total_chars:,}")
    print(f"Approx tokens: ~{total_chars // 4:,}")
    print()

    print("Last 15 entries:")
    for r in rows[:15]:
        msg = r.get("message", {})
        role = msg.get("role", "?")
        content = str(msg.get("content", ""))[:120].replace("\n", " ")
        rid = r["id"]
        print(f"  id={rid} [{role}] {content}")

    print()
    print("... oldest 3:")
    for r in rows[-3:]:
        msg = r.get("message", {})
        role = msg.get("role", "?")
        content = str(msg.get("content", ""))[:120].replace("\n", " ")
        rid = r["id"]
        print(f"  id={rid} [{role}] {content}")

    # Check how many are loaded with memory_window=8
    window = rows[:16]  # 8 pairs * 2
    window_chars = sum(len(str(r.get("message", {}).get("content", ""))) for r in window)
    print(f"\nMemory window (last 16 rows): {window_chars:,} chars, ~{window_chars // 4:,} tokens")


if __name__ == "__main__":
    asyncio.run(main())
