"""MCP tools for public, non-sensitive clinic operations information."""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from .database import connect
from mcp_servers.clinic_time import clinic_timezone_name, to_clinic_iso


Weekday = Literal[
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


class Doctor(BaseModel):
    id: str
    name: str
    specialty: str


class DoctorList(BaseModel):
    doctors: list[Doctor]


class QueueStatus(BaseModel):
    doctor_id: str
    doctor_name: str
    current_number: int
    last_issued_number: int
    updated_at: str
    timezone: str
    patient_number: int | None = None
    ticket_issued: bool | None = None
    ticket_status_notice: str | None = None


class ProcedureStatus(BaseModel):
    doctor_id: str
    doctor_name: str
    procedure_name: str
    status: str
    started_at: str | None
    estimated_end_at: str | None
    estimated_remaining_minutes: int | None
    completion_estimate_notice: str | None
    updated_at: str
    timezone: str


class ScheduleEntry(BaseModel):
    weekday: str
    start_time: str
    end_time: str
    location: str


class DoctorSchedule(BaseModel):
    doctor_id: str
    doctor_name: str
    schedule: list[ScheduleEntry]

mcp = MCPServer(
    "Clinic Operations",
    instructions=(
        "Use these tools for clinic queue, procedure, and schedule questions. "
        "The data is operational demo data and contains no patient records."
    ),
)


def _get_doctor(
    connection: sqlite3.Connection, doctor_id: str
) -> sqlite3.Row:
    doctor = connection.execute(
        "SELECT id, name, specialty FROM doctors WHERE id = ?", (doctor_id,)
    ).fetchone()
    if doctor is None:
        raise ToolError(
            f"Unknown doctor_id '{doctor_id}'. Call list_doctors to see valid IDs."
        )
    return doctor


@mcp.tool()
def list_doctors() -> DoctorList:
    """List doctors available to the clinic operations tools and their IDs."""
    with closing(connect()) as connection:
        rows = connection.execute(
            "SELECT id, name, specialty FROM doctors ORDER BY name"
        ).fetchall()
    return DoctorList(doctors=[Doctor(**dict(row)) for row in rows])


@mcp.tool()
def get_queue_status(
    doctor_id: str, patient_number: int | None = None
) -> QueueStatus:
    """Get a queue snapshot; ticket gaps cannot establish people ahead or wait."""
    if patient_number is not None and patient_number < 1:
        raise ToolError("patient_number must be a positive integer.")

    with closing(connect()) as connection:
        doctor = _get_doctor(connection, doctor_id)
        queue = connection.execute(
            """
            SELECT current_number, last_issued_number, updated_at
            FROM queue_status
            WHERE doctor_id = ?
            """,
            (doctor_id,),
        ).fetchone()

    if queue is None:
        raise ToolError(f"No queue information is available for '{doctor_id}'.")

    result = {
        "doctor_id": doctor["id"],
        "doctor_name": doctor["name"],
        "current_number": queue["current_number"],
        "last_issued_number": queue["last_issued_number"],
        "updated_at": to_clinic_iso(queue["updated_at"]),
        "timezone": clinic_timezone_name(),
    }

    if patient_number is not None:
        result["patient_number"] = patient_number
        if patient_number > queue["last_issued_number"]:
            result["ticket_issued"] = False
            result["ticket_status_notice"] = (
                "This number is above the last issued number in the synthetic "
                "snapshot, so it has not been issued. The number gap does not "
                "establish an actual queue position or wait."
            )
        else:
            result["ticket_status_notice"] = (
                "This snapshot does not record individual ticket issuance or "
                "calling order. The number may have been skipped; its issuance, "
                "actual queue position, and wait cannot be confirmed."
            )

    return QueueStatus(**result)


@mcp.tool()
def get_procedure_status(doctor_id: str) -> ProcedureStatus:
    """Get a doctor's procedure status, remaining time, and estimate notice."""
    with closing(connect()) as connection:
        doctor = _get_doctor(connection, doctor_id)
        procedure = connection.execute(
            """
            SELECT procedure_name, status, started_at,
                   estimated_end_at, updated_at
            FROM procedures
            WHERE doctor_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (doctor_id,),
        ).fetchone()

    if procedure is None:
        raise ToolError(f"No procedure information is available for '{doctor_id}'.")

    remaining_minutes = None
    if procedure["estimated_end_at"]:
        estimated_end = datetime.fromisoformat(procedure["estimated_end_at"])
        remaining_minutes = max(
            0,
            math.ceil(
                (estimated_end - datetime.now(UTC)).total_seconds() / 60
            ),
        )

    return ProcedureStatus(**{
        "doctor_id": doctor["id"],
        "doctor_name": doctor["name"],
        "procedure_name": procedure["procedure_name"],
        "status": procedure["status"],
        "started_at": to_clinic_iso(procedure["started_at"]),
        "estimated_end_at": to_clinic_iso(procedure["estimated_end_at"]),
        "estimated_remaining_minutes": remaining_minutes,
        "completion_estimate_notice": (
            "Procedure completion estimates are not guaranteed; the actual "
            "end time can change as the procedure progresses."
            if procedure["estimated_end_at"] else None
        ),
        "updated_at": to_clinic_iso(procedure["updated_at"]),
        "timezone": clinic_timezone_name(),
    })


@mcp.tool()
def get_doctor_schedule(
    doctor_id: str, weekday: Weekday | None = None
) -> DoctorSchedule:
    """Get a doctor's recurring clinic schedule, optionally for one weekday."""
    with closing(connect()) as connection:
        doctor = _get_doctor(connection, doctor_id)
        if weekday is None:
            rows = connection.execute(
                """
                SELECT weekday, start_time, end_time, location
                FROM schedules
                WHERE doctor_id = ?
                ORDER BY id
                """,
                (doctor_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT weekday, start_time, end_time, location
                FROM schedules
                WHERE doctor_id = ? AND weekday = ?
                ORDER BY id
                """,
                (doctor_id, weekday),
            ).fetchall()

    return DoctorSchedule(**{
        "doctor_id": doctor["id"],
        "doctor_name": doctor["name"],
        "schedule": [ScheduleEntry(**dict(row)) for row in rows],
    })


if __name__ == "__main__":
    mcp.run()
