"""Command Center tests (§13/§22/§23/§33): notification center, extended read-model (mode,
agents, system health, hero funnel), and the protected emergency-stop control — all real data,
empty states where none exists, no fabricated performance."""

import math
from datetime import datetime, timedelta, timezone

from atp.brokers.base import Account
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.dashboard import Kind, NotificationCenter, Severity, build_snapshot
from atp.dashboard.api import DashboardContext
from atp.dashboard.snapshot import AGENT_NAMES
from atp.governance import StrategyRegistry
from atp.journal import InMemoryJournal
from atp.live import LiveRunner, ReplayFeed, build_paper_stack
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.strategy.momentum import MomentumStrategy

INST = Instrument("X", AssetClass.EQUITY)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- notifications
def test_notification_center_severity_and_recency():
    nc = NotificationCenter(capacity=10)
    nc.push(Kind.TRADE_OPENED, "opened GOLD long", severity=Severity.INFO)
    nc.push(Kind.RISK_HALT, "daily loss breached", severity=Severity.CRITICAL)
    recent = nc.recent()
    assert recent[0].kind is Kind.RISK_HALT           # newest first
    assert nc.unresolved_critical() == 1
    assert [n.severity for n in nc.by_severity(Severity.CRITICAL)] == [Severity.CRITICAL]


# --------------------------------------------------------------------------- read-model shapes
def _empty_account():
    return Account(cash=100_000.0, equity=100_000.0, realized_pnl=0.0, unrealized_pnl=0.0,
                   gross_exposure=0.0, net_exposure=0.0, positions={})


def _risk():
    return RiskEngine(limits=RiskLimits(), state=RiskState(100_000.0, 100_000.0))


def test_empty_snapshot_shows_no_fabricated_data():
    snap = build_snapshot(account=_empty_account(), risk=_risk(), mode="paper").as_dict()
    assert snap["mode"] == "paper"
    assert snap["system_status"] == "online"
    assert snap["n_trades"] == 0
    assert snap["positions"] == []
    assert snap["hero"]["scanned"] is None            # NO DATA, not a fake number
    # Agents are listed (the AI team) but with no invented performance.
    idle = {a["name"]: a for a in snap["agents"]}
    assert set(AGENT_NAMES).issubset(idle)
    assert idle["momentum"]["trades"] == 0 and idle["momentum"]["win_rate"] is None


def test_system_status_reflects_halt_and_disconnect():
    risk = _risk()
    risk.set_broker_connected(False)
    snap = build_snapshot(account=_empty_account(), risk=risk).as_dict()
    assert snap["system_status"] == "degraded"
    assert snap["system_health"]["broker"] == "offline"

    risk.set_broker_connected(True)
    risk.kill_switch("test")
    snap2 = build_snapshot(account=_empty_account(), risk=risk).as_dict()
    assert snap2["system_status"] == "halted"
    assert snap2["risk"]["killed"] is True


def test_hero_funnel_passthrough_when_provided():
    funnel = {"scanned": 1842, "opportunities": 127, "after_liquidity": 18,
              "after_statistical": 7, "portfolio_approved": 3, "risk_approved": 2}
    snap = build_snapshot(account=_empty_account(), risk=_risk(), scan_funnel=funnel).as_dict()
    assert snap["hero"]["scanned"] == 1842 and snap["hero"]["risk_approved"] == 2


async def test_agents_reflect_real_journal_edge():
    journal = InMemoryJournal()
    registry = StrategyRegistry()
    registry.register("momentum")
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal, registry=registry,
    )
    bars = [Bar(INST, p := 100 + 4 * math.sin(i / 6.0) + 0.05 * i, p * 1.002, p * 0.998, p,
                1000, T0 + timedelta(minutes=i)) for i in range(150)]
    await LiveRunner(desk=desk, broker=broker, feed=ReplayFeed(bars), max_bars=150).run()

    snap = build_snapshot(account=await broker.get_account(), risk=risk, journal=journal,
                          registry=registry, market=desk.latest_market()).as_dict()
    agents = {a["name"]: a for a in snap["agents"]}
    assert agents["momentum"]["trades"] > 0            # real recorded trades
    assert agents["momentum"]["status"] == "active"


# --------------------------------------------------------------------------- emergency stop control
async def test_emergency_stop_trips_kill_switch():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(),
    )
    nc = NotificationCenter()
    ctx = DashboardContext(broker=broker, risk=risk, desk=desk, notifications=nc)
    assert not risk.state.killed
    result = ctx.emergency_stop()
    assert result["status"] == "halted"
    assert risk.state.killed                            # engine is the authority; dashboard trips it
    assert nc.recent()[0].kind is Kind.EMERGENCY_STOP
    ctx.resume()
    assert not risk.state.killed


async def test_snapshot_dict_includes_mode_and_status():
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(),
    )
    ctx = DashboardContext(broker=broker, risk=risk, desk=desk, mode="paper")
    d = await ctx.snapshot_dict()
    assert d["mode"] == "paper" and d["system_status"] in ("online", "degraded", "halted")
    assert "agents" in d and "system_health" in d and "hero" in d
