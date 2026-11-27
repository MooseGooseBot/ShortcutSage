"""Shortcut Sage CLI entry point."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path


def default_config_dir() -> Path:
    """``$XDG_CONFIG_HOME/shortcut-sage`` (``~/.config/shortcut-sage``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "shortcut-sage"


DEFAULT_CONFIG_DIR = default_config_dir()
CONFIG_FILES = ("shortcuts.yaml", "rules.yaml")


def install_default_config(config_dir: Path, *, overwrite: bool = False) -> list[Path]:
    """Copy the bundled default shortcuts.yaml/rules.yaml into ``config_dir``.

    Existing files are kept unless ``overwrite`` is set. Returns the files written.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    data = resources.files("sage") / "data"
    for name in CONFIG_FILES:
        target = config_dir / name
        if target.exists() and not overwrite:
            continue
        with resources.as_file(data / name) as source:
            shutil.copyfile(source, target)
        written.append(target)
    return written


def _expand_path(path: str | None, *, default: Path | None = None) -> str:
    """Expand user/relative paths, falling back to default when empty."""
    if path:
        return str(Path(path).expanduser())
    if default is not None:
        return str(default.expanduser())
    raise ValueError("Path cannot be determined")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="shortcut-sage",
        description="Shortcut Sage daemon and overlay controller",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    daemon_parser = subparsers.add_parser("daemon", help="Start the Shortcut Sage daemon")
    daemon_parser.add_argument(
        "--config",
        metavar="DIR",
        default=None,
        help=f"Configuration directory (default: {DEFAULT_CONFIG_DIR}; created on first run)",
    )
    daemon_parser.add_argument(
        "--log-dir",
        metavar="DIR",
        default=None,
        help="Directory for daemon logs (default: ~/.local/share/shortcut-sage/logs)",
    )
    daemon_parser.add_argument(
        "--no-dbus",
        action="store_true",
        help="Disable DBus integration (fallback mode, useful on non-Linux dev machines)",
    )
    daemon_parser.add_argument(
        "--log-window-titles",
        action="store_true",
        help="Write window titles into the logs (testing/debugging only; "
        "also enabled by SHORTCUT_SAGE_LOG_WINDOW_TITLES=1)",
    )
    daemon_parser.add_argument("--debug", action="store_true", help="Verbose logging (DEBUG level)")

    overlay_parser = subparsers.add_parser("overlay", help="Launch the overlay UI")
    overlay_parser.add_argument(
        "--demo",
        action="store_true",
        help="Populate overlay with demo suggestions instead of waiting for DBus events",
    )
    overlay_parser.add_argument(
        "--no-dbus",
        action="store_true",
        help="Disable DBus listener and rely solely on demo/fallback suggestions",
    )
    overlay_parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        metavar="SECONDS",
        help="Hide suggestions after this many seconds (0 = never; default 8)",
    )

    init_parser = subparsers.add_parser(
        "init-config", help="Write the default shortcuts.yaml/rules.yaml"
    )
    init_parser.add_argument("--config", metavar="DIR", default=None)
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing files")

    send_parser = subparsers.add_parser(
        "send", help="Send a test event to the running daemon (manual testing)"
    )
    send_parser.add_argument("action", help="Action ID, e.g. show_desktop or tile_left")
    send_parser.add_argument("--type", default="test", help="Event type (default: test)")
    send_parser.add_argument("--app", default=None, help="Focused application class")
    send_parser.add_argument("--title", default=None, help="Focused window title")

    subparsers.add_parser("status", help="Show the running daemon's status")

    return parser


def _cmd_send(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from sage.dbus_client import DBusClient, DBusClientError

    metadata = {}
    if args.app:
        metadata["app"] = args.app
    if args.title:
        metadata["window"] = args.title
    try:
        client = DBusClient()
        client.send_event(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": args.type,
                "action": args.action,
                "metadata": metadata,
            }
        )
    except DBusClientError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Sent {args.action}")
    return 0


def _cmd_status() -> int:
    from sage.dbus_client import DBusClient, DBusClientError

    try:
        client = DBusClient()
        print(json.dumps(client.get_status(), indent=2))
    except DBusClientError as e:
        print(f"Daemon not reachable: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint callable from console_scripts; returns the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "daemon":
        import logging

        logging.basicConfig(
            level=logging.DEBUG if args.debug else logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        config_path = Path(_expand_path(args.config, default=DEFAULT_CONFIG_DIR))
        if not config_path.exists():
            if args.config is None:
                written = install_default_config(config_path)
                for path in written:
                    print(f"Created default config: {path}", file=sys.stderr)
            else:
                parser.error(f"Config directory not found: {config_path}")
        elif args.config is None and not all((config_path / f).exists() for f in CONFIG_FILES):
            for path in install_default_config(config_path):
                print(f"Created default config: {path}", file=sys.stderr)

        from sage.dbus_daemon import run_daemon

        log_dir = Path(args.log_dir).expanduser() if args.log_dir else None
        return run_daemon(
            str(config_path),
            str(log_dir) if log_dir else None,
            enable_dbus=not args.no_dbus,
            log_window_titles=args.log_window_titles,
        )
    elif args.command == "overlay":
        from sage.overlay import DBUS_AVAILABLE, run_overlay

        enable_dbus = DBUS_AVAILABLE and not args.no_dbus
        return run_overlay(enable_dbus=enable_dbus, demo=args.demo, timeout_seconds=args.timeout)
    elif args.command == "init-config":
        config_path = Path(_expand_path(args.config, default=DEFAULT_CONFIG_DIR))
        written = install_default_config(config_path, overwrite=args.force)
        for path in written:
            print(f"Wrote {path}")
        if not written:
            print(f"Config already present in {config_path} (use --force to overwrite)")
        return 0
    elif args.command == "send":
        return _cmd_send(args)
    elif args.command == "status":
        return _cmd_status()
    parser.print_help()  # pragma: no cover - argparse enforces the choices
    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
