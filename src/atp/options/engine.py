"""Options data engine (§5/§8).

Holds the latest chain-derived features per underlying and a rolling history of ATM IV so it
can report an **IV rank** (where current IV sits in its own recent range). This is the shared
state a volatility specialist reads — fed by an options-data feed in production, or synthesized
chains in tests/demos. Separate from the bar path: option chains aren't bars.
"""

from __future__ import annotations

from collections import deque

from .chain import ChainFeatures, OptionChain, compute_features


class OptionsEngine:
    def __init__(self, *, iv_history: int = 252) -> None:
        self._features: dict[str, ChainFeatures] = {}
        self._iv_hist: dict[str, deque[float]] = {}
        self._iv_history = iv_history

    def update(self, chain: OptionChain) -> ChainFeatures:
        feats = compute_features(chain)
        self._features[chain.underlying] = feats
        hist = self._iv_hist.setdefault(chain.underlying, deque(maxlen=self._iv_history))
        hist.append(feats.atm_iv)
        return feats

    def features(self, underlying: str) -> ChainFeatures | None:
        return self._features.get(underlying)

    def iv_rank(self, underlying: str) -> float | None:
        """Fraction of recent history at/below current ATM IV, [0,1]; None if too little data."""
        hist = self._iv_hist.get(underlying)
        if not hist or len(hist) < 5:
            return None
        cur = hist[-1]
        return sum(1 for v in hist if v <= cur) / len(hist)
