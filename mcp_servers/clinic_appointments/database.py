"""SQLite storage for anonymous demo appointment reservations."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

from mcp_servers.clinic_time import clinic_today


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clinic_appointments.db"
_INITIALIZE_LOCK = threading.Lock()


def get_database_path() -> Path:
    """Return the configured appointment database path."""
    configured_path = os.getenv("CLINIC_APPOINTMENTS_DB_PATH")
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()
    return DEFAULT_DATABASE_PATH


def connect() -> sqlite3.Connection:
    """Open the appointment database and initialize synthetic slots."""
    database_path = get_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    with _INITIALIZE_LOCK:
        _initialize_database(connection)
    return connection


def _initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS appointment_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id TEXT NOT NULL,
            doctor_name TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            is_available INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS reservations (
            booking_reference TEXT PRIMARY KEY,
            slot_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (slot_id) REFERENCES appointment_slots(id)
        );
        """
    )
    connection.execute("PRAGMA foreign_keys = ON")

    slot_count = connection.execute(
        "SELECT COUNT(*) FROM appointment_slots"
    ).fetchone()[0]
    if slot_count == 0:
        _seed_demo_slots(connection)
    else:
        _upgrade_legacy_demo_slots(connection)

    connection.commit()


def _seed_demo_slots(connection: sqlite3.Connection) -> None:
    """Create future, synthetic appointment slots without patient data."""
    today = clinic_today()
    slots = []
    for weekday, doctor_id, doctor_name in (
        (4, "dr-lee", "Dr. Amanda Lee"),
        (1, "dr-chen", "Dr. Michael Chen"),
        (3, "dr-rivera", "Dr. Sofia Rivera"),
        (4, "dr-park", "Dr. Daniel Park"),
    ):
        days_ahead = (weekday - today.weekday()) % 7 or 7
        appointment_date = (today + timedelta(days=days_ahead)).isoformat()
        for start_time, end_time in (("09:00", "09:30"), ("09:30", "10:00")):
            slots.append(
                (
                    doctor_id,
                    doctor_name,
                    appointment_date,
                    start_time,
                    end_time,
                )
            )

    connection.executemany(
        """
        INSERT INTO appointment_slots (
            doctor_id, doctor_name, appointment_date, start_time, end_time
        ) VALUES (?, ?, ?, ?, ?)
        """,
        slots,
    )


def _upgrade_legacy_demo_slots(connection: sqlite3.Connection) -> None:
    """Add new specialties once to a database with the original demo slots."""
    rows = connection.execute(
        "SELECT DISTINCT doctor_id, doctor_name FROM appointment_slots ORDER BY doctor_id"
    ).fetchall()
    if [(row["doctor_id"], row["doctor_name"]) for row in rows] != [
        ("dr-chen", "Dr. Michael Chen"),
        ("dr-lee", "Dr. Amanda Lee"),
    ]:
        return

    today = clinic_today()
    slots = []
    for weekday, doctor_id, doctor_name in (
        (3, "dr-rivera", "Dr. Sofia Rivera"),
        (4, "dr-park", "Dr. Daniel Park"),
    ):
        days_ahead = (weekday - today.weekday()) % 7 or 7
        appointment_date = (today + timedelta(days=days_ahead)).isoformat()
        for start_time, end_time in (("09:00", "09:30"), ("09:30", "10:00")):
            slots.append((doctor_id, doctor_name, appointment_date, start_time, end_time))
    connection.executemany(
        """INSERT INTO appointment_slots
           (doctor_id, doctor_name, appointment_date, start_time, end_time)
           VALUES (?, ?, ?, ?, ?)""",
        slots,
    )
