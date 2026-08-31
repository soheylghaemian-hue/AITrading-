"""PAPER AUTONOMOUS state machine (Phase 8.5) — arm/dry-run/two-step-start, gates, safety, audit.

Uses the real desk+PaperBroker+RiskEngine. No IBKR, no live execution.
"""

import asyncio
import math
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from atp.autonomous import AutonomousStatus, PaperAutonomousEngine
from atp.brokers.base import Account, Order
from atp.brokers.paper import PaperBroker
from atp.core.enums import AssetClass, OrderType, Side
from atp.core.events import Bar, Instrument, QuoteEvent
from atp.dashboard.api import DashboardContext
from atp.desk.desk import StepReport
from atp.execution.algo import ExecutionAlgo
from atp.execution.engine import ExecutionEngine
from atp.execution.scheduler import ExecutionScheduler
from atp.journal import InMemoryJournal
from atp.live import build_paper_stack
from atp.policy import TradingPolicy
from atp.risk.config import TradingRiskConfig
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
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
    audit = (await eng.snapshot())["audit"]
    assert any(a["new"] == "ARMED" for a in audit)
    assert any(a["new"] == "RUNNING" and "confirmation" in a["reason"] for a in audit)


async def test_snapshot_safety_fields():
    eng, _, _ = await _engine()
    await _running(eng)
    snap = await eng.snapshot(market_data=_md())
    assert snap["mode"] == "PAPER" and snap["status"] == "RUNNING"
    assert snap["live_execution"] is False and snap["ibkr_orders"] == 0
    assert snap["paper_boundary_verified"] is True
    assert snap["execution_adapter"] == "PaperBroker"
    assert snap["risk_config_bound"] is True
    assert snap["risk"] == "ACTIVE" and snap["data"] == "REALTIME" and snap["engine"] == "HEALTHY"


@pytest.mark.parametrize("mode", ["live", "PAPER", "", None])
async def test_constructor_rejects_every_nonpaper_mode(mode):
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    with pytest.raises(ValueError, match="exactly 'paper'"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=risk, mode=mode)


async def test_constructor_rejects_string_subclass_mode():
    class _PaperText(str):
        pass

    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    with pytest.raises(ValueError, match="exactly 'paper'"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=risk, mode=_PaperText("paper"))


async def test_constructor_rejects_fake_subclass_and_second_paper_brokers():
    class _PaperSubclass(PaperBroker):
        pass

    desk, _, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    subclass = _PaperSubclass(1_000_000.0)
    second = PaperBroker(1_000_000.0)
    await subclass.connect()
    await second.connect()
    for candidate in (object(), subclass, second):
        with pytest.raises(ValueError, match="unsafe paper wiring"):
            PaperAutonomousEngine(desk=desk, broker=candidate, risk=risk)


async def test_factory_rejects_an_unbound_injected_risk_engine():
    unbound = RiskEngine(
        limits=RiskLimits(),
        state=RiskState(day_start_equity=1_000_000.0, peak_equity=1_000_000.0),
    )
    with pytest.raises(ValueError, match="not bound"):
        await build_paper_stack(
            policy=TradingPolicy(capital=1_000_000.0),
            strategies=[MomentumStrategy()],
            risk=unbound,
        )


async def test_constructor_rejects_broker_and_risk_identity_mismatches():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    second_broker = PaperBroker(1_000_000.0)
    await second_broker.connect()
    desk._broker = second_broker  # noqa: SLF001 - adversarial wiring probe
    with pytest.raises(ValueError, match="desk broker"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)

    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    desk._execution._broker = second_broker  # noqa: SLF001
    with pytest.raises(ValueError, match="execution broker"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)

    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    other_risk = RiskEngine(
        limits=RiskLimits(max_capital=1_000_000.0),
        state=RiskState(day_start_equity=1_000_000.0, peak_equity=1_000_000.0),
    )
    with pytest.raises(ValueError, match="risk authority"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=other_risk)

    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    desk._risk = other_risk  # noqa: SLF001 - adversarial wiring probe
    with pytest.raises(ValueError, match="desk risk authority"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)

    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()])
    desk._execution._risk = other_risk  # noqa: SLF001 - adversarial wiring probe
    with pytest.raises(ValueError, match="execution risk authority"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)


