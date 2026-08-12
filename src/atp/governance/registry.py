"""Strategy registry — the live on/off switch under governance (§19).

§19: a strategy whose performance decays can be *automatically suspended* and rolled back to
an earlier version. The registry holds each strategy's current status and version; the desk
consults `is_active()` before it will act on that strategy's signals. This is the mechanism
that lets governance take a failing strategy offline without touching the strategy's code.

An unregistered strategy defaults to active, so a desk with no governance configured behaves
exactly as before — governance is strictly additive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..logging_config import get_logger

log = get_logger("governance")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StrategyStatus(str, Enum):
    ACTIVE = "active"
    PROBATION = "probation"   # still trading, but flagged and watched
    SUSPENDED = "suspended"   # taken offline; signals ignored by the desk


@dataclass(slots=True)
class StrategyState:
    name: str
    version: str = "v0"
    status: StrategyStatus = StrategyStatus.ACTIVE
    reason: str = ""
    since: datetime = field(default_factory=_now)

    @property
    def tradable(self) -> bool:
        return self.status is not StrategyStatus.SUSPENDED


class StrategyRegistry:
    def __init__(self) -> None:
        self._states: dict[str, StrategyState] = {}

    def register(self, name: str, *, version: str = "v0",
                 status: StrategyStatus = StrategyStatus.ACTIVE) -> StrategyState:
        state = StrategyState(name=name, version=version, status=status)
        self._states[name] = state
        return state

    def get(self, name: str) -> StrategyState | None:
        return self._states.get(name)

    def _ensure(self, name: str) -> StrategyState:
        state = self._states.get(name)
        if state is None:
            state = self._states[name] = StrategyState(name=name)
        return state

    def is_active(self, name: str) -> bool:
        """Desk gate: unknown strategies default to active (governance is opt-in)."""
        state = self._states.get(name)
        return state.tradable if state is not None else True

    def suspend(self, name: str, reason: str, *, when: datetime | None = None) -> StrategyState:
        state = self._ensure(name)
        if state.status is not StrategyStatus.SUSPENDED:
            log.warning("SUSPEND strategy '%s' — %s", name, reason)
        state.status = StrategyStatus.SUSPENDED
        state.reason = reason
        state.since = when or _now()
        return state

    def set_probation(self, name: str, reason: str, *, when: datetime | None = None) -> StrategyState:
        state = self._ensure(name)
        if state.status is StrategyStatus.ACTIVE:
            log.info("PROBATION strategy '%s' — %s", name, reason)
            state.status = StrategyStatus.PROBATION
            state.reason = reason
            state.since = when or _now()
        return state

    def reactivate(self, name: str, reason: str = "", *,
                   version: str | None = None, when: datetime | None = None) -> StrategyState:
        state = self._ensure(name)
        log.info("REACTIVATE strategy '%s'%s — %s", name, f" -> {version}" if version else "", reason)
        state.status = StrategyStatus.ACTIVE
        state.reason = reason
        if version is not None:
            state.version = version
        state.since = when or _now()
        return state

    def states(self) -> list[StrategyState]:
        return list(self._states.values())

    def suspended(self) -> list[str]:
        return [s.name for s in self._states.values() if s.status is StrategyStatus.SUSPENDED]
