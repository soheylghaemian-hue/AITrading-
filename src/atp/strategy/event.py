"""Event specialist (§8 Event, using §5 events).

Two behaviors around scheduled events:

1. **De-risk into the event.** When a high-impact event (earnings, CPI, FOMC) is imminent, it
   emits CLOSE to flatten — you don't want an open directional bet across a binary event.
2. **Trade the surprise.** Just after the event, if the released number beat/missed
   expectations beyond a threshold, it takes the surprise's direction (beat => buy, miss =>
   sell) for a short reaction window.

Reads the shared `EconomicCalendar`. Emits on state changes only (blackout / react / idle), so
it fires once per transition rather than every bar.
"""

from __future__ import annotations

from datetime import timedelta

from ..core.enums import Action, Regime
from ..features.engine import FeatureSet
from ..macro.calendar import EconomicCalendar
from .base import Signal, Strategy

_CLOSE, _IDLE, _BUY, _SELL = 2, 0, 1, -1


class EventStrategy(Strategy):
    active_regimes = frozenset()

    def __init__(
        self,
        calendar: EconomicCalendar,
        *,
        blackout: timedelta = timedelta(hours=24),
        react: timedelta = timedelta(hours=12),
        min_surprise: float = 0.02,
        importance: str = "high",
    ) -> None:
        self._cal = calendar
        self._blackout = blackout
        self._react = react
        self._min_surprise = min_surprise
        self._importance = importance
        self._prev: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "event"

    def reset(self) -> None:
        self._prev.clear()

    def generate(self, fs: FeatureSet, regime: Regime) -> Signal | None:
        if not fs.ready:
            return None
        key = fs.instrument.key
        now = fs.ts

        rationale = ""
        state = _IDLE
        blackout = self._cal.in_blackout(key, now, self._blackout, self._importance)
        if blackout is not None:
            state = _CLOSE
            rationale = f"flatten into {blackout.kind} @ {blackout.ts:%Y-%m-%d %H:%M}"
        else:
            ev = self._cal.recent_surprise(key, now, self._react, self._importance, self._min_surprise)
            if ev is not None:
                state = _BUY if ev.surprise > 0 else _SELL
                rationale = f"{ev.kind} surprise {ev.surprise:+.1%}"

        prev = self._prev.get(key, _IDLE)
        self._prev[key] = state
        if state == _IDLE or state == prev:
            return None

        action = {_CLOSE: Action.CLOSE, _BUY: Action.BUY, _SELL: Action.SELL}[state]
        # A modest event-reaction expected move, scaled by realized vol.
        expected_return = 0.0 if action is Action.CLOSE else max(fs.realized_vol, 0.001) * 2.0
        return Signal(
            instrument=fs.instrument,
            action=action,
            confidence=0.6 if action is Action.CLOSE else 0.8,
            expected_return=expected_return,
            stop_distance=fs.stop_distance,
            strategy=self.name,
            regime=regime,
            ts=fs.ts,
            rationale=rationale,
        )