async def test_constructor_rejects_scheduler_with_a_different_execution_path():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()], execution_slices=2)
    desk._scheduler._execution = ExecutionEngine(broker, risk)  # noqa: SLF001
    with pytest.raises(ValueError, match="scheduler execution"):
        PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)


@pytest.mark.parametrize(
    "config",
    [
        TradingRiskConfig(900_000.0, 0.01, 0.03),
        TradingRiskConfig(1_000_000.0, 0.005, 0.03),
        TradingRiskConfig(1_000_000.0, 0.01, 0.02),
    ],
)
async def test_start_rejects_each_unbound_risk_config_value(config):
    eng, _, _ = await _engine()
    eng.arm()
    result = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=config)
    assert result["ok"] is False
    assert any("not bound" in reason for reason in result["reasons"])
    assert eng.status is AutonomousStatus.ARMED


async def test_start_requires_exact_config_and_boolean_connection():
    eng, _, _ = await _engine()
    eng.arm()
    result = eng.start(confirm=True, connected=1, market_data=_md(), risk_config=CFG)
    assert result["ok"] is False
    assert any("not connected" in reason for reason in result["reasons"])

    class _DuckConfig:
        capital = 1_000_000.0
        risk_per_trade_pct = 0.01
        max_daily_loss_pct = 0.03

    result = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=_DuckConfig())
    assert result["ok"] is False
    assert any("exact TradingRiskConfig" in reason for reason in result["reasons"])


async def test_start_rejects_hostile_confirmation_comparison():
    class _HostileConfirmation:
        def __ne__(self, _other):
            return False

    eng, _, _ = await _engine()
    eng.arm()
    result = eng.start(
        confirm=_HostileConfirmation(),
        connected=True,
        market_data=_md(),
        risk_config=CFG,
    )
    assert result["ok"] is False
    assert eng.status is AutonomousStatus.ARMED


async def test_constructor_rejects_unvetted_execution_algorithm():
    class _CustomAlgo(ExecutionAlgo):
        def plan(self, order, *, adv=None, urgency="normal"):
            return [order]

    with pytest.raises(ValueError, match="exact vetted"):
        await build_paper_stack(
            policy=TradingPolicy(capital=1_000_000.0),
            strategies=[MomentumStrategy()],
            execution_algo=_CustomAlgo(),
        )


async def test_runtime_rejects_nonfinite_policy_and_risk_limit_drift():
    eng, broker, risk = await _engine()
    await _running(eng)
    eng._desk._policy.max_position_pct = math.inf  # noqa: SLF001
    risk.limits.max_position_pct = math.inf
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert eng.status is AutonomousStatus.DISABLED
    assert await broker.get_positions() == {}


async def test_runtime_rejects_coherent_finite_policy_and_risk_limit_drift():
    eng, broker, risk = await _engine()
    await _running(eng)
    eng._desk._policy.max_position_pct = 0.19  # noqa: SLF001
    risk.limits.max_position_pct = 0.19
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert eng.status is AutonomousStatus.DISABLED
    assert await broker.get_positions() == {}


async def test_runtime_rejects_nonfinite_risk_state_before_execution():
    eng, broker, risk = await _engine()
    await _running(eng)
    risk.state.day_start_equity = math.nan
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert eng.status is AutonomousStatus.DISABLED
    assert await broker.get_positions() == {}


async def test_hostile_risk_state_shape_keeps_status_and_snapshot_fail_closed():
    eng, _, risk = await _engine()
    await _running(eng)
    risk._state = object()  # noqa: SLF001 - hostile injected runtime collaborator

    assert eng.status is AutonomousStatus.DISABLED
    snap = await eng.snapshot(market_data=_md())
    assert snap["status"] == AutonomousStatus.DISABLED.value
    assert snap["paper_boundary_verified"] is False
    assert snap["paper_equity"] is None and snap["open_positions"] is None


