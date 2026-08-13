"""PAPER AUTONOMOUS engine — mode/status, data-quality gate, paper fills, risk lock, safety.

Uses the real desk+PaperBroker+RiskEngine (build_paper_stack). No IBKR, no live execution.
"""

from datetime import datetime, timedelta, timezone

from atp.autonomous import AutonomousStatus, PaperAutonomousEngine
from atp.brokers.base import Order
from atp.core.enums import AssetClass, OrderType, Side
from atp.core.events import Bar, Instrument
from atp.journal import InMemoryJournal
from atp.live import build_paper_stack
from atp.policy import TradingPolicy
from atp.strategy import MomentumStrategy

INST = Instrument("TESTX", AssetClass.EQUITY)
START = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)


def _uptrend_bars(n=60):
    out = []
    p = 100.0
    for i in range(n):
        p = 100.0 + 0.6 * i          # steady uptrend → momentum BUY
        out.append(Bar(INST, p, p * 1.002, p * 0.998, p, 5000, START + timedelta(minutes=i)))
    return out


def _md(status="DATA_AVAILABLE", last=135.0):
    return [{"symbol": "TESTX", "asset_class": "equity", "exchange": "NASDAQ",
             "status": status, "market_data_type": "REALTIME",
             "bid": last * 0.9999, "ask": last * 1.0001, "last": last}]


async def _engine(capital=1_000_000.0):
    journal = InMemoryJournal()
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=capital), strategies=[MomentumStrategy()], journal=journal)
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk, journal=journal)
    return eng, broker, risk, journal


# --------------------------------------------------------------------------- default / control
async def test_default_status_is_disabled_and_does_not_trade():
    eng, broker, _, _ = await _engine()
    assert eng.status is AutonomousStatus.DISABLED
    bars = _uptrend_bars()
    await eng.step(now=bars[-1].ts, bars=bars, market_data=_md())
    assert len(await broker.get_positions()) == 0       # nothing traded while DISABLED


async def test_arm_disarm_transitions():
    eng, _, _, _ = await _engine()
    assert eng.arm() is AutonomousStatus.RUNNING
    assert eng.disarm() is AutonomousStatus.DISABLED


# --------------------------------------------------------------------------- paper fill on real data
async def test_running_executes_paper_trade_on_valid_data():
    eng, broker, _, _ = await _engine()
    eng.arm()
    bars = _uptrend_bars()
    await eng.step(now=bars[-1].ts, bars=bars, market_data=_md())
    positions = await broker.get_positions()
    assert len(positions) == 1                           # a paper position was opened
    assert any(d.decision == "FILLED" for d in eng._decisions)  # noqa: SLF001
    snap = eng.snapshot(account=await broker.get_account())
    assert snap["trades_today"] >= 1
    assert snap["live_execution"] is False and snap["ibkr_orders"] == 0
    assert snap["status"] == "RUNNING" and snap["mode"] == "PAPER"


# --------------------------------------------------------------------------- data-quality gate
async def test_no_trade_on_unavailable_market_data():
    eng, broker, _, _ = await _engine()
    eng.arm()
    bars = _uptrend_bars()
    await eng.step(now=bars[-1].ts, bars=bars, market_data=_md(status="DATA_NOT_AVAILABLE"))
    assert len(await broker.get_positions()) == 0
    assert any(d.decision == "NO_DATA" for d in eng._decisions)  # noqa: SLF001


async def test_no_trade_on_stale_market_data():
    eng, broker, _, _ = await _engine()
    eng.arm()
    bars = _uptrend_bars()
    await eng.step(now=bars[-1].ts, bars=bars, market_data=_md(status="STALE"))
    assert len(await broker.get_positions()) == 0
    assert any(d.decision == "NO_DATA" for d in eng._decisions)  # noqa: SLF001


# --------------------------------------------------------------------------- risk / kill
async def test_daily_loss_lock_halts_new_trades():
    eng, broker, risk, _ = await _engine(capital=1_000_000.0)
    eng.arm()
    risk.mark_equity(950_000.0)                          # −5% > 3% default daily loss → halt
    assert eng.status is AutonomousStatus.HALTED
    bars = _uptrend_bars()
    await eng.step(now=bars[-1].ts, bars=bars, market_data=_md())
    assert len(await broker.get_positions()) == 0        # locked — no new trades
    assert any(d.decision == "HALTED" for d in eng._decisions)  # noqa: SLF001


async def test_kill_switch_blocks_everything():
    eng, broker, _, _ = await _engine()
    eng.arm()
    eng.kill("test")
    assert eng.status is AutonomousStatus.KILLED
    bars = _uptrend_bars()
    await eng.step(now=bars[-1].ts, bars=bars, market_data=_md())
    assert len(await broker.get_positions()) == 0


async def test_cannot_arm_while_killed():
    eng, _, _, _ = await _engine()
    eng.kill("test")
    try:
        eng.arm()
        assert False, "arming while killed must raise"
    except RuntimeError:
        pass


# --------------------------------------------------------------------------- realistic paper fill
async def test_paper_fill_has_slippage_and_commission():
    from atp.core.events import QuoteEvent
    _, broker, _, _ = await _engine()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.02, START))   # real bid/ask → realistic fill
    res = await broker.place_order(Order(INST, Side.BUY, 100, OrderType.MARKET))
    assert res.filled and res.fill is not None
    assert res.fill.commission > 0.0                     # commission charged
    assert res.fill.price >= 100.01                      # buys pay through the spread/slippage


# --------------------------------------------------------------------------- governance / safety
async def test_no_automatic_live_and_paper_only():
    eng, broker, _, _ = await _engine()
    eng.arm()
    snap = eng.snapshot(account=await broker.get_account())
    assert snap["mode"] == "PAPER"                       # never "LIVE"
    assert snap["live_execution"] is False
    assert eng.mode == "paper"


def test_engine_never_references_ibkr_order_calls():
    import inspect
    from atp.autonomous import engine as mod
    src = inspect.getsource(mod)
    for forbidden in ("placeOrder", "cancelOrder", "modifyOrder", "IBKRBroker", "ib_insync"):
        assert forbidden not in src, f"autonomous engine must not reference {forbidden}"
