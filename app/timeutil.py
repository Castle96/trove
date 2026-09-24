from __future__ import annotations

from datetime import UTC, datetime, timedelta

UTC = UTC


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (consistent SQLite storage)."""
    return datetime.now(UTC).replace(tzinfo=None)


def add_days(dt: datetime, days: int) -> datetime:
    return dt + timedelta(days=days)
