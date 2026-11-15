"""Verified appointment cards and short-lived, session-bound selection tokens."""

import base64
from contextvars import ContextVar
import hashlib
import hmac
import json
import secrets
import time

from pydantic import BaseModel, Field

from app.models import SessionId


card_context: ContextVar[tuple[str, list] | None] = ContextVar("result_cards", default=None)
_SIGNING_KEY = secrets.token_bytes(32)
OFFER_SECONDS = 900


class Slot(BaseModel):
    slot_id: int = Field(gt=0)
    doctor_id: str
    doctor_name: str
    appointment_date: str
    start_time: str
    end_time: str
    timezone: str


class BookingConfirmation(BaseModel):
    session_id: SessionId
    selection_token: str = Field(min_length=1, max_length=2048)


def selection_token(slot_id: int, session_id: str) -> str:
    payload = json.dumps([slot_id, session_id, int(time.time()) + OFFER_SECONDS]).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_SIGNING_KEY, encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_selection(token: str, session_id: str) -> int:
    try:
        encoded, signature = token.split(".")
        expected = hmac.new(_SIGNING_KEY, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        slot_id, owner, expires = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if owner != session_id or expires <= time.time() or type(slot_id) is not int or slot_id < 1:
            raise ValueError()
        return slot_id
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("This selection expired or is invalid. Refresh available times and select again.") from exc


def appointment_cards(messages, session_id: str) -> list[dict]:
    # Reuse the same current-turn evidence boundary as citations, including
    # verified specialist reports. Never turn model prose into a booking action.
    from app.services.chat import _tool_evidence_objects

    booked = []
    for item in _tool_evidence_objects(messages, "clinic_appointments_book_appointment"):
        if item.get("booking_reference") and isinstance(item.get("slot"), dict):
            try:
                slot = Slot.model_validate(item["slot"]).model_dump()
            except ValueError:
                continue
            booked.append({"kind": "reservation", "slot": slot,
                           "booking_reference": item["booking_reference"],
                           "source": "clinic_appointments_book_appointment"})
    if booked:
        return booked[:8]
    slots = {}
    for item in _tool_evidence_objects(messages, "clinic_appointments_list_available_appointments"):
        for raw in item.get("slots", []) if isinstance(item.get("slots"), list) else []:
            try:
                slot = Slot.model_validate(raw).model_dump()
            except ValueError:
                continue
            slots[slot["slot_id"]] = {"kind": "appointment_slot", "slot": slot,
                "selection_token": selection_token(slot["slot_id"], session_id),
                "source": "clinic_appointments_list_available_appointments"}
    return list(slots.values())[:8]