async def test_daily_halt_only_resets_when_trading_day_advances():
    eng, _, risk = await _engine()
    first_day = START.date()
    eng.start_new_day(1_000_000.0, first_day)
    risk.force_halt("daily loss")

    for invalid_day in (first_day, first_day - timedelta(days=1)):
        with pytest.raises(ValueError, match="must advance"):
            eng.start_new_day(1_000_000.0, invalid_day)
        assert risk.state.halted is True

    with pytest.raises(ValueError, match="exact date"):
        eng.start_new_day(1_000_000.0, START)
    assert risk.state.halted is True

    eng.start_new_day(1_000_000.0, first_day + timedelta(days=1))
    assert risk.state.halted is False


async def test_post_start_rewire_disables_before_any_order_path_runs():
    eng, broker, _ = await _engine()
    await _running(eng)
    second = PaperBroker(1_000_000.0)
    await second.connect()
    eng._desk._execution._broker = second  # noqa: SLF001 - simulate post-attestation drift

    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())

    assert eng.status is AutonomousStatus.DISABLED
    assert await broker.get_positions() == {}
    assert await second.get_positions() == {}
    snap = await eng.snapshot(market_data=_md())
    assert snap["paper_boundary_verified"] is False
    assert snap["live_execution"] is None and snap["ibkr_orders"] is None
    assert snap["engine"] == "ERROR"


async def test_rewire_after_outer_guard_cannot_change_canonical_execution_broker():
    eng, broker, risk = await _engine()
    await _running(eng)
    entered = Event()
    release = Event()
    original_check = risk.check_order

    def delayed_check(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2.0)
        return original_check(*args, **kwargs)

    risk.check_order = delayed_check
    old_step = asyncio.create_task(
        asyncio.to_thread(
            lambda: asyncio.run(
                eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
            )
        )
    )
    assert await asyncio.to_thread(entered.wait, 2.0)
    second = PaperBroker(1_000_000.0)
    await second.connect()
    from atp.core.events import QuoteEvent

    second.set_quote(QuoteEvent(INST, 134.9, 135.1, START))
    eng._desk._execution._broker = second  # noqa: SLF001 - operational callback rewire
    release.set()
    await old_step

    assert await broker.get_positions() == {}
    assert await second.get_positions() == {}
    assert eng.status is AutonomousStatus.DISABLED


async def test_post_start_config_drift_disables_before_execution():
    eng, broker, risk = await _engine()
    await _running(eng)
    risk.limits.max_trade_risk_pct = 0.005
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert eng.status is AutonomousStatus.DISABLED
    assert await broker.get_positions() == {}


async def test_snapshot_reads_only_the_verified_paper_broker_account():
    eng, broker, _ = await _engine()
    broker.credit_cash(123.0)
    snap = await eng.snapshot()
    assert snap["paper_equity"] == 1_000_123.0
    with pytest.raises(TypeError):
        await eng.snapshot(account=object())


async def test_snapshot_discards_account_when_boundary_drifts_during_await():
    eng, broker, _ = await _engine()
    await _running(eng)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_get_account = broker.get_account

    async def delayed_account():
        account = await original_get_account()
        entered.set()
        await release.wait()
        return account

    broker.get_account = delayed_account
    task = asyncio.create_task(eng.snapshot())
    await entered.wait()
    second = PaperBroker(1_000_000.0)
    await second.connect()
    eng._desk._execution._broker = second  # noqa: SLF001
    release.set()
    snap = await task
    assert snap["paper_boundary_verified"] is False
    assert snap["paper_equity"] is None and snap["open_positions"] is None
    assert eng.status is AutonomousStatus.DISABLED


async def test_old_snapshot_does_not_disable_valid_stop_start_epoch():
    eng, broker, _ = await _engine()
    await _running(eng)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_get_account = broker.get_account

    async def delayed_account():
        account = await original_get_account()
        entered.set()
        await release.wait()
        return account

    broker.get_account = delayed_account
    task = asyncio.create_task(eng.snapshot())
    await entered.wait()
    eng.stop()
    restarted = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=CFG)
    assert restarted["ok"] is True
    release.set()
    snap = await task

    assert snap["paper_boundary_verified"] is False
    assert snap["paper_equity"] is None and snap["open_positions"] is None
    assert eng.status is AutonomousStatus.RUNNING


