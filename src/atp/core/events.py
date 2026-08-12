"""Market-data value objects (§5 Datenebenen).

Immutable (frozen) so a bar/quote can be passed through the pipeline — feature engine,
strategies, opportunity engine — without any stage mutating another stage's input. This is
part of how we structurally prevent look-ahead/state-bleed (§13).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .enums import AssetClass


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable instrument. `symbol` + `asset_class` (+ derivative terms) is the identity.

    Derivative fields are optional so equities/ETFs/FX are unchanged; futures set `expiry`,
    options set `expiry`/`strike`/`right` and (usually) the `underlying` symbol (§2)."""

    symbol: str
    asset_class: AssetClass
    currency: str = "USD"
    multiplier: float = 1.0       # contract multiplier for futures/options (§2)
    expiry: str | None = None     # YYYYMMDD (futures/options)
    strike: float | None = None   # options
    right: str | None = None      # "C" or "P" (options)
    underlying: str | None = None # underlying symbol (options/derivatives)

    @property
    def key(self) -> str:
        base = f"{self.symbol}:{self.asset_class.value}"
        if self.strike is not None and self.right and self.expiry:
            base += f":{self.expiry}:{self.strike:g}:{self.right}"
        elif self.expiry:
            base += f":{self.expiry}"
        return base

    @property
    def is_option(self) -> bool:
        return self.asset_class is AssetClass.OPTION

    @property
    def underlying_key(self) -> str:
        """Key of the underlying (for options), else this instrument's own key."""
        if self.underlying:
            return f"{self.underlying}:equity"
        return self.key

    def __str__(self) -> str:  # nicer logs
        return self.key


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV bar (§5 Preis). `ts` is the bar's close timestamp (tz-aware, UTC)."""

    instrument: Instrument
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts: datetime

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    """Top-of-book quote (§5 Liquidität). Spread/mid are derived, never stored twice."""

    instrument: Instrument
    bid: float
    ask: float
    ts: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.spread / m) * 1e4 if m else 0.0
