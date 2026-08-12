"""Macro rates model (§5 Macro/Events).

A small table of per-currency short-term policy rates with history, exposing the two things
the macro/FX specialists need: the **carry** (rate differential) between two currencies, and
the **rate trend** (is a central bank hiking or cutting?). Fed by a macro-data feed in
production; set directly in tests/demos. Pure stdlib.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime


class RatesTable:
    def __init__(self, *, history: int = 24) -> None:
        self._rates: dict[str, float] = {}
        self._hist: dict[str, deque[float]] = {}
        self._history = history

    def set_rate(self, currency: str, rate: float, ts: datetime | None = None) -> None:
        """Set a currency's current policy rate (as a fraction, e.g. 0.045 for 4.5%)."""
        ccy = currency.upper()
        self._rates[ccy] = rate
        self._hist.setdefault(ccy, deque(maxlen=self._history)).append(rate)

    def rate(self, currency: str) -> float | None:
        return self._rates.get(currency.upper())

    def carry(self, base: str, quote: str) -> float | None:
        """Annualized carry of holding `base` funded in `quote` = rate(base) − rate(quote)."""
        rb, rq = self.rate(base), self.rate(quote)
        if rb is None or rq is None:
            return None
        return rb - rq

    def trend(self, currency: str) -> float:
        """Recent change in the policy rate (positive => hiking, negative => cutting)."""
        hist = self._hist.get(currency.upper())
        if not hist or len(hist) < 2:
            return 0.0
        return hist[-1] - hist[0]
