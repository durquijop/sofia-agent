"""
FIX: Actualizar id_kapso para el número de Urpe Integral (id=5)
y corregir el contacto/conversación de Eduardo que se creó con empresa incorrecta.
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.client import get_supabase


async def main():
    sb = await get_supabase()

    # ─── FIX 1: Actualizar id_kapso en wp_numeros para Urpe ─────────────
    print("=" * 70)
    print("FIX 1: Actualizar wp_numeros id=5 → id_kapso = 888368674356992")
    print("=" * 70)

    # Verify current state
    before = await sb.query("wp_numeros", filters={"id": 5}, single=True)
    print(f"  ANTES: id_kapso={before.get('id_kapso')} agente_id={before.get('agente_id')} nombre={before.get('nombre')}")

    # Update
    result = await sb._http.patch(
        "/wp_numeros",
        params={"id": "eq.5"},
        json={"id_kapso": "888368674356992"},
    )
    if result.status_code < 300:
        print(f"  ✅ Actualizado exitosamente (status {result.status_code})")
    else:
        print(f"  ❌ Error: {result.status_code} {result.text}")
        return

    # Verify
    after = await sb.query("wp_numeros", filters={"id": 5}, single=True)
    print(f"  DESPUÉS: id_kapso={after.get('id_kapso')} agente_id={after.get('agente_id')} nombre={after.get('nombre')}")

    # ─── FIX 2: Corregir conversación 65838 de Eduardo ──────────────────
    print("\n" + "=" * 70)
    print("FIX 2: Corregir conversación 65838 → agente_id=7, numero_id=5, empresa_id=4")
    print("=" * 70)

    conv_before = await sb.query("wp_conversaciones", filters={"id": 65838}, single=True)
    print(f"  ANTES: agente_id={conv_before.get('agente_id')} numero_id={conv_before.get('numero_id')} empresa_id={conv_before.get('empresa_id')}")

    result2 = await sb._http.patch(
        "/wp_conversaciones",
        params={"id": "eq.65838"},
        json={"agente_id": 7, "numero_id": 5, "empresa_id": 4},
    )
    if result2.status_code < 300:
        print(f"  ✅ Conversación corregida (status {result2.status_code})")
    else:
        print(f"  ❌ Error: {result2.status_code} {result2.text}")

    # ─── FIX 3: Corregir contacto 339320 de Eduardo ─────────────────────
    print("\n" + "=" * 70)
    print("FIX 3: Corregir contacto 339320 → empresa_id=4 (Urpe)")
    print("=" * 70)

    contact_before = await sb.query("wp_contactos", filters={"id": 339320}, single=True)
    print(f"  ANTES: empresa_id={contact_before.get('empresa_id')} nombre={contact_before.get('nombre')} tel={contact_before.get('telefono')}")

    result3 = await sb._http.patch(
        "/wp_contactos",
        params={"id": "eq.339320"},
        json={"empresa_id": 4},
    )
    if result3.status_code < 300:
        print(f"  ✅ Contacto corregido (status {result3.status_code})")
    else:
        print(f"  ❌ Error: {result3.status_code} {result3.text}")

    # ─── Verificación final ─────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("VERIFICACIÓN FINAL")
    print("=" * 70)
    numero_final = await sb.query("wp_numeros", filters={"id": 5}, single=True)
    print(f"  Número Urpe: id_kapso={numero_final.get('id_kapso')} agente_id={numero_final.get('agente_id')} ✅")

    conv_final = await sb.query("wp_conversaciones", filters={"id": 65838}, single=True)
    print(f"  Conversación Eduardo: agente_id={conv_final.get('agente_id')} empresa_id={conv_final.get('empresa_id')} ✅")

    contact_final = await sb.query("wp_contactos", filters={"id": 339320}, single=True)
    print(f"  Contacto Eduardo: empresa_id={contact_final.get('empresa_id')} ✅")

    print("\n🎯 Ahora cuando Kapso envíe phone_number_id=888368674356992,")
    print("   se resolverá a wp_numeros id=5 → agente_id=7 (Monica URPE INTEGRAL)")


if __name__ == "__main__":
    asyncio.run(main())
