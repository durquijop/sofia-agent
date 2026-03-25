"""Diagnóstico: por qué Eduardo recibió el agente incorrecto."""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.client import get_supabase


async def main():
    sb = await get_supabase()

    print("=" * 70)
    print("1. TODOS LOS NÚMEROS ACTIVOS EN wp_numeros")
    print("=" * 70)
    numeros = await sb.query(
        "wp_numeros",
        select="id,telefono,id_kapso,agente_id,empresa_id,activo,nombre",
        filters={"activo": True},
    )
    for n in (numeros or []):
        print(f"  id={n.get('id')} tel={n.get('telefono')} id_kapso={n.get('id_kapso')} "
              f"agente_id={n.get('agente_id')} empresa_id={n.get('empresa_id')} nombre={n.get('nombre')}")

    print("\n" + "=" * 70)
    print("2. BUSCAR NÚMERO POR TELÉFONO 14705500109 (Urpe)")
    print("=" * 70)
    urpe_num = await sb.query(
        "wp_numeros",
        filters={"telefono": "14705500109"},
    )
    print(f"  Resultado: {json.dumps(urpe_num, indent=2, default=str)}")

    # Also try the number from the image
    urpe_num2 = await sb.query(
        "wp_numeros",
        filters={"telefono": "14705550109"},
    )
    print(f"  Por 14705550109: {json.dumps(urpe_num2, indent=2, default=str)}")

    print("\n" + "=" * 70)
    print("3. AGENTES DISPONIBLES")
    print("=" * 70)
    agentes = await sb.query(
        "wp_agentes",
        select="id,nombre_agente,empresa_id,archivado",
        filters={"archivado": False},
    )
    for a in (agentes or []):
        print(f"  id={a.get('id')} nombre={a.get('nombre_agente')} empresa_id={a.get('empresa_id')}")

    print("\n" + "=" * 70)
    print("4. AGENTE FALLBACK (id=4)")
    print("=" * 70)
    fallback = await sb.query(
        "wp_agentes",
        select="id,nombre_agente,empresa_id",
        filters={"id": 4},
        single=True,
    )
    print(f"  Fallback agent: {json.dumps(fallback, indent=2, default=str)}")

    print("\n" + "=" * 70)
    print("5. BUSCAR CONTACTO 'Eduardo' (por nombre)")
    print("=" * 70)
    # Search for Eduardo in contacts
    contactos = await sb.query(
        "wp_contactos",
        select="id,nombre,apellido,telefono,empresa_id,metadata",
        limit=50,
    )
    for c in (contactos or []):
        nombre = (c.get("nombre") or "").lower()
        if "eduardo" in nombre or "ed" in nombre:
            print(f"  id={c.get('id')} nombre={c.get('nombre')} {c.get('apellido')} "
                  f"tel={c.get('telefono')} empresa_id={c.get('empresa_id')}")
            # Get their conversations
            convs = await sb.query(
                "wp_conversaciones",
                select="id,agente_id,numero_id,empresa_id,canal,created_at",
                filters={"contacto_id": c.get("id")},
                order="created_at",
                order_desc=True,
                limit=5,
            )
            for cv in (convs or []):
                print(f"    Conv id={cv.get('id')} agente_id={cv.get('agente_id')} "
                      f"numero_id={cv.get('numero_id')} empresa={cv.get('empresa_id')} "
                      f"canal={cv.get('canal')} created={cv.get('created_at')}")

    print("\n" + "=" * 70)
    print("6. NÚMERO FALLBACK HARDCODED: 14704047294")
    print("=" * 70)
    fallback_num = await sb.query(
        "wp_numeros",
        filters={"telefono": "14704047294"},
    )
    print(f"  Resultado: {json.dumps(fallback_num, indent=2, default=str)}")

    print("\n" + "=" * 70)
    print("7. ÚLTIMOS MENSAJES DE HOY con remitente=agente (buscar respuesta de bienes raíces)")
    print("=" * 70)
    msgs = await sb.query(
        "wp_mensajes",
        select="id,conversacion_id,contenido,remitente,created_at,modelo_llm,metadata",
        filters={"remitente": "agente"},
        order="created_at",
        order_desc=True,
        limit=10,
    )
    for m in (msgs or []):
        contenido = (m.get("contenido") or "")[:120]
        print(f"  id={m.get('id')} conv={m.get('conversacion_id')} model={m.get('modelo_llm')} "
              f"created={m.get('created_at')}")
        print(f"    contenido: {contenido}")
        meta = m.get("metadata")
        if meta:
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    pass
            if isinstance(meta, dict):
                print(f"    agent_id={meta.get('agent_id')} source={meta.get('source')}")


if __name__ == "__main__":
    asyncio.run(main())