async def test_dashboard_keeps_live_observation_account_separate_from_paper_equity():
    class _LiveReadBroker:
        @staticmethod
        def is_connected():
            return True

        @staticmethod
        async def get_account():
            return Account(
                cash=9_000_000.0,
                equity=9_000_000.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                positions={},
            )

    eng, _, risk = await _engine()
    context = DashboardContext(
        broker=_LiveReadBroker(),
        risk=risk,
        risk_config=CFG,
        market_data=_md(),
        autonomous_engine=eng,
    )
    snap = await context.snapshot_dict()
    assert snap["account"]["equity"] == 9_000_000.0
    assert snap["autonomous"]["paper_equity"] == 1_000_000.0
    assert snap["autonomous"]["paper_boundary_verified"] is True


async def test_risk_config_change_stops_and_requires_new_attestation():
    eng, broker, risk = await _engine()
    await _running(eng)

    def _bind_policy(config):
        eng._desk._policy = eng._desk._policy.model_copy(update={  # noqa: SLF001
            "capital": config.capital,
            "risk_per_trade": config.risk_per_trade_pct,
            "daily_loss_limit": config.max_daily_loss_pct,
        })

    context = DashboardContext(
        broker=broker,
        risk=risk,
        risk_config=CFG,
        autonomous_engine=eng,
        on_risk_config_change=_bind_policy,
    )
    context.set_risk_config(500_000.0, 0.005, 0.01)
    assert eng.status is AutonomousStatus.ARMED

    changed = TradingRiskConfig(500_000.0, 0.005, 0.01)
    result = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=changed)
    assert result["ok"] is True
    assert eng.status is AutonomousStatus.RUNNING


def _risk_context(eng, broker, risk):
    def bind_policy(config):
        eng._desk._policy = eng._desk._policy.model_copy(update={  # noqa: SLF001
            "capital": config.capital,
            "risk_per_trade": config.risk_per_trade_pct,
            "daily_loss_limit": config.max_daily_loss_pct,
        })

    return DashboardContext(
        broker=broker,
        risk=risk,
        risk_config=CFG,
        autonomous_engine=eng,
        on_risk_config_change=bind_policy,
    )


async def test_reconfiguration_invalidates_cycle_blocked_before_direct_submit():
    eng, broker, risk = await _engine()
    await _running(eng)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_step = eng._desk.step  # noqa: SLF001

    async def delayed_step(*, now):
        entered.set()
        await release.wait()
        return await original_step(now=now)

    eng._desk.step = delayed_step  # noqa: SLF001
    task = asyncio.create_task(
        eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    )
    await entered.wait()
    _risk_context(eng, broker, risk).set_risk_config(500_000.0, 0.005, 0.01)
    assert eng.status is AutonomousStatus.ARMED
    release.set()
    await task
    assert await broker.get_positions() == {}
    assert any("stale paper execution cycle" in d.reason for d in eng._decisions)  # noqa: SLF001


async def test_old_cycle_cannot_submit_after_stop_and_new_start():
    eng, broker, risk = await _engine()
    await _running(eng)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_step = eng._desk.step  # noqa: SLF001

    async def delayed_step(*, now):
        entered.set()
        await release.wait()
        return await original_step(now=now)

    eng._desk.step = delayed_step  # noqa: SLF001
    task = asyncio.create_task(
        eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    )
    await entered.wait()
    context = _risk_context(eng, broker, risk)
    context.set_risk_config(500_000.0, 0.005, 0.01)
    changed = TradingRiskConfig(500_000.0, 0.005, 0.01)
    result = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=changed)
    assert result["ok"] is True
    release.set()
    await task
    assert await broker.get_positions() == {}
    assert eng.status is AutonomousStatus.RUNNING


