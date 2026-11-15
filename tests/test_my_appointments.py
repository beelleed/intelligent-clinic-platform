"""Reservation ownership, cancellation, migration and model-tool isolation."""

import hashlib
import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import tool
from mcp import Client

from app.api import app
from app.rate_limit import clinic_rate_limiter
from app.services.booking_owner import COOKIE_NAME, booking_owner_context
from app.services.chat import _public_mcp_tools
from app.services.result_cards import selection_token
from mcp_servers.clinic_appointments.database import connect
from mcp_servers.clinic_appointments.server import mcp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def isolated_database(monkeypatch, tmp_path):
    path = tmp_path / "appointments.db"
    monkeypatch.setenv("CLINIC_APPOINTMENTS_DB_PATH", str(path))
    return path


@pytest.mark.anyio
async def test_ownership_cancel_rebook_and_repeated_cancel_preserve_other_booking(isolated_database):
    owner, other = "a" * 64, "b" * 64
    async with Client(mcp) as client:
        available = await client.call_tool("list_available_appointments", {})
        slot_id = available.structured_content["slots"][0]["slot_id"]
        result = await client.call_tool("book_appointment", {"slot_id": slot_id, "owner_hash": owner})
        reference = result.structured_content["booking_reference"]
        listed = await client.call_tool("list_owned_appointments", {"owner_hash": owner})
        assert listed.structured_content["reservations"][0]["booking_reference"] == reference
        assert listed.structured_content["reservations"][0]["can_cancel"] is True
        assert (await client.call_tool("list_owned_appointments", {"owner_hash": other})).structured_content["reservations"] == []
        assert (await client.call_tool("cancel_owned_appointment", {"booking_reference": reference, "owner_hash": other})).is_error
        cancelled = await client.call_tool("cancel_owned_appointment", {"booking_reference": reference, "owner_hash": owner})
        assert cancelled.structured_content["status"] == "cancelled"
        assert cancelled.structured_content["can_cancel"] is False
        rebooked = await client.call_tool("book_appointment", {"slot_id": slot_id, "owner_hash": other})
        assert not rebooked.is_error
        assert rebooked.structured_content["booking_reference"] != reference
        repeated = await client.call_tool("cancel_owned_appointment", {"booking_reference": reference, "owner_hash": owner})
        assert repeated.structured_content["status"] == "cancelled"
        remaining = await client.call_tool("list_available_appointments", {})
        assert slot_id not in {slot["slot_id"] for slot in remaining.structured_content["slots"]}


def test_legacy_database_migration_preserves_references_and_allows_rebooking(isolated_database):
    with sqlite3.connect(isolated_database) as connection:
        connection.executescript("""
            CREATE TABLE appointment_slots (id INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_id TEXT NOT NULL, doctor_name TEXT NOT NULL, appointment_date TEXT NOT NULL,
                start_time TEXT NOT NULL, end_time TEXT NOT NULL, is_available INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE reservations (booking_reference TEXT PRIMARY KEY,
                slot_id INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL,
                FOREIGN KEY(slot_id) REFERENCES appointment_slots(id));
            INSERT INTO appointment_slots VALUES (42, 'custom', 'Custom Doctor', '2099-11-24', '13:00', '13:30', 0);
            INSERT INTO reservations VALUES ('APT-OLD', 42, '2026-11-14T12:00:00+00:00');
        """)
    with connect() as connection:
        old = connection.execute("SELECT * FROM reservations WHERE booking_reference='APT-OLD'").fetchone()
        assert old["slot_id"] == 42 and old["owner_hash"] is None and old["cancelled_at"] is None
        connection.execute("UPDATE reservations SET cancelled_at='2026-11-14T13:00:00+00:00' WHERE booking_reference='APT-OLD'")
        connection.execute("INSERT INTO reservations (booking_reference,slot_id,created_at,owner_hash) VALUES ('APT-NEW',42,'2026-11-14T13:01:00+00:00',?)", ("a" * 64,))
        connection.commit()
    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 2


@pytest.mark.anyio
async def test_agent_cannot_choose_owner_or_call_private_management_tools():
    received = []

    @tool("clinic_appointments_book_appointment")
    async def book(slot_id: int, owner_hash: str | None = None) -> dict:
        """Reserve a demo time."""
        received.append((slot_id, owner_hash))
        return {"booking_reference": "APT-12345678"}

    @tool("clinic_appointments_list_owned_appointments")
    async def private_list(owner_hash: str) -> dict:
        """Private list."""
        return {}

    visible = _public_mcp_tools([book, private_list])
    assert [item.name for item in visible] == [book.name]
    assert set(visible[0].args) == {"slot_id"}
    token = booking_owner_context.set("a" * 64)
    try:
        await visible[0].ainvoke({"slot_id": 42})
    finally:
        booking_owner_context.reset(token)
    assert received == [(42, "a" * 64)]


@pytest.fixture
def web_clients(monkeypatch):
    monkeypatch.setenv("PUBLIC_CHAT_ENABLED", "true")
    clinic_rate_limiter.reset()
    yield TestClient(app), TestClient(app)
    clinic_rate_limiter.reset()


def test_owner_cookie_survives_refresh_and_is_isolated_between_browsers(web_clients, monkeypatch):
    first, other = web_clients
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.my_appointments", lookup)
    response = first.get("/appointments/mine")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    first_owner = lookup.await_args.args[0]
    assert first_owner == hashlib.sha256(first.cookies[COOKIE_NAME].encode()).hexdigest()
    assert first.get("/appointments/mine").status_code == 200
    assert lookup.await_args.args[0] == first_owner
    assert other.get("/appointments/mine").status_code == 200
    assert lookup.await_args.args[0] != first_owner


def test_cancellation_uses_cookie_owner_and_rejects_foreign_origin(web_clients, monkeypatch):
    first, _ = web_clients
    monkeypatch.setattr("app.api.my_appointments", AsyncMock(return_value=[]))
    first.get("/appointments/mine")
    cancel = AsyncMock(return_value={"kind": "reservation", "status": "cancelled"})
    monkeypatch.setattr("app.api.cancel_appointment", cancel)
    payload = {"booking_reference": "APT-12345678"}
    assert first.post("/appointments/cancel", json=payload, headers={"Origin": "https://attacker.example"}).status_code == 403
    cancel.assert_not_awaited()
    assert first.post("/appointments/cancel", json=payload).status_code == 200
    assert cancel.await_args.args == (payload["booking_reference"], hashlib.sha256(first.cookies[COOKIE_NAME].encode()).hexdigest())
    assert first.post("/appointments/cancel", json={"booking_reference": "invalid"}).status_code == 422


def test_booking_reuses_browser_owner_independently_of_chat_session(web_clients, monkeypatch):
    from tests.test_result_cards import SLOT
    first, _ = web_clients
    monkeypatch.setattr("app.api.my_appointments", AsyncMock(return_value=[]))
    first.get("/appointments/mine")
    book = AsyncMock(return_value={"slot": SLOT, "booking_reference": "APT-12345678"})
    monkeypatch.setattr("app.api.confirm_appointment_selection", book)
    for session_id in ("chat-one", "chat-two"):
        response = first.post("/appointments/confirm", json={"session_id": session_id, "selection_token": selection_token(3, session_id)})
        assert response.status_code == 200
        assert book.await_args.args[2] == hashlib.sha256(first.cookies[COOKIE_NAME].encode()).hexdigest()
