"""SQLite storage for the clinic operations MCP server."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clinic_operations.db"


def get_database_path() -> Path:
    """Return the configured database path without caching environment state."""
    configured_path = os.getenv("CLINIC_DB_PATH")
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()
    return DEFAULT_DATABASE_PATH


def connect() -> sqlite3.Connection:
    """Open the clinic database and ensure its schema and demo data exist."""
    database_path = get_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    _initialize_database(connection)
    return connection


def _initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS doctors (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            specialty TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS queue_status (
            doctor_id TEXT PRIMARY KEY,
            current_number INTEGER NOT NULL,
            last_issued_number INTEGER NOT NULL,
            average_visit_minutes INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        );

        CREATE TABLE IF NOT EXISTS procedures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id TEXT NOT NULL,
            procedure_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            estimated_end_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id TEXT NOT NULL,
            weekday TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            location TEXT NOT NULL,
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
        );
        """
    )

    doctor_count = connection.execute("SELECT COUNT(*) FROM doctors").fetchone()[0]
    if doctor_count == 0:
        _seed_demo_data(connection)

    if _demo_refresh_enabled():
        _refresh_demo_timestamps(connection)

    connection.commit()


def _demo_refresh_enabled() -> bool:
    return (
        os.getenv("CLINIC_REFRESH_DEMO_DATA", "true").strip().lower()
        == "true"
    )


def _refresh_demo_timestamps(connection: sqlite3.Connection) -> None:
    """Keep synthetic operational timestamps useful across later demo sessions."""
    now = datetime.now(UTC).replace(microsecond=0)
    now_value = now.isoformat()

    connection.execute(
        "UPDATE queue_status SET updated_at = ?",
        (now_value,),
    )
    connection.execute(
        """
        UPDATE procedures
        SET started_at = ?,
            estimated_end_at = ?,
            updated_at = ?
        WHERE status = 'in_progress'
          AND (estimated_end_at IS NULL OR estimated_end_at <= ?)
        """,
        (
            (now - timedelta(minutes=30)).isoformat(),
            (now + timedelta(minutes=45)).isoformat(),
            now_value,
            now_value,
        ),
    )


def _seed_demo_data(connection: sqlite3.Connection) -> None:
    """Insert synthetic development data; tool responses still query SQLite."""
    now = datetime.now(UTC).replace(microsecond=0)
    connection.executemany(
        "INSERT INTO doctors (id, name, specialty) VALUES (?, ?, ?)",
        [
            ("dr-lee", "Dr. Amanda Lee", "Family Medicine"),
            ("dr-chen", "Dr. Michael Chen", "General Surgery"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO queue_status (
            doctor_id, current_number, last_issued_number,
            average_visit_minutes, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("dr-lee", 18, 24, 12, now.isoformat()),
            ("dr-chen", 7, 10, 18, now.isoformat()),
        ],
    )
    connection.executemany(
        """
        INSERT INTO procedures (
            doctor_id, procedure_name, status, started_at,
            estimated_end_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "dr-chen",
                "Laparoscopic procedure",
                "in_progress",
                (now - timedelta(minutes=30)).isoformat(),
                (now + timedelta(minutes=45)).isoformat(),
                now.isoformat(),
            ),
            (
                "dr-lee",
                "No active procedure",
                "available",
                None,
                None,
                now.isoformat(),
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO schedules (
            doctor_id, weekday, start_time, end_time, location
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("dr-lee", "Monday", "09:00", "12:00", "Clinic A"),
            ("dr-lee", "Wednesday", "13:00", "17:00", "Clinic A"),
            ("dr-lee", "Friday", "09:00", "12:00", "Clinic B"),
            ("dr-chen", "Tuesday", "09:00", "12:00", "Surgery Clinic"),
            ("dr-chen", "Thursday", "13:00", "17:00", "Surgery Clinic"),
        ],
    )
