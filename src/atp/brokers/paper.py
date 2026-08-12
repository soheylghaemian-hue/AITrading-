"""Deterministic in-memory broker for backtesting & paper trading (§14/§24 Phase 14).

Models the frictions the concept insists on (§13/§20): commission, bid/ask spread (via the
quote it's given), and slippage. It is fully deterministic given the same quotes and orders,
which is what lets the backtester assert reproducibility and lets equity be a pure function
of the fed data — no fabricated P&L (§25).

Accounting notes (see docs/DECISIONS.md):
* `cash` reflects every cashflow AND every commission, so `equity = cash + unrealized`
  is net of all costs — the honest number.
* `realized_pnl` changes *only* when a position is reduced/closed, so the backtester can
  detect closed trades by watching it. Each close is booked net of that fill's commission;
  the entry commission is already in `cash`/`equity`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from typing import TYPE_CHECKING

from ..core.enums import OrderStatus, OrderType, Side
from ..core.events import Instrument, QuoteEvent
from ..logging_config import get_logger
from .base import Account, Broker, Fill, Order, OrderResult, Position

if TYPE_CHECKING:
    from ..execution.impact import MarketImpactModel

log = get_logger("broker.paper")


@dataclass(slots=True)
class _Pos:
    qty: float = 0.0        # signed
    avg: float = 0.0        # average entry price


class PaperBroker(Broker):
    def __init__(
        self,
        starting_cash: float = 100_000.0,
        *,
        commission_per_unit: float = 0.005,
        min_commission: float = 1.0,
        slippage_bps: float = 1.0,
        impact_model: "MarketImpactModel | None" = None,
        commission_model=None,
        slippage_model=None,
    ) -> None:
        self._cash = starting_cash
        self._commission_per_unit = commission_per_unit
        self._min_commission = min_commission
        self._slippage_bps = slippage_bps
        self._impact_model = impact_model
        # Optional pluggable cost models (§20). Defaults keep the prior fixed behavior.
        self._commission_model = commission_model
        self._slippage_model = slippage_model
        self._positions: dict[str, _Pos] = {}
        self._quotes: dict[str, QuoteEvent] = {}
        self._instruments: dict[str, Instrument] = {}
        self._liquidity: dict[str, float] = {}   # per-instrument avg volume, for impact (§16)
        self._realized_pnl = 0.0
        self._connected = False

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    # ------------------------------------------------------------- market data
    def set_liquidity(self, instrument_key: str, adv: float) -> None:
        """Set the average volume used by the market-impact model for an instrument (§16)."""
        self._liquidity[instrument_key] = adv

    def credit_cash(self, amount: float) -> None:
        """Credit (or debit, if negative) cash — used by dividends/adjustments (§3)."""
        self._cash += amount

    def adjust_position(self, instrument: Instrument, new_quantity: float, new_avg_price: float) -> None:
        """Set a position's quantity and average price directly — used by corporate actions
        (e.g. a stock split adjusts shares and basis) (§3)."""
        key = instrument.key
        self._instruments[key] = instrument
        self._positions[key] = _Pos(qty=new_quantity, avg=new_avg_price)

    def set_quote(self, quote: QuoteEvent) -> None:
        """Feed the latest top-of-book. Fills and marks use this (§16 execution)."""
        key = quote.instrument.key
        self._quotes[key] = quote
        self._instruments[key] = quote.instrument

    def _mark(self, key: str) -> float:
        q = self._quotes.get(key)
        return q.mid if q else (self._positions[key].avg if key in self._positions else 0.0)

    # ------------------------------------------------------------- accounting
    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def _commission(self, qty: float, price: float = 0.0, multiplier: float = 1.0) -> float:
        if self._commission_model is not None:
            return self._commission_model.commission(quantity=qty, price=price, multiplier=multiplier)
        return max(self._min_commission, self._commission_per_unit * qty)

    async def get_positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for key, p in self._positions.items():
            if p.qty == 0:
                continue
            out[key] = Position(
                instrument=self._instruments[key],
                quantity=p.qty,
                avg_price=p.avg,
                market_price=self._mark(key),
            )
        return out

    async def get_account(self) -> Account:
        unrealized = 0.0
        gross = 0.0
        net = 0.0
        for key, p in self._positions.items():
            if p.qty == 0:
                continue
            mark = self._mark(key)
            mult = self._instruments[key].multiplier
            unrealized += (mark - p.avg) * p.qty * mult
            gross += abs(p.qty) * mark * mult
            net += p.qty * mark * mult
        # Equity = cash + market value of positions. `cash` already reflects the cost/proceeds
        # of establishing each position (buying spent cash, shorting raised it), so the signed
        # position value `net` — NOT unrealized P&L — is what reconciles cash back to equity.
        # (unrealized is derived separately for reporting.)
        equity = self._cash + net
        return Account(
            cash=self._cash,
            equity=equity,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized,
            gross_exposure=gross,
            net_exposure=net,
            positions=await self.get_positions(),
        )

    # ------------------------------------------------------------- execution
    def _fill_price(self, order: Order, quote: QuoteEvent) -> float:
        """Marketable fill: BUY at ask, SELL at bid, plus adverse slippage and (size-
        dependent) market impact (§16). Impact only applies when an impact model and a
        liquidity reference are configured — otherwise fills are unchanged."""
        base = quote.ask if order.side is Side.BUY else quote.bid
        adv = self._liquidity.get(order.instrument.key)
        if self._slippage_model is not None:
            cost_bps = self._slippage_model.slippage_bps(
                quantity=order.quantity, price=base, adv=adv, spread_bps=quote.spread_bps,
            )
        else:
            cost_bps = self._slippage_bps
        if self._impact_model is not None and adv:
            cost_bps += self._impact_model.impact_bps(order.quantity, adv)
        adverse = base * (cost_bps / 1e4)
        return base + adverse if order.side is Side.BUY else base - adverse

    async def place_order(self, order: Order) -> OrderResult:
        key = order.instrument.key
        quote = self._quotes.get(key)
        if quote is None:
            return OrderResult(order, OrderStatus.REJECTED, reason="no market data")

        # Limit orders that aren't marketable at the current quote don't fill this step.
        fill_price = self._fill_price(order, quote)
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            assert order.limit_price is not None
            if order.side is Side.BUY and fill_price > order.limit_price:
                return OrderResult(order, OrderStatus.NEW, reason="limit not marketable")
            if order.side is Side.SELL and fill_price < order.limit_price:
                return OrderResult(order, OrderStatus.NEW, reason="limit not marketable")
            fill_price = order.limit_price

        self._instruments.setdefault(key, order.instrument)
        pos = self._positions.setdefault(key, _Pos())
        commission = self._commission(order.quantity, fill_price, order.instrument.multiplier)
        qd = order.signed_quantity
        q0, p0 = pos.qty, pos.avg

        if q0 == 0 or (q0 > 0) == (qd > 0):
            # Opening or adding in the same direction: weighted-average the entry.
            pos.avg = (p0 * abs(q0) + fill_price * abs(qd)) / (abs(q0) + abs(qd))
            pos.qty = q0 + qd
        else:
            # Reducing / closing / flipping: realize P&L on the closed portion.
            closing = min(abs(q0), abs(qd))
            mult = order.instrument.multiplier
            pnl = (fill_price - p0) * closing * mult if q0 > 0 else (p0 - fill_price) * closing * mult
            self._realized_pnl += pnl - commission
            new_qty = q0 + qd
            pos.qty = new_qty
            pos.avg = fill_price if (new_qty != 0 and (new_qty > 0) != (q0 > 0)) else (
                p0 if new_qty != 0 else 0.0
            )

        # Cash: buying spends cash, selling raises it; commission always costs.
        self._cash -= fill_price * qd * order.instrument.multiplier
        self._cash -= commission

        ts = quote.ts or datetime.now(timezone.utc)
        fill = Fill(order.instrument, order.side, order.quantity, fill_price, commission, ts)
        log.debug(
            "fill %s %s %.4f @ %.4f comm=%.2f -> pos=%.4f cash=%.2f",
            order.side.value, key, order.quantity, fill_price, commission, pos.qty, self._cash,
        )
        return OrderResult(order, OrderStatus.FILLED, fill=fill)
