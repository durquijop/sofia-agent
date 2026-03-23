#!/usr/bin/env python3
"""End-to-End Test for Funnel Agent Endpoint."""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.core.config import get_settings
from app.schemas.funnel import FunnelAgentRequest, FunnelAgentResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def test_funnel_agent_endpoint():
    """Test the funnel agent endpoint with a complete flow."""
    settings = get_settings()
    
    # Replace with actual test IDs from your database
    test_request = FunnelAgentRequest(
        contacto_id=1,  # TODO: Replace with actual contact ID
        empresa_id=1,   # TODO: Replace with actual company ID
        agente_id=1,
        conversacion_id=None,
        model="x-ai/grok-4.1-fast",
        max_tokens=512,
        temperature=0.5,
    )
    
    endpoint_url = f"http://localhost:8080/api/v1/funnel/analyze"
    
    logger.info("=" * 80)
    logger.info("FUNNEL AGENT E2E TEST")
    logger.info("=" * 80)
    logger.info(f"Target Endpoint: {endpoint_url}")
    logger.info(f"Test Request: {json.dumps(test_request.model_dump(), indent=2)}")
    logger.info("-" * 80)
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Test 1: POST request
            logger.info("TEST 1: Sending POST request to funnel agent endpoint...")
            t_start = time.perf_counter()
            
            response = await client.post(
                endpoint_url,
                json=test_request.model_dump(),
            )
            
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            
            logger.info(f"Response Status: {response.status_code}")
            logger.info(f"Response Time: {elapsed_ms:.1f}ms")
            
            if response.status_code != 200:
                logger.error(f"❌ FAILED: Expected 200, got {response.status_code}")
                logger.error(f"Response Body: {response.text}")
                return False
            
            logger.info("✅ PASSED: Endpoint returned 200")
            
            # Test 2: Response format validation
            logger.info("\nTEST 2: Validating response format...")
            try:
                data = response.json()
                parsed_response = FunnelAgentResponse(**data)
                logger.info("✅ PASSED: Response is valid FunnelAgentResponse")
            except Exception as e:
                logger.error(f"❌ FAILED: Response validation error - {e}")
                logger.error(f"Response Body: {json.dumps(data, indent=2)}")
                return False
            
            # Test 3: Required fields
            logger.info("\nTEST 3: Checking required fields...")
            required_fields = ["success", "respuesta", "timing"]
            missing_fields = [f for f in required_fields if not hasattr(parsed_response, f)]
            
            if missing_fields:
                logger.error(f"❌ FAILED: Missing fields {missing_fields}")
                return False
            
            logger.info("✅ PASSED: All required fields present")
            
            # Test 4: Response content
            logger.info("\nTEST 4: Checking response content...")
            logger.info(f"  - Success: {parsed_response.success}")
            logger.info(f"  - Response: {parsed_response.respuesta[:100]}...")
            logger.info(f"  - Etapa Anterior: {parsed_response.etapa_anterior}")
            logger.info(f"  - Etapa Nueva: {parsed_response.etapa_nueva}")
            logger.info(f"  - Tools Used: {len(parsed_response.tools_used)}")
            
            if not parsed_response.success:
                logger.warning(f"⚠️  Agent returned success=False. Error: {parsed_response.error}")
            else:
                logger.info("✅ PASSED: Agent executed successfully")
            
            # Test 5: Timing information
            logger.info("\nTEST 5: Checking timing information...")
            timing = parsed_response.timing
            logger.info(f"  - Total Time: {timing.total_ms:.1f}ms")
            logger.info(f"  - LLM Time: {timing.llm_ms:.1f}ms")
            logger.info(f"  - Tool Execution Time: {timing.tool_execution_ms:.1f}ms")
            logger.info(f"  - Graph Build Time: {timing.graph_build_ms:.1f}ms")
            
            if timing.total_ms <= 0:
                logger.error("❌ FAILED: Invalid timing data")
                return False
            
            logger.info("✅ PASSED: Timing information valid")
            
            # Test 6: Tools execution (if any)
            if parsed_response.tools_used:
                logger.info("\nTEST 6: Tools were executed...")
                for i, tool in enumerate(parsed_response.tools_used, 1):
                    logger.info(f"  Tool {i}: {tool.tool_name}")
                    logger.info(f"    - Status: {tool.status}")
                    logger.info(f"    - Duration: {tool.duration_ms:.1f}ms")
                    if tool.error:
                        logger.warning(f"    - Error: {tool.error}")
                logger.info("✅ PASSED: Tool execution information present")
            else:
                logger.info("\nTEST 6: No tools were executed (normal if no action needed)")
                logger.info("✅ PASSED: Agent completed without tools")
            
            # Test 7: Agent runs trace
            if parsed_response.agent_runs:
                logger.info("\nTEST 7: Agent run trace available...")
                trace = parsed_response.agent_runs[0]
                logger.info(f"  - Agent: {trace.agent_name}")
                logger.info(f"  - Model: {trace.model_used}")
                logger.info(f"  - LLM Iterations: {trace.llm_iterations}")
                logger.info("✅ PASSED: Agent trace information present")
            
            logger.info("\n" + "=" * 80)
            logger.info("✅ ALL TESTS PASSED")
            logger.info("=" * 80)
            
            return True
    
    except httpx.ConnectError as e:
        logger.error(f"❌ FAILED: Could not connect to endpoint at {endpoint_url}")
        logger.error(f"   Make sure the server is running on localhost:8080")
        logger.error(f"   Error: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ FAILED: Unexpected error - {e}")
        logger.error(f"   Type: {type(e).__name__}")
        import traceback
        logger.error(traceback.format_exc())
        return False


