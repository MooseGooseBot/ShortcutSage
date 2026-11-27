#!/usr/bin/env python3
"""``shortcut-sage-doctor``: diagnose a Shortcut Sage installation."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

Check = tuple[str, bool, str]

KWIN_SCRIPT_ID = "shortcut-sage"


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def check_system_requirements() -> list[Check]:
    """Python, platform and library checks."""
    results: list[Check] = []
    v = sys.version_info
    results.append(("Python 3.11+", v >= (3, 11), f"Current: {v.major}.{v.minor}.{v.micro}"))
    results.append(("Linux platform", sys.platform.startswith("linux"), f"Current: {sys.platform}"))

    pyside_ok = importlib.util.find_spec("PySide6") is not None
    results.append(
        ("PySide6", pyside_ok, "Available" if pyside_ok else "Missing - pip install PySide6")
    )
    if pyside_ok:
        qtdbus_ok = importlib.util.find_spec("PySide6.QtDBus") is not None
        results.append(
            (
                "QtDBus (PySide6)",
                qtdbus_ok,
                "Available" if qtdbus_ok else "Missing - your PySide6 build lacks QtDBus",
            )
        )
    return results


def _dbus_name_has_owner(name: str) -> bool | None:
    """True/False if the session bus could be queried, None if it could not."""
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtDBus import QDBusConnection
    except ImportError:
        return None
    if QCoreApplication.instance() is None:
        QCoreApplication([sys.argv[0] if sys.argv else "shortcut-sage-doctor"])
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return None
    reply = bus.interface().isServiceRegistered(name)
    return bool(reply.value()) if reply.isValid() else None


def check_kde_environment() -> list[Check]:
    """Desktop session checks (Plasma 6, KWin, kglobalaccel, session bus)."""
    results: list[Check] = []
    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
    results.append(("Session type", True, f"Detected: {session_type}"))

    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    has_kde = "kde" in desktop.lower() or "plasma" in os.environ.get("DESKTOP_SESSION", "").lower()
    results.append(("KDE Plasma session", has_kde, f"XDG_CURRENT_DESKTOP={desktop or 'unset'}"))

    plasma_version = os.environ.get("KDE_SESSION_VERSION", "")
    results.append(
        (
            "Plasma 6",
            plasma_version == "6",
            f"KDE_SESSION_VERSION={plasma_version or 'unset'} (the KWin script targets Plasma 6)",
        )
    )

    bus_ok = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
    results.append(
        ("DBus session bus", bus_ok, "Available" if bus_ok else "DBUS_SESSION_BUS_ADDRESS not set")
    )

    for name, label, why in (
        ("org.kde.KWin", "KWin running", "needed for the event-reporting KWin script"),
        ("org.kde.kglobalaccel", "kglobalaccel running", "manages global shortcuts"),
        ("org.shortcutsage.Daemon", "Shortcut Sage daemon running", "start: shortcut-sage daemon"),
    ):
        owned = _dbus_name_has_owner(name) if bus_ok else None
        if owned is None:
            results.append((label, False, f"could not query the session bus ({why})"))
        else:
            results.append((label, owned, why))
    return results


def check_kwin_script() -> list[Check]:
    """Is the KWin script installed and enabled?"""
    results: list[Check] = []
    installed = (_data_home() / "kwin" / "scripts" / KWIN_SCRIPT_ID / "metadata.json").exists()
    if not installed and shutil.which("kpackagetool6"):
        try:
            out = subprocess.run(
                ["kpackagetool6", "--type=KWin/Script", "--list"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            installed = KWIN_SCRIPT_ID in out.split()
        except (OSError, subprocess.SubprocessError):
            pass
    results.append(
        (
            "KWin script installed",
            installed,
            "OK" if installed else "run scripts/install-kwin-script.sh",
        )
    )

    kwinrc = _config_home() / "kwinrc"
    enabled = False
    if kwinrc.exists():
        in_plugins = False
        for line in kwinrc.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("["):
                in_plugins = line == "[Plugins]"
            elif in_plugins and line.replace(" ", "") == f"{KWIN_SCRIPT_ID}Enabled=true":
                enabled = True
    results.append(
        (
            "KWin script enabled",
            enabled,
            "OK" if enabled else "System Settings > Window Management > KWin Scripts",
        )
    )
    return results


def check_config_files(config_dir: Path) -> list[Check]:
    """Do the config files exist and load?"""
    results: list[Check] = []
    shortcuts_file = config_dir / "shortcuts.yaml"
    rules_file = config_dir / "rules.yaml"
    results.append(("shortcuts.yaml exists", shortcuts_file.exists(), str(shortcuts_file)))
    results.append(("rules.yaml exists", rules_file.exists(), str(rules_file)))
    if shortcuts_file.exists() and rules_file.exists():
        from sage.config import ConfigError, ConfigLoader, unknown_rule_actions

        loader = ConfigLoader(config_dir)
        try:
            shortcuts, rules = loader.reload()
            warnings = loader.warnings + unknown_rule_actions(shortcuts, rules)
            detail = f"{len(shortcuts.shortcuts)} shortcuts, {len(rules.rules)} rules"
            if warnings:
                detail += f"; {len(warnings)} warning(s): " + "; ".join(warnings[:3])
            results.append(("Config is valid", not warnings, detail))
        except ConfigError as e:
            results.append(("Config is valid", False, str(e)))
    return results


def create_default_configs(config_dir: Path) -> bool:
    """Install the bundled default configuration (existing files are kept)."""
    from sage.__main__ import install_default_config

    for path in install_default_config(config_dir):
        print(f"Created {path}")
    return True


def _print_section(title: str, results: list[Check]) -> None:
    print(f"\n{title}")
    print("-" * len(title))
    for name, passed, info in results:
        print(f"{'✓' if passed else '✗'} {name}: {info}")


def main(argv: list[str] | None = None) -> int:
    """Run all checks; returns 1 if any check failed."""
    parser = argparse.ArgumentParser(prog="shortcut-sage-doctor")
    parser.add_argument("--config", type=Path, default=_config_home() / "shortcut-sage")
    parser.add_argument(
        "--fix", action="store_true", help="Create missing default config files without asking"
    )
    args = parser.parse_args(argv)

    print("Shortcut Sage - Doctor")
    print("=" * 50)

    sections = [
        ("1. System requirements", check_system_requirements()),
        ("2. KDE environment", check_kde_environment()),
        ("3. KWin script", check_kwin_script()),
    ]
    for title, results in sections:
        _print_section(title, results)

    config_results = check_config_files(args.config)
    _print_section(f"4. Configuration ({args.config})", config_results)
    missing = not all(passed for name, passed, _ in config_results if name.endswith("exists"))
    if missing:
        create = args.fix
        if not create and sys.stdin.isatty():
            create = input("\nCreate default configuration files? (y/N): ").lower() in ("y", "yes")
        if create:
            create_default_configs(args.config)
            config_results = check_config_files(args.config)
            _print_section("4b. Configuration after --fix", config_results)

    all_checks = [c for _, results in sections for c in results] + config_results
    failed = [name for name, passed, _ in all_checks if not passed]
    print("\nSummary")
    print("-------")
    if failed:
        print(f"✗ {len(failed)} issue(s): " + ", ".join(failed))
        return 1
    print("✓ All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
