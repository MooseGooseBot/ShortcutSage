"""DBus client for talking to the Shortcut Sage daemon (QtDBus based)."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from sage.dbus_daemon import BUS_NAME, INTERFACE, OBJECT_PATH

logger = logging.getLogger(__name__)

try:
    from PySide6.QtCore import SLOT, QCoreApplication, QObject, Slot
    from PySide6.QtDBus import QDBus, QDBusConnection, QDBusInterface, QDBusMessage

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - PySide6 is a hard dependency
    DBUS_AVAILABLE = False


class DBusClientError(RuntimeError):
    """Raised when a DBus call fails (daemon not running, bus unavailable, ...)."""

    def __init__(self, message: str, error_name: str = "") -> None:
        super().__init__(f"{error_name}: {message}" if error_name else message)
        self.error_name = error_name


def _ensure_app() -> Any:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([sys.argv[0] if sys.argv else "shortcut-sage-client"])
    return app


if DBUS_AVAILABLE:

    class _SignalReceiver(QObject):
        def __init__(self, callback: Callable[[str], None]) -> None:
            super().__init__()
            self._callback = callback

        @Slot(str)
        def on_suggestions(self, payload: str) -> None:
            try:
                self._callback(payload)
            except Exception as e:  # never propagate into Qt
                logger.error("Suggestions callback failed: %s", e)


class DBusClient:
    """Client for the ``org.shortcutsage.Daemon`` service."""

    BUS_NAME = BUS_NAME
    OBJECT_PATH = OBJECT_PATH
    INTERFACE = INTERFACE

    def __init__(self, timeout_ms: int = 5000) -> None:
        """Connect to the session bus.

        Raises:
            DBusClientError: if the session bus is unavailable or no daemon is running.
        """
        if not DBUS_AVAILABLE:
            raise DBusClientError("QtDBus not available")

        self._app = _ensure_app()
        self.bus = QDBusConnection.sessionBus()
        if not self.bus.isConnected():
            raise DBusClientError(
                self.bus.lastError().message() or "Cannot connect to the session bus",
                "org.freedesktop.DBus.Error.NoServer",
            )
        if not self.is_daemon_running():
            raise DBusClientError(
                f"The name {BUS_NAME} is not owned (daemon not running)",
                "org.freedesktop.DBus.Error.NameHasNoOwner",
            )
        self.interface = QDBusInterface(BUS_NAME, OBJECT_PATH, INTERFACE, self.bus)
        self.interface.setTimeout(timeout_ms)
        self._receivers: list[Any] = []

    def _call(self, method: str, *args: Any) -> Any:
        reply = self.interface.call(method, *args)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            raise DBusClientError(reply.errorMessage(), reply.errorName())
        values = reply.arguments()
        return values[0] if values else None

    def send_event(self, event_json: str | dict[str, Any]) -> None:
        """Send an event to the daemon (dicts are serialized to JSON)."""
        if isinstance(event_json, dict):
            event_json = json.dumps(event_json)
        self._call("SendEvent", event_json)
        logger.debug("Sent event: %s", event_json)

    def send_event_async(self, event_json: str | dict[str, Any]) -> None:
        """Fire-and-forget variant (like KWin's ``callDBus``)."""
        if isinstance(event_json, dict):
            event_json = json.dumps(event_json)
        self.interface.call(QDBus.CallMode.NoBlock, "SendEvent", event_json)

    def ping(self) -> str:
        """Return ``"pong"`` if the daemon is alive."""
        return str(self._call("Ping"))

    def get_buffer_state(self) -> list[dict[str, Any]]:
        """Return the daemon's ring buffer contents."""
        payload = self._call("GetBufferState")
        result: list[dict[str, Any]] = json.loads(payload or "[]")
        return result

    def get_status(self) -> dict[str, Any]:
        """Return the daemon status object."""
        result: dict[str, Any] = json.loads(self._call("GetStatus") or "{}")
        return result

    def subscribe_suggestions(self, callback: Callable[[str], None]) -> None:
        """Call ``callback(suggestions_json)`` for every ``Suggestions`` signal.

        Signals are delivered by the Qt event loop; use :meth:`process_events` when
        not running ``app.exec()``.
        """
        receiver = _SignalReceiver(callback)
        # Match on path/interface rather than the well-known name: QtDBus' name-owner
        # cache goes stale when the daemon restarts while no event loop is running,
        # which silently drops signals.
        ok = self.bus.connect(  # type: ignore[call-overload]  # SLOT() is typed as str
            "",
            OBJECT_PATH,
            INTERFACE,
            "Suggestions",
            receiver,
            SLOT("on_suggestions(QString)"),
        )
        if not ok:
            raise DBusClientError("Could not subscribe to the Suggestions signal")
        self._receivers.append(receiver)

    def process_events(self, duration: float = 0.2) -> None:
        """Pump the Qt event loop for ``duration`` seconds."""
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self._app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def is_daemon_running() -> bool:
        """True if some process owns ``org.shortcutsage.Daemon`` on the session bus."""
        if not DBUS_AVAILABLE:
            return False
        _ensure_app()
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        reply = bus.interface().isServiceRegistered(BUS_NAME)
        return bool(reply.isValid() and reply.value())
