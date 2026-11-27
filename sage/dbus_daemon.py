"""Shortcut Sage daemon: rules engine exposed on the session bus via QtDBus.

DBus contract (service/interface ``org.shortcutsage.Daemon``, path
``/org/shortcutsage/Daemon``):

* ``SendEvent(s event_json)`` - called by the KWin script for every desktop event.
* ``Ping() -> s`` - returns ``"pong"``.
* ``GetBufferState() -> s`` - JSON list of the events currently in the ring buffer
  (diagnostics/tests; includes metadata such as window titles).
* ``GetStatus() -> s`` - JSON object with daemon status.
* signal ``Suggestions(s suggestions_json)`` - JSON list of
  ``{action, key, description, priority}`` emitted whenever there is something new
  to show (and an empty list when a shown suggestion was accepted).

QtDBus ships with PySide6, so no native ``dbus-python``/PyGObject build is needed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sage.__version__ import __version__
from sage.buffer import RingBuffer
from sage.config import ConfigError, ConfigLoader, unknown_rule_actions
from sage.events import Event
from sage.features import CONTEXT_EVENT_TYPES, FeatureExtractor
from sage.matcher import RuleMatcher
from sage.policy import PolicyEngine, SuggestionResult
from sage.telemetry import EventType, init_telemetry, log_event
from sage.timeutil import now_utc, to_aware

logger = logging.getLogger(__name__)

BUS_NAME = "org.shortcutsage.Daemon"
OBJECT_PATH = "/org/shortcutsage/Daemon"
INTERFACE = "org.shortcutsage.Daemon"

# How long after being shown a suggestion counts as "accepted" if the user performs it.
DEFAULT_ACCEPTANCE_WINDOW = 30.0
MIN_BUFFER_WINDOW = 3.0

try:
    from PySide6.QtCore import ClassInfo, QCoreApplication, QObject, QTimer, Signal, Slot
    from PySide6.QtDBus import QDBusAbstractAdaptor, QDBusConnection

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 is a hard dependency
    DBUS_AVAILABLE = False


class DaemonError(RuntimeError):
    """Raised when the daemon cannot start (e.g. bus unavailable or name taken)."""


def default_data_dir() -> Path:
    """``$XDG_DATA_HOME/shortcut-sage`` (``~/.local/share/shortcut-sage``)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "shortcut-sage"


def suggestions_to_json(suggestions: list[SuggestionResult]) -> str:
    """Serialize suggestions using the public DBus JSON contract."""
    return json.dumps(
        [
            {
                "action": s.action,
                "key": s.key,
                "description": s.description,
                "priority": s.priority,
            }
            for s in suggestions
        ]
    )


