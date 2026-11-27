"""Tests for the developer tools (dev-audit, dev-hints)."""

from __future__ import annotations

import json
from pathlib import Path

from sage.audit import TelemetryBatchProcessor

ENTRIES = [
    {"timestamp": "2026-01-01T10:00:00+00:00", "event_type": "event_received", "duration": 0.002},
    {
        "timestamp": "2026-01-01T10:00:01+00:00",
        "event_type": "suggestion_shown",
        "properties": {"action": "overview"},
    },
    {
        "timestamp": "2026-01-01T10:00:02",
        "event_type": "suggestion_accepted",
        "properties": {"action": "overview"},
    },
    {"timestamp": "2026-01-01T12:00:00Z", "event_type": "error_occurred"},
]


def _write_logs(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "telemetry.ndjson").write_text(
        "\n".join(json.dumps(e) for e in ENTRIES[2:]) + "\nnot json\n"
    )
    # rotated backup file name used by RotatingFileHandler
    (log_dir / "telemetry.ndjson.1").write_text("\n".join(json.dumps(e) for e in ENTRIES[:2]))


def test_audit_reads_rotated_files_and_mixed_timestamps(tmp_path: Path) -> None:
    _write_logs(tmp_path)
    report = TelemetryBatchProcessor(tmp_path).generate_report()
    assert report.summary["total_events"] == 4
    assert report.summary["suggestions_accepted"] == 1
    assert report.summary["acceptance_rate"] == 1.0
    assert report.summary["time_range"]["duration_hours"] > 1
    text = TelemetryBatchProcessor(tmp_path).generate_dev_report()
    assert "High error rate" in text


def test_audit_empty(tmp_path: Path) -> None:
    report = TelemetryBatchProcessor(tmp_path).generate_report()
    assert report.issues == ["No telemetry data found"]


def test_dev_hints_summary_and_panel_without_daemon(tmp_path: Path) -> None:
    from sage.dev_hints import DevHintsPanel, read_recent_telemetry, summarize

    _write_logs(tmp_path)
    entries = read_recent_telemetry(tmp_path / "telemetry.ndjson")
    assert len(entries) == 2  # malformed line skipped
    stats = summarize(ENTRIES)
    assert stats == {
        "events": 1,
        "shown": 1,
        "accepted": 1,
        "errors": 1,
        "acceptance_rate": 1.0,
        "avg_processing_ms": 2.0,
    }

    panel = DevHintsPanel(tmp_path / "telemetry.ndjson", refresh_ms=0)
    assert "suggestion_accepted" in panel.suggestions_trace.toPlainText()
    assert "Daemon" in panel.stats_text.toPlainText()
    panel.close()
