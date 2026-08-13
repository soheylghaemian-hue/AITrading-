"""Dynamic TRADEABLE universe + hard proof the AI cannot trade unavailable instruments (§ Phase 9)."""

from datetime import datetime, timedelta, timezone

from atp.autonomous import PaperAutonomousEngine
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.dashboard.snapshot import build_snapshot, tradeable_universe
from atp.journal import InMemoryJournal
from atp.live import build_paper_stack
from atp.policy import TradingPolicy
from atp.risk.config import TradingRiskConfig
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.strategy import MomentumStrategy

START = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)


def _risk():
    return RiskEngine(limits=RiskLimits(), state=RiskState(day_start_equity=1_000_000.0, peak_equity=1_000_000.0))


def _mk(symbol, status, mdt=None, bid=None, ask=None, err=None):
    return {"symbol": symbol, "asset_class": "equity", "exchange": "X", "status": status,
            "market_data_type": mdt, "bid": bid, "ask": ask, "last": None, "error_code": err,
            "timestamp": "2026-08-13T14:00:00Z", "reason": ""}


# --------------------------------------------------------------------------- universe derivation
def test_universe_matches_current_state():
    md = [
        {"symbol": "EUR.USD", "asset_class": "fx", "exchange": "IDEALPRO", "status": "DATA_AVAILABLE",
         "market_data_type": "REALTIME", "bid": 1.152, "ask": 1.1521, "last": None,
         "error_code": None, "timestamp": "2026-08-13T14:00:00Z"},
        _mk("AAPL", "DATA_NOT_AVAILABLE", err=10089),
        _mk("NVDA", "DATA_NOT_AVAILABLE", err=10089),
        _mk("SPY", "DATA_NOT_AVAILABLE", err=10089),
    ]
    u = {r["symbol"]: r for r in tradeable_universe(md)}
    assert u["EUR.USD"]["tradeable"] is True and u["EUR.USD"]["state"] == "TRADEABLE"
    assert u["EUR.USD"]["data_type"] == "REALTIME" and u["EUR.USD"]["last_valid_timestamp"]
    for sym in ("AAPL", "NVDA", "SPY"):
        assert u[sym]["tradeable"] is False and u[sym]["state"] == "BLOCKED"
        assert "10089" in u[sym]["reason"] and u[sym]["last_valid_timestamp"] is None


def test_stale_and_delayed_are_blocked():
    md = [_mk("X", "STALE", "REALTIME", 10, 10.1), _mk("Y", "DELAYED", "DELAYED", 10, 10.1)]
    u = {r["symbol"]: r for r in tradeable_universe(md)}
    assert not u["X"]["tradeable"] and "stale" in u["X"]["reason"].lower()
    assert not u["Y"]["tradeable"] and "delayed" in u["Y"]["reason"].lower()


def test_available_without_price_is_blocked():
    u = tradeable_universe([_mk("Z", "DATA_AVAILABLE", "REALTIME", bid=None, ask=None)])[0]
    assert u["tradeable"] is False and "price" in u["reason"].lower()


def test_snapshot_includes_tradeable_universe():
    snap = build_snapshot(account=None, risk=_risk(),
                          market_data=[_mk("AAPL", "DATA_NOT_AVAILABLE", err=10089)]).as_dict()
    assert snap["tradeable_universe"][0]["state"] == "BLOCKED"


# --------------------------------------------------------------------------- AI cannot trade blocked
async def test_ai_never_generates_executable_opportunity_for_unavailable_instrument():
    """Feed a strong uptrend for AAPL while its data is DATA_NOT_AVAILABLE (10089). Even RUNNING,
    the gate means AAPL is never fed to the desk → no signal, no opportunity, no approval, no trade."""
    aapl = Instrument("AAPL", AssetClass.EQUITY)
    journal = InMemoryJournal()
    desk, broker, risk = await build_paper_stack(
        policy=TradingPolicy(capital=1_000_000.0), strategies=[MomentumStrategy()], journal=journal)
    eng = PaperAutonomousEngine(desk=desk, broker=broker, risk=risk, journal=journal)
    eng.arm()
    eng.start(confirm=True, connected=True,
              market_data=[{"symbol": "EUR.USD", "status": "DATA_AVAILABLE", "market_data_type": "REALTIME",
                            "bid": 1.1, "ask": 1.1001}], risk_config=TradingRiskConfig(1e6, 0.01, 0.03))
    bars = [Bar(aapl, 100 + i, (100 + i) * 1.002, (100 + i) * 0.998, 100 + i, 5000,
                START + timedelta(minutes=i)) for i in range(60)]
    md = [{"symbol": "AAPL", "status": "DATA_NOT_AVAILABLE", "market_data_type": None,
           "bid": None, "ask": None, "error_code": 10089}]
    await eng.step(now=bars[-1].ts, bars=bars, market_data=md)

    assert len(await broker.get_positions()) == 0                       # never paper traded
    aapl_decisions = [d for d in eng._decisions if d.instrument == "AAPL"]  # noqa: SLF001
    assert aapl_decisions and all(d.execution_decision == "NO_TRADE" for d in aapl_decisions)
    assert all(d.risk_decision != "APPROVED" for d in aapl_decisions)   # no executable opportunity
    assert all(d.agent is None for d in aapl_decisions)                 # no agent ever saw it