class Daemon:
    """Shortcut Sage daemon (works with or without DBus)."""

    BUS_NAME = BUS_NAME
    OBJECT_PATH = OBJECT_PATH

    def __init__(
        self,
        config_dir: str | Path,
        enable_dbus: bool = True,
        log_events: bool = True,
        log_dir: str | Path | None = None,
        *,
        state_dir: str | Path | None = None,
        log_window_titles: bool = False,
        acceptance_window: float = DEFAULT_ACCEPTANCE_WINDOW,
    ) -> None:
        """Initialize the daemon.

        Args:
            config_dir: Directory with shortcuts.yaml and rules.yaml
            enable_dbus: Register on the session bus when :meth:`start` is called
            log_events: Log per-event processing details
            log_dir: Telemetry directory (default ``~/.local/share/shortcut-sage/logs``)
            state_dir: Where personalization state is stored (default: parent of log_dir)
            log_window_titles: Write window titles into telemetry (testing only)
            acceptance_window: Seconds during which performing a shown suggestion
                counts as accepting it
        """
        self.enable_dbus = enable_dbus and DBUS_AVAILABLE
        self.log_events = log_events
        self.log_window_titles = log_window_titles
        self.acceptance_window = acceptance_window

        self.log_dir = Path(log_dir) if log_dir is not None else default_data_dir() / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = Path(state_dir) if state_dir is not None else self.log_dir.parent
        self.state_file = self.state_dir / "personalization.json"

        self.telemetry = init_telemetry(self.log_dir, log_window_titles=log_window_titles)
        log_event(
            EventType.DAEMON_START,
            properties={
                "dbus_enabled": self.enable_dbus,
                "config_dir": str(config_dir),
                "version": __version__,
            },
        )

        self._lock = threading.RLock()

        # Load configuration (fail-soft per entry; structural errors raise ConfigError)
        self.config_loader = ConfigLoader(config_dir)
        self.shortcuts_config, self.rules_config = self.config_loader.reload()
        for warning in self.config_loader.warnings + unknown_rule_actions(
            self.shortcuts_config, self.rules_config
        ):
            logger.warning(warning)

        # Engine components
        self.rule_matcher = RuleMatcher(self.rules_config.rules)
        self.buffer = RingBuffer(window_seconds=self._buffer_window())
        self.feature_extractor = FeatureExtractor(self.buffer)
        self.policy_engine = PolicyEngine({s.action: s for s in self.shortcuts_config.shortcuts})
        self._load_state()

        # action -> (rule name, time shown) for suggestions currently on screen
        self._pending: dict[str, tuple[str, datetime]] = {}

        self._setup_config_reload()

        self.suggestions_callback: Callable[[list[SuggestionResult]], None] | None = None
        self._dbus_object: Any = None
        self._dbus_adaptor: Any = None
        self._dbus_connection: Any = None
        self._started = False

        logger.info(
            "Daemon initialized (DBus: %s, rules: %d, shortcuts: %d)",
            self.enable_dbus,
            len(self.rules_config.rules),
            len(self.shortcuts_config.shortcuts),
        )

    # ------------------------------------------------------------------ helpers
    def _buffer_window(self) -> float:
        return max(MIN_BUFFER_WINDOW, float(self.rule_matcher.max_window))

    def _buffer_snapshot(self) -> list[dict[str, Any]]:
        """Return the ring buffer contents as JSON-serializable dictionaries."""
        with self._lock:
            events = self.buffer.recent()
        return [
            {
                "action": event.action,
                "type": event.type,
                "timestamp": to_aware(event.timestamp).isoformat(),
                "metadata": dict(event.metadata or {}),
            }
            for event in events
        ]

    def get_buffer_state_json(self) -> str:
        """JSON list of buffered events (diagnostics)."""
        return json.dumps(self._buffer_snapshot(), default=str)

    def get_status(self) -> dict[str, Any]:
        """Daemon status summary."""
        with self._lock:
            return {
                "version": __version__,
                "dbus": self._dbus_object is not None,
                "rules": len(self.rule_matcher.rules),
                "shortcuts": len(self.policy_engine.shortcuts),
                "buffer_events": len(self.buffer),
                "buffer_window": self.buffer.window_seconds,
                "pending_suggestions": sorted(self._pending),
                "config_dir": str(self.config_loader.config_dir),
                "log_dir": str(self.log_dir),
            }

    # ------------------------------------------------------------------ persistence
    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.policy_engine.import_state(state)
            logger.info("Loaded personalization state from %s", self.state_file)
        except Exception as e:  # corrupted state must never prevent startup
            logger.warning("Ignoring unreadable personalization state %s: %s", self.state_file, e)

    def save_state(self) -> None:
        """Persist personalization data (atomic write)."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".json.tmp")
            with self._lock:
                payload = json.dumps(self.policy_engine.export_state(), indent=2)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            logger.warning("Could not save personalization state: %s", e)

    # ------------------------------------------------------------------ config reload
    def _setup_config_reload(self) -> None:
        """Set up configuration hot-reload."""
        from sage.watcher import ConfigWatcher

        self.watcher = ConfigWatcher(self.config_loader.config_dir, self.reload_config)

    def reload_config(self, filename: str) -> bool:
        """Reload one config file; keeps the previous config if the new one is invalid."""
        try:
            if filename == "shortcuts.yaml":
                shortcuts_config = self.config_loader.load_shortcuts()
                with self._lock:
                    self.shortcuts_config = shortcuts_config
                    self.policy_engine.shortcuts = {s.action: s for s in shortcuts_config.shortcuts}
            elif filename == "rules.yaml":
                rules_config = self.config_loader.load_rules()
                with self._lock:
                    self.rules_config = rules_config
                    self.rule_matcher = RuleMatcher(rules_config.rules)
                    self.buffer.set_window(self._buffer_window())
            else:
                return False
        except ConfigError as e:
            logger.error("Failed to reload %s (keeping previous config): %s", filename, e)
            log_event(
                EventType.ERROR_OCCURRED,
                properties={"error": str(e), "error_type": "ConfigError", "file": filename},
            )
            return False

        for warning in self.config_loader.warnings + unknown_rule_actions(
            self.shortcuts_config, self.rules_config
        ):
            logger.warning(warning)
        logger.info("Reloaded config: %s", filename)
        log_event(EventType.CONFIG_RELOAD, properties={"file": filename})
        return True

    # ------------------------------------------------------------------ events
    def send_event(self, event_json: str) -> str:
        """Process one event and return the suggestions JSON.

        Raises:
            ValueError: for malformed JSON or invalid event payloads (the DBus layer
                catches this, logs it and keeps running).
        """
        start_time = time.perf_counter()

        try:
            event = Event.from_dict(json.loads(event_json))
            now = now_utc()

            with self._lock:
                self.buffer.add(event)
                accepted = self._check_acceptance(event, now)

                features = self.feature_extractor.extract()
                matches = self.rule_matcher.match(features)
                suggestions = self.policy_engine.apply(
                    matches, now=now, exclude_actions={event.action}
                )
                for s in suggestions:
                    self._pending[s.action] = (s.rule, now)

            processing_time = time.perf_counter() - start_time
            latency = (now - to_aware(event.timestamp)).total_seconds()

            if self.log_events:
                logger.info(
                    "Event processed: %s -> %d suggestions (processing %.1f ms, latency %.1f ms)",
                    event.action,
                    len(suggestions),
                    processing_time * 1000,
                    latency * 1000,
                )
                if self.log_window_titles and event.metadata:
                    logger.info("Event metadata: %s", event.metadata)

            properties: dict[str, Any] = {
                "action": event.action,
                "type": event.type,
                "suggestions_count": len(suggestions),
                "processing_time": processing_time,
                "latency": latency,
            }
            if event.metadata:
                properties["metadata"] = event.metadata  # titles redacted unless enabled
            log_event(EventType.EVENT_RECEIVED, duration=processing_time, properties=properties)

            for suggestion in suggestions:
                log_event(
                    EventType.SUGGESTION_SHOWN,
                    properties={
                        "action": suggestion.action,
                        "key": suggestion.key,
                        "priority": suggestion.priority,
                        "adjusted_priority": suggestion.adjusted_priority,
                        "rule": suggestion.rule,
                    },
                )

            if accepted:
                self.save_state()

            # Only push to the overlay when there is something new to show, or to clear
            # it after the user accepted a suggestion. Empty results (e.g. focus changes
            # or cooled-down rules) leave the current suggestions alone; the overlay
            # hides them on its own timeout.
            return self.emit_suggestions(suggestions, force=bool(accepted))

        except Exception as e:
            processing_time = time.perf_counter() - start_time
            logger.error("Error processing event: %s", e)
            log_event(
                EventType.ERROR_OCCURRED,
                duration=processing_time,
                properties={"error": str(e), "error_type": type(e).__name__},
            )
            raise

    def _check_acceptance(self, event: Event, now: datetime) -> list[str]:
        """Mark shown suggestions as accepted when the user performs them."""
        # Drop expired pending suggestions
        expired = [
            action
            for action, (_, shown_at) in self._pending.items()
            if (now - shown_at).total_seconds() > self.acceptance_window
        ]
        for action in expired:
            del self._pending[action]

        if event.type in CONTEXT_EVENT_TYPES or event.action not in self._pending:
            return []

        rule_name, shown_at = self._pending.pop(event.action)
        self.policy_engine.mark_accepted(event.action, rule_name, now=now)
        log_event(
            EventType.SUGGESTION_ACCEPTED,
            properties={
                "action": event.action,
                "rule": rule_name,
                "seconds_after_shown": (now - shown_at).total_seconds(),
            },
        )
        logger.info("Suggestion accepted: %s (rule %s)", event.action, rule_name)
        # Anything else that was on screen is now stale
        self._pending.clear()
        return [event.action]

    def ping(self) -> str:
        """Simple ping method to check if daemon is alive."""
        return "pong"

    def emit_suggestions(self, suggestions: list[SuggestionResult], *, force: bool = False) -> str:
        """Emit suggestions on DBus and/or the in-process callback; return the JSON."""
        suggestions_json = suggestions_to_json(suggestions)
        if not suggestions and not force:
            return suggestions_json

        logger.debug("Emitting suggestions: %s", suggestions_json)
        if self._dbus_adaptor is not None:
            self._dbus_adaptor.Suggestions.emit(suggestions_json)
        if self.suggestions_callback is not None:
            self.suggestions_callback(suggestions)
        return suggestions_json

    def set_suggestions_callback(self, callback: Callable[[list[SuggestionResult]], None]) -> None:
        """Set an in-process callback for suggestions (fallback mode, demos, tests)."""
        self.suggestions_callback = callback

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the config watcher and (optionally) register on the session bus.

        Raises:
            DaemonError: if DBus is enabled but the bus is unavailable or another
                daemon already owns ``org.shortcutsage.Daemon``.
        """
        if self._started:
            return
        if self.enable_dbus:
            self._register_dbus()
        self.watcher.start()
        self._started = True
        logger.info("Daemon started (%s)", BUS_NAME if self.enable_dbus else "fallback mode")

    def stop(self) -> None:
        """Stop the daemon and persist state."""
        if self._dbus_connection is not None:
            self._dbus_connection.unregisterObject(OBJECT_PATH)
            self._dbus_connection.unregisterService(BUS_NAME)
        self._dbus_connection = None
        self._dbus_adaptor = None
        self._dbus_object = None
        if self._started:
            self.watcher.stop()
        self._started = False
        self.save_state()
        log_event(EventType.DAEMON_STOP)
        logger.info("Daemon stopped")

    def _register_dbus(self) -> None:
        if QCoreApplication.instance() is None:
            # QtDBus delivers calls through the Qt event loop.
            self._qt_app = QCoreApplication([sys.argv[0] if sys.argv else "shortcut-sage"])

        connection = QDBusConnection.sessionBus()
        if not connection.isConnected():
            raise DaemonError(
                "Cannot connect to the DBus session bus "
                f"({connection.lastError().message() or 'no DBUS_SESSION_BUS_ADDRESS?'})"
            )

        obj = QObject()
        adaptor = _DaemonAdaptor(obj, self)
        if not connection.registerObject(OBJECT_PATH, obj):
            raise DaemonError(f"Could not register DBus object {OBJECT_PATH}")
        if not connection.registerService(BUS_NAME):
            connection.unregisterObject(OBJECT_PATH)
            raise DaemonError(
                f"Could not claim {BUS_NAME} - is another Shortcut Sage daemon running? "
                f"({connection.lastError().message()})"
            )
        self._dbus_connection = connection
        self._dbus_object = obj
        self._dbus_adaptor = adaptor


