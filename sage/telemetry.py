"""Observability and hardening components for Shortcut Sage."""

import json
import logging
import logging.handlers
import os
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any


class EventType(Enum):
    """Types of events to track."""

    EVENT_RECEIVED = "event_received"
    SUGGESTION_SHOWN = "suggestion_shown"
    SUGGESTION_ACCEPTED = "suggestion_accepted"
    DAEMON_START = "daemon_start"
    DAEMON_STOP = "daemon_stop"
    CONFIG_RELOAD = "config_reload"
    ERROR_OCCURRED = "error_occurred"


@dataclass
class TelemetryEvent:
    """A telemetry event with timing and context."""

    event_type: EventType
    timestamp: datetime
    duration: float | None = None  # For timing measurements
    properties: dict[str, Any] | None = None  # Additional context
    redacted: bool = False  # Whether PII has been redacted


class MetricsCollector:
    """Collects and aggregates metrics for observability."""

    def __init__(self) -> None:
        self._lock = threading.RLock()  # re-entrant: export_metrics calls other getters
        self.counters: dict[str, int] = defaultdict(int)
        # Bounded so a long-running daemon does not grow without limit
        self.histograms: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10000))
        self.events: deque[TelemetryEvent] = deque(
            maxlen=10000
        )  # Circular buffer for recent events
        self.start_time = datetime.now()

    def increment_counter(self, name: str, value: int = 1) -> None:
        """Increment a counter."""
        with self._lock:
            self.counters[name] += value

    def record_timing(self, name: str, duration: float) -> None:
        """Record a timing measurement."""
        with self._lock:
            self.histograms[name].append(duration)

    def record_event(self, event: TelemetryEvent) -> None:
        """Record a telemetry event."""
        with self._lock:
            self.events.append(event)

    def get_counter(self, name: str) -> int:
        """Get the current value of a counter."""
        with self._lock:
            return self.counters[name]

    def get_histogram_stats(self, name: str) -> dict[str, float]:
        """Get statistics for a histogram."""
        with self._lock:
            values = list(self.histograms.get(name, ()))
            if not values:
                return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0}

            count = len(values)
            avg = sum(values) / count
            min_val = min(values)
            max_val = max(values)

            return {"count": count, "avg": avg, "min": min_val, "max": max_val}

    def get_uptime(self) -> timedelta:
        """Get the uptime of the collector."""
        return datetime.now() - self.start_time

    def export_metrics(self) -> dict[str, Any]:
        """Export all metrics for reporting."""
        with self._lock:
            return {
                "uptime": self.get_uptime().total_seconds(),
                "counters": dict(self.counters),
                "histograms": {name: self.get_histogram_stats(name) for name in self.histograms},
                "event_count": len(self.events),
            }

    def reset_counters(self) -> None:
        """Reset all counters (useful for testing)."""
        with self._lock:
            self.counters.clear()
            for hist in self.histograms.values():
                hist.clear()


# Metadata keys that may contain window titles / document names.
SENSITIVE_KEYS = frozenset({"window", "title", "caption", "window_title", "document", "url"})
REDACTED = "[redacted]"
LOG_WINDOW_TITLES_ENV = "SHORTCUT_SAGE_LOG_WINDOW_TITLES"


def window_title_logging_from_env() -> bool:
    """True if window-title logging was requested via environment (testing only)."""
    return os.environ.get(LOG_WINDOW_TITLES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


class LogRedactor:
    """Removes window titles (and similar) from data before it is written to logs.

    Window titles are *required* at runtime (rules can match on them), so they are
    always sent to the daemon and kept in memory. They are only kept out of log files.
    Set ``enabled=False`` (``--log-window-titles`` or
    ``SHORTCUT_SAGE_LOG_WINDOW_TITLES=1``) to log them for testing/debugging.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def redact(self, value: Any) -> Any:
        """Return a copy of ``value`` with sensitive keys masked (recursively)."""
        if not self.enabled:
            return value
        if isinstance(value, dict):
            return {
                k: (REDACTED if str(k).lower() in SENSITIVE_KEYS and v else self.redact(v))
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.redact(v) for v in value]
        return value


class RotatingTelemetryLogger:
    """Telemetry logger (NDJSON) with rotation and redaction."""

    def __init__(
        self,
        log_dir: str | Path,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        log_window_titles: bool = False,
    ):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Set up the NDJSON log file with rotation
        self.log_file = self.log_dir / "telemetry.ndjson"
        self.handler = logging.handlers.RotatingFileHandler(
            self.log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))

        # Dedicated logger; drop handlers left over from a previous instance so lines
        # are never duplicated (e.g. several daemons created in one process).
        self.logger = logging.getLogger("shortcut_sage.telemetry")
        for old in list(self.logger.handlers):
            self.logger.removeHandler(old)
            old.close()
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        self.logger.propagate = False

        self.log_window_titles = log_window_titles or window_title_logging_from_env()
        self.redactor = LogRedactor(enabled=not self.log_window_titles)
        self.metrics = MetricsCollector()

    def log_event(
        self,
        event_type: EventType,
        duration: float | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Log an event with timing and properties."""
        safe_properties = self.redactor.redact(properties or {})
        event = TelemetryEvent(
            event_type=event_type,
            timestamp=datetime.now(),
            duration=duration,
            properties=safe_properties,
            redacted=self.redactor.enabled,
        )

        # Record in metrics
        self.metrics.record_event(event)

        if duration is not None:
            self.metrics.record_timing(event_type.value, duration)

        self.metrics.increment_counter(event_type.value)

        log_entry = {
            "timestamp": event.timestamp.astimezone().isoformat(),
            "event_type": event_type.value,
            "duration": duration,
            "properties": safe_properties or None,
        }

        # Write as NDJSON (newline-delimited JSON)
        self.logger.info(json.dumps(log_entry, default=str))

    def log_error(self, error_msg: str, context: dict[str, Any] | None = None) -> None:
        """Log an error event."""
        self.log_event(
            EventType.ERROR_OCCURRED,
            properties={"error": error_msg, "context": context or {}},
        )

    def export_metrics(self) -> dict[str, Any]:
        """Export current metrics."""
        return self.metrics.export_metrics()

    def close(self) -> None:
        """Close the logger."""
        self.logger.removeHandler(self.handler)
        self.handler.close()


# Global telemetry instance
_telemetry_logger: RotatingTelemetryLogger | None = None


def init_telemetry(log_dir: str | Path, log_window_titles: bool = False) -> RotatingTelemetryLogger:
    """Initialize (or re-initialize) the global telemetry system."""
    global _telemetry_logger
    if _telemetry_logger is not None:
        _telemetry_logger.close()
    _telemetry_logger = RotatingTelemetryLogger(log_dir, log_window_titles=log_window_titles)
    return _telemetry_logger


def shutdown_telemetry() -> None:
    """Close the global telemetry logger (flushes the file handle)."""
    global _telemetry_logger
    if _telemetry_logger is not None:
        _telemetry_logger.close()
        _telemetry_logger = None


def get_telemetry() -> RotatingTelemetryLogger | None:
    """Get the global telemetry instance."""
    return _telemetry_logger


def log_event(
    event_type: EventType, duration: float | None = None, properties: dict[str, Any] | None = None
) -> None:
    """Log an event using the global telemetry logger."""
    telemetry = get_telemetry()
    if telemetry:
        telemetry.log_event(event_type, duration, properties)


def log_error(error_msg: str, context: dict[str, Any] | None = None) -> None:
    """Log an error using the global telemetry logger."""
    telemetry = get_telemetry()
    if telemetry:
        telemetry.log_error(error_msg, context)
