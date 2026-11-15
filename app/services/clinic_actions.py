"""Direct, typed clinic operations. These routes never invoke a language model."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.models import SessionId
from app.services.chat import read_clinic_tool, MCPConnectionError
from app.services.result_cards import Slot, selection_token
from mcp_servers.clinic_operations.server import DoctorList, DoctorSchedule, QueueStatus


class ClinicLookup(BaseModel):
    action: Literal["appointments", "schedule", "queue"]
    session_id: SessionId
    doctor_id: str | None = Field(default=None, min_length=1, max_length=80)
    appointment_date: date | None = None


async def _read(name, arguments, key):
    for item in await read_clinic_tool(name, arguments):
        if key in item:
            return item
    raise MCPConnectionError("Clinic information is temporarily unavailable.")


async def doctor_directory():
    data = await _read("clinic_operations_list_doctors", {}, "doctors")
    return DoctorList.model_validate(data).model_dump()


async def lookup(request: ClinicLookup):
    if request.action == "appointments":
        source = "clinic_appointments_list_available_appointments"
        arguments = {"doctor_id": request.doctor_id}
        data = await _read(source, arguments, "slots")
        slots = [Slot.model_validate(raw).model_dump() for raw in data["slots"]]
        dates = sorted({slot["appointment_date"] for slot in slots})
        selected_date = request.appointment_date.isoformat() if request.appointment_date else next(iter(dates), None)
        slots = [slot for slot in slots if slot["appointment_date"] == selected_date]
        cards = [{"kind": "appointment_slot", "slot": slot,
                  "selection_token": selection_token(slot["slot_id"], request.session_id),
                  "source": source} for slot in slots[:48]]
        return {"response": ("Select a time below to review your reservation."
                if cards else "No available times match this date. Select another available date or doctor."),
                "cards": cards, "available_dates": dates, "selected_date": selected_date}
    if not request.doctor_id:
        raise ValueError("Select a doctor first.")
    if request.action == "schedule":
        source = "clinic_operations_get_doctor_schedule"
        data = DoctorSchedule.model_validate(await _read(source, {"doctor_id": request.doctor_id}, "schedule"))
        return {"response": "Recurring hours only. This schedule does not confirm the doctor's presence today.",
                "cards": [{"kind": "schedule", "data": data.model_dump(), "source": source}]}
    source = "clinic_operations_get_queue_status"
    data = QueueStatus.model_validate(await _read(source, {"doctor_id": request.doctor_id}, "current_number"))
    return {"response": "This is a synthetic queue snapshot. Ticket-number gaps do not establish waiting time or people ahead.",
            "cards": [{"kind": "queue", "data": data.model_dump(), "source": source}]}
