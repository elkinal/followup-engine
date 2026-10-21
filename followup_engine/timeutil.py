"""Timestamp helpers. All timestamps in this system are UTC and stored as
ISO-8601 strings with a trailing 'Z' (matching the seed data's format), e.g.
'2026-08-15T00:00:00Z'.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 'Z'-suffixed timestamp into an aware UTC datetime."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    """Format an aware UTC datetime back into the 'Z'-suffixed ISO-8601 form."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_str() -> str:
    return format_ts(datetime.now(timezone.utc))