if DBUS_AVAILABLE:

    @ClassInfo({"D-Bus Interface": INTERFACE})  # type: ignore[call-arg]
    class _DaemonAdaptor(QDBusAbstractAdaptor):  # type: ignore[operator]
        """Exports the daemon on DBus."""

        Suggestions = Signal(str)

        def __init__(self, parent: QObject, daemon: Daemon) -> None:
            super().__init__(parent)
            self._daemon = daemon

        @Slot(str)
        def SendEvent(self, event_json: str) -> None:  # noqa: N802 - DBus naming
            # Errors are already logged; a bad payload must never kill the daemon.
            with contextlib.suppress(Exception):
                self._daemon.send_event(event_json)

        @Slot(result=str)
        def Ping(self) -> str:  # noqa: N802 - DBus naming
            return self._daemon.ping()

        @Slot(result=str)
        def GetBufferState(self) -> str:  # noqa: N802 - DBus naming
            return self._daemon.get_buffer_state_json()

        @Slot(result=str)
        def GetStatus(self) -> str:  # noqa: N802 - DBus naming
            return json.dumps(self._daemon.get_status())


def run_daemon(
    config_dir: str,
    log_dir: str | None = None,
    *,
    enable_dbus: bool | None = None,
    log_events: bool = True,
    log_window_titles: bool = False,
) -> int:
    """Run the daemon until SIGINT/SIGTERM. Returns a process exit code."""
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

    app = QCoreApplication.instance() or QCoreApplication(
        [sys.argv[0] if sys.argv else "shortcut-sage"]
    )
    effective_dbus = DBUS_AVAILABLE if enable_dbus is None else enable_dbus

    try:
        daemon = Daemon(
            config_dir,
            enable_dbus=effective_dbus,
            log_events=log_events,
            log_dir=log_dir,
            log_window_titles=log_window_titles,
        )
        daemon.start()
    except (ConfigError, DaemonError) as e:
        logger.error("%s", e)
        return 1

    def _signal_handler(signum: int, frame: Any) -> None:
        logger.info("Received signal %s, shutting down daemon", signum)
        app.quit()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Let the Python interpreter run signal handlers while Qt's loop is blocking.
    heartbeat = QTimer()
    heartbeat.start(250)
    heartbeat.timeout.connect(lambda: None)

    logger.info(
        "Daemon running (%s). Press Ctrl+C to stop.",
        "DBus" if daemon.enable_dbus else "fallback",
    )
    try:
        app.exec()
    finally:
        heartbeat.stop()
        daemon.stop()
    return 0


def main() -> None:
    """Legacy CLI entry point for direct module execution."""
    if len(sys.argv) not in [2, 3]:
        print("Usage: python -m sage.dbus_daemon <config_dir> [log_dir]")
        sys.exit(1)

    config_dir = sys.argv[1]
    log_dir = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(run_daemon(config_dir, log_dir))


if __name__ == "__main__":
    main()
