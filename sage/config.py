"""Configuration loading and validation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from sage.models import Rule, RulesConfig, Shortcut, ShortcutsConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ConfigError(Exception):
    """Configuration error."""

    pass


def _short_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts)


class ConfigLoader:
    """Loads and validates YAML configs.

    ``load_shortcuts`` / ``load_rules`` are *fail-soft*: an invalid individual entry is
    skipped with a warning (collected in :attr:`warnings`) instead of rejecting the
    whole file. Structural problems (unreadable file, bad YAML, wrong version, no valid
    entries at all) still raise :class:`ConfigError`.
    """

    def __init__(self, config_dir: Path | str):
        """
        Initialize config loader.

        Args:
            config_dir: Directory containing config files
        """
        self.config_dir = Path(config_dir)
        if not self.config_dir.exists():
            raise ConfigError(f"Config directory does not exist: {self.config_dir}")
        if not self.config_dir.is_dir():
            raise ConfigError(f"Config path is not a directory: {self.config_dir}")
        self.warnings: list[str] = []

    def _read_yaml(self, filename: str) -> Any:
        path = self.config_dir / filename

        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")

        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {filename}: {e}") from e
        except Exception as e:
            raise ConfigError(f"Failed to read {filename}: {e}") from e

        if data is None:
            raise ConfigError(f"Config file is empty: {filename}")
        return data

    def load(self, filename: str, model: type[T]) -> T:
        """
        Load and strictly validate a config file.

        Args:
            filename: Config filename (e.g., 'shortcuts.yaml')
            model: Pydantic model class to validate against

        Returns:
            Validated config model instance

        Raises:
            ConfigError: If file not found or validation fails
        """
        data = self._read_yaml(filename)
        try:
            return model.model_validate(data)
        except ValidationError as e:
            raise ConfigError(f"Invalid config in {filename}: {e}") from e

    def _warn(self, message: str) -> None:
        logger.warning(message)
        self.warnings.append(message)

    def _load_items(
        self, filename: str, list_key: str, item_model: type[T], unique_attr: str
    ) -> tuple[str, list[T]]:
        data = self._read_yaml(filename)
        if not isinstance(data, dict):
            raise ConfigError(f"Invalid config in {filename}: top level must be a mapping")

        version = str(data.get("version", "1.0"))
        if version != "1.0":
            raise ConfigError(f"Invalid config in {filename}: unsupported version {version!r}")

        raw_items = data.get(list_key)
        if not isinstance(raw_items, list):
            raise ConfigError(f"Invalid config in {filename}: '{list_key}' must be a list")

        items: list[T] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_items):
            label = raw.get("name") or raw.get("action") if isinstance(raw, dict) else None
            where = f"{filename} {list_key}[{index}]" + (f" ({label})" if label else "")
            try:
                item = item_model.model_validate(raw)
            except ValidationError as e:
                self._warn(f"Skipping invalid entry {where}: {_short_error(e)}")
                continue
            ident = str(getattr(item, unique_attr))
            if ident in seen:
                self._warn(f"Skipping duplicate {unique_attr} {ident!r} in {where}")
                continue
            seen.add(ident)
            items.append(item)

        if not items:
            raise ConfigError(f"Invalid config in {filename}: no valid {list_key} found")
        return version, items

    def load_shortcuts(self) -> ShortcutsConfig:
        """
        Load shortcuts.yaml configuration (fail-soft per entry).

        Raises:
            ConfigError: If the file is unusable or contains no valid shortcut
        """
        self.warnings = []
        _, items = self._load_items("shortcuts.yaml", "shortcuts", Shortcut, "action")
        return ShortcutsConfig(version="1.0", shortcuts=items)

    def load_rules(self) -> RulesConfig:
        """
        Load rules.yaml configuration (fail-soft per entry).

        Raises:
            ConfigError: If the file is unusable or contains no valid rule
        """
        self.warnings = []
        _, items = self._load_items("rules.yaml", "rules", Rule, "name")
        return RulesConfig(version="1.0", rules=items)

    def reload(self) -> tuple[ShortcutsConfig, RulesConfig]:
        """
        Reload both config files.

        Returns:
            Tuple of (shortcuts_config, rules_config)

        Raises:
            ConfigError: If loading fails
        """
        shortcuts = self.load_shortcuts()
        shortcut_warnings = list(self.warnings)
        rules = self.load_rules()
        self.warnings = shortcut_warnings + self.warnings
        return shortcuts, rules


def unknown_rule_actions(shortcuts: ShortcutsConfig, rules: RulesConfig) -> list[str]:
    """Return warnings for rules that suggest actions missing from shortcuts.yaml."""
    known = {s.action for s in shortcuts.shortcuts}
    warnings = []
    for rule in rules.rules:
        for suggestion in rule.suggest:
            if suggestion.action not in known:
                warnings.append(
                    f"Rule {rule.name!r} suggests {suggestion.action!r}, "
                    "which is not defined in shortcuts.yaml (it will never be shown)"
                )
    return warnings
