"""Private browser-owned appointment reads and cancellation through MCP."""

from pydantic import BaseModel, Field

from app.services.chat import read_clinic_tool, MCPConnectionError
from mcp_servers.clinic_appointments.server import ReservationList, ManagedReservation


class CancelReservation(BaseModel):
    booking_reference: str = Field(pattern=r"^APT-[A-F0-9]{8}$")


def reservation_card(reservation: ManagedReservation) -> dict:
    return {"kind": "reservation", "slot": reservation.slot.model_dump(),
            "booking_reference": reservation.booking_reference, "status": reservation.status,
            "can_cancel": reservation.can_cancel, "cancelled_at": reservation.cancelled_at,
            "source": "clinic_appointments_list_owned_appointments"}


async def my_appointments(owner_hash: str) -> list[dict]:
    for item in await read_clinic_tool("clinic_appointments_list_owned_appointments", {"owner_hash": owner_hash}):
        if "reservations" in item:
            reservations = ReservationList.model_validate(item).reservations
            # Upcoming visits first, followed by past and cancelled records.
            reservations.sort(key=lambda row: (row.status != "upcoming", row.slot.appointment_date, row.slot.start_time))
            return [reservation_card(row) for row in reservations]
    raise MCPConnectionError("Reservation information is unavailable.")


async def cancel_appointment(reference: str, owner_hash: str) -> dict:
    for item in await read_clinic_tool("clinic_appointments_cancel_owned_appointment",
                                     {"booking_reference": reference, "owner_hash": owner_hash}):
        if item.get("booking_reference") == reference and item.get("status") == "cancelled":
            card = reservation_card(ManagedReservation.model_validate(item))
            card["source"] = "clinic_appointments_cancel_owned_appointment"
            return card
    raise ValueError("This reservation could not be cancelled. Refresh My appointments and try again.")
