"""Regression tests for bugs found in the review (timezones, cooldowns, acceptance,
redaction, fail-soft config, persistence, overlay auto-hide, CLI)."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from sage.buffer import RingBuffer
from sage.config import ConfigError, ConfigLoader, unknown_rule_actions
from sage.events import Event, EventValidationError
from sage.features import FeatureExtractor
from sage.matcher import RuleMatcher
from sage.models import ContextMatch, Rule, Shortcut, Suggestion
from sage.policy import PolicyEngine
from sage.telemetry import (
    REDACTED,
    EventType,
    LogRedactor,
    MetricsCollector,
    RotatingTelemetryLogger,
    shutdown_telemetry,
)
from sage.timeutil import now_utc, parse_timestamp, to_aware

# --------------------------------------------------------------------------- helpers


def _rule(
    name: str,
    pattern: str | list[str],
    suggest: list[tuple[str, int]],
    cooldown: int = 300,
    **ctx: Any,
) -> Rule:
    return Rule(
        name=name,
        context=ContextMatch(type="event_sequence", pattern=pattern, **ctx),
        suggest=[Suggestion(action=a, priority=p) for a, p in suggest],
        cooldown=cooldown,
    )


def _shortcuts(*actions: str) -> dict[str, Shortcut]:
    return {
        a: Shortcut(key=f"Meta+{i}", action=a, description=a.replace("_", " "))
        for i, a in enumerate(actions)
    }


def _event(action: str, ts: datetime | None = None, type_: str = "test", **meta: Any) -> Event:
    return Event(timestamp=ts or now_utc(), type=type_, action=action, metadata=meta or None)


SHORTCUTS_YAML = """version: "1.0"
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
    description: "Tile window to left"
    category: "window"
  - key: "Meta+Right"
    action: "tile_right"
    description: "Tile window to right"
    category: "window"
"""

RULES_YAML = """version: "1.0"
rules:
  - name: "after_show_desktop"
    context:
      type: "event_sequence"
      pattern: ["show_desktop"]
      window: 3
    suggest:
      - action: "overview"
        priority: 80
      - action: "tile_left"
        priority: 60
    cooldown: 300
  - name: "after_tile_left"
    context:
      type: "event_sequence"
      pattern: ["tile_left"]
      window: 5
    suggest:
      - action: "tile_right"
        priority: 85
      - action: "show_desktop"
        priority: 50
    cooldown: 180
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "shortcuts.yaml").write_text(SHORTCUTS_YAML)
    (d / "rules.yaml").write_text(RULES_YAML)
    return d


@pytest.fixture
def daemon(config_dir: Path, tmp_path: Path):
    from sage.dbus_daemon import Daemon

    d = Daemon(
        str(config_dir),
        enable_dbus=False,
        log_events=True,
        log_dir=tmp_path / "logs",
        state_dir=tmp_path / "state",
    )
    yield d
    d.stop()
    shutdown_telemetry()


def _send(daemon: Any, action: str, ts: datetime | None = None, type_: str = "test", **meta: Any):
    payload = {
        "timestamp": (ts or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "type": type_,
        "action": action,
        "metadata": meta,
    }
    return json.loads(daemon.send_event(json.dumps(payload)))


# --------------------------------------------------------------------------- timeutil


class TestTimeutil:
    def test_naive_is_treated_as_local(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0, 0)
        aware = to_aware(naive)
        assert aware.tzinfo is not None
        assert aware.replace(tzinfo=None) == naive

    def test_aware_unchanged(self) -> None:
        dt = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5)))
        assert to_aware(dt) is dt

    @pytest.mark.parametrize(
        "value",
        [
            "2026-01-01T12:00:00Z",
            "2026-01-01T12:00:00.123z",
            "2026-01-01T14:00:00+02:00",
            1767268800,
            1767268800000,
            1767268800.5,
        ],
    )
    def test_parse_formats(self, value: Any) -> None:
        parsed = parse_timestamp(value)
        assert parsed.tzinfo is not None
        assert abs((parsed - datetime(2026, 1, 1, 12, tzinfo=UTC)).total_seconds()) < 1

    def test_parse_datetime_and_naive_string(self) -> None:
        assert parse_timestamp(datetime(2026, 1, 1)).tzinfo is not None
        assert parse_timestamp("2026-01-01T12:00:00").tzinfo is not None

    @pytest.mark.parametrize("value", [True, None, "not a date", [1]])
    def test_parse_invalid(self, value: Any) -> None:
        with pytest.raises((ValueError, TypeError)):
            parse_timestamp(value)


