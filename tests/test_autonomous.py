"""PAPER AUTONOMOUS state machine (Phase 8.5) — arm/dry-run/two-step-start, gates, safety, audit.

Uses the real desk+PaperBroker+RiskEngine. No IBKR, no live execution.
"""

from datetime import datetime, timedelta, timezone

from atp.autonomous import AutonomousStatus, PaperAutonomousEngine
from atp.brokers.base import Order
from atp.core.enums import AssetClass, OrderType, Side
from atp.core.events import Bar, Instrument
from atp.journal import InMemoryJournal
from atp.live import build_paper_stack
from atp.policy import TradingPolicy
from atp.risk.config import TradingRiskConfig
from atp.strategy import MomentumStrategy

INST = Instrument("TESTX", AssetClass.EQUITY)
START = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)   # Thursday
CFG = TradingRiskConfig(capital=1_000_000.0, risk_per_trade_pct=0.01, max_daily_loss_pct=0.03)


def _bars(n=60):
    return [Bar(INST, 100 + 0.6 * i, (100 + 0.6 * i) * 1.002, (100 + 0.6 * i) * 0.998,
                100 + 0.6 * i, 5000, START + timedelta(minutes=i)) for i in range(n)]


def _md(status="DATA_AVAILABLE", mdt="REALTIME", last=135.0):
    return [{"symbol": "TESTX", "asset_class": "equity", "exchange": "NASDAQ", "status": status,
             "market_data_type": mdt, "bid": last * 0.9999, "ask": last * 1.0001, "last": last}]


async def _engine(capital=1_000_000.0):
    journal = InMemoryJournal()
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=capital), strategies=[MomentumStrategy()], journal=journal)
    return PaperAutonomousEngine(desk=desk, broker=broker, risk=risk, journal=journal), broker, risk


async def _running(eng):
    eng.arm()
    res = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=CFG)
    assert res["ok"], res
    assert eng.status is AutonomousStatus.RUNNING


# --------------------------------------------------------------------------- cannot trade unless RUNNING
async def test_disabled_cannot_trade():
    eng, broker, _ = await _engine()
    assert eng.status is AutonomousStatus.DISABLED
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert len(await broker.get_positions()) == 0


async def test_armed_computes_but_does_not_trade():
    eng, broker, _ = await _engine()
    eng.arm()
    assert eng.status is AutonomousStatus.ARMED
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert len(await broker.get_positions()) == 0                 # ARMED never places orders
    assert any(d.execution_decision.startswith("NO_ORDER") for d in eng._decisions)  # noqa: SLF001


async def test_dry_run_computes_but_does_not_trade():
    eng, broker, _ = await _engine()
    eng.dry_run()
    assert eng.status is AutonomousStatus.DRY_RUN
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert len(await broker.get_positions()) == 0


async def test_running_can_paper_trade():
    eng, broker, _ = await _engine()
    await _running(eng)
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert len(await broker.get_positions()) == 1
    assert any(d.execution_decision == "PAPER_EXECUTED" for d in eng._decisions)  # noqa: SLF001


# --------------------------------------------------------------------------- two-step activation
async def test_start_requires_arm_first():
    eng, _, _ = await _engine()
    res = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=CFG)
    assert not res["ok"] and eng.status is AutonomousStatus.DISABLED


async def test_start_requires_confirmation():
    eng, _, _ = await _engine()
    eng.arm()
    res = eng.start(confirm=False, connected=True, market_data=_md(), risk_config=CFG)
    assert not res["ok"] and "confirmation" in res["reasons"][0].lower()
    assert eng.status is AutonomousStatus.ARMED


# --------------------------------------------------------------------------- start safety
async def test_start_fails_without_ibkr():
    eng, _, _ = await _engine()
    eng.arm()
    res = eng.start(confirm=True, connected=False, market_data=_md(), risk_config=CFG)
    assert not res["ok"] and any("not connected" in r for r in res["reasons"])


async def test_start_fails_without_realtime_data():
    eng, _, _ = await _engine()
    eng.arm()
    res = eng.start(confirm=True, connected=True, market_data=_md(status="DATA_NOT_AVAILABLE"), risk_config=CFG)
    assert not res["ok"] and any("market data" in r.lower() for r in res["reasons"])


