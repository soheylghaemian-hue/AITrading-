"""Read-only AI observation pass for the Command Center (§ Phase 2A).

Runs the specialists over REAL warmed features and reports, per (agent, instrument), one of:

  * SIGNAL       — the specialist has a directional (BUY/SELL) view,
  * OBSERVATION  — the specialist ran but has no directional view (HOLD/None),
  * NO DATA      — features could not be warmed, or the specialist needs data we don't have.

This is strictly analysis: it never touches a broker, never sizes, never routes an order. It is
the read-only counterpart of the desk's step() — same features/regime/strategies, no execution.
Nothing is fabricated (§33): missing data yields NO DATA, never an invented signal.
"""

from __future__ import annotations

from ..core.events import Bar
from ..features.engine import FeatureEngine
from ..regime.classifier import RegimeClassifier
from ..strategy.base import Strategy


def observe_readonly(
    bars_by_key: dict[str, list[Bar]],
    strategies: list[Strategy],
    *,
    regime: RegimeClassifier | None = None,
    fast: int = 10,
    slow: int = 30,
    vol_window: int = 20,
) -> list[dict]:
    """Return read-only observations for every (agent, instrument). No execution, ever."""
    regime = regime or RegimeClassifier()
    out: list[dict] = []
    for key, bars in bars_by_key.items():
        inst = bars[-1].instrument if bars else None
        fe = FeatureEngine(fast=fast, slow=slow, vol_window=vol_window)
        for b in bars:
            fe.update(b)
        fs = fe.latest(inst) if inst is not None else None

        if fs is None or not fs.ready:
            for strat in strategies:
                out.append({
                    "agent": strat.name, "instrument": key,
                    "status": "NO DATA", "action": None, "confidence": None,
                    "expected_return": None,
                    "reason": "insufficient real data to warm features",
                })
            continue

        reg = regime.classify(fs)
        for strat in strategies:
            try:
                sig = strat.generate(fs, reg)
            except Exception as exc:  # noqa: BLE001 — engine-backed specialists lacking data
                out.append({
                    "agent": strat.name, "instrument": key, "status": "NO DATA",
                    "action": None, "confidence": None, "expected_return": None,
                    "reason": f"needs data this specialist can't source read-only ({type(exc).__name__})",
                })
                continue
            if sig is not None and sig.is_directional:
                out.append({
                    "agent": strat.name, "instrument": key, "status": "SIGNAL",
                    "action": sig.action.value, "confidence": sig.confidence,
                    "expected_return": sig.expected_return, "regime": reg.value,
                    "reason": sig.rationale or "",
                })
            else:
                out.append({
                    "agent": strat.name, "instrument": key, "status": "OBSERVATION",
                    "action": (sig.action.value if sig is not None else None),
                    "confidence": (sig.confidence if sig is not None else None),
                    "expected_return": None, "regime": reg.value,
                    "reason": "no directional view",
                })
    return out