# --------------------------------------------------------------------------- events


class TestEventValidation:
    def test_from_dict_normalises(self) -> None:
        e = Event.from_dict(
            {"action": "  Show_Desktop ", "type": " test ", "timestamp": "2026-01-01T00:00:00Z"}
        )
        assert e.action == "show_desktop"
        assert e.type == "test"
        assert e.timestamp.tzinfo is not None

    def test_missing_timestamp_defaults_to_now(self) -> None:
        e = Event.from_dict({"action": "x"})
        assert abs((now_utc() - e.timestamp).total_seconds()) < 5
        assert e.type == "unknown"

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"type": "test"},
            {"action": ""},
            {"action": "x", "type": 5},
            {"action": "x", "timestamp": "yesterday"},
            {"action": "x", "metadata": "nope"},
        ],
    )
    def test_invalid_payloads(self, payload: Any) -> None:
        with pytest.raises(EventValidationError):
            Event.from_dict(payload)

    def test_age_mixed_naive_and_aware(self) -> None:
        e = _event("x", ts=datetime.now(UTC) - timedelta(seconds=2))
        assert 1.5 < e.age_seconds(datetime.now()) < 3


# --------------------------------------------------------------------------- buffer / features / matcher


class TestBufferAndMatcher:
    def test_mixed_timezone_events_do_not_crash(self) -> None:
        buf = RingBuffer(window_seconds=5)
        buf.add(_event("a", ts=datetime.now()))
        buf.add(_event("b", ts=datetime.now(UTC)))
        assert buf.actions() == ["a", "b"]

    def test_out_of_order_events_sorted(self) -> None:
        buf = RingBuffer(window_seconds=10)
        now = now_utc()
        buf.add(_event("late", ts=now))
        buf.add(_event("early", ts=now - timedelta(seconds=1)))
        assert buf.actions() == ["early", "late"]

    def test_set_window_validates(self) -> None:
        buf = RingBuffer(window_seconds=3)
        buf.set_window(7)
        assert buf.window_seconds == 7
        with pytest.raises(ValueError):
            buf.set_window(0)

    def test_old_events_pruned(self) -> None:
        buf = RingBuffer(window_seconds=2)
        buf.add(_event("old", ts=now_utc() - timedelta(seconds=10)))
        buf.add(_event("new"))
        assert buf.actions() == ["new"]
        buf.clear()
        assert len(buf) == 0

    def test_features_active_window(self) -> None:
        buf = RingBuffer(window_seconds=5)
        buf.add(_event("window_focus", type_="window_focus", app="konsole", window="~ : bash"))
        buf.add(_event("tile_left"))
        f = FeatureExtractor(buf).extract()
        assert f["active_app"] == "konsole"
        assert f["active_title"] == "~ : bash"
        assert f["last_action"] == "tile_left"

    def test_per_rule_window(self) -> None:
        buf = RingBuffer(window_seconds=10)
        now = now_utc()
        buf.add(_event("show_desktop", ts=now - timedelta(seconds=4)))
        buf.add(_event("window_focus", ts=now))
        features = FeatureExtractor(buf).extract()
        short = _rule("short", "show_desktop", [("overview", 50)], window=3)
        long = _rule("long", "show_desktop", [("overview", 50)], window=5)
        matcher = RuleMatcher([short, long])
        assert matcher.max_window == 5
        assert [r.name for r, _ in matcher.match(features)] == ["long"]

    def test_app_and_title_filters(self) -> None:
        rule = _rule(
            "kate", "tile_left", [("tile_right", 50)], app="^org\\.kde\\.kate$", title="\\.py"
        )
        matcher = RuleMatcher([rule])
        base = {"recent_actions": ["tile_left"]}
        assert matcher.match({**base, "active_app": "org.kde.kate", "active_title": "a.py"})
        assert not matcher.match({**base, "active_app": "org.kde.kate", "active_title": "a.txt"})
        assert not matcher.match({**base, "active_app": "konsole", "active_title": "a.py"})
        assert not matcher.match(base)

    def test_invalid_regex_rejected(self) -> None:
        with pytest.raises(ValueError):
            ContextMatch(type="event_sequence", pattern="x", app="(")


