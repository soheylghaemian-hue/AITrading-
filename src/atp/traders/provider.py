"""Trader data provider abstraction (§ Phase G2.5).

Defines the `TraderProvider` interface every integration must implement, plus the plain data shapes it
returns. NO real provider is wired here: integrating eToro Popular Investors, Collective2, Darwinex,
TradingView Ideas (only where permitted/licensed), broker strategy marketplaces, institutional filings
or analyst portfolios each requires the provider's OWN authorized API + license — never illegal
scraping. Until a licensed provider is configured, `resolve_provider()` returns the NullTraderProvider
(yields nothing) → the pipeline persists nothing → the terminal shows NO DATA. Nothing is fabricated.

No credentials, no broker access, no trading permissions, no execution live anywhere in this module.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class TraderInfo:
    id: str
    name: str
    source: str
    market_focus: str | None = None
    strategy_type: str | None = None
    track_record_days: int | None = None


@dataclass(slots=True)
class TraderPerformance:
    trader_id: str
    total_return: float | None = None          # fraction, e.g. 0.35 = +35%
    annualized_return: float | None = None
    win_rate: float | None = None              # 0..1
    max_drawdown: float | None = None          # fraction, magnitude or signed
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    average_holding_period: float | None = None  # days
    number_of_trades: int | None = None


@dataclass(slots=True)
class TraderPosition:
    trader_id: str
    symbol: str
    direction: str                              # LONG / SHORT / NEUTRAL
    entry_price: float | None = None
    position_size: float | None = None
    timestamp: str = ""


@dataclass(slots=True)
class StrategyMetadata:
    trader_id: str
    strategy_type: str | None = None
    market_focus: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)


class TraderProvider(ABC):
    """Interface for a licensed external trader-intelligence source. Read-only; no execution."""

    name: str = "provider"

    @property
    def configured(self) -> bool:
        return False

    @abstractmethod
    def get_traders(self) -> list[TraderInfo]:
        """The tracked traders (identity + light metadata)."""

    @abstractmethod
    def get_performance(self, trader_id: str) -> TraderPerformance | None:
        """A trader's track record. None when unavailable (→ NO DATA, never fabricated)."""

    @abstractmethod
    def get_positions(self, trader_id: str) -> list[TraderPosition]:
        """A trader's current disclosed positions. Empty when unavailable."""

    @abstractmethod
    def get_strategy_metadata(self, trader_id: str) -> StrategyMetadata | None:
        """A trader's strategy classification. None when unavailable."""


class NullTraderProvider(TraderProvider):
    """The default: no licensed source configured → yields nothing (NO DATA everywhere)."""

    name = "null"

    @property
    def configured(self) -> bool:
        return False

    def get_traders(self) -> list[TraderInfo]:
        return []

    def get_performance(self, trader_id: str) -> TraderPerformance | None:
        return None

    def get_positions(self, trader_id: str) -> list[TraderPosition]:
        return []

    def get_strategy_metadata(self, trader_id: str) -> StrategyMetadata | None:
        return None


# Registry for licensed integrations. `resolve_provider` picks one from ATP_TRADER_PROVIDER; the real
# SEC 13F provider (§ R1.2) is loaded LAZILY to avoid a circular import. Default is always Null.
PROVIDERS: dict[str, type[TraderProvider]] = {"null": NullTraderProvider}


def _load_provider_class(key: str) -> type[TraderProvider]:
    if key in PROVIDERS:
        return PROVIDERS[key]
    if key in ("sec13f", "sec", "edgar", "13f"):
        from .sec13f import Sec13FTraderProvider          # lazy import (sec13f imports this module)
        return Sec13FTraderProvider
    return NullTraderProvider


def resolve_provider() -> TraderProvider:
    """Select the configured provider (env ATP_TRADER_PROVIDER); default = Null (NO DATA). 'sec13f'
    activates the real SEC 13F institutional-holdings provider (needs ATP_SEC_USER_AGENT)."""
    key = (os.environ.get("ATP_TRADER_PROVIDER") or "null").strip().lower()
    try:
        return _load_provider_class(key)()
    except Exception:
        return NullTraderProvider()
