"""Focused protocol tests for the clinic operations MCP server."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest
from mcp import Client

from mcp_servers.clinic_operations.database import connect
from mcp_servers.clinic_operations.server import mcp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_clinic_database(monkeypatch, tmp_path):
    monkeypatch.setenv("CLINIC_DB_PATH", str(tmp_path / "clinic.db"))


def test_parallel_read_connections_can_refresh_synthetic_timestamps():
    def read_queue() -> int:
        with closing(connect()) as connection:
            return connection.execute(
                "SELECT current_number FROM queue_status WHERE doctor_id = 'dr-lee'"
            ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _index: read_queue(), range(16))) == [18] * 16


@pytest.mark.anyio
async def test_tools_are_discoverable_with_input_schemas():
    async with Client(mcp) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {
        "list_doctors",
        "get_queue_status",
        "get_procedure_status",
        "get_doctor_schedule",
    }
    assert tools["get_queue_status"].input_schema["properties"] == {
        "doctor_id": {"title": "Doctor Id", "type": "string"},
        "patient_number": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": None,
            "title": "Patient Number",
        },
    }
    assert tools["get_queue_status"].input_schema["required"] == ["doctor_id"]


@pytest.mark.anyio
async def test_multi_specialty_directory_and_schedule():
    async with Client(mcp) as client:
        doctors = await client.call_tool("list_doctors", {})
        schedule = await client.call_tool(
            "get_doctor_schedule", {"doctor_id": "dr-rivera", "weekday": "Thursday"}
        )

    specialties = {
        doctor["id"]: doctor["specialty"]
        for doctor in doctors.structured_content["doctors"]
    }
    assert specialties == {
        "dr-lee": "Family Medicine",
        "dr-chen": "General Surgery",
        "dr-rivera": "Dermatology",
        "dr-park": "Otolaryngology (ENT)",
    }
    assert schedule.structured_content["schedule"] == [
        {
            "weekday": "Thursday",
            "start_time": "09:00",
            "end_time": "12:00",
            "location": "Specialty Suite",
        }
    ]


def test_legacy_demo_database_adds_specialties_without_replacing_queue():
    with connect() as connection:
        connection.execute("UPDATE queue_status SET current_number = 19 WHERE doctor_id = 'dr-lee'")
        connection.execute("DELETE FROM schedules WHERE doctor_id IN ('dr-rivera', 'dr-park')")
        connection.execute("DELETE FROM procedures WHERE doctor_id IN ('dr-rivera', 'dr-park')")
        connection.execute("DELETE FROM queue_status WHERE doctor_id IN ('dr-rivera', 'dr-park')")
        connection.execute("DELETE FROM doctors WHERE id IN ('dr-rivera', 'dr-park')")
        connection.commit()

    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 4
        assert connection.execute(
            "SELECT current_number FROM queue_status WHERE doctor_id = 'dr-lee'"
        ).fetchone()[0] == 19
    with connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 4


@pytest.mark.anyio
async def test_queue_status_does_not_infer_wait_from_issued_number_gap():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_queue_status",
            {"doctor_id": "dr-lee", "patient_number": 21},
        )

    assert result.is_error is False
    assert result.structured_content["current_number"] == 18
    assert result.structured_content["ticket_issued"] is None
    assert "cannot be confirmed" in result.structured_content["ticket_status_notice"]
    assert "patients_ahead" not in result.structured_content
    assert "estimated_wait_minutes" not in result.structured_content
    assert "hypothetical_queue_positions" not in result.structured_content
    assert "average_visit_minutes" not in result.structured_content
    assert result.structured_content["timezone"] == "America/Los_Angeles"
    updated_at = datetime.fromisoformat(result.structured_content["updated_at"])
    assert updated_at.utcoffset().total_seconds() in {-28800, -25200}


@pytest.mark.anyio
async def test_unissued_ticket_has_no_wait_estimate():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_queue_status",
            {"doctor_id": "dr-lee", "patient_number": 28},
        )

    assert result.is_error is False
    assert result.structured_content["current_number"] == 18
    assert result.structured_content["last_issued_number"] == 24
    assert result.structured_content["ticket_issued"] is False
    assert "has not been issued" in result.structured_content["ticket_status_notice"]
    assert "patients_ahead" not in result.structured_content
    assert "estimated_wait_minutes" not in result.structured_content
    assert "hypothetical_queue_positions" not in result.structured_content
    assert "hypothetical_wait_minutes" not in result.structured_content


@pytest.mark.anyio
async def test_procedure_and_schedule_tools_return_structured_data():
    async with Client(mcp) as client:
        procedure = await client.call_tool(
            "get_procedure_status", {"doctor_id": "dr-chen"}
        )
        schedule = await client.call_tool(
            "get_doctor_schedule",
            {"doctor_id": "dr-chen", "weekday": "Thursday"},
        )

    assert procedure.is_error is False
    assert procedure.structured_content["status"] == "in_progress"
    assert procedure.structured_content["estimated_remaining_minutes"] in {44, 45}
    assert procedure.structured_content["completion_estimate_notice"] == (
        "Procedure completion estimates are not guaranteed; the actual end "
        "time can change as the procedure progresses."
    )
    assert procedure.structured_content["timezone"] == "America/Los_Angeles"
    estimated_end = datetime.fromisoformat(
        procedure.structured_content["estimated_end_at"]
    )
    assert estimated_end.utcoffset().total_seconds() in {-28800, -25200}
    assert schedule.is_error is False
    assert schedule.structured_content["schedule"] == [
        {
            "weekday": "Thursday",
            "start_time": "13:00",
            "end_time": "17:00",
            "location": "Surgery Clinic",
        }
    ]


def test_expired_demo_procedure_is_refreshed() -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE procedures
            SET estimated_end_at = '2020-01-01T00:00:00+00:00',
                updated_at = '2020-01-01T00:00:00+00:00'
            WHERE doctor_id = 'dr-chen'
            """
        )
        connection.commit()

    with connect() as connection:
        procedure = connection.execute(
            """
            SELECT estimated_end_at, updated_at
            FROM procedures
            WHERE doctor_id = 'dr-chen'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert procedure["estimated_end_at"] > procedure["updated_at"]
    assert not procedure["updated_at"].startswith("2020-")


@pytest.mark.anyio
async def test_unknown_doctor_returns_controlled_tool_error():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_queue_status", {"doctor_id": "not-a-doctor"}
        )

    assert result.is_error is True
    assert "Unknown doctor_id" in result.content[0].text
