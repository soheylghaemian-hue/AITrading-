"""Dashboard read-model tests (§22): the snapshot assembled from a real paper run has the
right shape and values, and the (FastAPI-free) context serializes it."""

import math
from datetime import datetime, timedelta, timezone

from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.dashboard.api import DashboardContext
from atp.dashboard.snapshot import build_snapshot
from atp.governance import StrategyRegistry
from atp.journal import InMemoryJournal
from atp.live import LiveRunner, ReplayFeed, build_paper_stack
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.strategy.momentum import MomentumStrategy

INST = Instrument("X", AssetClass.EQUITY)
T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday


def _bars(n=150):
    return [Bar(INST, p := 100 + 4 * math.sin(i / 6.0) + 0.05 * i, p * 1.002, p * 0.998, p,
                1000 + i, T0 + timedelta(minutes=i)) for i in range(n)]


async def _run():
    journal = InMemoryJournal()
    registry = StrategyRegistry()
    registry.register("momentum")
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=100_000.0), strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
        journal=journal, registry=registry,
    )
    await LiveRunner(desk=desk, broker=broker, feed=ReplayFeed(_bars()), max_bars=150).run()
    return desk, broker, risk, journal, registry


def test_snapshot_empty_is_wellformed():
    risk = RiskEngine(limits=RiskLimits(), state=RiskState(100_000, 100_000))

    class _Acct:
        cash = equity = 100_000.0
        realized_pnl = unrealized_pnl = gross_exposure = net_exposure = 0.0
        positions: dict = {}
        gross_leverage = 0.0

    snap = build_snapshot(account=_Acct(), risk=risk).as_dict()
    assert snap["n_trades"] == 0
    assert snap["positions"] == []
    assert snap["analytics_overall"]["n_trades"] == 0
    assert snap["risk"]["halted"] is False


async def test_snapshot_from_paper_run_has_expected_shape():
    desk, broker, risk, journal, registry = await _run()
    account = await broker.get_account()
    snap = build_snapshot(
        account=account, risk=risk, journal=journal, registry=registry,
        market=desk.latest_market(),
    ).as_dict()

    # Top-level shape.
    for key in ("account", "risk", "positions", "market", "governance",
                "analytics_by_strategy", "recent_trades", "n_trades"):
        assert key in snap

    assert snap["account"]["equity"] == account.equity
    assert snap["n_trades"] == len(journal)
    assert snap["n_trades"] > 0
    # Governance reflects the registry.
    assert any(g["name"] == "momentum" for g in snap["governance"])
    # Market view carries the instrument's regime.
    assert INST.key in snap["market"]
    assert "regime" in snap["market"][INST.key]
    # Recent trades are newest-first and capped.
    assert len(snap["recent_trades"]) <= 20


async def test_dashboard_context_snapshot_dict():
    desk, broker, risk, journal, registry = await _run()
    ctx = DashboardContext(broker=broker, risk=risk, desk=desk, journal=journal, registry=registry)
    d = await ctx.snapshot_dict()
    assert d["account"]["equity"] > 0
    assert isinstance(d["analytics_by_strategy"], list)
    assert d["market"]  # non-empty market view
