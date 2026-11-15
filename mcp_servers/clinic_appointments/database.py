"""SQLite storage for anonymous demo appointment reservations."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

from mcp_servers.clinic_time import clinic_today
from mcp_servers.demo_data import DEMO_DOCTORS, DEMO_SCHEDULES


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
            slot_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            owner_hash TEXT,
            cancelled_at TEXT,
            FOREIGN KEY (slot_id) REFERENCES appointment_slots(id)
        );
        """
    )
    connection.execute("PRAGMA foreign_keys = ON")
    _migrate_reservations(connection)

    slot_count = connection.execute(
        "SELECT COUNT(*) FROM appointment_slots"
    ).fetchone()[0]
    if slot_count == 0:
        _seed_demo_slots(connection)
    elif os.getenv("CLINIC_REFRESH_DEMO_DATA", "true").strip().lower() == "true":
        expected = {doctor_id: name for doctor_id, name, _ in DEMO_DOCTORS}
        existing = connection.execute("SELECT DISTINCT doctor_id, doctor_name FROM appointment_slots").fetchall()
        if all(expected.get(row[0]) == row[1] for row in existing):
            _seed_demo_slots(connection)

    connection.commit()


def _migrate_reservations(connection: sqlite3.Connection) -> None:
    """Keep existing references while allowing a cancelled slot to be rebooked."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(reservations)")}
    if "cancelled_at" not in columns:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE reservations RENAME TO legacy_reservations")
        connection.execute("""CREATE TABLE reservations (
            booking_reference TEXT PRIMARY KEY, slot_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, owner_hash TEXT, cancelled_at TEXT,
            FOREIGN KEY (slot_id) REFERENCES appointment_slots(id))""")
        connection.execute("""INSERT INTO reservations (booking_reference, slot_id, created_at)
            SELECT booking_reference, slot_id, created_at FROM legacy_reservations""")
        connection.execute("DROP TABLE legacy_reservations")
    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_reservation_slot
        ON reservations(slot_id) WHERE cancelled_at IS NULL""")
    connection.execute("CREATE INDEX IF NOT EXISTS reservation_owner ON reservations(owner_hash)")


def _seed_demo_slots(connection: sqlite3.Connection) -> None:
    """Fill 28 upcoming days from recurring hours, preserving bookings and IDs."""
    today = clinic_today()
    slots = []
    names = {doctor_id: name for doctor_id, name, _ in DEMO_DOCTORS}
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    for offset in range(29):
        day = today + timedelta(days=offset)
        for doctor_id, weekday, start, end, _ in DEMO_SCHEDULES:
            if weekdays[day.weekday()] != weekday:
                continue
            start_hour, start_minute = map(int, start.split(":"))
            end_hour, end_minute = map(int, end.split(":"))
            for minute in range(start_hour * 60 + start_minute, end_hour * 60 + end_minute, 30):
                slots.append((doctor_id, names[doctor_id], day.isoformat(),
                              f"{minute // 60:02d}:{minute % 60:02d}",
                              f"{(minute + 30) // 60:02d}:{(minute + 30) % 60:02d}"))

    connection.executemany(
        """
        INSERT INTO appointment_slots (
            doctor_id, doctor_name, appointment_date, start_time, end_time
        ) SELECT ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM appointment_slots
            WHERE doctor_id = ? AND appointment_date = ? AND start_time = ?
        )
        """,
        [(*slot, slot[0], slot[2], slot[3]) for slot in slots],
    )