# --------------------------------------------------------------------------- policy


class TestPolicy:
    def test_excludes_action_just_performed(self) -> None:
        engine = PolicyEngine(_shortcuts("show_desktop", "overview"))
        rule = _rule("r", "show_desktop", [("show_desktop", 90), ("overview", 50)])
        results = engine.apply([(rule, s) for s in rule.suggest], exclude_actions={"show_desktop"})
        assert [r.action for r in results] == ["overview"]

    def test_cooldown_only_for_shown_suggestions(self) -> None:
        engine = PolicyEngine(_shortcuts("a", "b", "c", "d"))
        rule = _rule("r", "x", [("a", 90), ("b", 80), ("c", 70), ("d", 10)])
        matches = [(rule, s) for s in rule.suggest]
        first = engine.apply(matches, top_n=3)
        assert [r.action for r in first] == ["a", "b", "c"]
        second = engine.apply(matches, top_n=3)
        assert [r.action for r in second] == ["d"]  # d was not cooled down

    def test_best_entry_per_action_and_rule_name(self) -> None:
        engine = PolicyEngine(_shortcuts("a"))
        r1 = _rule("r1", "x", [("a", 10)])
        r2 = _rule("r2", "x", [("a", 90)])
        results = engine.apply([(r1, r1.suggest[0]), (r2, r2.suggest[0])])
        assert len(results) == 1
        assert results[0].rule == "r2"

    def test_unknown_action_skipped(self) -> None:
        engine = PolicyEngine(_shortcuts("a"))
        rule = _rule("r", "x", [("missing", 99), ("a", 1)])
        assert [r.action for r in engine.apply([(rule, s) for s in rule.suggest])] == ["a"]

    def test_state_roundtrip_and_personalization(self) -> None:
        engine = PolicyEngine(_shortcuts("a", "b"))
        rule = _rule("r", "x", [("a", 50), ("b", 50)], cooldown=0)
        matches = [(rule, s) for s in rule.suggest]
        start = now_utc()
        for i in range(6):
            engine.apply(matches, now=start + timedelta(seconds=i))
            engine.mark_accepted("a", "r", now=start + timedelta(seconds=i))
        state = engine.export_state()
        json.dumps(state)  # serializable

        restored = PolicyEngine(_shortcuts("a", "b"))
        restored.import_state(state)
        assert restored.get_acceptance_count("a") == 6
        results = restored.apply(matches, now=start + timedelta(seconds=10))
        by_action = {r.action: r.adjusted_priority for r in results}
        assert by_action["a"] > by_action["b"]

    def test_import_state_tolerates_garbage(self) -> None:
        engine = PolicyEngine(_shortcuts("a"))
        engine.import_state({"accepted": "nope", "personalization": {"x": 5}})
        assert engine.get_acceptance_count("a") == 0


# --------------------------------------------------------------------------- config


