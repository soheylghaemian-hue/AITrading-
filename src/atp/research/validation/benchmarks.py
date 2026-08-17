"""§ R3.1A — deterministic naive benchmarks a real predictor must beat (RESEARCH ONLY).

A prediction has no edge unless it beats trivial baselines. Computed per horizon from the frozen samples:
  * always_bullish / always_bearish — the majority-class shortcut.
  * naive_persistence — predict the same direction the symbol realized in its previous canonical session.
  * market (SPY) actual-direction distribution — the passive market-direction baseline.
Pure, no trading, no optimization.
"""
from __future__ import annotations

from ..intel import policy

_CLASSES = ("BULLISH", "BEARISH", "NEUTRAL")


def _rate(n, d):
    return round(n / d, 4) if d else None


def compute(samples: list[dict]) -> dict:
    out = {}
    for h in policy.HORIZONS:
        sh = [s for s in samples if s["horizon"] == h and s.get("actual") in _CLASSES]
        n = len(sh)
        always = {c: _rate(sum(1 for s in sh if s["actual"] == c), n) for c in _CLASSES}
        # naive persistence: order each symbol's samples by session; predict previous actual
        persist_correct = persist_total = 0
        for sym in {s["symbol"] for s in sh}:
            seq = sorted([s for s in sh if s["symbol"] == sym], key=lambda x: x["session_date"])
            for prev, cur in zip(seq, seq[1:]):
                persist_total += 1
                if prev["actual"] == cur["actual"]:
                    persist_correct += 1
        spy = [s for s in sh if s["symbol"] == "SPY"]
        spy_dist = {c: _rate(sum(1 for s in spy if s["actual"] == c), len(spy)) for c in _CLASSES}
        out[str(h)] = {
            "n": n,
            "always_bullish_accuracy": always["BULLISH"], "always_bearish_accuracy": always["BEARISH"],
            "always_neutral_accuracy": always["NEUTRAL"],
            "naive_persistence_accuracy": _rate(persist_correct, persist_total),
            "market_spy_direction_distribution": spy_dist,
        }
    return out
