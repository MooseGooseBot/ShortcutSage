# 2026-11-27: Bug-fix pass

## Summary

A review found that the end-to-end loop (KWin → daemon → overlay) could not work on a
real desktop, and that every CI run had failed. This pass fixes the bugs that were
found and brings the MVP (PR-00…PR-06) to a testable state on Plasma 6.

| Check | Before | After |
|-------|--------|-------|
| pytest | 81 passed (overlay/DBus excluded), 58.8% coverage | 189 passed (incl. 16 real-DBus tests), 87.8% coverage, gate 80% |
| KWin script tests | none | 29 (Node, mocked KWin 6 and KWin 5 APIs) |
| mypy | 12 errors | clean |
| ruff format | 2 files | clean |
| CI | always red (dbus-python build; invalid visual-tests workflow) | fixed (no native deps; private DBus session) |

## Bugs fixed

1. **Every KWin event crashed the daemon**: KWin sends UTC `...Z` timestamps, the
   engine compared them with naive `datetime.now()`, so it raised `TypeError`. All time
   handling now goes through `sage/timeutil.py`, which accepts ISO strings with or
   without an offset, epoch seconds, and epoch milliseconds.
2. **It suggested the action you just performed.** The performed action is now excluded.
3. **Cooldowns were set for suggestions cut by top-3**, so they were silently
   suppressed for minutes. Cooldowns now apply only to suggestions actually shown.
4. **KWin action names didn't match the rules**: the script emitted `window_focus`,
   `switch_desktop` and `maximize`, while the rules expected `tile_left`,
   `show_desktop`, `switch_desktop_next` and so on. The new script emits the rule
   actions (enforced by a test).
5. **The KWin script used the Plasma 5 API** (`clientActivated`, `workspace.knownInterfaces`)
   and had no `metadata.json` or installer. It is rewritten as a proper KPackage
   (`kwin/shortcut-sage/`) for KWin 6, with a KWin 5 fallback, and
   `scripts/install-kwin-script.sh` installs it.
6. **The overlay couldn't be positioned on Wayland.** The KWin script now pins it
   top-left, keeps it above other windows and on all desktops, and gives focus back if
   it gets activated.
7. **`MetricsCollector.export_metrics()` deadlocked** (non-reentrant lock), and its
   histograms grew without bound. It now uses an RLock and capped deques.
8. **`mark_accepted` was never called.** The daemon now detects acceptance (you perform
   a shown suggestion within 30 s), records it and persists personalization in
   `personalization.json`.
9. **The overlay never hid** (fade animations were defined but never used). It now
   auto-hides after 8 s, caps at 3 chips, and skips malformed suggestions.
10. **A second daemon silently clobbered the first daemon's DBus name.** It now refuses
    to start (exit 1).
11. **`GetBufferState` raised `AttributeError`.** Fixed, and `GetStatus` was added.
12. **dbus-python/PyGObject** blocked installs and CI. Replaced by QtDBus (ships with
    PySide6).
13. **`LogRedactor` was a no-op.** Window titles are still sent to the daemon (rules
    need them) but are redacted from logs unless `--log-window-titles` or
    `SHORTCUT_SAGE_LOG_WINDOW_TITLES=1` is set.
14. **One bad config entry rejected the whole file.** Loading is now fail-soft per entry,
    with warnings, and a broken hot-reload keeps the previous config.
15. **The hot-reload watcher missed editors that save via rename.** It now handles
    created and moved events, with debounce.
16. **Per-rule `window` was ignored.** Rules now honour their own window, and can
    filter on app class (`app`) or window title (`title`) regexes.
17. **The exporter called a non-existent kglobalaccel method.** It now parses
    `kglobalshortcutsrc` (with KConfig escapes), maps KDE actions to Sage actions, and
    can `--sync` keys in place.
18. **`doctor` checked `kglobalaccel5`.** It now checks Plasma 6: KWin, kglobalaccel,
    the daemon (all via DBus), the KWin script install/enable state, and config
    validity.
19. **The autostart `.desktop` ran `shortcut-sage` with no subcommand.** Replaced by
    systemd user units with an XDG autostart fallback (`scripts/install-autostart.sh`).
20. **The default shortcuts were Plasma 5 era.** For example, Overview is `Meta+W`, not
    `Meta+Tab`, and `Meta+Up` tiles to the top rather than maximizing. Defaults were
    updated from the KWin source.
21. **`dev-hints` showed in-process metrics only**, so it was always empty. It now reads
    the daemon over DBus plus the telemetry file. **`dev-audit` skipped rotated logs.**
    Fixed, and it now reports the acceptance rate.
22. **The visual test slept on the GUI thread**, so the fade-in never ran and chips were
    captured at opacity 0. Fixed, with a widget-grab fallback.

## Still needs a human

A ~10-minute manual check on a real Plasma 6 session; see the README "Trying it out"
section. Things CI cannot verify:

- the overlay is pinned top-left and never steals focus;
- `hiddenByShowDesktopChanged` fires for Peek at Desktop;
- quick-tile detection works on your screen layout;
- `install-kwin-script.sh` and `install-autostart.sh` on your distro.

`overview`, `application_launcher` and the screenshot actions can't be observed from
a KWin script (they are effects or other processes), so rules that trigger on them only
fire when another source sends those events (e.g. `shortcut-sage send overview`).
