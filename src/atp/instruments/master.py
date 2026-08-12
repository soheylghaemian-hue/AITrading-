"""Instrument Master — the unified reference-data model (§5).

An instrument is not just a symbol. `InstrumentSpec` is the full reference record — identity,
venue, contract terms, margin, liquidity, derivative terms — and `InstrumentMaster` is the
registry that holds them and, crucially, understands **underlying relationships**: GOLD's spot,
future, ETF/ETC, option and certificate all reference the same underlying and are linked.

The lightweight `core.events.Instrument` remains the tradeable used in the hot path;
`InstrumentSpec.to_instrument()` projects a master record onto it. Reference data (specs) is
loaded from the broker/data vendors later; this is the model everything else depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..core.enums import AssetClass
from ..core.events import Instrument


class SettlementType(str, Enum):
    CASH = "cash"
    PHYSICAL = "physical"


class LiquidityTier(str, Enum):
    UNKNOWN = "unknown"
    TIER_1 = "tier_1"     # deep, tight — index futures, mega-cap equities, majors
    TIER_2 = "tier_2"     # liquid
    TIER_3 = "tier_3"     # thin — trade with care / smaller size


class ProductType(str, Enum):
    SPOT = "spot"
    CASH_EQUITY = "cash_equity"
    ETF = "etf"
    INDEX = "index"
    FUTURE = "future"
    OPTION = "option"
    BOND = "bond"
    FX_PAIR = "fx_pair"
    CERTIFICATE = "certificate"
    STRUCTURED = "structured"
    CRYPTO = "crypto"


@dataclass(slots=True)
class InstrumentSpec:
    # --- identity ----------------------------------------------------------
    internal_id: str
    symbol: str
    asset_class: AssetClass
    underlying: str | None = None          # underlying symbol (links a family together)

    # --- venue / classification -------------------------------------------
    exchange: str | None = None
    currency: str = "USD"
    country: str | None = None
    product_type: ProductType | None = None
    issuer: str | None = None
    calendar: str = "24x5"                 # MarketCalendar name

    # --- contract terms ----------------------------------------------------
    contract_size: float = 1.0
    multiplier: float = 1.0
    tick_size: float = 0.01
    min_quantity: float = 1.0
    lot_size: float = 1.0

    # --- risk / financing --------------------------------------------------
    leverage: float = 1.0
    margin: float | None = None            # initial margin fraction (None => unknown)
    borrow_cost: float | None = None       # annualized borrow/financing rate
    liquidity_tier: LiquidityTier = LiquidityTier.UNKNOWN

    # --- expiry / settlement (futures/options) -----------------------------
    expiration: str | None = None          # YYYYMMDD
    settlement: SettlementType = SettlementType.CASH

    # --- option-specific ---------------------------------------------------
    option_strike: float | None = None
    option_type: str | None = None         # "C" or "P"
    greeks: dict = field(default_factory=dict)   # cached greeks snapshot (filled from data)

    @property
    def key(self) -> str:
        return self.to_instrument().key

    def to_instrument(self) -> Instrument:
        return Instrument(
            symbol=self.symbol, asset_class=self.asset_class, currency=self.currency,
            multiplier=self.multiplier, expiry=self.expiration,
            strike=self.option_strike, right=self.option_type,
            underlying=self.underlying,
        )

    def round_price(self, price: float) -> float:
        """Snap a price to the instrument's tick size."""
        if self.tick_size <= 0:
            return price
        return round(price / self.tick_size) * self.tick_size

    def valid_quantity(self, qty: float) -> bool:
        return qty >= self.min_quantity and abs(qty % self.lot_size) < 1e-9


class InstrumentMaster:
    """Registry of `InstrumentSpec`s, indexed by id/key/underlying/asset class."""

    def __init__(self) -> None:
        self._by_id: dict[str, InstrumentSpec] = {}
        self._by_key: dict[str, InstrumentSpec] = {}
        self._by_underlying: dict[str, list[InstrumentSpec]] = {}

    def register(self, spec: InstrumentSpec) -> InstrumentSpec:
        self._by_id[spec.internal_id] = spec
        self._by_key[spec.key] = spec
        # A spec belongs to its underlying family; a spot/index instrument IS its own family root.
        root = spec.underlying or spec.symbol
        self._by_underlying.setdefault(root, []).append(spec)
        return spec

    def get(self, id_or_key: str) -> InstrumentSpec | None:
        return self._by_id.get(id_or_key) or self._by_key.get(id_or_key)

    def by_asset_class(self, asset_class: AssetClass) -> list[InstrumentSpec]:
        return [s for s in self._by_id.values() if s.asset_class is asset_class]

    def related(self, underlying_symbol: str) -> list[InstrumentSpec]:
        """All instruments in an underlying's family (spot, future, ETF, option, …)."""
        return list(self._by_underlying.get(underlying_symbol, []))

    def underlyings(self) -> list[str]:
        return sorted(self._by_underlying)

    def all(self) -> list[InstrumentSpec]:
        return list(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)
