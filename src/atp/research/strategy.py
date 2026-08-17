"""§ R3.0 — versioned research-strategy contract + the immutable OHLC_TREND_BASELINE v1.

A research decision is intelligence about historical logic — NOT a live trading signal. It cannot be
passed to execution. The contract is a PURE function of a point-in-time context (only completed bars
available at the decision time) → a `ResearchDecision`. No optimizer, no tunable search.

OHLC_TREND_BASELINE v1 (correction #2): fast SMA(20), slow SMA(50), ATR(14) — all over COMPLETED bars.
  * entry  → bullish SMA cross (fast crosses above slow) on the just-completed bar.
  * exit   → bearish SMA cross (fast crosses below slow).
  * initial stop = decision-bar close − 2·ATR14, computed ONLY from completed data at the entry decision
    and persisted with the trade — never widened or recomputed with future bars.
The engine turns ENTER_LONG into a next-bar-open fill and sizes it from `risk_per_share = 2·ATR14`.
"""
from __future__ import annotations

import abc
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

ENTER_LONG, EXIT, HOLD, NO_DECISION = "ENTER_LONG", "EXIT", "HOLD", "NO_DECISION"
ACTIONS = (ENTER_LONG, EXIT, HOLD, NO_DECISION)


@dataclass(frozen=True, slots=True)
class ResearchBar:
    ts: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(slots=True)
class PitContext:
    """Point-in-time context. `bars` are completed bars whose availability ≤ the decision time, oldest→
    newest; the last is the decision bar. A strategy may read NOTHING else (no future bars, no outcomes)."""
    symbol: str
    bars: list[ResearchBar]


@dataclass(slots=True)
class ResearchDecision:
    ts: str
    symbol: str
    strategy_id: str
    strategy_version: int
    action: str
    confidence: float | None = None
    evidence: dict = field(default_factory=dict)
    missing_inputs: list[str] = field(default_factory=list)
    reason: str = ""

    def checksum(self) -> str:
        payload = {
            "ts": self.ts, "symbol": self.symbol, "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version, "action": self.action,
            "confidence": (None if self.confidence is None else round(float(self.confidence), 6)),
            "evidence": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in sorted(self.evidence.items())},
            "missing_inputs": sorted(self.missing_inputs),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sma(closes: list[Decimal], n: int) -> Decimal | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:], Decimal(0)) / Decimal(n)


def atr(bars: list[ResearchBar], n: int) -> Decimal | None:
    """Average True Range over `n` completed bars (simple mean of true ranges — deterministic, auditable).
    Needs n+1 bars (the first TR references the prior close)."""
    if len(bars) < n + 1:
        return None
    trs: list[Decimal] = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs, Decimal(0)) / Decimal(n)


class ResearchStrategy(abc.ABC):
    strategy_id: str
    version: int
    warmup_bars: int

    @property
    @abc.abstractmethod
    def config(self) -> dict: ...

    def config_checksum(self) -> str:
        payload = {"strategy_id": self.strategy_id, "version": self.version, "config": self.config}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @abc.abstractmethod
    def decide(self, ctx: PitContext) -> ResearchDecision: ...


class OhlcTrendBaseline(ResearchStrategy):
    strategy_id = "OHLC_TREND_BASELINE"
    version = 1

    def __init__(self, *, fast: int = 20, slow: int = 50, atr_period: int = 14,
                 atr_stop_mult: str | Decimal = "2") -> None:
        self.fast, self.slow, self.atr_period = fast, slow, atr_period
        self.atr_stop_mult = Decimal(str(atr_stop_mult))
        self.warmup_bars = max(slow, atr_period + 1)

    @property
    def config(self) -> dict:
        return {"fast": self.fast, "slow": self.slow, "atr_period": self.atr_period,
                "atr_stop_mult": str(self.atr_stop_mult), "long_only": True,
                "pyramiding": False, "profit_target": None,
                "exit": ["SMA_BEARISH_CROSS", "INITIAL_STOP", "EOT_LIQUIDATION"]}

    def decide(self, ctx: PitContext) -> ResearchDecision:
        bars = ctx.bars
        ts = bars[-1].ts if bars else ""
        base = dict(ts=ts, symbol=ctx.symbol, strategy_id=self.strategy_id, strategy_version=self.version)
        if len(bars) < self.warmup_bars:
            return ResearchDecision(**base, action=NO_DECISION,
                                    missing_inputs=[f"insufficient_bars<{self.warmup_bars}"],
                                    reason="warm-up not satisfied")
        closes = [b.close for b in bars]
        cur_fast, cur_slow = sma(closes, self.fast), sma(closes, self.slow)
        prev_fast, prev_slow = sma(closes[:-1], self.fast), sma(closes[:-1], self.slow)
        a = atr(bars, self.atr_period)
        if None in (cur_fast, cur_slow, prev_fast, prev_slow, a):
            return ResearchDecision(**base, action=NO_DECISION, missing_inputs=["indicators_unavailable"])
        decision_close = bars[-1].close
        stop = decision_close - self.atr_stop_mult * a
        risk_per_share = decision_close - stop     # == atr_stop_mult * ATR by construction
        ev = {"fast_sma": cur_fast, "slow_sma": cur_slow, "prev_fast_sma": prev_fast,
              "prev_slow_sma": prev_slow, "atr": a, "decision_close": decision_close,
              "expected_entry_ref": decision_close, "initial_stop": stop, "risk_per_share": risk_per_share}
        if prev_fast <= prev_slow and cur_fast > cur_slow:
            return ResearchDecision(**base, action=ENTER_LONG, evidence=ev, reason="bullish SMA cross")
        if prev_fast >= prev_slow and cur_fast < cur_slow:
            return ResearchDecision(**base, action=EXIT, evidence=ev, reason="bearish SMA cross")
        return ResearchDecision(**base, action=HOLD, evidence=ev, reason="no cross")


OHLC_TREND_BASELINE = "OHLC_TREND_BASELINE"
_REGISTRY = {(OHLC_TREND_BASELINE, 1): OhlcTrendBaseline}


def get_strategy(strategy_id: str, version: int) -> ResearchStrategy:
    cls = _REGISTRY.get((strategy_id, int(version)))
    if cls is None:
        raise ValueError(f"unknown strategy {strategy_id} v{version}")
    return cls()
