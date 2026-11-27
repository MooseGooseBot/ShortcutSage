"""Checks for the KWin script package (kwin/shortcut-sage)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sage.config import ConfigLoader
from sage.overlay import OVERLAY_APP_ID, OVERLAY_WINDOW_TITLE

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "kwin" / "shortcut-sage"
MAIN_JS = PACKAGE / "contents" / "code" / "main.js"


def test_metadata_is_valid_kpackage() -> None:
    metadata = json.loads((PACKAGE / "metadata.json").read_text())
    assert metadata["KPackageStructure"] == "KWin/Script"
    assert metadata["X-Plasma-API"] == "javascript"
    assert metadata["X-Plasma-MainScript"] == "code/main.js"
    assert metadata["KPlugin"]["Id"] == PACKAGE.name
    assert MAIN_JS.exists()


def test_script_targets_daemon_interface() -> None:
    from sage.dbus_daemon import BUS_NAME, INTERFACE, OBJECT_PATH

    src = MAIN_JS.read_text()
    assert f'"{BUS_NAME}"' in src
    assert f'"{OBJECT_PATH}"' in src
    assert f'"{INTERFACE}"' in src
    assert '"SendEvent"' in src


def test_script_recognises_overlay_window() -> None:
    src = MAIN_JS.read_text()
    assert f'"{OVERLAY_APP_ID}"' in src
    assert f'"{OVERLAY_WINDOW_TITLE}"' in src


def test_default_rules_are_triggerable() -> None:
    """Every action a default rule listens for is either emitted by the KWin script or
    is a known action that can only come from elsewhere (e.g. `shortcut-sage send`)."""
    src = MAIN_JS.read_text()
    loader = ConfigLoader(ROOT / "config")
    rules = loader.load_rules()
    not_from_kwin = {"overview", "application_launcher", "screenshot_full"}
    for rule in rules.rules:
        patterns = rule.context.pattern
        for action in [patterns] if isinstance(patterns, str) else patterns:
            if action in not_from_kwin:
                continue
            assert f'"{action}"' in src, f"rule {rule.name} waits for {action}, never emitted"


def test_packaged_default_config_matches_repo_config() -> None:
    for name in ("shortcuts.yaml", "rules.yaml"):
        assert (ROOT / "sage" / "data" / name).read_text() == (ROOT / "config" / name).read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_kwin_script_behaviour_under_node() -> None:
    result = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "kwin" / "main.test.js")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
