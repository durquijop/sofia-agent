#!/usr/bin/env python3
"""
Script de prueba para el Agente de Embudo (Funnel Agent).
Ejecuta un análisis contra un contacto específico.

Uso:
    python test_funnel_agent.py --contacto-id 123 --empresa-id 5 --agente-id 10
"""
import asyncio
import json
import sys
from pathlib import Path

# Agregar el proyecto al path
sys.path.insert(0, str(Path(__file__).parent))

from app.agents.funnel import run_funnel_agent
from app.schemas.funnel import FunnelAgentRequest


async def main():
    """Prueba el agente de embudo."""
    
    # Solicitud de prueba
    request = FunnelAgentRequest(
        contacto_id=328159,  # Cambiar según necesidad
        empresa_id=1,         # Cambiar según necesidad
        agente_id=1,          # Cambiar según necesidad
        conversacion_id=None, # Opcional
        model="x-ai/grok-4.1-fast",
        max_tokens=512,
        temperature=0.5,
    )
    
    print("=" * 80)
    print("🚀 EJECUTANDO AGENTE DE EMBUDO")
    print("=" * 80)
    print(f"\nParámetros:")
    print(f"  Contacto ID:    {request.contacto_id}")
    print(f"  Empresa ID:     {request.empresa_id}")
    print(f"  Agente ID:      {request.agente_id}")
    print(f"  Modelo:         {request.model}")
    print(f"  Max Tokens:     {request.max_tokens}")
    print(f"  Temperature:    {request.temperature}")
    print()
    
    try:
        print("⏳ Analizando contexto del embudo...")
        print("   (Cargando: contacto, etapas, conversación, mensajes en paralelo)")
        print()
        
        response = await run_funnel_agent(request)
        
        if response.success:
            print("✅ ÉXITO\n")
            print("📊 RESPUESTA DEL AGENTE:")
            print("-" * 80)
            print(response.respuesta)
            print("-" * 80)
            print()
            
            print("📈 CAMBIOS REALIZADOS:")
            if response.etapa_anterior:
                print(f"  Etapa Anterior:     {response.etapa_anterior}")
            if response.etapa_nueva:
                print(f"  ✏️ Etapa Nueva:      Orden #{response.etapa_nueva}")
            if response.metadata_actualizada:
                print(f"  📝 Metadata:")
                for key, val in response.metadata_actualizada.items():
                    print(f"     {key}: {val}")
            print()
            
            print("🔧 HERRAMIENTAS UTILIZADAS:")
            if response.tools_used:
                for tool in response.tools_used:
                    status_icon = "✅" if tool.status == "ok" else "❌"
                    print(f"  {status_icon} {tool.tool_name} ({tool.duration_ms}ms)")
                    if tool.error:
                        print(f"     Error: {tool.error}")
            else:
                print("  (Ninguna - solo análisis)")
            print()
            
            print("⏱️ TIMING:")
            print(f"  Total:          {response.timing.total_ms}ms")
            print(f"  LLM:            {response.timing.llm_ms}ms")
            print(f"  Herramientas:   {response.timing.tool_execution_ms}ms")
            print(f"  Grafo:          {response.timing.graph_build_ms}ms")
            print()
            
            print("🤖 DETALLES DEL AGENTE:")
            for run in response.agent_runs:
                print(f"  Agent Key:      {run.agent_key}")
                print(f"  Agent Name:     {run.agent_name}")
                print(f"  Iteraciones:    {run.llm_iterations}")
        else:
            print("❌ ERROR\n")
            print(f"Error: {response.error}")
            
    except Exception as e:
        print(f"❌ Excepción: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(f"Nota: Este script usa valores hardcoded.")
        print(f"Para cambiar parámetros, edita el archivo directamente.\n")
    
    asyncio.run(main())