async def test_stop_invalidates_queued_scheduler_slice():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy()],
        execution_slices=2,
    )
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert desk._scheduler.has_work()  # noqa: SLF001

    entered = asyncio.Event()
    release = asyncio.Event()
    original_submit = desk._execution.submit  # noqa: SLF001

    async def delayed_submit(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_submit(*args, **kwargs)

    desk._execution.submit = delayed_submit  # noqa: SLF001
    next_bar = _bars()[-1]
    task = asyncio.create_task(
        eng.step(now=next_bar.ts + timedelta(minutes=1), bars=[next_bar], market_data=_md())
    )
    await entered.wait()
    eng.stop()
    release.set()
    await task
    assert await broker.get_positions() == {}
    assert not desk._scheduler.has_work()  # noqa: SLF001


async def test_stop_cancels_queued_scheduler_before_restart():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy()],
        execution_slices=2,
    )
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert desk._scheduler.has_work()  # noqa: SLF001

    eng.stop()
    assert not desk._scheduler.has_work()  # noqa: SLF001
    restarted = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=CFG)
    assert restarted["ok"] is True
    assert not desk._scheduler.has_work()  # noqa: SLF001


async def test_old_cycle_cannot_enqueue_scheduler_parent_after_stop_restart():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy()],
        execution_slices=2,
    )
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    entered = Event()
    release = Event()
    original_submit_parent = desk._scheduler.submit_parent  # noqa: SLF001

    def delayed_submit_parent(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2.0)
        return original_submit_parent(*args, **kwargs)

    desk._scheduler.submit_parent = delayed_submit_parent  # noqa: SLF001
    old_step = asyncio.create_task(
        asyncio.to_thread(
            lambda: asyncio.run(
                eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
            )
        )
    )
    assert await asyncio.to_thread(entered.wait, 2.0)
    eng.stop()
    restarted = eng.start(confirm=True, connected=True, market_data=_md(), risk_config=CFG)
    assert restarted["ok"] is True
    release.set()
    await old_step

    assert not desk._scheduler.has_work()  # noqa: SLF001
    assert await broker.get_positions() == {}


async def test_parallel_steps_are_single_flight():
    eng, broker, _ = await _engine()
    await _running(eng)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_step = eng._desk.step  # noqa: SLF001

    async def delayed_step(*, now):
        entered.set()
        await release.wait()
        return await original_step(now=now)

    eng._desk.step = delayed_step  # noqa: SLF001
    first = asyncio.create_task(
        eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    )
    await entered.wait()
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    release.set()
    await first

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert any(d.execution_decision == "CYCLE_REJECTED" for d in eng._decisions)  # noqa: SLF001


async def test_observe_is_rejected_after_engine_is_armed():
    eng, _, _ = await _engine()
    eng.arm()
    with pytest.raises(RuntimeError, match="remain DISABLED"):
        await eng.observe(now=_bars()[-1].ts, bars=_bars(), market_data=_md())


async def test_runtime_rejects_invalid_scheduler_profile():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy()],
        execution_slices=2,
    )
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    desk._scheduler._profile = (2.0, -1.0)  # noqa: SLF001 - hostile runtime config
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert eng.status is AutonomousStatus.DISABLED
    assert await broker.get_positions() == {}


async def test_runtime_rejects_replacement_scheduler_with_preloaded_work():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy()],
        execution_slices=2,
    )
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    replacement = ExecutionScheduler(desk._execution, slices=2)  # noqa: SLF001
    assert replacement.submit_parent(Order(INST, Side.SELL, 10), price=135.0)
    replacement.bind_work_guard(eng._schedule_guard)  # noqa: SLF001
    desk._scheduler = replacement  # noqa: SLF001 - operational callback rewire

    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert eng.status is AutonomousStatus.DISABLED
    assert await broker.get_positions() == {}


async def test_paper_fill_has_slippage_and_commission():
    from atp.core.events import QuoteEvent
    broker = PaperBroker(1_000_000.0)
    await broker.connect()
    broker.set_quote(QuoteEvent(INST, 100.0, 100.02, START))
    res = await broker.place_order(Order(INST, Side.BUY, 100, OrderType.MARKET))
    assert res.filled and res.fill.commission > 0.0 and res.fill.price >= 100.01


async def test_engine_bound_paper_broker_rejects_direct_order_while_disabled():
    eng, broker, _ = await _engine()

    with pytest.raises(ValueError, match="must be callable"):
        broker.bind_order_guard(None)
    broker.set_quote(QuoteEvent(INST, 100.0, 100.02, START))
    result = await broker.place_order(Order(INST, Side.BUY, 1, OrderType.MARKET))
    assert eng.status is AutonomousStatus.DISABLED
    assert not result.filled and "not risk-authorized" in result.reason
    assert await broker.get_positions() == {}

    broker._order_guard = None  # noqa: SLF001 - a bound broker must fail closed if guard is lost
    unbound = await broker.place_order(Order(INST, Side.BUY, 1, OrderType.MARKET))
    assert not unbound.filled and "guard unavailable" in unbound.reason
    assert await broker.get_positions() == {}


