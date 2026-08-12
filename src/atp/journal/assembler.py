"""Trade assembler — fills → completed trade records (§11).

The broker/desk deal in *fills*; §11 wants *trades* (an entry→flat episode) with the whole
story attached. This assembler is the bridge. It tracks one open episode per instrument,
folds in adds (weighted-average entry), reductions and flips, and emits a `TradeRecord` the
moment a position returns to flat — carrying the entry attribution (strategy, regime,
confidence, expected return) plus path statistics (MFE/MAE) accumulated from marks.

Pure and deterministic: given the same fills and marks it always produces the same records,
so it runs in the offline suite and its P&L is an independent check on the broker's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..brokers.base import Fill
from ..core.enums import Side
from ..core.events import Instrument
from .record import TradeRecord, TradeResult

_TOL = 1e-9


@dataclass(slots=True)
class TradeContext:
    """Entry attribution captured when an episode opens (§1/§11)."""

    strategy: str = "unknown"
    regime: str = "unknown"
    confidence: float = 0.0
    expected_return: float = 0.0
    model_version: str = "v0"
    decision_price: float | None = None   # mid at signal time, for slippage
    rationale: str = ""
    features: dict = field(default_factory=dict)
    # --- full learning attribution (§1) -----------------------------------
    agent: str = ""                       # trader/agent (defaults to strategy if blank)
    signal_action: str = ""
    signal_strength: float = 0.0
    expected_risk: float = 0.0            # stop distance in price units, from the signal
    strategy_version: str = "v0"
    financing_cost: float = 0.0


@dataclass(slots=True)
class _Episode:
    instrument: Instrument
    direction: int                 # +1 long, -1 short
    ctx: TradeContext
    entry_ts: datetime
    open_qty: float = 0.0
    entry_qty: float = 0.0
    entry_value: float = 0.0       # Σ price*qty of entry fills
    exit_qty: float = 0.0
    exit_value: float = 0.0        # Σ price*qty of exit fills
    commission: float = 0.0
    slippage: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    bars: int = 0
    last_ts: datetime | None = None

    @property
    def entry_price(self) -> float:
        return self.entry_value / self.entry_qty if self.entry_qty else 0.0


class TradeAssembler:
    def __init__(self) -> None:
        self._open: dict[str, _Episode] = {}
        self._seq = 0

    @property
    def open_instruments(self) -> list[str]:
        return list(self._open)

    def on_mark(self, instrument_key: str, price: float, ts: datetime) -> None:
        """Update path statistics (MFE/MAE, bars held) for an open episode."""
        ep = self._open.get(instrument_key)
        if ep is None or ep.entry_price <= 0:
            return
        excursion = ep.direction * (price - ep.entry_price) / ep.entry_price
        ep.mfe = max(ep.mfe, excursion)
        ep.mae = min(ep.mae, excursion)
        ep.bars += 1
        ep.last_ts = ts

    def on_fill(self, fill: Fill, context: TradeContext | None = None) -> TradeRecord | None:
        """Fold a fill into the position episode. Returns a TradeRecord iff it closed one."""
        key = fill.instrument.key
        fill_dir = 1 if fill.side is Side.BUY else -1
        ep = self._open.get(key)

        if ep is None:
            self._open_episode(fill, fill_dir, context)
            return None

        if fill_dir == ep.direction:
            # Adding in the position's direction: weighted-average the entry.
            ep.entry_qty += fill.quantity
            ep.entry_value += fill.price * fill.quantity
            ep.open_qty += fill.quantity
            ep.commission += fill.commission
            return None

        # Opposite direction: reduce / close / flip.
        close_qty = min(fill.quantity, ep.open_qty)
        ep.exit_qty += close_qty
        ep.exit_value += fill.price * close_qty
        ep.open_qty -= close_qty
        ep.commission += fill.commission

        if ep.open_qty > _TOL:
            return None  # partial reduction; episode stays open

        record = self._finalize(ep, exit_ts=fill.ts)
        del self._open[key]

        remainder = fill.quantity - close_qty
        if remainder > _TOL:
            # Flip: the same fill opens a new episode in the opposite direction.
            flip = Fill(fill.instrument, fill.side, remainder, fill.price, 0.0, fill.ts)
            self._open_episode(flip, fill_dir, context)
        return record

    # ------------------------------------------------------------- internals
    def _open_episode(self, fill: Fill, direction: int, context: TradeContext | None) -> None:
        ctx = context or TradeContext()
        slippage = 0.0
        if ctx.decision_price:
            slippage = direction * (fill.price - ctx.decision_price) / ctx.decision_price
        self._open[fill.instrument.key] = _Episode(
            instrument=fill.instrument,
            direction=direction,
            ctx=ctx,
            entry_ts=fill.ts,
            open_qty=fill.quantity,
            entry_qty=fill.quantity,
            entry_value=fill.price * fill.quantity,
            commission=fill.commission,
            slippage=slippage,
            last_ts=fill.ts,
        )

    def _finalize(self, ep: _Episode, *, exit_ts: datetime) -> TradeRecord:
        self._seq += 1
        mult = ep.instrument.multiplier
        entry_price = ep.entry_price
        exit_price = ep.exit_value / ep.exit_qty if ep.exit_qty else 0.0
        gross = ep.direction * (ep.exit_value - ep.entry_value) * mult
        pnl = gross - ep.commission
        entry_notional = ep.entry_value * mult
        realized_return = pnl / entry_notional if entry_notional else 0.0
        holding_seconds = (exit_ts - ep.entry_ts).total_seconds()

        # Planned stop/target from the entry signal's risk & expected move (§1).
        ctx = ep.ctx
        stop_price = entry_price - ep.direction * ctx.expected_risk if ctx.expected_risk else 0.0
        target_price = entry_price * (1 + ep.direction * ctx.expected_return) if ctx.expected_return else 0.0
        expected_risk_frac = (ctx.expected_risk / entry_price) if entry_price > 0 else 0.0

        return TradeRecord(
            trade_id=f"T{self._seq:06d}-{ep.instrument.symbol}",
            instrument_key=ep.instrument.key,
            asset_class=ep.instrument.asset_class.value,
            direction="long" if ep.direction > 0 else "short",
            strategy=ep.ctx.strategy,
            regime=ep.ctx.regime,
            model_version=ep.ctx.model_version,
            entry_ts=ep.entry_ts,
            exit_ts=exit_ts,
            quantity=ep.entry_qty,
            entry_price=entry_price,
            exit_price=exit_price,
            confidence=ep.ctx.confidence,
            expected_return=ep.ctx.expected_return,
            realized_return=realized_return,
            gross_pnl=gross,
            commission=ep.commission,
            realized_pnl=pnl,
            slippage=ep.slippage,
            mfe=ep.mfe,
            mae=ep.mae,
            bars_held=ep.bars,
            holding_seconds=holding_seconds,
            result=TradeResult.of(pnl),
            rationale=ctx.rationale,
            features=dict(ctx.features),
            underlying=ep.instrument.underlying or ep.instrument.symbol,
            agent=ctx.agent or ctx.strategy,
            signal_action=ctx.signal_action,
            signal_strength=ctx.signal_strength or ctx.confidence,
            expected_risk=expected_risk_frac,
            stop_price=stop_price,
            target_price=target_price,
            financing_cost=ctx.financing_cost,
            strategy_version=ctx.strategy_version,
        )
