"""Timezone helpers.

KWin sends UTC timestamps (``new Date().toISOString()`` -> ``...Z``) while Python's
``datetime.now()`` is naive local time. Mixing the two raises ``TypeError`` on
subtraction, so every comparison inside the engine goes through these helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def to_aware(dt: datetime) -> datetime:
    """Return a timezone-aware datetime.

    Naive datetimes are interpreted as *local* time (that is what ``datetime.now()``
    produces), aware datetimes are returned unchanged.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.astimezone()
    return dt


def now_utc() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def parse_timestamp(value: Any) -> datetime:
    """Parse a timestamp coming from an event payload.

    Accepts ISO-8601 strings (with ``Z``/offset or naive), epoch seconds or epoch
    milliseconds (as produced by JavaScript's ``Date.now()``), or a ``datetime``.
    Always returns an aware datetime.
    """
    if isinstance(value, datetime):
        return to_aware(value)
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"Invalid timestamp: {value!r}")
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds > 1e11:  # clearly milliseconds
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        return to_aware(datetime.fromisoformat(text))
    raise ValueError(f"Invalid timestamp: {value!r}")
