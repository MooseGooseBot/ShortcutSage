"""Ring buffer for time-windowed events."""

from __future__ import annotations

from collections import deque
from datetime import timedelta

from sage.events import Event
from sage.timeutil import to_aware


class RingBuffer:
    """Time-windowed event buffer with automatic pruning.

    The window is measured relative to the newest event. Timestamps may be naive or
    aware; comparisons are always done on aware values so mixed input never raises.
    """

    def __init__(self, window_seconds: float = 3.0, max_events: int = 256):
        """
        Initialize ring buffer.

        Args:
            window_seconds: Time window to keep events (default 3.0 seconds)
            max_events: Hard cap on stored events (protects against event floods)
        """
        if window_seconds <= 0:
            raise ValueError("Window size must be positive")
        if max_events <= 0:
            raise ValueError("max_events must be positive")

        self.window = timedelta(seconds=window_seconds)
        self._events: deque[Event] = deque(maxlen=max_events)

    @property
    def window_seconds(self) -> float:
        """Current window size in seconds."""
        return self.window.total_seconds()

    def set_window(self, window_seconds: float) -> None:
        """Change the window size (used when rules are reloaded)."""
        if window_seconds <= 0:
            raise ValueError("Window size must be positive")
        self.window = timedelta(seconds=window_seconds)
        self._prune()

    def add(self, event: Event) -> None:
        """
        Add event to buffer and prune old events.

        Events are kept ordered by timestamp even if they arrive slightly out of order.
        """
        ts = to_aware(event.timestamp)
        if self._events and ts < to_aware(self._events[-1].timestamp):
            items = list(self._events)
            idx = len(items)
            while idx > 0 and to_aware(items[idx - 1].timestamp) > ts:
                idx -= 1
            items.insert(idx, event)
            self._events.clear()
            self._events.extend(items[-(self._events.maxlen or len(items)) :])
        else:
            self._events.append(event)
        self._prune()

    def _prune(self) -> None:
        """Remove events outside the time window."""
        if not self._events:
            return

        cutoff = to_aware(self._events[-1].timestamp) - self.window

        while self._events and to_aware(self._events[0].timestamp) <= cutoff:
            self._events.popleft()

    def recent(self) -> list[Event]:
        """
        Get all events in current window.

        Returns:
            List of recent events (oldest to newest)
        """
        self._prune()
        return list(self._events)

    def actions(self) -> list[str]:
        """
        Get sequence of recent action IDs.

        Returns:
            List of action IDs from recent events
        """
        return [e.action for e in self.recent()]

    def clear(self) -> None:
        """Clear all events from buffer."""
        self._events.clear()

    def __len__(self) -> int:
        """Get number of events in buffer."""
        return len(self._events)

    @property
    def events(self) -> list[Event]:
        """Backward-compatible accessor for tests/diagnostics."""
        return self.recent()
