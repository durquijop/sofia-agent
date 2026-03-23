"""Quick test script for debug dashboard data loading."""
import asyncio
import json
import urllib.request
from app.api.debug_dashboard import _get_merged_events, _build_interactions


async def test_direct():
    """Test direct Python function calls."""
    print("=== Direct function test ===")
    events = await _get_merged_events(200)
    print(f"Merged events: {len(events)}")

    interactions = _build_interactions(events)
    print(f"Interactions: {len(interactions)}\n")

    for i, inter in enumerate(interactions):
        print(f"--- Interaction {i+1} ---")
        print(f"  contact_name:    {inter.get('contact_name')}")
        print(f"  from_phone:      {inter.get('from_phone')}")
        print(f"  status:          {inter.get('status')}")
        print(f"  agent_name:      {inter.get('agent_name')}")
        print(f"  model_used:      {inter.get('model_used')}")
        print(f"  duration_ms:     {inter.get('duration_ms')}")
        print(f"  message_text:    {str(inter.get('message_text', ''))[:80]}")
        print(f"  response_preview:{str(inter.get('response_preview', ''))[:80]}")
        print(f"  reply_type:      {inter.get('reply_type')}")
        print(f"  reaction_emoji:  {inter.get('reaction_emoji')}")
        timing = inter.get("timing", {})
        print(f"  timing:          total={timing.get('total_ms')} llm={timing.get('llm_ms')} tools={timing.get('tool_execution_ms')}")
        print(f"  tools_used:      {len(inter.get('tools_used', []))} tools")
        print(f"  agent_runs:      {len(inter.get('agent_runs', []))} runs")
        print(f"  funnel_etapa:    {inter.get('funnel_etapa_nueva')}")
        print()


def test_http(port=8765):
    """Test via HTTP endpoint (server must be running)."""
    print(f"\n=== HTTP test (localhost:{port}) ===")
    try:
        r = urllib.request.urlopen(f"http://localhost:{port}/debug/kapso/data")
        data = json.loads(r.read())
        print(f"interactions: {len(data.get('interactions', []))}")
        print(f"events:       {len(data.get('fastapi_events', []))}")
        for idx, inter in enumerate(data.get("interactions", [])):
            print(f"  [{idx+1}] {inter.get('contact_name','?')} | {inter.get('status','?')} | dur={inter.get('duration_ms')} | {str(inter.get('message_text',''))[:50]}")
        print("\nHTTP test PASSED")
    except Exception as e:
        print(f"HTTP test skipped (server not running?): {e}")


asyncio.run(test_direct())
test_http()
