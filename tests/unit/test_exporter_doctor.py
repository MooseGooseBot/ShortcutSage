"""Tests for the KDE shortcut exporter and the doctor command."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sage.config import ConfigLoader
from sage.exporter import ShortcutExporter, main, parse_kglobalshortcutsrc

KGLOBALSHORTCUTSRC = r"""[ActivityManager]
_k_friendly_name=Activity Manager
switch-to-activity-123=none,none,Switch to activity "Default"

[kwin]
_k_friendly_name=KWin
Show Desktop=Meta+F12,Meta+D,Peek at Desktop
Overview=Meta+W,Meta+W,Toggle Overview
Window Quick Tile Left=Meta+Left,Meta+Left,Quick Tile Window to the Left
Window Quick Tile Right=Meta+Right\tCtrl+Alt+Right,Meta+Right,Quick Tile Window to the Right
Switch to Next Desktop=none,,Switch to Next Desktop
Switch One Desktop to the Right=Meta+Ctrl+Right,Meta+Ctrl+Right,Switch One Desktop to the Right
Window Maximize=Meta+PgUp,Meta+PgUp,Maximize Window
Kill Window=Meta+Ctrl+Esc,Meta+Ctrl+Esc,Kill Window
Weird Comma=Meta+\,,none,Comma shortcut

[plasmashell]
activate application launcher=Meta\tAlt+F1,Meta\tAlt+F1,Activate Application Launcher

[services][org.kde.krunner.desktop]
_launch=Alt+Space\tAlt+F2\tSearch

[org.kde.spectacle.desktop]
RectangularRegionScreenShot=Meta+Shift+Print,Meta+Shift+Print,Capture Rectangular Region
"""


@pytest.fixture
def rcfile(tmp_path: Path) -> Path:
    path = tmp_path / "kglobalshortcutsrc"
    path.write_text(KGLOBALSHORTCUTSRC)
    return path


def test_parse_handles_tabs_none_escapes_and_nested_groups() -> None:
    parsed = parse_kglobalshortcutsrc(KGLOBALSHORTCUTSRC)
    assert parsed[("kwin", "Show Desktop")] == ("Meta+F12", "Peek at Desktop")
    assert parsed[("kwin", "Window Quick Tile Right")][0] == "Meta+Right"
    assert parsed[("kwin", "Switch to Next Desktop")][0] is None
    assert parsed[("kwin", "Weird Comma")][0] == "Meta+,"
    assert parsed[("plasmashell", "activate application launcher")][0] == "Meta"
    assert parsed[("org.kde.krunner.desktop", "_launch")][0] == "Alt+Space"
    assert ("kwin", "_k_friendly_name") not in parsed


def test_discover_maps_to_sage_actions(rcfile: Path) -> None:
    found = {d.action_id: d for d in ShortcutExporter(rcfile).discover_shortcuts()}
    assert found["show_desktop"].key_sequence == "Meta+F12"  # user's rebinding wins
    assert found["switch_desktop_next"].key_sequence == "Meta+Ctrl+Right"  # fallback action
    assert found["krunner"].key_sequence == "Alt+Space"
    assert found["screenshot_region"].key_sequence == "Meta+Shift+Print"
    assert "kill_window" not in found
    assert "tile_top" not in found  # not bound in this file


def test_discover_all_includes_unmapped(rcfile: Path) -> None:
    found = {
        d.action_id for d in ShortcutExporter(rcfile).discover_shortcuts(include_unmapped=True)
    }
    assert "kwin_kill_window" in found
    assert not any(a.startswith("activitymanager") for a in found)  # unbound


def test_export_writes_loadable_yaml(rcfile: Path, tmp_path: Path) -> None:
    out = tmp_path / "cfg" / "shortcuts.yaml"
    assert main(["--source", str(rcfile), str(out)]) == 0
    data = yaml.safe_load(out.read_text())
    actions = {s["action"]: s["key"] for s in data["shortcuts"]}
    assert actions["show_desktop"] == "Meta+F12"


def test_export_without_kde_config_fails(tmp_path: Path) -> None:
    assert main(["--source", str(tmp_path / "missing"), str(tmp_path / "out.yaml")]) == 1


def test_sync_updates_keys_preserving_comments(rcfile: Path, tmp_path: Path) -> None:
    repo_shortcuts = Path(__file__).resolve().parents[2] / "config" / "shortcuts.yaml"
    target = tmp_path / "config" / "shortcuts.yaml"
    target.parent.mkdir()
    target.write_text(repo_shortcuts.read_text())
    (target.parent / "rules.yaml").write_text((repo_shortcuts.parent / "rules.yaml").read_text())

    assert main(["--source", str(rcfile), "--sync", str(target)]) == 0
    text = target.read_text()
    assert "# Shortcut Sage - Shortcuts Configuration" in text
    shortcuts = {s.action: s.key for s in ConfigLoader(target.parent).load_shortcuts().shortcuts}
    assert shortcuts["show_desktop"] == "Meta+F12"
    assert shortcuts["overview"] == "Meta+W"
    assert shortcuts["close_window"] == "Alt+F4"  # untouched (not in rc file)

    # second run is a no-op
    changes = ShortcutExporter(rcfile).sync_yaml(target)
    assert changes == []


def test_sync_missing_file(rcfile: Path, tmp_path: Path) -> None:
    assert main(["--source", str(rcfile), "--sync", str(tmp_path / "nope.yaml")]) == 1


def test_doctor_runs_and_fixes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from sage import doctor

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    config = tmp_path / "cfg"
    rc = doctor.main(["--config", str(config), "--fix"])
    out = capsys.readouterr().out
    assert rc == 1  # no Plasma session in the test environment
    assert (config / "shortcuts.yaml").exists()
    assert "✓ Config is valid" in out
    assert "KWin script installed" in out


def test_doctor_detects_enabled_kwin_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sage import doctor

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "kwinrc").write_text("[Plugins]\nshortcut-sageEnabled=true\n")
    script_dir = tmp_path / "data" / "kwin" / "scripts" / "shortcut-sage"
    script_dir.mkdir(parents=True)
    (script_dir / "metadata.json").write_text("{}")
    results = dict((name, ok) for name, ok, _ in doctor.check_kwin_script())
    assert results == {"KWin script installed": True, "KWin script enabled": True}
