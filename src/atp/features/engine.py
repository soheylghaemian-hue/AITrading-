"""Feature Engine (§5 Datenebenen, §24 Phase 5).

Turns a stream of bars into the rolling, point-in-time features the strategies and regime
engine consume. Everything is computed from bars *already seen* (append-only deques), so a
feature can never peek at the future — look-ahead is impossible by construction (§13).

Dependency-free (stdlib `statistics`) so it runs in the offline suite (§25).
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from ..core.events import Bar, Instrument


@dataclass(slots=True)
class FeatureSet:
    """Point-in-time features for one instrument."""

    instrument: Instrument
    ts: datetime
    price: float
    n_bars: int
    ready: bool
    sma_fast: float
    sma_slow: float
    close_std: float          # stdev of close over the slow window (price units)
    trend: float              # (sma_fast - sma_slow) / close_std  — dimensionless
    ret: float                # last bar simple return
    realized_vol: float       # stdev of recent returns
    vol_percentile: float     # rank of realized_vol within its own history [0,1]
    rel_volume: float         # volume / avg recent volume

    @property
    def stop_distance(self) -> float:
        """A volatility-scaled stop distance in price units, used for risk sizing (§10)."""
        return max(self.close_std, self.price * 0.005)


class _State:
    __slots__ = ("closes", "volumes", "returns", "vol_hist", "prev_close")

    def __init__(self, slow: int, vol_window: int) -> None:
        self.closes: deque[float] = deque(maxlen=slow)
        self.volumes: deque[float] = deque(maxlen=slow)
        self.returns: deque[float] = deque(maxlen=vol_window)
        self.vol_hist: deque[float] = deque(maxlen=252)
        self.prev_close: float | None = None


class FeatureEngine:
    def __init__(self, *, fast: int = 10, slow: int = 30, vol_window: int = 20) -> None:
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self._fast = fast
        self._slow = slow
        self._vol_window = vol_window
        self._state: dict[str, _State] = {}
        self._latest: dict[str, FeatureSet] = {}

    def update(self, bar: Bar) -> FeatureSet:
        """Ingest a bar and return the refreshed FeatureSet for its instrument."""
        key = bar.instrument.key
        st = self._state.get(key)
        if st is None:
            st = self._state[key] = _State(self._slow, self._vol_window)

        st.closes.append(bar.close)
        st.volumes.append(bar.volume)
        if st.prev_close is not None and st.prev_close != 0:
            st.returns.append((bar.close - st.prev_close) / st.prev_close)
        st.prev_close = bar.close

        closes = list(st.closes)
        sma_fast = statistics.fmean(closes[-self._fast:]) if len(closes) >= self._fast else statistics.fmean(closes)
        sma_slow = statistics.fmean(closes)
        close_std = statistics.pstdev(closes) if len(closes) >= 2 else 0.0
        trend = (sma_fast - sma_slow) / close_std if close_std > 0 else 0.0

        rets = list(st.returns)
        realized_vol = statistics.pstdev(rets) if len(rets) >= 2 else 0.0
        st.vol_hist.append(realized_vol)
        vh = list(st.vol_hist)
        vol_percentile = (sum(1 for v in vh if v <= realized_vol) / len(vh)) if vh else 0.5

        avg_vol = statistics.fmean(st.volumes) if st.volumes else 0.0
        rel_volume = (bar.volume / avg_vol) if avg_vol > 0 else 1.0

        fs = FeatureSet(
            instrument=bar.instrument,
            ts=bar.ts,
            price=bar.close,
            n_bars=len(closes),
            ready=len(closes) >= self._slow,
            sma_fast=sma_fast,
            sma_slow=sma_slow,
            close_std=close_std,
            trend=trend,
            ret=rets[-1] if rets else 0.0,
            realized_vol=realized_vol,
            vol_percentile=vol_percentile,
            rel_volume=rel_volume,
        )
        self._latest[key] = fs
        return fs

    def latest(self, instrument: Instrument) -> FeatureSet | None:
        return self._latest.get(instrument.key)

    def all_latest(self) -> dict[str, FeatureSet]:
        return dict(self._latest)
