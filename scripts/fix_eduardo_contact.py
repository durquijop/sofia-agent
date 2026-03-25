"""Fix contacto: vincular conversación 65838 al contacto correcto de Urpe."""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.client import get_supabase


async def main():
    sb = await get_supabase()

    # Buscar el contacto correcto de Eduardo bajo empresa Urpe (4)
    print("=" * 70)
    print("1. BUSCAR CONTACTO CORRECTO (tel=19132077232, empresa_id=4)")
    print("=" * 70)
    contactos = await sb.query(
        "wp_contactos",
        select="id,nombre,apellido,telefono,empresa_id,notas,metadata",
        filters={"telefono": "19132077232", "empresa_id": 4},
    )
    if contactos:
        correcto = contactos[0]
        print(f"  Contacto CORRECTO: id={correcto['id']} nombre={correcto.get('nombre')} empresa_id={correcto['empresa_id']}")
        
        # Actualizar conversación para apuntar al contacto correcto
        print(f"\n  Actualizando conversación 65838 → contacto_id={correcto['id']}")
        result = await sb._http.patch(
            "/wp_conversaciones",
            params={"id": "eq.65838"},
            json={"contacto_id": correcto['id']},
        )
        if result.status_code < 300:
            print(f"  ✅ Conversación vinculada al contacto correcto (status {result.status_code})")
        else:
            print(f"  ❌ Error: {result.status_code} {result.text}")

        # Actualizar los mensajes de esa conversación al contacto correcto
        print(f"\n  Actualizando mensajes de conv 65838...")
        msgs = await sb.query(
            "wp_mensajes",
            select="id",
            filters={"conversacion_id": 65838},
        )
        print(f"  {len(msgs or [])} mensajes encontrados")
    else:
        print("  ❌ No se encontró contacto bajo empresa 4")

    # También ver el contacto duplicado (339320 bajo empresa 109)
    print("\n" + "=" * 70)
    print("2. CONTACTO DUPLICADO (id=339320, empresa_id=109)")
    print("=" * 70)
    dup = await sb.query("wp_contactos", filters={"id": 339320}, single=True)
    print(f"  id={dup.get('id')} tel={dup.get('telefono')} empresa_id={dup.get('empresa_id')} nombre={dup.get('nombre')}")
    
    # Verificar si tiene otras conversaciones
    convs = await sb.query(
        "wp_conversaciones",
        select="id,agente_id,numero_id,empresa_id",
        filters={"contacto_id": 339320},
    )
    print(f"  Conversaciones del contacto duplicado: {len(convs or [])}")
    for c in (convs or []):
        print(f"    conv_id={c['id']} agente_id={c.get('agente_id')} empresa_id={c.get('empresa_id')}")

    # Verificar conversaciones previas de Eduardo (el correcto) 
    if contactos:
        correcto = contactos[0]
        print(f"\n" + "=" * 70)
        print(f"3. CONVERSACIONES DEL CONTACTO CORRECTO (id={correcto['id']})")
        print("=" * 70)
        convs_c = await sb.query(
            "wp_conversaciones",
            select="id,agente_id,numero_id,empresa_id,canal,created_at",
            filters={"contacto_id": correcto["id"]},
            order="created_at",
            order_desc=True,
            limit=10,
        )
        for c in (convs_c or []):
            print(f"  conv_id={c['id']} agente_id={c.get('agente_id')} numero_id={c.get('numero_id')} "
                  f"empresa={c.get('empresa_id')} created={c.get('created_at')}")


if __name__ == "__main__":
    asyncio.run(main())
