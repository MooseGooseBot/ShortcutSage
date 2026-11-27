"""Read the user's real KDE shortcuts and export/sync them to shortcuts.yaml.

KDE stores global shortcuts in ``~/.config/kglobalshortcutsrc``::

    [kwin]
    Show Desktop=Meta+D,Meta+D,Peek at Desktop
    Window Quick Tile Left=Meta+Left,Meta+Left,Quick Tile Window to the Left

Each value is ``<active keys>,<default keys>,<friendly name>``; several keys are
separated by tabs and ``none`` means unbound. (The old implementation queried a
non-existent ``org.kde.kglobalaccel.shortcuts`` DBus method; the config file is the
authoritative, always-available source, so it is used instead.)

Usage::

    export-shortcuts out.yaml            # write the mapped Shortcut Sage actions
    export-shortcuts --all out.yaml      # also include every other bound KDE shortcut
    export-shortcuts --sync ~/.config/shortcut-sage/shortcuts.yaml
                                         # update keys in an existing file in place
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sage.models import Shortcut, ShortcutsConfig

# (component, KDE action name) -> (Shortcut Sage action, description, category).
# Several KDE actions may map to one Sage action; the first one with a binding wins.
KDE_ACTION_MAP: list[tuple[str, str, str, str, str]] = [
    ("kwin", "Show Desktop", "show_desktop", "Peek at desktop", "desktop"),
    ("kwin", "Overview", "overview", "Toggle Overview", "desktop"),
    ("kwin", "ExposeAll", "present_windows", "Show all windows (Present Windows)", "desktop"),
    (
        "kwin",
        "Switch One Desktop to the Left",
        "switch_desktop_prev",
        "Switch one desktop to the left",
        "desktop",
    ),
    (
        "kwin",
        "Switch to Previous Desktop",
        "switch_desktop_prev",
        "Switch to previous desktop",
        "desktop",
    ),
    (
        "kwin",
        "Switch One Desktop to the Right",
        "switch_desktop_next",
        "Switch one desktop to the right",
        "desktop",
    ),
    ("kwin", "Switch to Next Desktop", "switch_desktop_next", "Switch to next desktop", "desktop"),
    ("kwin", "Window Quick Tile Left", "tile_left", "Tile window to the left half", "window"),
    ("kwin", "Window Quick Tile Right", "tile_right", "Tile window to the right half", "window"),
    ("kwin", "Window Quick Tile Top", "tile_top", "Tile window to the top half", "window"),
    ("kwin", "Window Quick Tile Bottom", "tile_bottom", "Tile window to the bottom half", "window"),
    (
        "kwin",
        "Window Quick Tile Top Left",
        "tile_top_left",
        "Tile window to the top-left quarter",
        "window",
    ),
    (
        "kwin",
        "Window Quick Tile Top Right",
        "tile_top_right",
        "Tile window to the top-right quarter",
        "window",
    ),
    (
        "kwin",
        "Window Quick Tile Bottom Left",
        "tile_bottom_left",
        "Tile window to the bottom-left quarter",
        "window",
    ),
    (
        "kwin",
        "Window Quick Tile Bottom Right",
        "tile_bottom_right",
        "Tile window to the bottom-right quarter",
        "window",
    ),
    ("kwin", "Window Maximize", "maximize", "Maximize window", "window"),
    ("kwin", "Window Minimize", "minimize", "Minimize window", "window"),
    ("kwin", "Window Close", "close_window", "Close window", "window"),
    ("kwin", "Window Operations Menu", "window_menu", "Open window menu", "window"),
    (
        "kwin",
        "Window One Desktop to the Left",
        "move_window_left_desktop",
        "Move window one desktop to the left",
        "window",
    ),
    (
        "kwin",
        "Window to Previous Desktop",
        "move_window_left_desktop",
        "Move window to previous desktop",
        "window",
    ),
    (
        "kwin",
        "Window One Desktop to the Right",
        "move_window_right_desktop",
        "Move window one desktop to the right",
        "window",
    ),
    (
        "kwin",
        "Window to Next Desktop",
        "move_window_right_desktop",
        "Move window to next desktop",
        "window",
    ),
    (
        "plasmashell",
        "activate application launcher",
        "application_launcher",
        "Open application launcher",
        "launcher",
    ),
    ("org.kde.krunner.desktop", "_launch", "krunner", "Open KRunner", "launcher"),
    ("org.kde.konsole.desktop", "_launch", "terminal", "Open terminal", "launcher"),
    (
        "org.kde.spectacle.desktop",
        "FullScreenScreenShot",
        "screenshot_full",
        "Screenshot the entire desktop",
        "screenshot",
    ),
    (
        "org.kde.spectacle.desktop",
        "ActiveWindowScreenShot",
        "screenshot_window",
        "Screenshot the active window",
        "screenshot",
    ),
    (
        "org.kde.spectacle.desktop",
        "RectangularRegionScreenShot",
        "screenshot_region",
        "Screenshot a rectangular region",
        "screenshot",
    ),
]

# Older/alternate component names (Plasma 5 used "krunner.desktop" etc.)
COMPONENT_ALIASES = {
    "krunner.desktop": "org.kde.krunner.desktop",
    "services/org.kde.krunner.desktop": "org.kde.krunner.desktop",
    "services/org.kde.konsole.desktop": "org.kde.konsole.desktop",
    "services/org.kde.spectacle.desktop": "org.kde.spectacle.desktop",
    "org.kde.spectacle": "org.kde.spectacle.desktop",
}


@dataclass
class DiscoveredShortcut:
    """A shortcut found in the KDE configuration."""

    action_id: str
    key_sequence: str
    description: str
    category: str
    source: str  # "<component>/<KDE action name>"


def default_kglobalshortcutsrc() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "kglobalshortcutsrc"


_KCONFIG_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "s": " ", "\\": "\\"}


def _split_unescaped(value: str, sep: str, maxsplit: int) -> list[str]:
    """Split a KConfig value on ``sep`` and decode KConfig escapes.

    KConfig writes tabs (the separator between multiple keys) as ``\\t`` and list
    separators inside an entry as ``\\,`` (so ``Meta+\\,`` is the key ``Meta+,``).
    """
    parts: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            current.append(_KCONFIG_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if ch == sep and (maxsplit < 0 or len(parts) < maxsplit):
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _first_key(keys: str) -> str | None:
    for key in keys.split("\t"):
        key = key.strip()
        if key and key.lower() != "none":
            return key
    return None


def parse_kglobalshortcutsrc(text: str) -> dict[tuple[str, str], tuple[str | None, str]]:
    """Parse kglobalshortcutsrc into ``{(component, action): (active key, friendly name)}``."""
    result: dict[tuple[str, str], tuple[str | None, str]] = {}
    component: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            # Nested groups are written as [services][org.kde.konsole.desktop]
            groups = re.findall(r"\[([^\]]*)\]", line)
            component = "/".join(groups)
            component = COMPONENT_ALIASES.get(component, component)
            continue
        if component is None or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name == "_k_friendly_name":
            continue
        fields = _split_unescaped(value, ",", 2)
        active = _first_key(fields[0]) if fields else None
        friendly = fields[2].strip() if len(fields) > 2 and fields[2].strip() else name
        result[(component, name)] = (active, friendly)
    return result


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "action"


class ShortcutExporter:
    """Discover KDE shortcuts and map them to Shortcut Sage actions."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_kglobalshortcutsrc()
        self.discovered_shortcuts: list[DiscoveredShortcut] = []

    def read_bindings(self) -> dict[tuple[str, str], tuple[str | None, str]]:
        if not self.config_path.exists():
            print(
                f"Warning: {self.config_path} not found (not a KDE Plasma session?)",
                file=sys.stderr,
            )
            return {}
        try:
            return parse_kglobalshortcutsrc(self.config_path.read_text(encoding="utf-8"))
        except OSError as e:
            print(f"Warning: could not read {self.config_path}: {e}", file=sys.stderr)
            return {}

    def discover_shortcuts(self, include_unmapped: bool = False) -> list[DiscoveredShortcut]:
        """Return Shortcut Sage actions with the user's active keys.

        With ``include_unmapped`` every other bound KDE shortcut is added too, with an
        action ID derived from its component and name.
        """
        bindings = self.read_bindings()
        found: dict[str, DiscoveredShortcut] = {}
        mapped_sources: set[tuple[str, str]] = set()

        for component, kde_name, action, description, category in KDE_ACTION_MAP:
            mapped_sources.add((component, kde_name))
            if action in found:
                continue
            key, _friendly = bindings.get((component, kde_name), (None, ""))
            if key:
                found[action] = DiscoveredShortcut(
                    action, key, description, category, f"{component}/{kde_name}"
                )

        if include_unmapped:
            for (component, kde_name), (key, friendly) in sorted(bindings.items()):
                if not key or (component, kde_name) in mapped_sources:
                    continue
                action = _slug(f"{component.split('/')[-1].removesuffix('.desktop')}_{kde_name}")
                if action not in found:
                    found[action] = DiscoveredShortcut(
                        action, key, friendly, "kde", f"{component}/{kde_name}"
                    )

        self.discovered_shortcuts = list(found.values())
        return self.discovered_shortcuts

    def to_config(self) -> ShortcutsConfig:
        models: list[Shortcut] = []
        for ds in self.discovered_shortcuts:
            try:
                models.append(
                    Shortcut(
                        key=ds.key_sequence,
                        action=ds.action_id,
                        description=ds.description,
                        category=ds.category,
                    )
                )
            except ValidationError as e:
                print(f"Skipping invalid shortcut {ds.action_id}: {e}", file=sys.stderr)
        return ShortcutsConfig(version="1.0", shortcuts=models)

    def export_to_yaml(self, output_file: Path) -> bool:
        """Write discovered shortcuts as a shortcuts.yaml file."""
        try:
            config = self.to_config()
            data = {
                "version": config.version,
                "shortcuts": [s.model_dump() for s in config.shortcuts],
            }
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, "w", encoding="utf-8") as f:
                f.write("# Generated by export-shortcuts from kglobalshortcutsrc\n")
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            print(f"Exported {len(config.shortcuts)} shortcuts to {output_file}")
            return True
        except Exception as e:
            print(f"Error exporting shortcuts: {e}", file=sys.stderr)
            return False

    def sync_yaml(self, shortcuts_file: Path) -> list[tuple[str, str, str]]:
        """Update keys in an existing shortcuts.yaml in place.

        Only the ``key:`` values of actions whose KDE binding differs are rewritten;
        comments, ordering and unknown actions are preserved. Returns
        ``(action, old key, new key)`` for every change.
        """
        discovered = {ds.action_id: ds.key_sequence for ds in self.discover_shortcuts()}
        lines = shortcuts_file.read_text(encoding="utf-8").splitlines(keepends=True)

        # Locate "- key: ..." / "action: ..." pairs belonging to the same list item.
        changes: list[tuple[str, str, str]] = []
        item_start: int | None = None
        key_line: int | None = None
        key_value: str | None = None

        def flush(action: str | None) -> None:
            if action is None or key_line is None or key_value is None:
                return
            new = discovered.get(action)
            if new and new != key_value:
                old_line = lines[key_line]
                lines[key_line] = re.sub(
                    r"(key:\s*)(\"[^\"]*\"|'[^']*'|\S+)",
                    lambda m: m.group(1) + yaml_quote(new),
                    old_line,
                    count=1,
                )
                changes.append((action, key_value, new))

        current_action: str | None = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("- "):
                flush(current_action)
                item_start, key_line, key_value, current_action = i, None, None, None
                stripped = stripped[2:].strip()
            if item_start is None:
                continue
            m = re.match(r"(key|action):\s*(.+?)\s*(#.*)?$", stripped)
            if not m:
                continue
            value = _unquote(m.group(2))
            if m.group(1) == "key":
                key_line, key_value = i, value
            else:
                current_action = value
        flush(current_action)

        if changes:
            new_text = "".join(lines)
            # Never write a file we can't load back
            ShortcutsConfig.model_validate(yaml.safe_load(new_text))
            shortcuts_file.write_text(new_text, encoding="utf-8")
        return changes


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: str) -> str:
    loaded: Any = yaml.safe_load(value)
    return str(loaded) if loaded is not None else ""


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``export-shortcuts`` tool."""
    parser = argparse.ArgumentParser(
        prog="export-shortcuts",
        description="Export your KDE shortcuts (kglobalshortcutsrc) to Shortcut Sage format",
    )
    parser.add_argument("output", type=Path, help="shortcuts.yaml to write (or update with --sync)")
    parser.add_argument(
        "--sync", action="store_true", help="Update keys in an existing file in place"
    )
    parser.add_argument(
        "--all", action="store_true", help="Include KDE shortcuts with no Sage action"
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=f"kglobalshortcutsrc to read (default: {default_kglobalshortcutsrc()})",
    )
    args = parser.parse_args(argv)

    exporter = ShortcutExporter(args.source)

    if args.sync:
        if not args.output.exists():
            print(f"Error: {args.output} does not exist", file=sys.stderr)
            return 1
        try:
            changes = exporter.sync_yaml(args.output)
        except (ValidationError, yaml.YAMLError) as e:
            print(f"Error: refusing to write invalid config: {e}", file=sys.stderr)
            return 1
        if not changes:
            print("Already in sync - no changes.")
        for action, old, new in changes:
            print(f"{action}: {old} -> {new}")
        return 0

    discovered = exporter.discover_shortcuts(include_unmapped=args.all)
    if not discovered:
        print(
            "No bound shortcuts found. Run this inside a KDE Plasma session, or pass --source.",
            file=sys.stderr,
        )
        return 1
    return 0 if exporter.export_to_yaml(args.output) else 1


if __name__ == "__main__":
    sys.exit(main())
