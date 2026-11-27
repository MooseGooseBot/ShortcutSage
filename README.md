# Shortcut Sage

> Context-aware keyboard shortcut suggestions for KDE Plasma 6

[![CI](https://github.com/MooseGooseBot/ShortcutSage/actions/workflows/ci.yml/badge.svg)](https://github.com/MooseGooseBot/ShortcutSage/actions/workflows/ci.yml)
[![Visual Tests](https://github.com/MooseGooseBot/ShortcutSage/actions/workflows/visual-tests.yml/badge.svg)](https://github.com/MooseGooseBot/ShortcutSage/actions/workflows/visual-tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

**Shortcut Sage** watches what you do on your desktop (show the desktop, tile a window,
switch virtual desktops…) and suggests the next keyboard shortcut that would help, in a
small overlay in the top-left corner. Think of it as "autocomplete for your hands."

## Features

- **Context-aware suggestions**: up to 3 shortcuts based on your recent actions, the
  focused application and window title
- **Learns from you**: performing a suggested action within 30 s counts as accepting
  it, and suggestions you act on rank higher (persisted across restarts). KWin reports
  *what* happened, not *how*, so doing the action with the mouse also counts.
- **Config-driven**: rules live in YAML and hot-reload while the daemon runs
- **Uses your real bindings**: `export-shortcuts --sync` copies your actual KDE
  shortcuts into the config
- **Minimal UI**: small always-on-top overlay that never takes focus and fades out
  after 8 seconds
- **No keylogging**: only symbolic window-manager events, all processed locally

## Architecture

```
KWin script ──SendEvent──▶ Daemon (ring buffer → features → rules → policy) ──Suggestions──▶ Overlay
            (DBus, session bus: org.shortcutsage.Daemon)
```

| Component | Path | What it does |
|-----------|------|--------------|
| KWin script | `kwin/shortcut-sage/` | Reports desktop actions with the focused window's title and app class, and pins the overlay top-left |
| Daemon | `sage/dbus_daemon.py` | Matches recent actions against `rules.yaml`, applies cooldowns and personalization, emits suggestions |
| Overlay | `sage/overlay.py` | PySide6 window showing suggestion chips |

The DBus layer uses **QtDBus from PySide6**, so there is no native `dbus-python` or
PyGObject to build.

## Requirements

- KDE Plasma 6 (Wayland or X11)
- Python 3.11+
- PySide6 ≥ 6.6, pydantic, PyYAML, watchdog (installed automatically)

## Installation

```bash
git clone https://github.com/MooseGooseBot/ShortcutSage.git
cd ShortcutSage

# 1. Install the Python package (pipx keeps it isolated; pip install --user . works too)
pipx install .

# 2. Install and enable the KWin script
scripts/install-kwin-script.sh

# 3. Optional: copy your real KDE shortcut bindings into the config
shortcut-sage init-config
export-shortcuts --sync ~/.config/shortcut-sage/shortcuts.yaml

# 4. Start everything now and on every login (systemd user units; --xdg for autostart files)
scripts/install-autostart.sh
```

To run it by hand instead of step 4:

```bash
shortcut-sage daemon     # creates ~/.config/shortcut-sage with defaults on first run
shortcut-sage overlay    # in a second terminal
```

Check your setup with `shortcut-sage-doctor`.

### Trying it out

- Peek at the desktop (**Meta+D** or the panel button): the overlay suggests Overview and tiling.
- Drag a window to the left screen edge: it suggests **Meta+Right** to tile the next window.
- Press **Ctrl+Alt+S** to send a test event from the KWin script.
- Without KWin: `shortcut-sage send show_desktop`, then check `shortcut-sage status`.

## Configuration

The config lives in `~/.config/shortcut-sage/` and is reloaded automatically when you
edit it. If a single entry is invalid, it is skipped with a warning and the rest still
loads.

**shortcuts.yaml**: the shortcuts that can be suggested

```yaml
version: "1.0"
shortcuts:
  - key: "Meta+D"
    action: "show_desktop"
    description: "Peek at desktop"
    category: "desktop"
```

**rules.yaml**: when to suggest them

```yaml
version: "1.0"
rules:
  - name: "after_show_desktop"
    context:
      type: "event_sequence"
      pattern: ["show_desktop"]   # any of these actions...
      window: 3                   # ...within the last 3 seconds
      app: "^org\\.kde\\."        # optional: regex on the focused app class
      title: "\\.py"              # optional: regex on the focused window title
    suggest:
      - action: "overview"
        priority: 80
    cooldown: 300                 # seconds before this rule may show the same suggestion again
```

Actions reported by the KWin script: `window_focus`, `show_desktop`, `tile_left`,
`tile_right`, `tile_top`, `tile_bottom`, `tile_top_left`, `tile_top_right`,
`tile_bottom_left`, `tile_bottom_right`, `maximize`, `restore`, `minimize`,
`switch_desktop_next`, `switch_desktop_prev`, `switch_desktop`,
`move_window_left_desktop`, `move_window_right_desktop`, `krunner`, `test_shortcut`.

## Window titles and logging

Window titles **are** sent to the daemon, because rules can match on them. They are
**not** written to logs. To include them for testing and debugging:

```bash
shortcut-sage daemon --log-window-titles      # or SHORTCUT_SAGE_LOG_WINDOW_TITLES=1
kwriteconfig6 --file kwinrc --group Script-shortcut-sage --key debug true   # KWin journal output
```

Logs: `~/.local/share/shortcut-sage/logs/telemetry.ndjson` (rotated).
Personalization state: `~/.local/share/shortcut-sage/personalization.json`.

## Command reference

| Command | Purpose |
|---------|---------|
| `shortcut-sage daemon [--config DIR] [--log-dir DIR] [--log-window-titles] [--debug]` | Run the engine |
| `shortcut-sage overlay [--timeout SECONDS] [--demo]` | Run the overlay (`--timeout 0` = never hide) |
| `shortcut-sage init-config [--force]` | Write the default config |
| `shortcut-sage send ACTION [--app APP] [--title TITLE]` | Send a test event |
| `shortcut-sage status` | Show daemon status as JSON |
| `shortcut-sage-doctor [--fix]` | Diagnose the installation |
| `export-shortcuts [--sync] [--all] FILE` | Export or sync your KDE shortcuts |
| `dev-hints` | Live debug panel (daemon state, buffer, recent telemetry) |
| `dev-audit LOG_DIR` | Telemetry report (acceptance rate, errors, timing) |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

dbus-run-session -- pytest        # full suite incl. DBus integration tests
pytest                            # DBus tests are skipped without a session bus
node --test tests/kwin/main.test.js   # KWin script against a mocked KWin API
ruff check sage tests && ruff format --check sage tests && mypy sage
```

Headless runs need `QT_QPA_PLATFORM=offscreen` (the test suite sets it).
`just --list` shows shortcut recipes. Visual screenshot tests are described in
[docs/plans/visual-test-checklist.md](docs/plans/visual-test-checklist.md).

## Project status

MVP (PR-00…PR-06 in [implementation-plan.md](implementation-plan.md)) is functionally
complete: the KWin script, daemon, overlay, acceptance tracking, personalization,
exporter, doctor, autostart and dev tools are in place and covered by tests. It still
needs a hands-on run on a real Plasma 6 session: overlay placement, the show-desktop
signal, and the tiling heuristics can't be fully exercised in CI.

## Documentation

- [Product Bible](shortcut-sage-bible.md): vision, principles, architecture
- [Implementation Plan](implementation-plan.md): phased plan
- [Contributing](CONTRIBUTING.md)

## License

MIT, see [LICENSE](LICENSE).
