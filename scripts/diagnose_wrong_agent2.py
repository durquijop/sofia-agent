"""Buscar el phone_number_id de Kapso para Eduardo y el número Urpe."""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.client import get_supabase


async def main():
    sb = await get_supabase()

    # 1. Buscar mensajes recientes de la conversación 65838 (Eduardo - respuesta bienes raíces)
    print("=" * 70)
    print("1. MENSAJES DE CONVERSACIÓN 65838 (Eduardo)")
    print("=" * 70)
    msgs = await sb.query(
        "wp_mensajes",
        select="id,conversacion_id,contenido,remitente,created_at,metadata",
        filters={"conversacion_id": 65838},
        order="created_at",
        order_desc=True,
        limit=10,
    )
    for m in (msgs or []):
        contenido = (m.get("contenido") or "")[:100]
        meta = m.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except:
                pass
        print(f"  id={m.get('id')} rem={m.get('remitente')} created={m.get('created_at')}")
        print(f"    contenido: {contenido}")
        if isinstance(meta, dict):
            print(f"    metadata: phone_number_id={meta.get('phone_number_id')} "
                  f"agent_id={meta.get('agent_id')} kapso_conv={meta.get('kapso_conversation_id')} "
                  f"message_id={meta.get('message_id')}")

    # 2. Buscar la conversación 65838 para ver número y agente
    print("\n" + "=" * 70)
    print("2. CONVERSACIÓN 65838 DETALLE")
    print("=" * 70)
    conv = await sb.query(
        "wp_conversaciones",
        filters={"id": 65838},
        single=True,
    )
    print(f"  {json.dumps(conv, indent=2, default=str)}")

    # 3. Debug events recientes de Supabase
    print("\n" + "=" * 70)
    print("3. DEBUG EVENTS RECIENTES (fallback)")
    print("=" * 70)
    events = await sb.query(
        "debug_events",
        order="created_at",
        order_desc=True,
        limit=30,
    )
    for e in (events or []):
        event_type = e.get("event_type") or e.get("type") or ""
        payload = e.get("payload") or e.get("data") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except:
                pass
        # Show all events, especially fallback ones
        print(f"  {e.get('created_at')} source={e.get('source')} type={event_type}")
        if isinstance(payload, dict):
            pni = payload.get("phone_number_id")
            if pni:
                print(f"    phone_number_id={pni} agent_id={payload.get('agent_id')} "
                      f"resolved_agente_id={payload.get('resolved_agente_id')} "
                      f"message_id={payload.get('message_id')}")

    # 4. Buscar el contacto Eduardo por la conversación
    print("\n" + "=" * 70)
    print("4. CONTACTO DE CONVERSACIÓN 65838")
    print("=" * 70)
    if conv and conv.get("contacto_id"):
        contacto = await sb.query(
            "wp_contactos",
            select="id,nombre,apellido,telefono,empresa_id,metadata",
            filters={"id": conv["contacto_id"]},
            single=True,
        )
        print(f"  {json.dumps(contacto, indent=2, default=str)}")


if __name__ == "__main__":
    asyncio.run(main())
