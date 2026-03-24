"""Rutas de agendamiento — réplica de sdk-vercel-test-master/src/http-server.js en Python/FastAPI.

Endpoints:
  POST /api/v1/scheduling/disponibilidad
  POST /api/v1/scheduling/crear-evento
  POST /api/v1/scheduling/reagendar-evento
  POST /api/v1/scheduling/eliminar-evento
"""

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from app.db.client import get_supabase
from app.nylas_client.client import NylasClient, get_nylas, get_nylas2
from app.schemas.scheduling import (
    CrearEventoRequest,
    CrearEventoResponse,
    DisponibilidadRequest,
    DisponibilidadResponse,
    EliminarEventoRequest,
    EliminarEventoResponse,
    ReagendarEventoRequest,
    ReagendarEventoResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/scheduling", tags=["scheduling"])

# ════════════════════════════════════════════════════════════
# Helpers Supabase
# ════════════════════════════════════════════════════════════


async def _get_nylas_for_asesor(asesor: dict[str, Any]) -> "NylasClient":
    """Retorna el NylasClient correcto según el campo nylas_key del asesor.

    Cada asesor tiene su propia aplicación Nylas (y note-taker).
    nylas_key=2 usa NYLAS_API_KEY_2, cualquier otro valor usa NYLAS_API_KEY.
    """
    if asesor.get("nylas_key") == 2:
        client = await get_nylas2()
        if client:
            return client
        logger.warning("NYLAS_API_KEY_2 no configurada, fallback a key principal para asesor %s", asesor.get("id"))
    return await get_nylas()


async def _get_asesores_by_empresa(empresa_id: int) -> list[dict[str, Any]]:
    """Obtiene asesores activos con calendario de una empresa."""
    db = await get_supabase()
    asesores = await db.query(
        "wp_team_humano",
        select="id,nombre,apellido,email,grant_id,timezone,duracion_cita_minutos,disponibilidad,nylas_key",
        filters={"empresa_id": empresa_id, "is_active": True, "acepta_citas": True},
    )
    if not asesores or not isinstance(asesores, list):
        return []
    return [a for a in asesores if a.get("grant_id") and a["grant_id"] != "Solicitud enviada"]


async def _get_asesor_by_id(asesor_id: int) -> dict[str, Any] | None:
    db = await get_supabase()
    return await db.query(
        "wp_team_humano",
        select="id,nombre,apellido,email,grant_id,timezone,duracion_cita_minutos,disponibilidad,nylas_key",
        filters={"id": asesor_id, "is_active": True, "acepta_citas": True},
        single=True,
    )


async def _get_asesor_fijo_de_contacto(contacto_id: int) -> dict[str, Any] | None:
    """Si el contacto tiene cita 'Realizada', retorna su asesor fijo."""
    db = await get_supabase()
    # Buscar cita realizada más reciente
    cita = await db.query(
        "wp_citas",
        select="team_humano_id",
        filters={"contacto_id": contacto_id},
        order="fecha_hora",
        order_desc=True,
        limit=20,
    )
    # Filtrar por estado que contenga 'realizada' (case-insensitive via Python)
    cita_realizada = None
    if isinstance(cita, list):
        # Need to filter by estado containing "realizada" — PostgREST ilike not easily done
        # So we fetch citas and filter manually
        citas_all = await db.query(
            "wp_citas",
            select="team_humano_id,estado",
            filters={"contacto_id": contacto_id},
            order="fecha_hora",
            order_desc=True,
            limit=20,
        )
        if isinstance(citas_all, list):
            for c in citas_all:
                if c.get("estado") and "realizada" in c["estado"].lower():
                    cita_realizada = c
                    break

    if not cita_realizada or not cita_realizada.get("team_humano_id"):
        return None

    asesor = await _get_asesor_by_id(cita_realizada["team_humano_id"])
    if not asesor or not asesor.get("grant_id") or asesor["grant_id"] == "Solicitud enviada":
        return None

    logger.info("🔒 Contacto %s tiene asesor fijo: %s %s", contacto_id, asesor["nombre"], asesor.get("apellido", ""))
    return asesor


async def _get_conteo_citas_por_asesor(empresa_id: int) -> dict[int, int]:
    db = await get_supabase()
    citas = await db.query(
        "wp_citas",
        select="team_humano_id",
        filters={"empresa_id": empresa_id, "estado": "confirmada"},
    )
    conteo: dict[int, int] = {}
    if isinstance(citas, list):
        for c in citas:
            tid = c.get("team_humano_id")
            if tid:
                conteo[tid] = conteo.get(tid, 0) + 1
    return conteo


async def _get_cita_contacto(contacto_id: int) -> dict[str, Any]:
    db = await get_supabase()
    citas = await db.query(
        "wp_citas",
        select="id,fecha_hora,titulo,ubicacion,estado,team_humano_id,empresa_id,event_id",
        filters={"contacto_id": contacto_id},
        order="fecha_hora",
        order_desc=True,
        limit=5,
    )
    if not isinstance(citas, list) or not citas:
        return {"tiene_cita": False, "texto": None, "link": None, "fecha": None, "estado": "(Sin cita registrada)"}

    # Preferir confirmada, luego cualquiera no cancelada
    cita = next((c for c in citas if c.get("estado") == "confirmada"), None)
    if not cita:
        cita = next((c for c in citas if c.get("estado") != "cancelada"), None)
    if not cita:
        cita = citas[0]

    empresa_nombre = ""
    if cita.get("empresa_id"):
        emp = await db.query("wp_empresa_perfil", select="nombre", filters={"id": cita["empresa_id"]}, single=True)
        empresa_nombre = (emp or {}).get("nombre", "")

    asesor_nombre = ""
    if cita.get("team_humano_id"):
        ase = await db.query("wp_team_humano", select="nombre,apellido", filters={"id": cita["team_humano_id"]}, single=True)
        if ase:
            asesor_nombre = f"{ase['nombre']} {(ase.get('apellido') or '')[:1]}"

    ubicacion = cita.get("ubicacion") or ""
    es_virtual = any(x in ubicacion.lower() for x in ["meet.google.com", "zoom", "virtual"])
    modalidad = "Virtual" if es_virtual else "Presencial"

    return {
        "tiene_cita": True,
        "texto": f"🗓️ | {asesor_nombre} | {empresa_nombre} | {modalidad}",
        "link": ubicacion if es_virtual else None,
        "fecha": cita.get("fecha_hora"),
        "estado": cita.get("estado") or "confirmada",
    }


async def _guardar_cita_en_supabase(params: dict[str, Any]) -> int | None:
    db = await get_supabase()
    ahora = datetime.now(timezone.utc).isoformat()
    event_id = params["eventId"]

    # Verificar si ya existe
    existente = await db.query("wp_citas", select="id", filters={"event_id": event_id}, single=True)

    if existente:
        await db.update("wp_citas", {"id": existente["id"]}, {
            "team_humano_id": params["asesorId"],
            "fecha_hora": params["fechaHora"],
            "duracion": params["duracion"],
            "titulo": params["titulo"],
            "ubicacion": params.get("ubicacion"),
            "estado": params.get("estado", "confirmada"),
            "updated_at": ahora,
            "sincronizacion": "sincronizado",
        })
        logger.info("✅ wp_citas actualizado (id: %s)", existente["id"])
        return existente["id"]
    else:
        nueva = await db.insert("wp_citas", {
            "contacto_id": params["contactoId"],
            "empresa_id": params["empresaId"],
            "team_humano_id": params["asesorId"],
            "event_id": event_id,
            "fecha_hora": params["fechaHora"],
            "duracion": params["duracion"],
            "titulo": params["titulo"],
            "ubicacion": params.get("ubicacion"),
            "estado": params.get("estado", "confirmada"),
            "created_at": ahora,
            "updated_at": ahora,
            "sincronizacion": "sincronizado",
        })
        logger.info("✅ wp_citas insertado (id: %s)", nueva.get("id"))
        return nueva.get("id")


async def _actualizar_estado_cita(event_id: str, nuevo_estado: str) -> bool:
    db = await get_supabase()
    ahora = datetime.now(timezone.utc).isoformat()
    await db.update("wp_citas", {"event_id": event_id}, {"estado": nuevo_estado, "updated_at": ahora})
    logger.info("✅ Estado de cita actualizado a: %s", nuevo_estado)
    return True


async def _actualizar_asesor_en_contacto(contacto_id: int, asesor_id: int):
    db = await get_supabase()
    ahora = datetime.now(timezone.utc).isoformat()
    await db.update("wp_contactos", {"id": contacto_id}, {"team_humano_id": asesor_id, "updated_at": ahora})
    logger.info("✅ wp_contactos actualizado — asesor %s asignado a contacto %s", asesor_id, contacto_id)


# ════════════════════════════════════════════════════════════
# Lógica de slots disponibles
# ════════════════════════════════════════════════════════════

DIAS_SEMANA = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _ahora_en_tz(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def _periodo_dia(hora: int) -> str:
    if hora < 12:
        return "Mañana"
    if hora < 18:
        return "Tarde"
    return "Noche"


def _calcular_slots(
    fecha: datetime,
    busy_periods: list[dict[str, int]],
    disponibilidad: dict[str, Any] | None,
    duracion_min: int,
    tz_name: str,
) -> list[dict[str, Any]]:
    """Calcula slots disponibles para un día cruzando busy periods con horarios del asesor."""
    slots: list[dict[str, Any]] = []
    if not disponibilidad:
        return slots

    dia_idx = fecha.weekday()  # 0=lunes en Python
    dia_nombre = DIAS_SEMANA[dia_idx]

    horarios_normales = (disponibilidad.get("horarios_normales") or {}).get(dia_nombre, [])
    if not horarios_normales:
        return slots

    tz = ZoneInfo(tz_name)
    ahora_unix = int(time.time())
    fecha_str = fecha.strftime("%Y-%m-%d")

    for horario in horarios_normales:
        inicio_h, inicio_m = map(int, horario["inicio"].split(":"))
        fin_h, fin_m = map(int, horario["fin"].split(":"))

        current_h, current_m = inicio_h, inicio_m

        while current_h < fin_h or (current_h == fin_h and current_m < fin_m):
            # Construir datetime local y convertir a UTC
            local_dt = datetime(
                fecha.year, fecha.month, fecha.day,
                current_h, current_m, 0,
                tzinfo=tz,
            )
            slot_start_utc = local_dt.astimezone(timezone.utc)
            slot_end_utc = slot_start_utc + timedelta(minutes=duracion_min)

            start_unix = int(slot_start_utc.timestamp())
            end_unix = int(slot_end_utc.timestamp())

            # Verificar si está ocupado
            esta_ocupado = any(
                _rangos_solapan(start_unix, end_unix, bp["start"], bp["end"])
                for bp in busy_periods
            )

            if not esta_ocupado and start_unix > ahora_unix:
                hora_local = local_dt.strftime("%I:%M %p").lower().lstrip("0")
                slots.append({
                    "inicio": slot_start_utc.isoformat(),
                    "fin": slot_end_utc.isoformat(),
                    "hora": hora_local,
                    "startUnix": start_unix,
                    "endUnix": end_unix,
                })

            current_m += duracion_min
            while current_m >= 60:
                current_m -= 60
                current_h += 1

    return slots


def _rangos_solapan(start1: int, end1: int, start2: int, end2: int) -> bool:
    """Verifica si dos rangos de tiempo se solapan."""
    return start1 < end2 and end1 > start2


async def _asesor_ocupado(nylas, asesor: dict, start_unix: int, end_unix: int,
                         exclude_event_id: str | None = None) -> bool:
    """Verifica si un asesor está ocupado usando free/busy + list_events como doble check.

    Si exclude_event_id se proporciona (reagendar), se omite free/busy y se usa
    solo list_events filtrando ese evento, para evitar que la cita propia bloquee.
    """
    grant_id = asesor["grant_id"]
    email = asesor["email"]

    # 1) Free/Busy check (se omite al reagendar porque no puede excluir un evento específico)
    if not exclude_event_id:
        try:
            fb_data = await nylas.get_free_busy(grant_id, email, start_unix, end_unix)
            if isinstance(fb_data, list):
                for fb in fb_data:
                    for slot in fb.get("time_slots") or []:
                        if slot.get("status") == "busy":
                            bp_start = slot.get("start_time", 0)
                            bp_end = slot.get("end_time", 0)
                            if _rangos_solapan(start_unix, end_unix, bp_start, bp_end):
                                logger.info("🚫 Asesor %s ocupado (free/busy): %s-%s solapa con %s-%s",
                                            asesor["id"], start_unix, end_unix, bp_start, bp_end)
                                return True
        except Exception as e:
            logger.warning("Error free/busy asesor %s: %s — fallback a list_events", asesor["id"], e)

    # 2) List events (filtra exclude_event_id para reagendar)
    try:
        events = await nylas.list_events(grant_id, email, start_unix, end_unix)
        for ev in events:
            if exclude_event_id and ev.get("id") == exclude_event_id:
                continue
            when = ev.get("when") or {}
            ev_start = when.get("start_time") or when.get("start_date")
            ev_end = when.get("end_time") or when.get("end_date")
            if isinstance(ev_start, int) and isinstance(ev_end, int):
                if _rangos_solapan(start_unix, end_unix, ev_start, ev_end):
                    ev_status = ev.get("status", "confirmed")
                    if ev_status != "cancelled":
                        logger.info("🚫 Asesor %s ocupado (list_events): evento '%s' en %s-%s",
                                    asesor["id"], ev.get("title", "?"), ev_start, ev_end)
                        return True
    except Exception as e:
        logger.warning("Error list_events asesor %s: %s", asesor["id"], e)

    return False


# ════════════════════════════════════════════════════════════
# Selección inteligente de asesor
# ════════════════════════════════════════════════════════════


async def _seleccionar_mejor_asesor(
    empresa_id: int, fecha_hora_iso: str, tz_name: str, contacto_id: int | None = None,
    duracion_min: int | None = None, exclude_event_id: str | None = None,
) -> dict[str, Any] | None:
    """Selecciona el mejor asesor: disponible en horario + menos citas pendientes.
    Si el contacto tiene cita Realizada, usa siempre el mismo asesor."""

    # Parsear fecha
    dt = datetime.fromisoformat(fecha_hora_iso.replace("Z", "+00:00")) if "T" in fecha_hora_iso else datetime.fromisoformat(fecha_hora_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    start_unix = int(dt.timestamp())

    # Si hay contacto, verificar asesor fijo
    if contacto_id:
        asesor_fijo = await _get_asesor_fijo_de_contacto(contacto_id)
        if asesor_fijo:
            dur = duracion_min or asesor_fijo.get("duracion_cita_minutos") or 30
            end_unix = start_unix + (dur * 60)

            nylas_fijo = await _get_nylas_for_asesor(asesor_fijo)
            ocupado = await _asesor_ocupado(nylas_fijo, asesor_fijo, start_unix, end_unix, exclude_event_id)

            if ocupado:
                return {
                    "error": f"El asesor asignado ({asesor_fijo['nombre']} {asesor_fijo.get('apellido', '')}) no está disponible en ese horario."
                }

            return {"asesor": asesor_fijo, "citas_pendientes": 0, "total_disponibles": 1, "es_asesor_fijo": True}

    # Sin asesor fijo — lógica normal
    asesores = await _get_asesores_by_empresa(empresa_id)
    if not asesores:
        return None

    # Free/Busy + list_events en paralelo por asesor
    async def _check(asesor: dict) -> dict:
        dur = duracion_min or asesor.get("duracion_cita_minutos") or 30
        end_unix = start_unix + (dur * 60)
        try:
            nylas_asesor = await _get_nylas_for_asesor(asesor)
            ocupado = await _asesor_ocupado(nylas_asesor, asesor, start_unix, end_unix, exclude_event_id)
            return {"asesor": asesor, "ok": True, "ocupado": ocupado}
        except Exception:
            return {"asesor": asesor, "ok": False, "ocupado": True}

    results = await asyncio.gather(*[_check(a) for a in asesores])
    disponibles = [r["asesor"] for r in results if r["ok"] and not r["ocupado"]]

    if not disponibles:
        return None

    # Ordenar por menos citas pendientes
    conteo = await _get_conteo_citas_por_asesor(empresa_id)
    disponibles.sort(key=lambda a: conteo.get(a["id"], 0))

    return {
        "asesor": disponibles[0],
        "citas_pendientes": conteo.get(disponibles[0]["id"], 0),
        "total_disponibles": len(disponibles),
        "es_asesor_fijo": False,
    }


def _parse_iso_to_unix(iso_str: str) -> tuple[int, datetime]:
    """Parsea una cadena ISO a unix timestamp y datetime UTC."""
    parts = iso_str.split("T")
    y, m, d = map(int, parts[0].split("-"))
    time_parts = (parts[1] if len(parts) > 1 else "00:00:00").split(":")
    h = int(time_parts[0]) if len(time_parts) > 0 else 0
    mi = int(time_parts[1]) if len(time_parts) > 1 else 0
    s = int(float(time_parts[2])) if len(time_parts) > 2 else 0

    dt = datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)
    return int(dt.timestamp()), dt


# ════════════════════════════════════════════════════════════
# ENDPOINT 1: POST /disponibilidad
# ════════════════════════════════════════════════════════════


@router.post("/disponibilidad", response_model=DisponibilidadResponse)
async def disponibilidad_agenda(req: DisponibilidadRequest):
    """Consulta la disponibilidad de asesores para los próximos 7 días."""
    start_time = time.time()
    tz_name = req.time_zone_contacto or "America/Bogota"

    # Asesor fijo
    asesor_fijo = await _get_asesor_fijo_de_contacto(req.contacto_id)
    cita_info = await _get_cita_contacto(req.contacto_id)

    if asesor_fijo:
        asesores = [asesor_fijo]
    else:
        asesores = await _get_asesores_by_empresa(req.empresa_id)

    if not asesores:
        return DisponibilidadResponse(
            contacto_id=req.contacto_id,
            empresa_id=req.empresa_id,
            time_zone=tz_name,
            hora_actual=_ahora_en_tz(tz_name).isoformat(),
            total_asesores=0,
            asesores_consultados=0,
            tiempo_consulta_ms=int((time.time() - start_time) * 1000),
            disponibilidad=[],
            hay_disponibilidad=False,
            error="No se encontraron asesores con calendario configurado",
        )

    ahora = _ahora_en_tz(tz_name)
    ahora_unix = int(ahora.timestamp())
    fin_rango = ahora + timedelta(days=7)
    fin_unix = int(fin_rango.timestamp())

    # Free/Busy en paralelo (cada asesor usa su propia Nylas API key)
    async def _fb(asesor: dict):
        try:
            nylas_asesor = await _get_nylas_for_asesor(asesor)
            data = await nylas_asesor.get_free_busy(asesor["grant_id"], asesor["email"], ahora_unix, fin_unix)
            return {"asesor": asesor, "freeBusy": data, "ok": True}
        except Exception as e:
            logger.warning("Error free/busy asesor %s: %s", asesor["id"], e)
            return {"asesor": asesor, "freeBusy": None, "ok": False}

    resultados = await asyncio.gather(*[_fb(a) for a in asesores])
    asesores_ok = [r for r in resultados if r["ok"]]

    if not asesores_ok:
        return DisponibilidadResponse(
            contacto_id=req.contacto_id,
            empresa_id=req.empresa_id,
            time_zone=tz_name,
            hora_actual=ahora.isoformat(),
            total_asesores=len(asesores),
            asesores_consultados=0,
            tiempo_consulta_ms=int((time.time() - start_time) * 1000),
            disponibilidad=[],
            hay_disponibilidad=False,
            error="No se pudo obtener disponibilidad de ningún asesor",
        )

    # Procesar slots por día
    disponibilidad_por_dia: dict[str, dict] = {}

    for item in asesores_ok:
        asesor = item["asesor"]
        fb_data = item["freeBusy"]

        # Extraer busy periods
        busy_periods: list[dict[str, int]] = []
        if isinstance(fb_data, list):
            for fb in fb_data:
                for slot in fb.get("time_slots") or []:
                    if slot.get("status") == "busy":
                        busy_periods.append({"start": slot["start_time"], "end": slot["end_time"]})

        duracion = asesor.get("duracion_cita_minutos") or 30
        dispo = asesor.get("disponibilidad")
        if isinstance(dispo, str):
            import json
            try:
                dispo = json.loads(dispo)
            except Exception:
                dispo = None

        for i in range(7):
            fecha = ahora + timedelta(days=i)
            fecha_key = fecha.strftime("%Y-%m-%d")

            if fecha_key not in disponibilidad_por_dia:
                disponibilidad_por_dia[fecha_key] = {
                    "fecha": fecha_key,
                    "fecha_obj": fecha,
                    "horarios_unicos": {},
                }

            slots = _calcular_slots(fecha, busy_periods, dispo, duracion, tz_name)

            dia_data = disponibilidad_por_dia[fecha_key]
            for slot in slots:
                key = slot["hora"]
                if key not in dia_data["horarios_unicos"]:
                    dia_data["horarios_unicos"][key] = {
                        "inicio": slot["inicio"],
                        "fin": slot["fin"],
                        "hora": slot["hora"],
                        "startUnix": slot["startUnix"],
                        "endUnix": slot["endUnix"],
                        "asesores_disponibles": 0,
                    }
                dia_data["horarios_unicos"][key]["asesores_disponibles"] += 1

    # Formatear respuesta
    dias_resultado = []
    for fecha_key in sorted(disponibilidad_por_dia.keys()):
        dia_data = disponibilidad_por_dia[fecha_key]
        if not dia_data["horarios_unicos"]:
            continue

        fecha_obj = dia_data["fecha_obj"]
        fecha_texto = fecha_obj.strftime("%A %d de %B").lower()
        slots_unicos = sorted(dia_data["horarios_unicos"].values(), key=lambda s: s["startUnix"])

        por_periodo: dict[str, list] = {}
        for s in slots_unicos:
            # Determinar periodo basado en hora UTC → local
            try:
                dt_local = datetime.fromisoformat(s["inicio"]).astimezone(ZoneInfo(tz_name))
                periodo = _periodo_dia(dt_local.hour)
            except Exception:
                periodo = "Mañana"
            por_periodo.setdefault(periodo, []).append(s)

        dias_resultado.append({
            "fecha": fecha_key,
            "fechaTexto": fecha_texto,
            "total_horarios": len(slots_unicos),
            "slots": slots_unicos,
            "porPeriodo": por_periodo,
        })

    asesor_fijo_info = None
    if asesor_fijo:
        asesor_fijo_info = {
            "id": asesor_fijo["id"],
            "nombre": f"{asesor_fijo['nombre']} {asesor_fijo.get('apellido', '')}".strip(),
            "email": asesor_fijo["email"],
            "mensaje": "Este contacto tiene una cita Realizada. Solo se muestra disponibilidad de su asesor asignado.",
        }

    return DisponibilidadResponse(
        cita_actual=cita_info,
        asesor_fijo=asesor_fijo_info,
        contacto_id=req.contacto_id,
        empresa_id=req.empresa_id,
        time_zone=tz_name,
        hora_actual=ahora.isoformat(),
        total_asesores=len(asesores),
        asesores_consultados=len(asesores_ok),
        tiempo_consulta_ms=int((time.time() - start_time) * 1000),
        disponibilidad=dias_resultado,
        hay_disponibilidad=len(dias_resultado) > 0,
    )


# ════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════
# ENDPOINT 2: POST /crear-evento
# ════════════════════════════════════════════════════════════


@router.post("/crear-evento", response_model=CrearEventoResponse)
async def crear_evento_calendario(req: CrearEventoRequest):
    """Crea un evento/cita en el calendario del asesor."""
    tz_name = req.time_zone_contacto or "America/Bogota"
    db = await get_supabase()

    logger.info("📅 Crear evento — Contacto: %s, Empresa: %s, Horario: %s", req.contacto_id, req.empresa_id, req.start)

    # Obtener empresa_id si no viene
    empresa_id = req.empresa_id
    if not empresa_id:
        contacto = await db.query("wp_contactos", select="empresa_id", filters={"id": req.contacto_id}, single=True)
        empresa_id = (contacto or {}).get("empresa_id")
    if not empresa_id:
        return CrearEventoResponse(error="No se pudo determinar la empresa del contacto")

    # Seleccionar mejor asesor
    seleccion = await _seleccionar_mejor_asesor(empresa_id, req.start, tz_name, req.contacto_id)
    if not seleccion:
        return CrearEventoResponse(error="No hay asesores disponibles en ese horario")
    if seleccion.get("error"):
        return CrearEventoResponse(error=seleccion["error"])

    asesor = seleccion["asesor"]
    calendar_id = asesor["email"]

    # Parsear timestamps
    start_unix, fecha_inicio = _parse_iso_to_unix(req.start)
    duracion_min = asesor.get("duracion_cita_minutos") or 30
    end_unix = start_unix + (duracion_min * 60)

    es_virtual = (req.Virtual_presencial == "Virtual")
    nombre_contacto = (req.summary.split("|")[1].strip() if "|" in req.summary else "Invitado")

    # Hora local para descripción
    hora_local = fecha_inicio.astimezone(ZoneInfo(tz_name)).strftime("%I:%M %p")
    desc_final = req.description or ""
    if "Hora:" not in desc_final:
        desc_final += f"\n- Hora: {hora_local} hora Colombia ({tz_name})"

    event_data: dict[str, Any] = {
        "title": req.summary,
        "description": desc_final,
        "when": {
            "start_time": start_unix,
            "end_time": end_unix,
            "start_timezone": tz_name,
            "end_timezone": tz_name,
        },
        "participants": [
            {"name": nombre_contacto, "email": req.attendeeEmail, "status": "yes"},
            {"name": f"{asesor['nombre']} {asesor.get('apellido', '')}".strip(), "email": asesor["email"], "status": "yes"},
        ],
        "reminders": {
            "use_default": False,
            "overrides": [
                {"reminder_minutes": 1440, "reminder_method": "email"},
                {"reminder_minutes": 120, "reminder_method": "email"},
                {"reminder_minutes": 30, "reminder_method": "popup"},
                {"reminder_minutes": 10, "reminder_method": "popup"},
            ],
        },
    }

    if es_virtual:
        event_data["conferencing"] = {"provider": "Google Meet", "autocreate": {}}
    else:
        event_data["location"] = "Presencial"

    logger.info("📤 Creando evento en Nylas (asesor %s, nylas_key=%s)...", asesor["id"], asesor.get("nylas_key", 1))
    nylas = await _get_nylas_for_asesor(asesor)
    evento = await nylas.create_event(asesor["grant_id"], calendar_id, event_data)
    logger.info("✅ Evento creado: %s", evento.get("id"))

    meet_link = (evento.get("conferencing") or {}).get("details", {}).get("url")

    # Persistir en Supabase
    await _guardar_cita_en_supabase({
        "contactoId": req.contacto_id,
        "empresaId": empresa_id,
        "asesorId": asesor["id"],
        "eventId": evento["id"],
        "fechaHora": fecha_inicio.isoformat(),
        "duracion": duracion_min,
        "titulo": req.summary,
        "ubicacion": meet_link or ("Virtual" if es_virtual else "Presencial"),
        "estado": "confirmada",
    })
    await _actualizar_asesor_en_contacto(req.contacto_id, asesor["id"])

    inicio_local = fecha_inicio.astimezone(ZoneInfo(tz_name)).strftime("%d/%m/%Y %I:%M %p")

    return CrearEventoResponse(
        success=True,
        event_id=evento["id"],
        contacto_id=req.contacto_id,
        asesor_id=asesor["id"],
        asesor=f"{asesor['nombre']} {asesor.get('apellido', '')}".strip(),
        asesor_email=asesor["email"],
        asesor_citas_pendientes=seleccion["citas_pendientes"],
        asesores_disponibles=seleccion["total_disponibles"],
        participante=req.attendeeEmail,
        inicio=inicio_local,
        duracion_minutos=duracion_min,
        modalidad=req.Virtual_presencial,
        summary=req.summary,
        meet_link=meet_link,
    )


# ════════════════════════════════════════════════════════════
# ENDPOINT 3: POST /reagendar-evento
# ════════════════════════════════════════════════════════════


@router.post("/reagendar-evento", response_model=ReagendarEventoResponse)
async def reagendar_evento(req: ReagendarEventoRequest):
    """Reagenda un evento existente a nueva fecha/hora."""
    tz_name = req.time_zone_contacto or "America/Bogota"
    db = await get_supabase()

    logger.info("📅 Reagendar evento: %s → %s", req.event_id, req.start)

    # Buscar cita actual
    cita = await db.query("wp_citas", select="team_humano_id,empresa_id,contacto_id", filters={"event_id": req.event_id}, single=True)
    if not cita:
        return ReagendarEventoResponse(error="No se encontró la cita con ese event_id")

    empresa_id = req.empresa_id or cita.get("empresa_id")
    contacto_id = req.contacto_id or cita.get("contacto_id")

    # Asesor actual
    asesor_actual = await _get_asesor_by_id(cita["team_humano_id"]) if cita.get("team_humano_id") else None

    # Seleccionar mejor asesor para nuevo horario (excluir evento actual para no auto-bloquearse)
    seleccion = await _seleccionar_mejor_asesor(empresa_id, req.start, tz_name, contacto_id,
                                                 exclude_event_id=req.event_id)
    if not seleccion:
        return ReagendarEventoResponse(error="No hay asesores disponibles en ese horario")
    if seleccion.get("error"):
        return ReagendarEventoResponse(error=seleccion["error"])

    asesor_nuevo = seleccion["asesor"]
    cambio_asesor = not seleccion.get("es_asesor_fijo") and (asesor_actual is None or asesor_actual["id"] != asesor_nuevo["id"])

    # Parsear timestamps
    start_unix, fecha_inicio = _parse_iso_to_unix(req.start)
    duracion_min = req.Duracion_minutos or asesor_nuevo.get("duracion_cita_minutos") or 30
    end_unix = start_unix + (duracion_min * 60)

    nombre_contacto = (req.summary.split("|")[1].strip() if req.summary and "|" in req.summary else "Invitado")
    hora_local = fecha_inicio.astimezone(ZoneInfo(tz_name)).strftime("%I:%M %p")
    desc_final = req.description or ""
    if "Hora:" not in desc_final:
        desc_final += f"\n- Hora: {hora_local} hora Colombia ({tz_name})"

    calendar_id = asesor_nuevo["email"]
    modalidad = req.Virtual_presencial
    es_virtual = modalidad == "Virtual"

    if cambio_asesor:
        # Marcar cita anterior como reagendada
        await _actualizar_estado_cita(req.event_id, "reagendada")

        # Eliminar evento del calendario anterior (con la Nylas key del asesor anterior)
        if asesor_actual and asesor_actual.get("grant_id"):
            try:
                nylas_anterior = await _get_nylas_for_asesor(asesor_actual)
                await nylas_anterior.delete_event(asesor_actual["grant_id"], asesor_actual["email"], req.event_id)
            except Exception as e:
                logger.warning("No se pudo eliminar evento anterior: %s", e)

        # Crear nuevo evento
        event_data: dict[str, Any] = {
            "title": req.summary or "Cita reagendada",
            "description": desc_final,
            "when": {"start_time": start_unix, "end_time": end_unix, "start_timezone": tz_name, "end_timezone": tz_name},
            "participants": [
                {"name": nombre_contacto, "email": req.attendeeEmail or "", "status": "yes"},
                {"name": f"{asesor_nuevo['nombre']} {asesor_nuevo.get('apellido', '')}".strip(), "email": asesor_nuevo["email"], "status": "yes"},
            ],
            "reminders": {
                "use_default": False,
                "overrides": [
                    {"reminder_minutes": 1440, "reminder_method": "email"},
                    {"reminder_minutes": 120, "reminder_method": "email"},
                    {"reminder_minutes": 30, "reminder_method": "popup"},
                    {"reminder_minutes": 10, "reminder_method": "popup"},
                ],
            },
        }
        if es_virtual:
            event_data["conferencing"] = {"provider": "Google Meet", "autocreate": {}}
        else:
            event_data["location"] = "Presencial"

        nylas_nuevo = await _get_nylas_for_asesor(asesor_nuevo)
        evento = await nylas_nuevo.create_event(asesor_nuevo["grant_id"], calendar_id, event_data)
    else:
        # Mismo asesor — solo actualizar
        update_data: dict[str, Any] = {
            "when": {"start_time": start_unix, "end_time": end_unix, "start_timezone": tz_name, "end_timezone": tz_name},
            "reminders": {
                "use_default": False,
                "overrides": [
                    {"reminder_minutes": 1440, "reminder_method": "email"},
                    {"reminder_minutes": 120, "reminder_method": "email"},
                    {"reminder_minutes": 30, "reminder_method": "popup"},
                    {"reminder_minutes": 10, "reminder_method": "popup"},
                ],
            },
        }
        if req.summary:
            update_data["title"] = req.summary
        if desc_final:
            update_data["description"] = desc_final
        if req.attendeeEmail:
            update_data["participants"] = [
                {"name": nombre_contacto, "email": req.attendeeEmail, "status": "yes"},
                {"name": f"{asesor_nuevo['nombre']} {asesor_nuevo.get('apellido', '')}".strip(), "email": asesor_nuevo["email"], "status": "yes"},
            ]
        if es_virtual:
            update_data["conferencing"] = {"provider": "Google Meet", "autocreate": {}}
        elif modalidad == "Presencial":
            update_data["location"] = "Presencial"

        nylas_nuevo = await _get_nylas_for_asesor(asesor_nuevo)
        evento = await nylas_nuevo.update_event(asesor_nuevo["grant_id"], calendar_id, req.event_id, update_data)

    meet_link = (evento.get("conferencing") or {}).get("details", {}).get("url")

    # Persistir
    await _guardar_cita_en_supabase({
        "contactoId": contacto_id,
        "empresaId": empresa_id,
        "asesorId": asesor_nuevo["id"],
        "eventId": evento["id"],
        "fechaHora": fecha_inicio.isoformat(),
        "duracion": duracion_min,
        "titulo": req.summary or "Cita reagendada",
        "ubicacion": meet_link or ("Virtual" if es_virtual else "Presencial"),
        "estado": "confirmada",
    })

    if cambio_asesor and contacto_id:
        await _actualizar_asesor_en_contacto(contacto_id, asesor_nuevo["id"])

    inicio_local = fecha_inicio.astimezone(ZoneInfo(tz_name)).strftime("%d/%m/%Y %I:%M %p")

    return ReagendarEventoResponse(
        success=True,
        event_id=evento["id"],
        event_id_anterior=req.event_id if cambio_asesor else None,
        contacto_id=contacto_id,
        asesor_anterior=f"{asesor_actual['nombre']} {asesor_actual.get('apellido', '')}".strip() if cambio_asesor and asesor_actual else None,
        asesor_id=asesor_nuevo["id"],
        asesor=f"{asesor_nuevo['nombre']} {asesor_nuevo.get('apellido', '')}".strip(),
        asesor_email=asesor_nuevo["email"],
        asesor_citas_pendientes=seleccion["citas_pendientes"],
        cambio_asesor=cambio_asesor,
        nuevo_inicio=inicio_local,
        duracion_minutos=duracion_min,
        modalidad=modalidad,
        meet_link=meet_link,
        mensaje=(
            f"Evento reagendado con nuevo asesor: {asesor_nuevo['nombre']} {asesor_nuevo.get('apellido', '')}".strip()
            if cambio_asesor
            else "Evento reagendado correctamente"
        ),
    )


# ════════════════════════════════════════════════════════════
# ENDPOINT 4: POST /eliminar-evento
# ════════════════════════════════════════════════════════════


@router.post("/eliminar-evento", response_model=EliminarEventoResponse)
async def eliminar_evento(req: EliminarEventoRequest):
    """Cancela un evento — elimina de Nylas y marca como 'cancelada' en Supabase."""
    db = await get_supabase()

    logger.info("🗑️ Eliminar evento: %s", req.event_id)

    # Buscar cita
    cita = await db.query("wp_citas", select="team_humano_id,empresa_id,contacto_id", filters={"event_id": req.event_id}, single=True)
    if not cita:
        return EliminarEventoResponse(error="No se encontró la cita con ese event_id")

    # Obtener asesor
    asesor = await db.query(
        "wp_team_humano",
        select="id,nombre,apellido,email,grant_id,nylas_key",
        filters={"id": cita["team_humano_id"]},
        single=True,
    )
    if not asesor:
        return EliminarEventoResponse(error="No se encontró el asesor de la cita")
    if not asesor.get("grant_id") or asesor["grant_id"] == "Solicitud enviada":
        return EliminarEventoResponse(error="El asesor no tiene calendario configurado")

    calendar_id = asesor["email"]
    eliminado_en_nylas = False

    try:
        nylas = await _get_nylas_for_asesor(asesor)
        await nylas.delete_event(asesor["grant_id"], calendar_id, req.event_id)
        eliminado_en_nylas = True
        logger.info("✅ Evento eliminado de Nylas (nylas_key=%s)", asesor.get("nylas_key", 1))
    except Exception as e:
        logger.warning("⚠️ Error al eliminar en Nylas: %s — continuando con cancelación en Supabase", e)

    # Actualizar estado en Supabase
    await _actualizar_estado_cita(req.event_id, "cancelada")

    return EliminarEventoResponse(
        success=True,
        event_id=req.event_id,
        contacto_id=req.contacto_id or cita.get("contacto_id"),
        asesor=f"{asesor['nombre']} {asesor.get('apellido', '')}".strip(),
        asesor_email=asesor["email"],
        eliminado_en_nylas=eliminado_en_nylas,
        mensaje="Evento eliminado correctamente" if eliminado_en_nylas else "Cita cancelada en Supabase (el evento ya no existía en el calendario)",
    )