class TestConfigFailSoft:
    def test_invalid_entries_are_skipped_with_warnings(self, config_dir: Path) -> None:
        (config_dir / "shortcuts.yaml").write_text(
            SHORTCUTS_YAML
            + """  - key: ""
    action: "broken"
    description: "x"
    category: "x"
  - key: "Meta+X"
    action: "overview"
    description: "dup"
    category: "x"
"""
        )
        loader = ConfigLoader(config_dir)
        shortcuts, rules = loader.reload()
        assert {s.action for s in shortcuts.shortcuts} == {
            "show_desktop",
            "overview",
            "tile_left",
            "tile_right",
        }
        assert len(loader.warnings) == 2
        assert len(rules.rules) == 2

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "just a string",
            'version: "2.0"\nshortcuts: []',
            'version: "1.0"\nshortcuts: 5',
            ": : :",
        ],
    )
    def test_structural_errors_raise(self, config_dir: Path, content: str) -> None:
        (config_dir / "shortcuts.yaml").write_text(content)
        with pytest.raises(ConfigError):
            ConfigLoader(config_dir).load_shortcuts()

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            ConfigLoader(tmp_path).load_rules()

    def test_unknown_rule_actions(self, config_dir: Path) -> None:
        (config_dir / "rules.yaml").write_text(
            RULES_YAML.replace('action: "tile_right"', 'action: "does_not_exist"')
        )
        shortcuts, rules = ConfigLoader(config_dir).reload()
        warnings = unknown_rule_actions(shortcuts, rules)
        assert len(warnings) == 1
        assert "does_not_exist" in warnings[0]


# --------------------------------------------------------------------------- telemetry


class TestTelemetry:
    def test_redactor_masks_titles_recursively(self) -> None:
        r = LogRedactor()
        data = {
            "action": "x",
            "metadata": {"window": "Secret", "app": "kate"},
            "list": [{"title": "t"}],
        }
        out = r.redact(data)
        assert out["metadata"] == {"window": REDACTED, "app": "kate"}
        assert out["list"] == [{"title": REDACTED}]
        assert data["metadata"]["window"] == "Secret"  # input untouched
        assert LogRedactor(enabled=False).redact(data) is data

    def test_titles_not_written_unless_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SHORTCUT_SAGE_LOG_WINDOW_TITLES", raising=False)
        logger = RotatingTelemetryLogger(tmp_path / "a")
        logger.log_event(
            EventType.EVENT_RECEIVED, properties={"metadata": {"window": "Secret doc"}}
        )
        logger.close()
        assert "Secret doc" not in (tmp_path / "a" / "telemetry.ndjson").read_text()

        logger = RotatingTelemetryLogger(tmp_path / "b", log_window_titles=True)
        logger.log_event(
            EventType.EVENT_RECEIVED, properties={"metadata": {"window": "Secret doc"}}
        )
        logger.close()
        assert "Secret doc" in (tmp_path / "b" / "telemetry.ndjson").read_text()

    def test_env_var_enables_title_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHORTCUT_SAGE_LOG_WINDOW_TITLES", "1")
        logger = RotatingTelemetryLogger(tmp_path)
        assert logger.log_window_titles
        logger.close()

    def test_export_metrics_does_not_deadlock(self) -> None:
        metrics = MetricsCollector()
        metrics.increment_counter("c")
        metrics.record_timing("t", 0.5)
        result: dict[str, Any] = {}
        thread = threading.Thread(
            target=lambda: result.update(metrics.export_metrics()), daemon=True
        )
        thread.start()
        thread.join(5)
        assert not thread.is_alive(), "export_metrics deadlocked"
        assert result["counters"]["c"] == 1

    def test_histograms_are_bounded(self) -> None:
        metrics = MetricsCollector()
        for i in range(10050):
            metrics.record_timing("t", float(i))
        assert len(metrics.histograms["t"]) == 10000
        stats = metrics.get_histogram_stats("t")
        assert stats["count"] == 10000


# --------------------------------------------------------------------------- daemon (in-process)