async def test_injected_cost_hook_cannot_reuse_send_context_in_spawned_task():
    class _SpawningImpact:
        broker = None
        task = None

        def impact_bps(self, _quantity, _adv):
            if self.task is None:
                rogue = Order(INST, Side.BUY, 100_000, OrderType.MARKET)
                self.task = asyncio.create_task(self.broker.place_order(rogue))
            return 0.0

    impact = _SpawningImpact()
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy()],
        impact_model=impact,
    )
    impact.broker = broker
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert impact.task is not None
    rogue_result = await impact.task

    assert not rogue_result.filled
    assert "not risk-authorized" in rogue_result.reason
    positions = await broker.get_positions()
    assert positions[INST.key].quantity < 100_000
    assert eng.status is AutonomousStatus.RUNNING


async def test_spawned_callback_task_cannot_reuse_attested_cycle_epoch():
    eng, broker, _ = await _engine()
    await _running(eng)
    spawned = []
    original_step = eng._desk.step  # noqa: SLF001

    async def spawning_step(*, now):
        forged = Account(
            cash=1_000_000.0,
            equity=1_000_000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            gross_exposure=0.0,
            net_exposure=0.0,
            positions={},
        )
        spawned.append(
            asyncio.create_task(
                eng._desk._execution.submit(  # noqa: SLF001
                    Order(INST, Side.BUY, 1_400),
                    forged,
                    price=135.0,
                    current_qty=0.0,
                )
            )
        )
        return await original_step(now=now)

    eng._desk.step = spawning_step  # noqa: SLF001
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    rogue_result = await spawned[0]

    assert not rogue_result.approved
    assert "not desk-authorized" in rogue_result.reason
    positions = await broker.get_positions()
    assert positions[INST.key].quantity < 2_800


async def test_same_task_observer_cannot_reenter_execution_with_forged_account():
    class _ReentrantObserver:
        execution = None
        result = None

        def update_bar(self, bar):
            if self.result is not None:
                return
            forged = Account(
                cash=1_000_000_000.0,
                equity=1_000_000_000.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                gross_exposure=0.0,
                net_exposure=0.0,
                positions={},
            )
            coroutine = self.execution.submit(
                Order(bar.instrument, Side.BUY, 100_000),
                forged,
                price=bar.close,
                current_qty=-100_000,
            )
            try:
                coroutine.send(None)
            except StopIteration as stopped:
                self.result = stopped.value
            else:
                coroutine.close()
                raise AssertionError("unauthorized reentrant submit unexpectedly suspended")

    observer = _ReentrantObserver()
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[],
        observers=[observer],
    )
    observer.execution = desk._execution  # noqa: SLF001
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)

    await eng.step(now=_bars()[-1].ts, bars=[_bars()[-1]], market_data=_md())

    assert observer.result is not None and not observer.result.approved
    assert "not desk-authorized" in observer.result.reason
    assert await broker.get_positions() == {}
    assert eng.status is AutonomousStatus.RUNNING


async def test_guarded_submit_uses_canonical_account_and_current_position():
    eng, broker, _ = await _engine()
    await _running(eng)
    submitted = []

    async def forged_desk_step(*, now):
        order = Order(INST, Side.BUY, 100_000)
        forged = Account(
            cash=1_000_000_000.0,
            equity=1_000_000_000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            gross_exposure=0.0,
            net_exposure=0.0,
            positions={},
        )
        with eng._desk._execution._authorize_submit(order):  # noqa: SLF001
            result = await eng._desk._execution.submit(  # noqa: SLF001
                order,
                forged,
                price=135.0,
                current_qty=-100_000,
            )
        submitted.append(result)
        return StepReport(now, blocked=[result])

    eng._desk.step = forged_desk_step  # noqa: SLF001
    await eng.step(now=_bars()[-1].ts, bars=[_bars()[-1]], market_data=_md())

    assert len(submitted) == 1 and not submitted[0].approved
    assert "position" in submitted[0].reason
    assert await broker.get_positions() == {}


