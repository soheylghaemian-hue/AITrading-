"""Trade journal tests (§11): assembler round-trips, MFE/MAE, stores, analytics, and the
independent P&L check against the broker via a real backtest."""

import math
from datetime import datetime, timedelta, timezone

from atp.brokers.base import Fill
from atp.core.enums import AssetClass, Side
from atp.core.events import Bar, Instrument
from atp.journal import (
    InMemoryJournal,
    SQLiteJournal,
    TradeAnalytics,
    TradeAssembler,
    TradeContext,
    TradeResult,
)
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy.momentum import MomentumStrategy

INST = Instrument("AAPL", AssetClass.EQUITY, multiplier=1.0)
FUT = Instrument("ES", AssetClass.FUTURE, multiplier=50.0)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _fill(side, qty, price, commission=0.0, minute=0):
    return Fill(INST, side, qty, price, commission, T0 + timedelta(minutes=minute))


# --------------------------------------------------------------------------- assembler
def test_long_round_trip_pnl_and_result():
    a = TradeAssembler()
    ctx = TradeContext(strategy="momentum", regime="trending_up", confidence=0.6, expected_return=0.02)
    assert a.on_fill(_fill(Side.BUY, 100, 150.0, commission=1.0, minute=0), ctx) is None
    rec = a.on_fill(_fill(Side.SELL, 100, 155.0, commission=1.0, minute=10), None)

    assert rec is not None
    assert rec.direction == "long"
    assert rec.strategy == "momentum" and rec.regime == "trending_up"
    assert rec.gross_pnl == 500.0                         # (155-150)*100
    assert rec.commission == 2.0                          # entry + exit
    assert rec.realized_pnl == 498.0                      # gross - commission
    assert rec.result is TradeResult.WIN
    assert math.isclose(rec.realized_return, 498.0 / (150.0 * 100))
    assert rec.holding_seconds == 600.0


def test_short_round_trip_pnl():
    a = TradeAssembler()
    a.on_fill(_fill(Side.SELL, 10, 200.0), TradeContext(strategy="s", regime="r"))
    rec = a.on_fill(_fill(Side.BUY, 10, 190.0), None)
    assert rec.direction == "short"
    assert rec.gross_pnl == 100.0                         # (200-190)*10 for a short
    assert rec.result is TradeResult.WIN


def test_multiplier_applied_for_futures():
    a = TradeAssembler()
    a.on_fill(Fill(FUT, Side.BUY, 2, 5000.0, 0.0, T0), TradeContext())
    rec = a.on_fill(Fill(FUT, Side.SELL, 2, 5010.0, 0.0, T0 + timedelta(minutes=1)), None)
    assert rec.gross_pnl == (5010 - 5000) * 2 * 50        # multiplier applied


def test_partial_reduce_then_close_emits_single_record():
    a = TradeAssembler()
    a.on_fill(_fill(Side.BUY, 100, 100.0), TradeContext(strategy="m", regime="x"))
    assert a.on_fill(_fill(Side.SELL, 40, 110.0), None) is None   # partial, still open
    rec = a.on_fill(_fill(Side.SELL, 60, 120.0), None)            # closes remainder
    assert rec is not None
    assert rec.quantity == 100
    # exit price is quantity-weighted: (40*110 + 60*120)/100 = 116
    assert math.isclose(rec.exit_price, 116.0)
    assert math.isclose(rec.gross_pnl, (116.0 - 100.0) * 100)


def test_flip_emits_record_and_opens_new_episode():
    a = TradeAssembler()
    a.on_fill(_fill(Side.BUY, 100, 100.0), TradeContext(strategy="m", regime="up"))
    # Sell 150: closes the 100 long (record) and opens a 50 short.
    rec = a.on_fill(_fill(Side.SELL, 150, 110.0), TradeContext(strategy="m", regime="down"))
    assert rec is not None and rec.direction == "long"
    assert a.open_instruments == [INST.key]               # the 50 short is now open
    # Closing the short back at 105 profits (sold at 110, buy back 105).
    rec2 = a.on_fill(_fill(Side.BUY, 50, 105.0), None)
    assert rec2.direction == "short"
    assert math.isclose(rec2.gross_pnl, (110.0 - 105.0) * 50)
    assert rec2.regime == "down"                          # new episode took the flip context


def test_mfe_mae_tracked_from_marks():
    a = TradeAssembler()
    a.on_fill(_fill(Side.BUY, 10, 100.0), TradeContext(strategy="m", regime="up"))
    a.on_mark(INST.key, 108.0, T0 + timedelta(minutes=1))  # +8% favorable
    a.on_mark(INST.key, 94.0, T0 + timedelta(minutes=2))   # -6% adverse
    rec = a.on_fill(_fill(Side.SELL, 10, 103.0, minute=3), None)
    assert math.isclose(rec.mfe, 0.08)
    assert math.isclose(rec.mae, -0.06)
    assert rec.bars_held == 2


def test_slippage_from_decision_price():
    a = TradeAssembler()
    ctx = TradeContext(strategy="m", regime="up", decision_price=100.0)
    a.on_fill(_fill(Side.BUY, 10, 100.5), ctx)             # filled 0.5 above decision
    rec = a.on_fill(_fill(Side.SELL, 10, 101.0), None)
    assert math.isclose(rec.slippage, 0.005)              # +0.5% adverse for a buy


# --------------------------------------------------------------------------- stores
def _sample_record(a):
    a.on_fill(_fill(Side.BUY, 10, 100.0), TradeContext(strategy="m", regime="up"))
    return a.on_fill(_fill(Side.SELL, 10, 110.0), None)


