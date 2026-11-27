"""Event models and types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sage.timeutil import now_utc, parse_timestamp, to_aware

# Event types emitted by the KWin script. Kept as plain strings (not a Literal) so new
# event sources can add types without breaking the engine.
KNOWN_EVENT_TYPES = frozenset(
    {
        "window_focus",
        "window_state",
        "window_desktop",
        "desktop_switch",
        "desktop_state",
        "launcher",
        "test",
    }
)


class EventValidationError(ValueError):
    """Raised when an incoming event payload is malformed."""


@dataclass(frozen=True)
class Event:
    """Desktop event.

    ``metadata`` carries context such as the window title (``window``) and the
    application class (``app``). It is kept in memory for rule matching only; it is
    never written to logs unless window-title logging is explicitly enabled.
    """

    timestamp: datetime
    type: str
    action: str
    metadata: dict[str, Any] | None = None

    def age_seconds(self, now: datetime) -> float:
        """Age of the event in seconds relative to ``now`` (naive or aware)."""
        return (to_aware(now) - to_aware(self.timestamp)).total_seconds()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        """Create an Event from a decoded JSON payload.

        Raises:
            EventValidationError: if required fields are missing or invalid.
        """
        if not isinstance(data, dict):
            raise EventValidationError("Event payload must be a JSON object")

        action = data.get("action")
        if not isinstance(action, str) or not action.strip():
            raise EventValidationError("Event is missing a non-empty 'action'")

        event_type = data.get("type", "unknown")
        if not isinstance(event_type, str) or not event_type.strip():
            raise EventValidationError("Event 'type' must be a non-empty string")

        raw_ts = data.get("timestamp")
        try:
            timestamp = now_utc() if raw_ts is None else parse_timestamp(raw_ts)
        except (TypeError, ValueError) as exc:
            raise EventValidationError(f"Invalid event timestamp: {raw_ts!r}") from exc

        metadata = data.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise EventValidationError("Event 'metadata' must be an object")

        return cls(
            timestamp=timestamp,
            type=event_type.strip(),
            action=action.strip().lower(),
            metadata=metadata,
        )