async def test_start_fails_with_stale_data():
    eng, _, _ = await _engine()
    eng.arm()
    res = eng.start(confirm=True, connected=True, market_data=_md(status="STALE"), risk_config=CFG)
    assert not res["ok"]


async def test_start_fails_with_delayed_data():
    eng, _, _ = await _engine()
    eng.arm()
    res = eng.start(confirm=True, connected=True, market_data=_md(mdt="DELAYED"), risk_config=CFG)
    assert not res["ok"]


async def test_start_fails_with_invalid_risk_config():
    eng, _, _ = await _engine()
    eng.arm()
    assert not eng.start(confirm=True, connected=True, market_data=_md(), risk_config=None)["ok"]


async def test_start_fails_with_kill_switch():
    eng, _, _ = await _engine()
    eng.arm()
    eng.kill("test")
    res = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=CFG)
    assert not res["ok"] and eng.status is AutonomousStatus.KILLED


# --------------------------------------------------------------------------- data-quality gate while running
async def test_running_no_trade_on_unavailable_data():
    eng, broker, _ = await _engine()
    await _running(eng)
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md(status="DATA_NOT_AVAILABLE"))
    assert len(await broker.get_positions()) == 0
    assert any(d.reason in ("SUBSCRIPTION_REQUIRED", "DATA_UNAVAILABLE") for d in eng._decisions)  # noqa: SLF001


# --------------------------------------------------------------------------- daily loss / kill
async def test_daily_loss_causes_halted():
    eng, broker, risk = await _engine()
    await _running(eng)
    risk.mark_equity(950_000.0)                     # −5% > 3% → halt
    assert eng.status is AutonomousStatus.HALTED
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert len(await broker.get_positions()) == 0


async def test_kill_causes_killed_and_no_restart_without_reset():
    eng, _, _ = await _engine()
    await _running(eng)
    eng.kill("panic")
    assert eng.status is AutonomousStatus.KILLED
    try:
        eng.arm()
        assert False, "must not arm while killed"
    except RuntimeError:
        pass
    eng.reset_kill()
    assert eng.status is AutonomousStatus.DISABLED
    assert eng.arm() is AutonomousStatus.ARMED       # can re-arm only after explicit reset


async def test_killed_still_observes_decisions():
    eng, broker, _ = await _engine()
    await _running(eng)
    eng.kill("panic")
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert len(await broker.get_positions()) == 0    # no execution
    # still logging decisions (observe): at least the evaluate/gate entries exist
    assert len(eng._decisions) > 0                    # noqa: SLF001


# --------------------------------------------------------------------------- audit + safety
async def test_audit_log_records_transitions():
    eng, _, _ = await _engine()
    eng.arm(actor="user")
    eng.start(confirm=True, actor="user", connected=True, market_data=_md(), risk_config=CFG)
    audit = eng.snapshot(account=None)["audit"]
    assert any(a["new"] == "ARMED" for a in audit)
    assert any(a["new"] == "RUNNING" and "confirmation" in a["reason"] for a in audit)


async def test_snapshot_safety_fields():
    eng, broker, _ = await _engine()
    await _running(eng)
    snap = eng.snapshot(account=await broker.get_account(), risk_config=CFG, market_data=_md())
    assert snap["mode"] == "PAPER" and snap["status"] == "RUNNING"
    assert snap["live_execution"] is False and snap["ibkr_orders"] == 0
    assert snap["risk"] == "ACTIVE" and snap["data"] == "REALTIME" and snap["engine"] == "HEALTHY"


async def test_paper_fill_has_slippage_and_commission():
    from atp.core.events import QuoteEvent
    _, broker, _ = await _engine()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.02, START))
    res = await broker.place_order(Order(INST, Side.BUY, 100, OrderType.MARKET))
    assert res.filled and res.fill.commission > 0.0 and res.fill.price >= 100.01


def test_engine_never_references_ibkr_order_calls():
    import inspect
    from atp.autonomous import engine as mod
    src = inspect.getsource(mod)
    for forbidden in ("placeOrder", "cancelOrder", "modifyOrder", "IBKRBroker", "ib_insync"):
        assert forbidden not in src, f"autonomous engine must not reference {forbidden}"
