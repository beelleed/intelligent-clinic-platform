"""MCP tools for anonymous clinic appointment availability and booking."""

from __future__ import annotations

import secrets
import re
from contextlib import closing
from datetime import UTC, date, datetime

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from .database import connect
from mcp_servers.clinic_time import clinic_timezone, clinic_timezone_name, to_clinic_iso


class AppointmentSlot(BaseModel):
    slot_id: int
    doctor_id: str
    doctor_name: str
    appointment_date: str
    start_time: str
    end_time: str
    timezone: str


class AppointmentAvailability(BaseModel):
    slots: list[AppointmentSlot]


class AppointmentReservation(BaseModel):
    booking_reference: str
    slot: AppointmentSlot
    created_at: str
    notice: str


class ManagedReservation(AppointmentReservation):
    status: str
    can_cancel: bool
    cancelled_at: str | None = None


class ReservationList(BaseModel):
    reservations: list[ManagedReservation]


mcp = MCPServer(
    "Clinic Appointments",
    instructions=(
        "Use these tools to find and reserve synthetic clinic appointment slots. "
        "Reservations use an anonymous booking reference and never collect patient data."
    ),
)


def _validated_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ToolError("appointment_date must use YYYY-MM-DD format.") from exc


def _slot_from_row(row) -> AppointmentSlot:
    return AppointmentSlot(
        slot_id=row["id"],
        doctor_id=row["doctor_id"],
        doctor_name=row["doctor_name"],
        appointment_date=row["appointment_date"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        timezone=clinic_timezone_name(),
    )


def _slot_is_future(row, now: datetime) -> bool:
    start = datetime.fromisoformat(
        f"{row['appointment_date']}T{row['start_time']}"
    ).replace(tzinfo=clinic_timezone())
    return start.astimezone(UTC) > now.astimezone(UTC)


@mcp.tool()
def list_available_appointments(
    doctor_id: str | None = None,
    appointment_date: str | None = None,
) -> AppointmentAvailability:
    """List unreserved future slots in clinic time, filtered by doctor or ISO date."""
    appointment_date = _validated_date(appointment_date)
    now = datetime.now(clinic_timezone())
    clauses = ["is_available = 1", "appointment_date >= ?"]
    parameters: list[str] = [now.date().isoformat()]
    if doctor_id:
        clauses.append("doctor_id = ?")
        parameters.append(doctor_id)
    if appointment_date:
        clauses.append("appointment_date = ?")
        parameters.append(appointment_date)

    query = f"""
        SELECT id, doctor_id, doctor_name, appointment_date, start_time, end_time
        FROM appointment_slots
        WHERE {' AND '.join(clauses)}
        ORDER BY appointment_date, start_time, doctor_name
    """
    with closing(connect()) as connection:
        if doctor_id:
            valid_ids = {
                row[0] for row in connection.execute(
                    "SELECT DISTINCT doctor_id FROM appointment_slots"
                )
            }
            if doctor_id not in valid_ids:
                raise ToolError(
                    f"Unknown doctor_id '{doctor_id}'. Valid doctor IDs: "
                    + ", ".join(sorted(valid_ids))
                    + ". Use list_doctors to resolve a doctor or specialty."
                )
        rows = connection.execute(query, parameters).fetchall()

    return AppointmentAvailability(slots=[
        _slot_from_row(row) for row in rows if _slot_is_future(row, now)
    ])


@mcp.tool()
def book_appointment(slot_id: int, owner_hash: str | None = None) -> AppointmentReservation:
    """Reserve one advertised slot and return an anonymous booking reference."""
    if slot_id < 1:
        raise ToolError("slot_id must be a positive integer.")
    if owner_hash is not None:
        _validate_owner(owner_hash)

    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        slot = connection.execute(
            """
            SELECT id, doctor_id, doctor_name, appointment_date,
                   start_time, end_time, is_available
            FROM appointment_slots
            WHERE id = ?
            """,
            (slot_id,),
        ).fetchone()
        if slot is None:
            raise ToolError(f"Unknown slot_id '{slot_id}'.")
        if not slot["is_available"]:
            raise ToolError(f"Appointment slot '{slot_id}' is no longer available.")
        if not _slot_is_future(slot, datetime.now(UTC)):
            raise ToolError(f"Appointment slot '{slot_id}' has already started or expired.")

        booking_reference = f"APT-{secrets.token_hex(4).upper()}"
        created_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        connection.execute(
            "INSERT INTO reservations (booking_reference, slot_id, created_at, owner_hash) "
            "VALUES (?, ?, ?, ?)",
            (booking_reference, slot_id, created_at, owner_hash),
        )
        connection.execute(
            "UPDATE appointment_slots SET is_available = 0 WHERE id = ?",
            (slot_id,),
        )
        connection.commit()

    return AppointmentReservation(
        booking_reference=booking_reference,
        slot=_slot_from_row(slot),
        created_at=to_clinic_iso(created_at),
        notice="Demo reservation only; no patient or insurance data was collected.",
    )


def _validate_owner(owner_hash: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", owner_hash):
        raise ToolError("Reservation access is invalid.")


def _managed_reservation(row) -> ManagedReservation:
    future = _slot_is_future(row, datetime.now(UTC))
    status = "cancelled" if row["cancelled_at"] else ("upcoming" if future else "past")
    return ManagedReservation(
        booking_reference=row["booking_reference"], slot=_slot_from_row(row),
        created_at=to_clinic_iso(row["created_at"]), cancelled_at=to_clinic_iso(row["cancelled_at"]),
        status=status, can_cancel=status == "upcoming",
        notice="Synthetic demo reservation. No patient information was collected.",
    )


RESERVATION_QUERY = """SELECT r.booking_reference, r.created_at, r.cancelled_at,
    s.id, s.doctor_id, s.doctor_name, s.appointment_date, s.start_time, s.end_time
    FROM reservations r JOIN appointment_slots s ON s.id = r.slot_id"""


@mcp.tool()
def list_owned_appointments(owner_hash: str) -> ReservationList:
    """Private application operation: list reservations for a verified browser owner."""
    _validate_owner(owner_hash)
    with closing(connect()) as connection:
        rows = connection.execute(RESERVATION_QUERY + " WHERE r.owner_hash = ? ORDER BY s.appointment_date DESC, s.start_time DESC, r.created_at DESC", (owner_hash,)).fetchall()
    return ReservationList(reservations=[_managed_reservation(row) for row in rows])


@mcp.tool()
def cancel_owned_appointment(booking_reference: str, owner_hash: str) -> ManagedReservation:
    """Private application operation: cancel an owned future reservation atomically."""
    _validate_owner(owner_hash)
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(RESERVATION_QUERY + " WHERE r.booking_reference=? AND r.owner_hash=?", (booking_reference, owner_hash)).fetchone()
        if row is None:
            raise ToolError("This reservation is unavailable.")
        if row["cancelled_at"]:
            return _managed_reservation(row)
        if not _slot_is_future(row, datetime.now(UTC)):
            raise ToolError("Only upcoming reservations can be cancelled.")
        connection.execute("UPDATE reservations SET cancelled_at=? WHERE booking_reference=? AND owner_hash=?", (datetime.now(UTC).isoformat(), booking_reference, owner_hash))
        connection.execute("UPDATE appointment_slots SET is_available=1 WHERE id=?", (row["id"],))
        connection.commit()
        updated = connection.execute(RESERVATION_QUERY + " WHERE r.booking_reference=? AND r.owner_hash=?", (booking_reference, owner_hash)).fetchone()
        return _managed_reservation(updated)


if __name__ == "__main__":
    mcp.run()
