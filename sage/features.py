"""Feature extraction from event sequences."""

from __future__ import annotations

from typing import Any

from sage.buffer import RingBuffer
from sage.timeutil import to_aware

# Event types that describe *which window* the user is in, rather than an action.
CONTEXT_EVENT_TYPES = frozenset({"window_focus"})


class FeatureExtractor:
    """Extracts context features from event buffer."""

    def __init__(self, buffer: RingBuffer):
        """
        Initialize feature extractor.

        Args:
            buffer: RingBuffer containing recent events
        """
        self.buffer = buffer

    def extract(self) -> dict[str, Any]:
        """
        Extract context features from recent events.

        Returns:
            Dictionary of extracted features. ``recent_events`` holds
            ``(action, age_seconds)`` pairs where the age is measured relative to the
            newest event, so rules can honour their own ``window``.
            ``active_app`` / ``active_title`` describe the most recently focused window.
        """
        events = self.buffer.recent()
        actions = [e.action for e in events]

        if not events:
            return {
                "recent_actions": [],
                "recent_events": [],
                "event_count": 0,
                "last_action": None,
                "action_sequence": "",
                "unique_actions": [],
                "active_app": None,
                "active_title": None,
            }

        newest = to_aware(events[-1].timestamp)
        recent_events = [
            (e.action, (newest - to_aware(e.timestamp)).total_seconds()) for e in events
        ]

        active_app: str | None = None
        active_title: str | None = None
        for event in reversed(events):
            meta = event.metadata or {}
            if active_app is None and meta.get("app"):
                active_app = str(meta["app"])
            if active_title is None and meta.get("window"):
                active_title = str(meta["window"])
            if active_app is not None and active_title is not None:
                break

        return {
            "recent_actions": actions,
            "recent_events": recent_events,
            "event_count": len(events),
            "last_action": actions[-1],
            "action_sequence": "_".join(actions[-3:]),  # Last 3 actions
            "unique_actions": list(dict.fromkeys(actions)),
            "active_app": active_app,
            "active_title": active_title,
        }
