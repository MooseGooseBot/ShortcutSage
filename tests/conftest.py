"""Shared pytest fixtures."""

import os
from pathlib import Path

import pytest

# Headless Qt for every test, and one QApplication shared by the whole session.
# It has to exist before anything creates a plain QCoreApplication (e.g. the DBus
# client), otherwise later widget tests abort.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    _QT_APP = QApplication.instance() or QApplication(["shortcut-sage-tests"])
except Exception:  # pragma: no cover - Qt unavailable
    _QT_APP = None


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Temporary config directory for tests."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def sample_shortcuts_yaml(tmp_config_dir: Path) -> Path:
    """Create a sample shortcuts.yaml file."""
    shortcuts_file = tmp_config_dir / "shortcuts.yaml"
    shortcuts_file.write_text("""version: "1.0"
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
""")
    return shortcuts_file


@pytest.fixture
def sample_rules_yaml(tmp_config_dir: Path) -> Path:
    """Create a sample rules.yaml file."""
    rules_file = tmp_config_dir / "rules.yaml"
    rules_file.write_text("""version: "1.0"
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
        priority: 50
    cooldown: 300
""")
    return rules_file
