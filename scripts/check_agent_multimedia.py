"""Check agent instructions for phone 14704047294 to verify multimedia/comandos config."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def main():
    from app.db.client import get_supabase
    from app.db import queries as db

    supabase = await get_supabase()

    # 1. Find the numero record for this phone
    print("=" * 80)
    print("1. Looking up phone 14704047294 in wp_numeros...")
    print("=" * 80)
    numero = await supabase.query(
        "wp_numeros",
        select="id, telefono, agente_id, empresa_id, canal",
        filters={"telefono": "14704047294"},
        single=True,
    )
    if not numero:
        print("  NOT FOUND! Trying with +1...")
        numero = await supabase.query(
            "wp_numeros",
            select="id, telefono, agente_id, empresa_id, canal",
            filters={"telefono": "+14704047294"},
            single=True,
        )
    
    if not numero:
        print("  NOT FOUND with either format.")
        return
    
    print(f"  Found: {numero}")
    agente_id = numero.get("agente_id")
    
    if not agente_id:
        print("  No agente_id assigned!")
        return

    # 2. Get agent config
    print(f"\n{'=' * 80}")
    print(f"2. Fetching agent {agente_id} from wp_agentes...")
    print("=" * 80)
    agent = await db.get_agente(agente_id)
    if not agent:
        print(f"  Agent {agente_id} not found!")
        return

    print(f"  nombre_agente: {agent.get('nombre_agente')}")
    print(f"  empresa_id: {agent.get('empresa_id')}")
    
    # 3. Check manejo_herramientas
    print(f"\n{'=' * 80}")
    print("3. manejo_herramientas:")
    print("=" * 80)
    mh = agent.get("manejo_herramientas") or ""
    if mh:
        print(mh[:3000])
        if len(mh) > 3000:
            print(f"\n... (truncated, total {len(mh)} chars)")
    else:
        print("  EMPTY - No manejo_herramientas configured!")

    # 4. Check instrucciones
    print(f"\n{'=' * 80}")
    print("4. instrucciones (searching for multimedia/audio/video/image/comando):")
    print("=" * 80)
    instrucciones = agent.get("instrucciones") or ""
    if instrucciones:
        # Search for multimedia-related keywords
        keywords = ["multimedia", "audio", "video", "image", "imagen", "comando", "comandos", "ogg", "mp3", "mp4", "supabase.co/storage"]
        found_any = False
        for kw in keywords:
            if kw.lower() in instrucciones.lower():
                # Find the context around the keyword
                idx = instrucciones.lower().find(kw.lower())
                start = max(0, idx - 200)
                end = min(len(instrucciones), idx + 500)
                print(f"\n  FOUND '{kw}' at position {idx}:")
                print(f"  ...{instrucciones[start:end]}...")
                found_any = True
        if not found_any:
            print("  No multimedia keywords found in instrucciones.")
            print(f"  (total {len(instrucciones)} chars)")
    else:
        print("  EMPTY!")

    # 5. Check instrucciones_multimedia
    print(f"\n{'=' * 80}")
    print("5. instrucciones_multimedia:")
    print("=" * 80)
    im = agent.get("instrucciones_multimedia") or ""
    if im:
        print(im[:2000])
    else:
        print("  EMPTY")

    # 6. Summary
    print(f"\n{'=' * 80}")
    print("SUMMARY")
    print("=" * 80)
    has_mh = bool(mh.strip())
    has_mm_in_instrucciones = any(kw.lower() in (instrucciones or "").lower() for kw in ["multimedia", "audio", "video", "comando", "ogg", "supabase.co/storage"])
    has_im = bool(im.strip())
    
    print(f"  manejo_herramientas present: {'YES' if has_mh else 'NO'}")
    print(f"  multimedia keywords in instrucciones: {'YES' if has_mm_in_instrucciones else 'NO'}")
    print(f"  instrucciones_multimedia present: {'YES' if has_im else 'NO'}")
    
    if has_mh or has_mm_in_instrucciones:
        print("\n  ✅ Agent HAS multimedia instructions - ejecutar_comando should work!")
    else:
        print("\n  ❌ Agent does NOT have multimedia instructions - ejecutar_comando won't know what to send")


asyncio.run(main())
