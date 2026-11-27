"""Integration tests for DBus IPC against a real daemon process.

These tests start ``python -m sage daemon`` as a subprocess on the current session
bus. CI runs them under ``dbus-run-session``; locally they are skipped when no session
bus is reachable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from sage.dbus_client import DBusClient, DBusClientError


def _session_bus_available() -> bool:
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    probe = (
        "import sys\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from PySide6.QtDBus import QDBusConnection\n"
        "app = QCoreApplication([])\n"
        "sys.exit(0 if QDBusConnection.sessionBus().isConnected() else 1)\n"
    )
    try:
        return subprocess.run([sys.executable, "-c", probe], timeout=20).returncode == 0
    except Exception:
        return False


# CI sets SHORTCUT_SAGE_REQUIRE_DBUS=1 so a broken bus fails the run instead of
# silently skipping every DBus test.
pytestmark = pytest.mark.skipif(
    not os.environ.get("SHORTCUT_SAGE_REQUIRE_DBUS") and not _session_bus_available(),
    reason="No DBus session bus (run under dbus-run-session)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with test configuration."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    (config_dir / "shortcuts.yaml").write_text(
        """version: "1.0"
shortcuts:
  - key: "Meta+D"
    action: "show_desktop"
    description: "Show desktop"
    category: "desktop"
  - key: "Meta+Tab"
    action: "overview"
    description: "Show overview"
    category: "desktop"
  - key: "Meta+Left"
    action: "tile_left"
    description: "Tile window left"
    category: "window"
  - key: "Meta+Right"
    action: "tile_right"
    description: "Tile window right"
    category: "window"
"""
    )
    (config_dir / "rules.yaml").write_text(
        """version: "1.0"
rules:
  - name: "after_show_desktop"
    context:
      type: "event_sequence"
      pattern: ["show_desktop"]
      window: 3
    suggest:
      - action: "overview"
        priority: 80
    cooldown: 300
  - name: "after_tile_left"
    context:
      type: "event_sequence"
      pattern: ["tile_left"]
      window: 5
    suggest:
      - action: "tile_right"
        priority: 85
    cooldown: 300
  - name: "terminal_only"
    context:
      type: "event_sequence"
      pattern: ["maximize"]
      window: 3
      app: "konsole"
    suggest:
      - action: "tile_left"
        priority: 70
    cooldown: 300