class TestDaemon:
    def test_kwin_utc_timestamp_accepted(self, daemon: Any) -> None:
        result = _send(daemon, "show_desktop")
        assert [s["action"] for s in result] == ["overview", "tile_left"]

    def test_does_not_suggest_performed_action(self, daemon: Any) -> None:
        result = _send(daemon, "tile_left")
        assert "tile_left" not in [s["action"] for s in result]

    def test_invalid_json_raises_value_error(self, daemon: Any) -> None:
        with pytest.raises(ValueError):
            daemon.send_event("{not json")
        with pytest.raises(ValueError):
            daemon.send_event(json.dumps({"type": "test"}))
        assert daemon.ping() == "pong"

    def test_acceptance_detection_and_persistence(self, daemon: Any, tmp_path: Path) -> None:
        received: list[list[Any]] = []
        daemon.set_suggestions_callback(lambda s: received.append(list(s)))
        _send(daemon, "show_desktop")
        assert daemon.get_status()["pending_suggestions"] == ["overview", "tile_left"]

        # focus changes don't count as accepting
        _send(daemon, "window_focus", type_="window_focus", app="kate", window="x")
        assert daemon.get_status()["pending_suggestions"] == ["overview", "tile_left"]

        _send(daemon, "tile_left")  # user performs a suggested action
        assert daemon.policy_engine.get_acceptance_count("tile_left") == 1
        # the stale "overview" suggestion is replaced by the follow-up rule's ones
        assert [s.action for s in received[-1]] == ["tile_right", "show_desktop"]
        assert daemon.get_status()["pending_suggestions"] == ["show_desktop", "tile_right"]

        state = json.loads((tmp_path / "state" / "personalization.json").read_text())
        assert state["accepted"] == {"tile_left": 1}

    def test_state_restored_on_startup(self, config_dir: Path, tmp_path: Path) -> None:
        from sage.dbus_daemon import Daemon

        state_dir = tmp_path / "state2"
        state_dir.mkdir()
        (state_dir / "personalization.json").write_text(json.dumps({"accepted": {"overview": 3}}))
        d = Daemon(str(config_dir), enable_dbus=False, log_dir=tmp_path / "l2", state_dir=state_dir)
        assert d.policy_engine.get_acceptance_count("overview") == 3
        d.stop()

    def test_corrupt_state_ignored(self, config_dir: Path, tmp_path: Path) -> None:
        from sage.dbus_daemon import Daemon

        state_dir = tmp_path / "state3"
        state_dir.mkdir()
        (state_dir / "personalization.json").write_text("{garbage")
        d = Daemon(str(config_dir), enable_dbus=False, log_dir=tmp_path / "l3", state_dir=state_dir)
        assert d.ping() == "pong"
        d.stop()

    def test_acceptance_window_expires(self, daemon: Any) -> None:
        old = datetime.now(UTC)
        _send(daemon, "show_desktop", ts=old)
        assert daemon.get_status()["pending_suggestions"]
        daemon.acceptance_window = 0
        time.sleep(0.01)
        _send(daemon, "tile_left")
        assert daemon.policy_engine.get_acceptance_count("tile_left") == 0

    def test_acceptance_without_follow_up_clears_overlay(self, daemon: Any) -> None:
        received: list[Any] = []
        daemon.set_suggestions_callback(lambda s: received.append(list(s)))
        _send(daemon, "show_desktop")
        _send(daemon, "overview")  # accepted; no rule follows "overview"
        assert received[-1] == []
        assert daemon.get_status()["pending_suggestions"] == []

    def test_empty_results_do_not_clear_overlay(self, daemon: Any) -> None:
        received: list[Any] = []
        daemon.set_suggestions_callback(lambda s: received.append(list(s)))
        _send(daemon, "show_desktop")
        _send(daemon, "window_focus", type_="window_focus")
        assert len(received) == 1

    def test_window_titles_not_logged_by_default(self, daemon: Any, tmp_path: Path) -> None:
        _send(daemon, "show_desktop", window="Top secret.odt", app="libreoffice")
        shutdown_telemetry()
        text = (tmp_path / "logs" / "telemetry.ndjson").read_text()
        assert "Top secret.odt" not in text
        assert "libreoffice" in text

    def test_buffer_state_and_status(self, daemon: Any) -> None:
        _send(daemon, "show_desktop", app="kate")
        buffer = json.loads(daemon.get_buffer_state_json())
        assert buffer[0]["action"] == "show_desktop"
        assert buffer[0]["metadata"] == {"app": "kate"}
        status = daemon.get_status()
        assert status["rules"] == 2
        assert status["shortcuts"] == 4
        assert status["dbus"] is False
        assert status["buffer_window"] == 5

    def test_reload_keeps_old_config_on_error(self, daemon: Any, config_dir: Path) -> None:
        (config_dir / "rules.yaml").write_text("version: '1.0'\nrules: 7")
        assert daemon.reload_config("rules.yaml") is False
        assert len(daemon.rule_matcher.rules) == 2

        (config_dir / "rules.yaml").write_text(RULES_YAML.split('  - name: "after_tile_left"')[0])
        assert daemon.reload_config("rules.yaml") is True
        assert len(daemon.rule_matcher.rules) == 1
        assert daemon.buffer.window_seconds == 3

        assert daemon.reload_config("shortcuts.yaml") is True
        assert daemon.reload_config("other.yaml") is False

    def test_logging_window_titles_when_enabled(
        self, config_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sage.dbus_daemon import Daemon

        d = Daemon(
            str(config_dir),
            enable_dbus=False,
            log_events=True,
            log_dir=tmp_path / "l4",
            state_dir=tmp_path / "s4",
            log_window_titles=True,
        )
        with caplog.at_level(logging.INFO, logger="sage.dbus_daemon"):
            _send(d, "show_desktop", window="Debug title")
        d.stop()
        shutdown_telemetry()
        assert "Debug title" in (tmp_path / "l4" / "telemetry.ndjson").read_text()
        assert "Debug title" in caplog.text


# --------------------------------------------------------------------------- overlay


class TestOverlay:
    @pytest.fixture
    def overlay(self):
        from PySide6.QtWidgets import QApplication

        from sage.overlay import OverlayWindow

        assert QApplication.instance() is not None
        w = OverlayWindow(dbus_available=False, timeout_seconds=0.05)
        yield w
        w.close()

    def test_auto_hide(self, overlay: Any) -> None:
        from PySide6.QtWidgets import QApplication

        overlay.show()
        overlay.set_suggestions_fallback(
            [{"key": "Meta+D", "description": "Show desktop", "priority": 80}]
        )
        assert overlay.hide_timer.isActive()
        deadline = time.time() + 3
        while overlay.chips and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert overlay.chips == []
        assert overlay.isVisible()  # stays mapped so KWin keeps its placement

    def test_max_three_and_malformed_skipped(self, overlay: Any) -> None:
        overlay.on_suggestions(
            json.dumps(
                [
                    {"description": "no key"},
                    {"key": "A", "description": "a", "priority": "high"},
                    {"key": "B", "description": "b", "priority": 1},
                    {"key": "C", "description": "c", "priority": 1},
                    {"key": "D", "description": "d", "priority": 1},
                    {"key": "E", "description": "e", "priority": 1},
                ]
            )
        )
        assert [c.key for c in overlay.chips] == ["B", "C", "D"]

    def test_bad_payload_ignored(self, overlay: Any) -> None:
        overlay.on_suggestions("{}")
        overlay.on_suggestions("not json")
        assert overlay.chips == []

    def test_empty_list_stops_timer(self, overlay: Any) -> None:
        overlay.set_suggestions_fallback([{"key": "A", "description": "a", "priority": 1}])
        overlay.set_suggestions_fallback([])
        assert not overlay.hide_timer.isActive()
        assert overlay.chips == []

    def test_window_identity(self, overlay: Any) -> None:
        from sage.overlay import OVERLAY_WINDOW_TITLE

        assert overlay.windowTitle() == OVERLAY_WINDOW_TITLE


# --------------------------------------------------------------------------- CLI


class TestCli:
    def test_init_config_writes_defaults(self, tmp_path: Path) -> None:
        from sage.__main__ import main

        target = tmp_path / "cfg"
        assert main(["init-config", "--config", str(target)]) == 0
        assert (target / "shortcuts.yaml").exists()
        assert (target / "rules.yaml").exists()
        ConfigLoader(target).reload()  # bundled defaults are valid

        (target / "rules.yaml").write_text("custom")
        main(["init-config", "--config", str(target)])
        assert (target / "rules.yaml").read_text() == "custom"
        main(["init-config", "--config", str(target), "--force"])
        assert (target / "rules.yaml").read_text() != "custom"

    def test_requires_subcommand(self) -> None:
        from sage.__main__ import main

        with pytest.raises(SystemExit):
            main([])
