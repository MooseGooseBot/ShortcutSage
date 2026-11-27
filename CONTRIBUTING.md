# Contributing to Shortcut Sage

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Node 18+ is needed for the KWin script tests. A DBus session bus (`dbus-run-session`)
is needed for the DBus integration tests; without one they are skipped.

## Before opening a PR

```bash
ruff format sage tests
ruff check sage tests
mypy sage
node --test tests/kwin/main.test.js
dbus-run-session -- pytest        # coverage gate: 80%
```

CI runs the same commands (`.github/workflows/ci.yml`).

## Conventions

- Conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `ci:`, `refactor:`…).
- Every bug fix comes with a regression test (see `tests/unit/test_regressions.py`).
- Timestamps: always compare through `sage.timeutil` (KWin sends UTC `...Z` strings).
- Window titles are required at runtime but must never reach log files unless
  `--log-window-titles` / `SHORTCUT_SAGE_LOG_WINDOW_TITLES=1` is set. Put new
  sensitive metadata keys in `sage.telemetry.SENSITIVE_KEYS`.
- New action IDs: emit them from `kwin/shortcut-sage/contents/code/main.js`, add a
  test in `tests/kwin/main.test.js`, a shortcut in `config/shortcuts.yaml`, and the
  KDE mapping in `sage/exporter.py`. Keep `sage/data/` in sync with `config/`
  (a test enforces this).

## Working on the KWin script

The script targets the KWin 6 scripting API (with a KWin 5 fallback) and is plain ES5.
To try changes on a live session:

```bash
scripts/install-kwin-script.sh                 # reinstalls and reloads
journalctl --user -f QT_CATEGORY=js QT_CATEGORY=kwin_scripting
```

Set `kwriteconfig6 --file kwinrc --group Script-shortcut-sage --key debug true` to
log every event (including window titles) to the journal.
