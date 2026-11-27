"""Policy engine for suggestion filtering and ranking."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any, NamedTuple

from sage.models import Rule, Shortcut, Suggestion
from sage.timeutil import now_utc, parse_timestamp, to_aware

logger = logging.getLogger(__name__)

# Personalization only kicks in once a suggestion has been shown this many times.
MIN_SAMPLES_FOR_PERSONALIZATION = 5


class SuggestionResult(NamedTuple):
    """Final suggestion result with resolved shortcut info."""

    action: str
    key: str
    description: str
    priority: int
    adjusted_priority: int
    rule: str = ""


class PersonalizationData:
    """Stores personalization data for CTR calculation."""

    def __init__(self) -> None:
        self.suggestion_count: int = 0  # Times shown to the user
        self.acceptance_count: int = 0  # Times accepted
        self.last_suggested: datetime | None = None
        self.last_accepted: datetime | None = None

    @property
    def ctr(self) -> float:
        """Click-through (acceptance) rate."""
        if self.suggestion_count == 0:
            return 0.0
        return self.acceptance_count / self.suggestion_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_count": self.suggestion_count,
            "acceptance_count": self.acceptance_count,
            "last_suggested": self.last_suggested.isoformat() if self.last_suggested else None,
            "last_accepted": self.last_accepted.isoformat() if self.last_accepted else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonalizationData:
        item = cls()
        item.suggestion_count = int(data.get("suggestion_count", 0))
        item.acceptance_count = int(data.get("acceptance_count", 0))
        if data.get("last_suggested"):
            item.last_suggested = parse_timestamp(data["last_suggested"])
        if data.get("last_accepted"):
            item.last_accepted = parse_timestamp(data["last_accepted"])
        return item


class PolicyEngine:
    """Applies cooldowns, de-duplication, top-N filtering and acceptance tracking."""

    def __init__(self, shortcuts: dict[str, Shortcut], enable_personalization: bool = True):
        """
        Initialize policy engine.

        Args:
            shortcuts: Dictionary mapping action IDs to Shortcut objects
            enable_personalization: Whether to enable personalization features
        """
        self.shortcuts = shortcuts
        self.enable_personalization = enable_personalization

        # Cooldown tracking: "rule:action" -> last time it was *shown*
        self._cooldowns: dict[str, datetime] = {}

        # Personalization data keyed by "rule:action"
        self._personalization: dict[str, PersonalizationData] = defaultdict(PersonalizationData)

        # Global acceptance counts per action
        self._accepted: dict[str, int] = {}

    def apply(
        self,
        matches: list[tuple[Rule, Suggestion]],
        now: datetime | None = None,
        top_n: int = 3,
        exclude_actions: Iterable[str] = (),
    ) -> list[SuggestionResult]:
        """
        Apply policy and return top N suggestions.

        Pipeline: drop excluded/unknown actions -> drop cooled-down entries ->
        personalize priority -> keep the best entry per action -> sort -> top N.
        Cooldowns and "shown" statistics are only recorded for suggestions that are
        actually returned.

        Args:
            matches: List of (rule, suggestion) tuples from matcher
            now: Current time (naive local or aware; defaults to now)
            top_n: Maximum number of suggestions to return
            exclude_actions: Actions never to suggest (e.g. the action just performed)

        Returns:
            List of SuggestionResult objects (max top_n)
        """
        now = now_utc() if now is None else to_aware(now)
        excluded = set(exclude_actions)

        best: dict[str, tuple[int, Rule, Suggestion]] = {}
        for rule, suggestion in matches:
            if suggestion.action in excluded:
                continue
            if suggestion.action not in self.shortcuts:
                logger.debug(
                    "Rule %s suggests unknown action %s; skipping", rule.name, suggestion.action
                )
                continue

            key = f"{rule.name}:{suggestion.action}"
            last_shown = self._cooldowns.get(key)
            if last_shown is not None and (now - last_shown).total_seconds() < rule.cooldown:
                continue

            adjusted = (
                self._adjusted_priority(key, suggestion.priority, now)
                if self.enable_personalization
                else suggestion.priority
            )

            current = best.get(suggestion.action)
            if current is None or adjusted > current[0]:
                best[suggestion.action] = (adjusted, rule, suggestion)

        ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)[: max(0, top_n)]

        results: list[SuggestionResult] = []
        for adjusted, rule, suggestion in ranked:
            key = f"{rule.name}:{suggestion.action}"
            self._cooldowns[key] = now
            if self.enable_personalization:
                data = self._personalization[key]
                data.suggestion_count += 1
                data.last_suggested = now

            shortcut = self.shortcuts[suggestion.action]
            results.append(
                SuggestionResult(
                    action=suggestion.action,
                    key=shortcut.key,
                    description=shortcut.description,
                    priority=suggestion.priority,
                    adjusted_priority=adjusted,
                    rule=rule.name,
                )
            )

        return results

    def _adjusted_priority(self, key: str, priority: int, now: datetime) -> int:
        """Adjust priority based on personalization (CTR with time decay)."""
        data = self._personalization.get(key)
        if data is None or data.suggestion_count < MIN_SAMPLES_FOR_PERSONALIZATION:
            return priority

        ctr = data.ctr

        # Decay: suggestions not accepted for a long time slowly lose weight
        time_factor = 1.0
        if data.last_accepted is not None:
            since = (now - to_aware(data.last_accepted)).total_seconds()
            decay_time = max(0.0, since - 3600)  # Start decay after 1 hour
            time_factor = 0.9 ** (decay_time / (7 * 24 * 3600))  # ~10% per week

        if ctr > 0.4:
            ctr_factor = 1.15
        elif ctr > 0.2:
            ctr_factor = 1.0
        else:
            ctr_factor = 0.85

        return max(0, min(100, int(priority * ctr_factor * time_factor)))

    def mark_accepted(
        self, action: str, rule_name: str = "unknown", now: datetime | None = None
    ) -> None:
        """
        Mark a suggestion as accepted by the user.

        Args:
            action: Action ID that was accepted
            rule_name: Name of the rule that triggered the suggestion (for personalization)
            now: Time of acceptance (defaults to now)
        """
        now = now_utc() if now is None else to_aware(now)
        self._accepted[action] = self._accepted.get(action, 0) + 1

        if self.enable_personalization:
            key = f"{rule_name}:{action}"
            data = self._personalization[key]
            data.acceptance_count += 1
            data.last_accepted = now
            logger.debug(
                "Marked suggestion as accepted: %s, CTR now %d/%d",
                key,
                data.acceptance_count,
                data.suggestion_count,
            )

    def get_acceptance_count(self, action: str) -> int:
        """
        Get number of times an action was accepted.

        Args:
            action: Action ID

        Returns:
            Acceptance count
        """
        return self._accepted.get(action, 0)

    def clear_cooldowns(self) -> None:
        """Clear all cooldown timers (useful for testing)."""
        self._cooldowns.clear()

    # ------------------------------------------------------------------ persistence
    def export_state(self) -> dict[str, Any]:
        """Serializable personalization state."""
        return {
            "version": 1,
            "accepted": dict(self._accepted),
            "personalization": {k: v.to_dict() for k, v in self._personalization.items()},
        }

    def import_state(self, state: dict[str, Any]) -> None:
        """Restore personalization state produced by :meth:`export_state`."""
        accepted = state.get("accepted", {})
        if isinstance(accepted, dict):
            self._accepted = {str(k): int(v) for k, v in accepted.items()}
        items = state.get("personalization", {})
        if isinstance(items, dict):
            for key, value in items.items():
                if isinstance(value, dict):
                    self._personalization[str(key)] = PersonalizationData.from_dict(value)