"""
    )
    return config_dir


def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def daemon_process(temp_config_dir: Path, tmp_path: Path):
    """Run the real daemon CLI as a subprocess on the session bus."""
    assert not DBusClient.is_daemon_running(), "another daemon already owns the bus name"
    log_dir = tmp_path / "logs"
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), QT_QPA_PLATFORM="offscreen")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sage",
            "daemon",
            "--config",
            str(temp_config_dir),
            "--log-dir",
            str(log_dir),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_until(lambda: DBusClient.is_daemon_running() or process.poll() is not None)
        if process.poll() is not None:
            out = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"daemon exited early ({process.returncode}):\n{out}")
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        _wait_until(lambda: not DBusClient.is_daemon_running())


@pytest.fixture
def dbus_client(daemon_process) -> DBusClient:  # noqa: ARG001 - fixture ensures daemon running
    """Create a DBus client for testing."""
    return DBusClient()


def _event(action: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "type": "test",
        "action": action,
        "metadata": {},
    }
    payload.update(extra)
    return payload


def test_ping(dbus_client: DBusClient) -> None:
    assert dbus_client.ping() == "pong"


def test_status(dbus_client: DBusClient, temp_config_dir: Path) -> None:
    status = dbus_client.get_status()
    assert status["dbus"] is True
    assert status["rules"] == 3
    assert status["shortcuts"] == 4
    assert status["config_dir"] == str(temp_config_dir)


def test_send_event_kwin_utc_timestamp(dbus_client: DBusClient) -> None:
    """KWin sends ISO timestamps ending in 'Z' - this used to crash the daemon."""
    dbus_client.send_event(_event("show_desktop"))
    buffer = dbus_client.get_buffer_state()
    assert [e["action"] for e in buffer] == ["show_desktop"]
    assert dbus_client.ping() == "pong"


def test_send_event_naive_and_offset_timestamps(dbus_client: DBusClient) -> None:
    """Naive local, offset and epoch-ms timestamps can be mixed in one buffer."""
    now = datetime.now()
    dbus_client.send_event(_event("tile_left", timestamp=now.isoformat()))
    dbus_client.send_event(
        _event("tile_right", timestamp=now.astimezone(timezone(timedelta(hours=-7))).isoformat())
    )
    dbus_client.send_event(_event("overview", timestamp=int(time.time() * 1000)))
    assert [e["action"] for e in dbus_client.get_buffer_state()] == [
        "tile_left",
        "tile_right",
        "overview",
    ]


def test_send_event_json_string(dbus_client: DBusClient) -> None:
    dbus_client.send_event(json.dumps(_event("tile_left")))
    buffer = dbus_client.get_buffer_state()
    assert len(buffer) == 1
    assert buffer[0]["action"] == "tile_left"


def test_window_title_metadata_reaches_daemon(dbus_client: DBusClient) -> None:
    """Window titles are required at runtime and must arrive intact."""
    dbus_client.send_event(
        _event(
            "window_focus",
            type="window_focus",
            metadata={"window": "notes.txt - Kate", "app": "org.kde.kate"},
        )
    )
    buffer = dbus_client.get_buffer_state()
    assert buffer[0]["metadata"] == {"window": "notes.txt - Kate", "app": "org.kde.kate"}


def test_send_event_malformed_json(dbus_client: DBusClient) -> None:
    dbus_client.send_event("{invalid json}")
    assert dbus_client.get_buffer_state() == []
    assert dbus_client.ping() == "pong"


def test_send_event_missing_fields(dbus_client: DBusClient) -> None:
    dbus_client.send_event({"timestamp": datetime.now(UTC).isoformat(), "type": "window_focus"})
    assert dbus_client.get_buffer_state() == []
    assert dbus_client.ping() == "pong"


def test_suggestions_signal(dbus_client: DBusClient) -> None:
    received: list[list[dict[str, Any]]] = []
    dbus_client.subscribe_suggestions(lambda payload: received.append(json.loads(payload)))

    dbus_client.send_event(_event("show_desktop"))
    deadline = time.monotonic() + 5
    while not received and time.monotonic() < deadline:
        dbus_client.process_events(0.05)

    assert received, "no Suggestions signal received"
    assert received[0] == [
        {"action": "overview", "key": "Meta+Tab", "description": "Show overview", "priority": 80}
    ]


def test_acceptance_clears_overlay(dbus_client: DBusClient) -> None:
    """Performing a suggested action marks it accepted and emits an empty list."""
    received: list[list[dict[str, Any]]] = []
    dbus_client.subscribe_suggestions(lambda payload: received.append(json.loads(payload)))

    dbus_client.send_event(_event("tile_left"))  # suggests tile_right
    dbus_client.send_event(_event("tile_right"))  # user follows the suggestion
    deadline = time.monotonic() + 5
    while len(received) < 2 and time.monotonic() < deadline:
        dbus_client.process_events(0.05)

    assert received[0][0]["action"] == "tile_right"
    assert received[-1] == []
    assert dbus_client.get_status()["pending_suggestions"] == []


def test_app_filtered_rule(dbus_client: DBusClient) -> None:
    """Rules can be restricted to an application via window metadata."""
    received: list[list[dict[str, Any]]] = []
    dbus_client.subscribe_suggestions(lambda payload: received.append(json.loads(payload)))

    dbus_client.send_event(_event("window_focus", type="window_focus", metadata={"app": "firefox"}))
    dbus_client.send_event(_event("maximize"))
    dbus_client.process_events(0.3)
    assert received == []

    dbus_client.send_event(_event("window_focus", type="window_focus", metadata={"app": "konsole"}))
    dbus_client.send_event(_event("maximize"))
    deadline = time.monotonic() + 5
    while not received and time.monotonic() < deadline:
        dbus_client.process_events(0.05)
    assert received and received[0][0]["action"] == "tile_left"


def test_multiple_events_sequence(dbus_client: DBusClient) -> None:
    for action in ["show_desktop", "tile_left", "tile_right"]:
        dbus_client.send_event(_event(action))
    assert len(dbus_client.get_buffer_state()) == 3


def test_second_daemon_refuses_to_start(
    daemon_process,  # noqa: ARG001
    temp_config_dir: Path,
    tmp_path: Path,
) -> None:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sage",
            "daemon",
            "--config",
            str(temp_config_dir),
            "--log-dir",
            str(tmp_path / "logs2"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "another Shortcut Sage daemon" in result.stdout + result.stderr


def test_daemon_is_running_without_daemon() -> None:
    assert DBusClient.is_daemon_running() is False


def test_client_errors_when_daemon_missing() -> None:
    with pytest.raises(DBusClientError) as exc_info:
        DBusClient()
    assert exc_info.value.error_name == "org.freedesktop.DBus.Error.NameHasNoOwner"


def test_sigterm_shuts_down_cleanly(daemon_process) -> None:
    daemon_process.terminate()
    assert daemon_process.wait(timeout=10) == 0