async def test_scheduler_refreshes_account_between_two_queued_instruments():
    second = Instrument("TESTY", AssetClass.EQUITY)
    policy = TradingPolicy(
        capital=1_000_000.0,
        max_position_pct=1.0,
        max_leverage=2.0,
        max_open_positions=1,
    )
    desk, broker, risk = await build_paper_stack(
        policy=policy,
        strategies=[],
        execution_slices=1,
    )
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    broker.set_quote(QuoteEvent(second, 99.9, 100.1, START))
    results = []

    async def two_parent_step(*, now):
        scheduler = desk._scheduler  # noqa: SLF001
        for instrument in (INST, second):
            parent = Order(instrument, Side.BUY, 100)
            with scheduler._authorize_parent(parent):  # noqa: SLF001
                assert scheduler.submit_parent(parent, price=100.0)
        stale_empty_account = await broker.get_account()
        pairs = await scheduler.tick(
            stale_empty_account,
            price_fn=lambda _key: 100.0,
            now=now,
        )
        results.extend(result for result, _context in pairs)
        return StepReport(
            now,
            executed=[result for result in results if result.filled],
            blocked=[result for result in results if not result.filled],
        )

    desk.step = two_parent_step
    await eng.step(now=_bars()[-1].ts, bars=[_bars()[-1]], market_data=_md())

    assert len(results) == 2
    assert sum(result.filled for result in results) == 1
    assert len(await broker.get_positions()) == 1


async def test_same_task_callback_cannot_queue_parent_without_desk_risk_lease():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0),
        strategies=[MomentumStrategy()],
        execution_slices=2,
    )
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk)
    await _running(eng)
    attempted = []
    original_step = desk.step

    async def injecting_step(*, now):
        attempted.append(
            desk._scheduler.submit_parent(  # noqa: SLF001
                Order(INST, Side.SELL, 10), price=135.0
            )
        )
        return await original_step(now=now)

    desk.step = injecting_step
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())

    assert attempted == [False]
    assert all(
        work.side is not Side.SELL for work in desk._scheduler._working.values()  # noqa: SLF001
    )


async def test_disconnected_paper_broker_cannot_arm_or_continue_running():
    eng, broker, _ = await _engine()
    await broker.disconnect()
    with pytest.raises(RuntimeError, match="disconnected"):
        eng.arm()

    eng, broker, _ = await _engine()
    await _running(eng)
    await broker.disconnect()
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    assert eng.status is AutonomousStatus.DISABLED


async def test_nonfinite_realtime_quote_cannot_start_or_trade():
    bad = _md()
    bad[0]["ask"] = math.inf
    eng, broker, _ = await _engine()
    eng.arm()
    result = eng.start(confirm=True, connected=True, market_data=bad, risk_config=CFG)
    assert result["ok"] is False
    assert any("no healthy" in reason for reason in result["reasons"])
    assert await broker.get_positions() == {}


async def test_paper_broker_rejects_direct_order_while_disconnected():
    _, broker, _ = await _engine()
    from atp.core.events import QuoteEvent

    broker.set_quote(QuoteEvent(INST, 100.0, 100.02, START))
    await broker.disconnect()
    result = await broker.place_order(Order(INST, Side.BUY, 1, OrderType.MARKET))
    assert not result.filled and "disconnected" in result.reason
    assert await broker.get_positions() == {}


async def test_decision_telemetry_uses_effective_capital_mandate():
    eng, broker, _ = await _engine(capital=100_000.0)
    broker.credit_cash(900_000.0)
    eng.arm()
    await eng.step(now=_bars()[-1].ts, bars=_bars(), market_data=_md())
    decision = next(d for d in eng._decisions if d.max_allowed_risk is not None)  # noqa: SLF001
    assert decision.max_allowed_risk == 1_000.0
    assert decision.remaining_daily_budget == 3_000.0
    assert decision.risk_pct_capital == pytest.approx(
        decision.monetary_risk / 100_000.0
    )
