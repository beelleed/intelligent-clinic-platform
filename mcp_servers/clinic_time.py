"""Clinic-local time helpers while preserving UTC for stored timestamps."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_CLINIC_TIMEZONE = "America/Los_Angeles"


def clinic_timezone_name() -> str:
    """Return the configured IANA timezone name for public clinic times."""
    return os.getenv("CLINIC_TIMEZONE", DEFAULT_CLINIC_TIMEZONE).strip() or (
        DEFAULT_CLINIC_TIMEZONE
    )


def clinic_timezone() -> ZoneInfo:
    """Resolve the configured timezone, falling back safely to Pacific time."""
    name = clinic_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_CLINIC_TIMEZONE)


def clinic_today() -> date:
    """Return today's date in the clinic's local timezone."""
    return datetime.now(clinic_timezone()).date()


def to_clinic_iso(value: str | None) -> str | None:
    """Convert an ISO timestamp to a clinic-local, offset-aware timestamp."""
    if value is None:
        return None
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(clinic_timezone()).isoformat(timespec="seconds")
