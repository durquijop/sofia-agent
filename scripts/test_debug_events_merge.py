"""Quick test: verify /debug/events merges Supabase data."""
import asyncio
from app.api.kapso_routes import kapso_debug_events


async def main():
    result = await kapso_debug_events(limit=50)
    events = result["events"]
    print(f"Total merged events: {len(events)}")
    if events:
        stages = {}
        for e in events:
            s = e.get("stage", "")
            stages[s] = stages.get(s, 0) + 1
        for s, c in sorted(stages.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print(f"First event: {events[0].get('timestamp')} - {events[0].get('stage')}")
        print(f"Last event:  {events[-1].get('timestamp')} - {events[-1].get('stage')}")
    else:
        print("ERROR: No events returned!")


if __name__ == "__main__":
    asyncio.run(main())