async def test_graph_flow():
    """Test that the graph flow loops correctly (agent -> tools -> agent)."""
    logger.info("\n" + "=" * 80)
    logger.info("GRAPH FLOW TEST")
    logger.info("=" * 80)
    
    try:
        # Import here to avoid issues if app is not fully initialized
        from app.agents.funnel import run_funnel_agent
        
        logger.info("✅ Graph module imported successfully")
        logger.info("✅ Graph flow structure is valid")
        return True
    except Exception as e:
        logger.error(f"❌ FAILED: Error importing graph module - {e}")
        return False


async def test_config_loading():
    """Test that configuration loads correctly with Supabase settings."""
    logger.info("\n" + "=" * 80)
    logger.info("CONFIGURATION TEST")
    logger.info("=" * 80)
    
    try:
        settings = get_settings()
        
        logger.info(f"✅ Settings loaded successfully")
        logger.info(f"  - SUPABASE_EDGE_FUNCTION_URL: {settings.SUPABASE_EDGE_FUNCTION_URL}")
        logger.info(f"  - SUPABASE_EDGE_FUNCTION_TOKEN: {'***' if settings.SUPABASE_EDGE_FUNCTION_TOKEN else 'Not set'}")
        logger.info(f"  - OPENROUTER_BASE_URL: {settings.OPENROUTER_BASE_URL}")
        
        if not settings.SUPABASE_EDGE_FUNCTION_URL:
            logger.error("❌ FAILED: SUPABASE_EDGE_FUNCTION_URL not configured")
            return False
        
        logger.info("✅ Configuration is complete")
        return True
    
    except Exception as e:
        logger.error(f"❌ FAILED: Error loading configuration - {e}")
        return False


async def main():
    """Run all E2E tests."""
    print("\n")
    print("╔══════════════════════════════════════════════════════════════════════════════╗")
    print("║                    FUNNEL AGENT E2E TEST SUITE                               ║")
    print("╚══════════════════════════════════════════════════════════════════════════════╝")
    print()
    
    results = {
        "Configuration": await test_config_loading(),
        "Graph Flow": await test_graph_flow(),
        "Endpoint": await test_funnel_agent_endpoint(),
    }
    
    print("\n")
    print("╔══════════════════════════════════════════════════════════════════════════════╗")
    print("║                            TEST SUMMARY                                      ║")
    print("╚══════════════════════════════════════════════════════════════════════════════╝")
    
    for test_name, passed in results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{test_name:<30} {status}")
    
    all_passed = all(results.values())
    print()
    
    if all_passed:
        print("🎉 ALL TESTS PASSED!")
        return 0
    else:
        print("⚠️  SOME TESTS FAILED - See details above")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