def test_inmemory_and_sqlite_roundtrip_equivalent():
    rec = _sample_record(TradeAssembler())

    mem = InMemoryJournal()
    mem.record(rec)
    assert len(mem) == 1
    assert mem.by_strategy("m")[0].trade_id == rec.trade_id

    db = SQLiteJournal(":memory:")
    db.record(rec)
    loaded = db.all()
    assert len(loaded) == 1
    got = loaded[0]
    assert got.trade_id == rec.trade_id
    assert math.isclose(got.realized_pnl, rec.realized_pnl)
    assert got.result is TradeResult.WIN
    assert got.entry_ts == rec.entry_ts                   # timestamps survive the round-trip
    db.close()


def test_sqlite_insert_or_replace_is_idempotent():
    rec = _sample_record(TradeAssembler())
    db = SQLiteJournal(":memory:")
    db.record(rec)
    db.record(rec)                                        # same trade_id
    assert len(db.all()) == 1
    db.close()


# --------------------------------------------------------------------------- analytics
def test_analytics_group_by_strategy_and_regime():
    trades = []
    a = TradeAssembler()
    # momentum win in trending_up
    a.on_fill(_fill(Side.BUY, 10, 100.0), TradeContext(strategy="momentum", regime="trending_up"))
    trades.append(a.on_fill(_fill(Side.SELL, 10, 110.0), None))
    # momentum loss in range
    a.on_fill(_fill(Side.BUY, 10, 100.0), TradeContext(strategy="momentum", regime="range"))
    trades.append(a.on_fill(_fill(Side.SELL, 10, 95.0), None))

    an = TradeAnalytics(trades)
    overall = an.overall()
    assert overall.n_trades == 2
    assert math.isclose(overall.win_rate, 0.5)

    by_regime = {g.label: g for g in an.by_regime()}
    assert by_regime["trending_up"].total_pnl > 0
    assert by_regime["range"].total_pnl < 0


# --------------------------------------------------------------------------- integration
async def test_backtest_journal_pnl_matches_broker():
    """The journal's realized P&L (assembled independently from fills) must reconcile with
    the backtester's own realized-trade P&L — an independent check that neither lies."""
    from atp.backtest import Backtester

    bars = []
    for i in range(200):
        p = 100 + 4 * math.sin(i / 6.0) + 0.05 * i
        bars.append(Bar(INST, p, p * 1.002, p * 0.998, p, 1000 + i, T0 + timedelta(minutes=i)))

    journal = InMemoryJournal()
    # Commissions off so the two P&L conventions coincide: the broker books only the exit
    # commission into realized_pnl (ADR-3), the assembler books the full round trip. With zero
    # commission this isolates and cross-checks the pure price/quantity P&L logic.
    bt = Backtester(
        policy=TradingPolicy(capital=100_000.0),
        strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        commission_per_unit=0.0,
        min_commission=0.0,
        journal=journal,
    )
    res = await bt.run(bars)

    assert len(journal) > 0
    journal_pnl = sum(t.realized_pnl for t in journal.all())
    broker_trade_pnl = sum(res.trade_pnls)
    # Independently assembled from fills vs. the broker's own realized-trade P&L — must agree.
    assert math.isclose(journal_pnl, broker_trade_pnl, rel_tol=1e-6, abs_tol=1e-6)


# --------------------------------------------------------------------------- extended learning fields (§1)
def test_extended_learning_fields_populated():
    from atp.core.enums import Action
    FUT2 = Instrument("ES", AssetClass.FUTURE, multiplier=50.0, underlying="SPX")
    a = TradeAssembler()
    ctx = TradeContext(strategy="momentum", regime="trending_up", confidence=0.7,
                       expected_return=0.02, agent="momentum", signal_action=Action.BUY.value,
                       signal_strength=0.9, expected_risk=5.0, strategy_version="v3",
                       decision_price=100.0)
    a.on_fill(Fill(FUT2, Side.BUY, 2, 100.0, 0.0, T0), ctx)
    rec = a.on_fill(Fill(FUT2, Side.SELL, 2, 110.0, 0.0, T0 + timedelta(minutes=5)), None)
    assert rec.underlying == "SPX"
    assert rec.agent == "momentum" and rec.signal_action == "buy"
    assert rec.signal_strength == 0.9
    assert rec.expected_risk == 0.05                 # 5.0 / 100.0 entry
    assert rec.stop_price == 95.0                     # long: entry - stop distance
    assert rec.target_price == 102.0                  # entry * (1 + 0.02)
    assert rec.strategy_version == "v3"


def test_extended_fields_survive_sqlite_roundtrip():
    a = TradeAssembler()
    ctx = TradeContext(strategy="m", regime="up", agent="m", signal_action="buy",
                       signal_strength=0.8, expected_risk=2.0, strategy_version="v2",
                       decision_price=100.0)
    a.on_fill(Fill(INST, Side.BUY, 10, 100.0, 0.0, T0), ctx)
    rec = a.on_fill(Fill(INST, Side.SELL, 10, 110.0, 0.0, T0 + timedelta(minutes=5)), None)
    db = SQLiteJournal(":memory:")
    db.record(rec)
    got = db.all()[0]
    assert got.agent == "m" and got.signal_action == "buy"
    assert got.signal_strength == 0.8 and got.strategy_version == "v2"
    assert got.stop_price == 98.0
    db.close()
