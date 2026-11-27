"""Rule matching engine."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from sage.models import ContextMatch, Rule, Suggestion


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


class RuleMatcher:
    """Matches context features against rules to find suggestions."""

    def __init__(self, rules: list[Rule]):
        """
        Initialize rule matcher.

        Args:
            rules: List of rules to match against
        """
        self.rules = rules

    @property
    def max_window(self) -> int:
        """Largest ``context.window`` across all rules (seconds)."""
        return max((r.context.window for r in self.rules), default=3)

    def match(self, features: dict[str, Any]) -> list[tuple[Rule, Suggestion]]:
        """
        Find matching rules and their suggestions based on features.

        Args:
            features: Context features extracted from events

        Returns:
            List of (rule, suggestion) tuples for matching rules
        """
        matches: list[tuple[Rule, Suggestion]] = []

        for rule in self.rules:
            if self._matches_context(rule.context, features):
                for suggestion in rule.suggest:
                    matches.append((rule, suggestion))

        return matches

    def _matches_context(self, context: ContextMatch, features: dict[str, Any]) -> bool:
        """
        Check if context pattern matches features.

        Args:
            context: ContextMatch from rule
            features: Extracted features

        Returns:
            True if context matches
        """
        if not self._matches_window_filters(context, features):
            return False

        if context.type in ("event_sequence", "recent_window", "desktop_state"):
            return self._match_actions(context, features)

        return False

    @staticmethod
    def _matches_window_filters(context: ContextMatch, features: dict[str, Any]) -> bool:
        """Apply the optional ``app`` / ``title`` regex filters."""
        if context.app is not None:
            app = features.get("active_app")
            if not app or not _compile(context.app).search(str(app)):
                return False
        if context.title is not None:
            title = features.get("active_title")
            if not title or not _compile(context.title).search(str(title)):
                return False
        return True

    @staticmethod
    def _actions_within(window: float, features: dict[str, Any]) -> list[str]:
        """Actions that happened within ``window`` seconds of the newest event."""
        recent_events = features.get("recent_events")
        if recent_events is None:
            # Features without timing info (e.g. hand-built in tests): use everything.
            return list(features.get("recent_actions", []))
        return [action for action, age in recent_events if age <= window]

    def _match_actions(self, context: ContextMatch, features: dict[str, Any]) -> bool:
        """Match if any pattern action occurred inside the rule's window.

        ``pattern`` may be a single action ID or a list; a list matches when *any* of
        its actions occurred (this is how the shipped rules use it, e.g.
        ``[switch_desktop_next, switch_desktop_prev]``).
        """
        recent = self._actions_within(context.window, features)
        if not recent:
            return False

        patterns = [context.pattern] if isinstance(context.pattern, str) else context.pattern
        return any(p in recent for p in patterns)
