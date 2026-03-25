"""Verificar: números con canal Kapso pero sin id_kapso configurado."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.client import get_supabase


async def main():
    sb = await get_supabase()

    numeros = await sb.query(
        "wp_numeros",
        select="id,telefono,id_kapso,agente_id,empresa_id,activo,nombre,canal",
        filters={"activo": True},
    )
    
    print("Números con canal Kapso o sin id_kapso que podrían necesitar configuración:\n")
    for n in (numeros or []):
        has_kapso = n.get("id_kapso") not in (None, "", "null")
        canal = (n.get("canal") or "").lower()
        nombre = n.get("nombre") or ""
        
        if "kapso" in canal.lower() and not has_kapso:
            print(f"  ⚠️  id={n.get('id')} tel={n.get('telefono')} nombre={nombre}")
            print(f"      canal={canal} id_kapso=FALTA agente_id={n.get('agente_id')} empresa_id={n.get('empresa_id')}")
        elif has_kapso:
            print(f"  ✅ id={n.get('id')} tel={n.get('telefono')} nombre={nombre}")
            print(f"      canal={canal} id_kapso={n.get('id_kapso')} agente_id={n.get('agente_id')}")


if __name__ == "__main__":
    asyncio.run(main())
