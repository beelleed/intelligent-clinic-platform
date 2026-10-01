"""Focused protocol tests for the clinic appointments MCP server."""

import pytest
from mcp import Client

from mcp_servers.clinic_appointments.server import mcp


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
    assert len(availability_after.structured_content["slots"]) == 3


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
