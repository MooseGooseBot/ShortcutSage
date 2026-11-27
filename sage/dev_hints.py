"""Developer hints panel: live view of a running daemon.

Runs as a separate process, so it cannot read the daemon's in-memory metrics. Instead
it polls the daemon over DBus (``GetStatus`` / ``GetBufferState``) and tails the
daemon's telemetry file (``~/.local/share/shortcut-sage/logs/telemetry.ndjson``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def default_log_file() -> Path:
    from sage.dbus_daemon import default_data_dir

    return default_data_dir() / "logs" / "telemetry.ndjson"


def read_recent_telemetry(log_file: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return the last ``limit`` telemetry entries (malformed lines skipped)."""
    if not log_file.exists():
        return []
    entries: deque[dict[str, Any]] = deque(maxlen=limit)
    with open(log_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return list(entries)


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, acceptance rate and average processing time from telemetry entries."""
    counts = Counter(str(e.get("event_type", "unknown")) for e in entries)
    durations = [
        float(e["duration"])
        for e in entries
        if e.get("event_type") == "event_received" and isinstance(e.get("duration"), int | float)
    ]
    shown = counts.get("suggestion_shown", 0)
    accepted = counts.get("suggestion_accepted", 0)
    return {
        "events": counts.get("event_received", 0),
        "shown": shown,
        "accepted": accepted,
        "errors": counts.get("error_occurred", 0),
        "acceptance_rate": (accepted / shown) if shown else 0.0,
        "avg_processing_ms": (sum(durations) / len(durations) * 1000) if durations else 0.0,
    }


def format_entry(entry: dict[str, Any]) -> str:
    ts = str(entry.get("timestamp", ""))
    time_part = ts[11:23] if len(ts) >= 23 else ts
    line = f"[{time_part}] {entry.get('event_type', '?')}"
    if isinstance(entry.get("duration"), int | float):
        line += f" ({entry['duration'] * 1000:.1f} ms)"
    props = entry.get("properties")
    if props:
        line += f" {json.dumps(props, default=str)}"
    return line


class DevHintsPanel(QWidget):
    """Developer debugging panel showing the daemon's state and recent activity."""

    def __init__(self, log_file: Path | None = None, refresh_ms: int = 2000) -> None:
        super().__init__()
        self.log_file = log_file or default_log_file()
        self._client: Any = None
        self.setup_ui()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        if refresh_ms > 0:
            self.refresh_timer.start(refresh_ms)
        self.refresh_data()

    def setup_ui(self) -> None:
        self.setWindowTitle("Shortcut Sage - Dev Hints")
        self.resize(900, 650)
        layout = QVBoxLayout(self)

        title = QLabel("Shortcut Sage - Developer Hints")
        font = QFont()
        font.setBold(True)
        font.setPointSize(14)
        title.setFont(font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        def section(label: str, max_height: int | None = None) -> QTextEdit:
            box = QTextEdit()
            box.setReadOnly(True)
            box.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            if max_height:
                box.setMaximumHeight(max_height)
            layout.addWidget(QLabel(label))
            layout.addWidget(box)
            return box

        self.stats_text = section("Daemon status & statistics:", 170)
        self.buffer_text = section("Event buffer (what rules currently see):", 130)
        self.suggestions_trace = section("Recent suggestions:", 130)
        self.events_trace = section(f"Recent telemetry ({self.log_file}):")

        controls = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_data)
        controls.addWidget(refresh)
        controls.addStretch()
        layout.addLayout(controls)

    def _daemon(self) -> Any:
        if self._client is None:
            from sage.dbus_client import DBusClient

            self._client = DBusClient(timeout_ms=1000)
        return self._client

    def refresh_data(self) -> None:
        status: dict[str, Any] | None = None
        buffer: list[dict[str, Any]] = []
        error = ""
        try:
            client = self._daemon()
            status = client.get_status()
            buffer = client.get_buffer_state()
        except Exception as e:  # daemon not running / no bus
            self._client = None
            error = str(e)

        entries = read_recent_telemetry(self.log_file)
        stats = summarize(entries)

        lines = []
        if status is not None:
            lines.append(
                f"Daemon: running (v{status.get('version')}), rules={status.get('rules')}, "
                f"shortcuts={status.get('shortcuts')}, buffer window={status.get('buffer_window')}s"
            )
            lines.append(
                f"On screen now: {', '.join(status.get('pending_suggestions', [])) or '-'}"
            )
            lines.append(f"Config: {status.get('config_dir')}")
        else:
            lines.append(f"Daemon: NOT reachable ({error or 'unknown error'})")
            lines.append("Start it with: shortcut-sage daemon")
        lines.append("")
        lines.append(
            f"Last {len(entries)} log entries: {stats['events']} events, {stats['shown']} "
            f"suggestions shown, {stats['accepted']} accepted "
            f"({stats['acceptance_rate']:.0%}), {stats['errors']} errors, "
            f"avg processing {stats['avg_processing_ms']:.2f} ms"
        )
        if stats["events"] and not stats["shown"]:
            lines.append(
                "Hint: events arrive but nothing is suggested - check rules.yaml patterns."
            )
        if status is not None and not stats["events"]:
            lines.append("Hint: no events logged - is the KWin script installed and enabled?")
        self.stats_text.setPlainText("\n".join(lines))

        self.buffer_text.setPlainText(
            "\n".join(
                f"{e.get('timestamp', '')[11:19]} {e.get('type')}/{e.get('action')} "
                f"{json.dumps(e.get('metadata') or {})}"
                for e in buffer
            )
            or "(empty)"
        )
        self.suggestions_trace.setPlainText(
            "\n".join(
                format_entry(e)
                for e in reversed(entries)
                if e.get("event_type") in ("suggestion_shown", "suggestion_accepted")
            )[:20000]
        )
        self.events_trace.setPlainText(
            "\n".join(format_entry(e) for e in reversed(entries[-50:])) or "(no telemetry yet)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev-hints")
    parser.add_argument("--log-file", type=Path, default=None, help="telemetry.ndjson to read")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv)
    panel = DevHintsPanel(args.log_file)
    panel.show()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
