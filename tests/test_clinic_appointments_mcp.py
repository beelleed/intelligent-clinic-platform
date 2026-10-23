"""Focused protocol tests for the clinic appointments MCP server."""

import pytest
from datetime import UTC, datetime
from mcp import Client

from mcp_servers.clinic_appointments.database import connect
from mcp_servers.clinic_appointments.server import mcp
from mcp_servers.clinic_appointments import server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_appointment_database(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "CLINIC_APPOINTMENTS_DB_PATH", str(tmp_path / "appointments.db")
    )


@pytest.mark.anyio
async def test_tools_are_discoverable_with_valid_input_schemas():
    async with Client(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {"list_available_appointments", "book_appointment"}
    assert tools["book_appointment"].input_schema["required"] == ["slot_id"]
    assert (
        tools["book_appointment"].input_schema["properties"]["slot_id"]["type"]
        == "integer"
    )


@pytest.mark.anyio
async def test_list_and_book_anonymous_appointment():
    async with Client(mcp) as client:
        availability = await client.call_tool("list_available_appointments", {})
        slot = availability.structured_content["slots"][0]
        reservation = await client.call_tool(
            "book_appointment", {"slot_id": slot["slot_id"]}
        )
        availability_after = await client.call_tool(
            "list_available_appointments", {}
        )

    assert availability.is_error is False
    assert slot["timezone"] == "America/Los_Angeles"
    assert reservation.is_error is False
    assert reservation.structured_content["booking_reference"].startswith("APT-")
    assert reservation.structured_content["slot"] == slot
    assert len(availability_after.structured_content["slots"]) == 7


@pytest.mark.anyio
async def test_specialty_slots_match_recurring_days_and_support_filter():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "list_available_appointments", {"doctor_id": "dr-rivera"}
        )

    from datetime import date

    slots = result.structured_content["slots"]
    assert len(slots) == 2
    assert all(slot["doctor_name"] == "Dr. Sofia Rivera" for slot in slots)
    assert all(date.fromisoformat(slot["appointment_date"]).weekday() == 3 for slot in slots)


@pytest.mark.anyio
async def test_unknown_doctor_filter_is_an_error_not_empty_availability():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "list_available_appointments", {"doctor_id": "synthetic_derm"}
        )

    assert result.is_error is True
    assert "Unknown doctor_id 'synthetic_derm'" in result.content[0].text
    assert "dr-rivera" in result.content[0].text


def test_legacy_demo_appointments_gain_new_specialties_once():
    with connect() as connection:
        connection.execute("DELETE FROM appointment_slots WHERE doctor_id IN ('dr-rivera', 'dr-park')")
        connection.execute("UPDATE appointment_slots SET is_available = 0 WHERE id = 1")
        connection.commit()

    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM appointment_slots").fetchone()[0] == 8
        assert connection.execute(
            "SELECT is_available FROM appointment_slots WHERE id = 1"
        ).fetchone()[0] == 0
    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM appointment_slots").fetchone()[0] == 8


@pytest.mark.anyio
async def test_booking_unavailable_slot_returns_controlled_tool_error():
    async with Client(mcp) as client:
        availability = await client.call_tool("list_available_appointments", {})
        slot_id = availability.structured_content["slots"][0]["slot_id"]
        first_booking = await client.call_tool(
            "book_appointment", {"slot_id": slot_id}
        )
        duplicate_booking = await client.call_tool(
            "book_appointment", {"slot_id": slot_id}
        )

    assert first_booking.is_error is False
    assert duplicate_booking.is_error is True
    assert "no longer available" in duplicate_booking.content[0].text


@pytest.fixture
def fixed_clinic_clock(monkeypatch):
    # UTC has crossed midnight; Pacific is still October 8 at 17:30.
    fixed = datetime(2026, 10, 9, 0, 30, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setenv("CLINIC_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setattr(server, "datetime", FixedDatetime)
    with connect() as connection:
        connection.execute("UPDATE appointment_slots SET appointment_date = '2026-10-07'")
        for slot_date, start in (
            ("2026-10-07", "18:00"),
            ("2026-10-08", "17:00"),
            ("2026-10-08", "17:30"),
            ("2026-10-08", "18:00"),
            ("2026-10-09", "09:00"),
        ):
            connection.execute(
                "INSERT INTO appointment_slots "
                "(doctor_id, doctor_name, appointment_date, start_time, end_time) "
                "VALUES ('dr-lee', 'Dr. Amanda Lee', ?, ?, '19:00')",
                (slot_date, start),
            )
        connection.commit()


@pytest.mark.anyio
async def test_availability_excludes_started_slots_in_clinic_timezone(fixed_clinic_clock):
    async with Client(mcp) as client:
        result = await client.call_tool("list_available_appointments", {"doctor_id": "dr-lee"})
        past_date = await client.call_tool(
            "list_available_appointments", {"appointment_date": "2026-10-07"}
        )
    assert result.is_error is False
    assert [(slot["appointment_date"], slot["start_time"])
            for slot in result.structured_content["slots"]] == [
        ("2026-10-08", "18:00"), ("2026-10-09", "09:00"),
    ]
    assert past_date.structured_content["slots"] == []


@pytest.mark.anyio
async def test_expired_slot_cannot_be_booked_by_id(fixed_clinic_clock):
    with connect() as connection:
        slot_id = connection.execute(
            "SELECT id FROM appointment_slots WHERE appointment_date = '2026-10-08' "
            "AND start_time = '17:30'"
        ).fetchone()[0]
    async with Client(mcp) as client:
        result = await client.call_tool("book_appointment", {"slot_id": slot_id})
    assert result.is_error is True
    assert "already started or expired" in result.content[0].text
    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        assert connection.execute(
            "SELECT is_available FROM appointment_slots WHERE id = ?", (slot_id,)
        ).fetchone()[0] == 1
