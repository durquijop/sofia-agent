"""Schemas Pydantic para los endpoints de agendamiento (scheduling)."""

from pydantic import BaseModel, Field
from typing import Any, Optional


# ── Requests ───────────────────────────────────────────────

class DisponibilidadRequest(BaseModel):
    person_id: int = Field(..., description="ID del contacto")
    enterprise_id: int = Field(..., description="ID de la empresa")
    time_zone_contacto: str = Field(default="America/Bogota", description="Timezone del contacto")


class CrearEventoRequest(BaseModel):
    start: str = Field(..., description="Fecha/hora ISO del evento (ej: 2026-03-25T14:00:00)")
    attendeeEmail: str = Field(..., description="Email del participante/contacto")
    summary: str = Field(..., description="Título del evento")
    description: Optional[str] = Field(default=None, description="Descripción del evento")
    person_id: int = Field(..., description="ID del contacto")
    enterprise_id: Optional[int] = Field(default=None, description="ID de la empresa")
    Virtual_presencial: str = Field(default="Virtual", alias="Virtual-presencial", description="Modalidad: Virtual o Presencial")
    time_zone_contacto: str = Field(default="America/Bogota")

    model_config = {"populate_by_name": True}


class ReagendarEventoRequest(BaseModel):
    event_id: str = Field(..., description="ID del evento de Nylas a reagendar")
    start: str = Field(..., description="Nueva fecha/hora ISO")
    attendeeEmail: Optional[str] = Field(default=None, description="Email del participante")
    summary: Optional[str] = Field(default=None, description="Nuevo título")
    description: Optional[str] = Field(default=None)
    person_id: Optional[int] = Field(default=None, description="ID del contacto")
    enterprise_id: Optional[int] = Field(default=None)
    Virtual_presencial: str = Field(default="Virtual", alias="Virtual-presencial")
    time_zone_contacto: str = Field(default="America/Bogota")
    Duracion_minutos: Optional[int] = Field(default=None, description="Duración personalizada en minutos")

    model_config = {"populate_by_name": True}


class EliminarEventoRequest(BaseModel):
    event_id: str = Field(..., description="ID del evento de Nylas a eliminar")
    person_id: Optional[int] = Field(default=None, description="ID del contacto")


# ── Responses ──────────────────────────────────────────────

class SlotDisponible(BaseModel):
    inicio: str
    fin: str
    hora: str
    startUnix: int
    endUnix: int
    asesores_disponibles: int = 1


class DisponibilidadDia(BaseModel):
    fecha: str
    fechaTexto: str
    total_horarios: int
    slots: list[SlotDisponible]
    porPeriodo: dict[str, list[SlotDisponible]]


class CitaActual(BaseModel):
    tiene_cita: bool
    texto: Optional[str] = None
    link: Optional[str] = None
    fecha: Optional[str] = None
    estado: str = "(Sin cita registrada)"


class AsesorFijoInfo(BaseModel):
    id: int
    nombre: str
    email: str
    mensaje: str


class DisponibilidadResponse(BaseModel):
    cita_actual: Optional[CitaActual] = None
    asesor_fijo: Optional[AsesorFijoInfo] = None
    person_id: int
    enterprise_id: int
    time_zone: str
    hora_actual: str
    total_asesores: int
    asesores_consultados: int
    tiempo_consulta_ms: int
    disponibilidad: list[DisponibilidadDia]
    hay_disponibilidad: bool
    error: Optional[str] = None


class CrearEventoResponse(BaseModel):
    success: bool = False
    event_id: Optional[str] = None
    person_id: Optional[int] = None
    asesor_id: Optional[int] = None
    asesor: Optional[str] = None
    asesor_email: Optional[str] = None
    asesor_citas_pendientes: Optional[int] = None
    asesores_disponibles: Optional[int] = None
    participante: Optional[str] = None
    inicio: Optional[str] = None
    duracion_minutos: Optional[int] = None
    modalidad: str = "Virtual"
    summary: Optional[str] = None
    meet_link: Optional[str] = None
    error: Optional[str] = None


class ReagendarEventoResponse(BaseModel):
    success: bool = False
    event_id: Optional[str] = None
    event_id_anterior: Optional[str] = None
    person_id: Optional[int] = None
    asesor_anterior: Optional[str] = None
    asesor_id: Optional[int] = None
    asesor: Optional[str] = None
    asesor_email: Optional[str] = None
    asesor_citas_pendientes: Optional[int] = None
    cambio_asesor: bool = False
    nuevo_inicio: Optional[str] = None
    duracion_minutos: Optional[int] = None
    modalidad: str = "Virtual"
    meet_link: Optional[str] = None
    mensaje: Optional[str] = None
    error: Optional[str] = None


class EliminarEventoResponse(BaseModel):
    success: bool = False
    event_id: Optional[str] = None
    person_id: Optional[int] = None
    asesor: Optional[str] = None
    asesor_email: Optional[str] = None
    eliminado_en_nylas: bool = False
    mensaje: Optional[str] = None
    error: Optional[str] = None
