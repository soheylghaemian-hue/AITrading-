"""Multi-leg option execution & settlement (§16/§5).

Options are rarely traded one leg at a time — spreads, straddles and strangles are placed as a
*combo*: several legs worked together with a single net debit/credit and combined greeks. This
module builds the common structures, prices them via the Black–Scholes layer, executes them
against a broker leg by leg, and settles them at expiry (cash settlement to intrinsic value).

Physical assignment (converting exercised equity options into underlying shares) is a
documented extension — cash settlement is modeled here, which is exact and broker-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..brokers.base import Broker, Order, OrderResult
from ..core.enums import AssetClass, OrderType, Side
from ..core.events import Instrument, QuoteEvent
from ..logging_config import get_logger
from .pricing import Greeks, black_scholes, greeks

log = get_logger("options.exec")


def option(underlying: str, expiry: str, strike: float, right: str, *,
           multiplier: float = 100.0, currency: str = "USD") -> Instrument:
    """Construct an option Instrument with a unique, descriptive symbol."""
    sym = f"{underlying}{expiry}{right}{strike:g}"
    return Instrument(sym, AssetClass.OPTION, currency=currency, multiplier=multiplier,
                      expiry=expiry, strike=strike, right=right.upper(), underlying=underlying)


@dataclass(slots=True)
class OptionLeg:
    instrument: Instrument
    side: Side
    ratio: float = 1.0        # contracts per unit of combo quantity


@dataclass(slots=True)
class Combo:
    name: str
    legs: list[OptionLeg]

    def net_debit(self, *, spot: float, T: float, r: float, iv: float) -> float:
        """Net premium per combo unit: positive = debit (you pay), negative = credit."""
        total = 0.0
        for leg in self.legs:
            px = black_scholes(spot, leg.instrument.strike, T, r, iv, leg.instrument.right)
            total += leg.side.sign * leg.ratio * px * leg.instrument.multiplier
        return total

    def greeks(self, *, spot: float, T: float, r: float, iv: float) -> Greeks:
        """Position greeks per combo unit (long legs +, short legs −)."""
        agg = Greeks(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        for leg in self.legs:
            g = greeks(spot, leg.instrument.strike, T, r, iv, leg.instrument.right)
            s = leg.side.sign * leg.ratio * leg.instrument.multiplier
            agg = Greeks(
                price=agg.price + s * g.price, delta=agg.delta + s * g.delta,
                gamma=agg.gamma + s * g.gamma, vega=agg.vega + s * g.vega,
                theta=agg.theta + s * g.theta, rho=agg.rho + s * g.rho,
            )
        return agg


# --- common structures -------------------------------------------------------
def vertical_call_spread(underlying: str, expiry: str, long_strike: float, short_strike: float) -> Combo:
    """Debit call spread: buy the lower-strike call, sell the higher-strike call."""
    return Combo("vertical_call_spread", [
        OptionLeg(option(underlying, expiry, long_strike, "C"), Side.BUY),
        OptionLeg(option(underlying, expiry, short_strike, "C"), Side.SELL),
    ])


def vertical_put_spread(underlying: str, expiry: str, long_strike: float, short_strike: float) -> Combo:
    """Debit put spread: buy the higher-strike put, sell the lower-strike put."""
    return Combo("vertical_put_spread", [
        OptionLeg(option(underlying, expiry, long_strike, "P"), Side.BUY),
        OptionLeg(option(underlying, expiry, short_strike, "P"), Side.SELL),
    ])


def straddle(underlying: str, expiry: str, strike: float, *, long: bool = True) -> Combo:
    """Long (or short) straddle: same-strike call + put."""
    side = Side.BUY if long else Side.SELL
    return Combo("straddle", [
        OptionLeg(option(underlying, expiry, strike, "C"), side),
        OptionLeg(option(underlying, expiry, strike, "P"), side),
    ])


def strangle(underlying: str, expiry: str, put_strike: float, call_strike: float, *, long: bool = True) -> Combo:
    """Long (or short) strangle: OTM put + OTM call."""
    side = Side.BUY if long else Side.SELL
    return Combo("strangle", [
        OptionLeg(option(underlying, expiry, put_strike, "P"), side),
        OptionLeg(option(underlying, expiry, call_strike, "C"), side),
    ])


# --- execution ---------------------------------------------------------------
@dataclass(slots=True)
class ComboResult:
    legs: list[OrderResult]
    net_cash: float          # signed cashflow: negative = net debit paid

    @property
    def filled(self) -> bool:
        return bool(self.legs) and all(r.filled for r in self.legs)


async def execute_combo(broker: Broker, combo: Combo, quantity: float) -> ComboResult:
    """Execute each leg as a market order (broker must have quotes for the legs). Returns the
    per-leg results and the net cashflow (negative = you paid a debit)."""
    results: list[OrderResult] = []
    net_cash = 0.0
    for leg in combo.legs:
        qty = quantity * leg.ratio
        if qty <= 0:
            continue
        result = await broker.place_order(
            Order(leg.instrument, leg.side, qty, OrderType.MARKET)
        )
        results.append(result)
        if result.fill is not None:
            f = result.fill
            cash = -f.side.sign * f.price * f.quantity * leg.instrument.multiplier
            net_cash += cash
    return ComboResult(legs=results, net_cash=net_cash)


# --- settlement --------------------------------------------------------------
def _intrinsic(right: str, strike: float, spot: float) -> float:
    return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)


@dataclass(slots=True)
class Settlement:
    instrument_key: str
    intrinsic: float
    quantity: float
    shares: float = 0.0        # underlying shares established (physical assignment); 0 for cash


async def settle_expiration(broker: Broker, spots: dict[str, float], now: datetime,
                            *, style: str = "cash") -> list[Settlement]:
    """Settle every option position at/after its expiry (§5).

    `spots` maps an underlying symbol (or its `:equity` key) to the settlement price.

    * ``style="cash"``     — close each option at intrinsic via a synthetic quote (index style).
    * ``style="physical"`` — the same booking, **plus** for in-the-money legs, establish the
      underlying share position at spot (equity style): exercise/assignment. Long call → long
      stock; long put → short stock; short call assigned → short stock; short put assigned →
      long stock. Because the option's P&L is realized at settlement and the stock enters at
      market, this is economically identical to physical exercise and keeps the book consistent.
    """
    if not hasattr(broker, "set_quote"):
        raise TypeError("settlement requires a broker with set_quote (e.g. PaperBroker)")
    today = now.strftime("%Y%m%d")
    settled: list[Settlement] = []
    positions = await broker.get_positions()
    for key, pos in positions.items():
        inst = pos.instrument
        if not inst.is_option or inst.expiry is None or inst.expiry > today:
            continue
        spot = spots.get(inst.underlying) or spots.get(inst.underlying_key)
        if spot is None:
            log.warning("no settlement spot for %s (underlying %s)", key, inst.underlying)
            continue

        # 1) Close the option at intrinsic (books the option's P&L).
        intrinsic = _intrinsic(inst.right, inst.strike, spot)
        broker.set_quote(QuoteEvent(inst, intrinsic, intrinsic, now))  # type: ignore[attr-defined]
        opt_side = Side.SELL if pos.quantity > 0 else Side.BUY
        await broker.place_order(Order(inst, opt_side, abs(pos.quantity), OrderType.MARKET, reduce_only=True))

        # 2) Physical: for ITM legs, establish the underlying share position at spot.
        shares = 0.0
        if style == "physical" and intrinsic > 0 and inst.underlying:
            right_sign = 1 if inst.right == "C" else -1
            pos_sign = 1 if pos.quantity > 0 else -1
            direction = right_sign * pos_sign           # +1 => long stock, -1 => short stock
            shares = abs(pos.quantity) * inst.multiplier
            stock = Instrument(inst.underlying, AssetClass.EQUITY, currency=inst.currency)
            broker.set_quote(QuoteEvent(stock, spot, spot, now))  # type: ignore[attr-defined]
            stock_side = Side.BUY if direction > 0 else Side.SELL
            await broker.place_order(Order(stock, stock_side, shares, OrderType.MARKET))

        settled.append(Settlement(key, intrinsic, pos.quantity, shares=shares))
        log.info("settled %s intrinsic=%.2f qty=%.0f style=%s shares=%.0f",
                 key, intrinsic, pos.quantity, style, shares)
    return settled
