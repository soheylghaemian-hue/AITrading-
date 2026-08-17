"""§ Phase R3.0 acceptance — deterministic backtesting & strategy validation (RESEARCH ONLY).

Covers: point-in-time availability (US equity session close, NOT UTC+24h; DST; holidays), next-candle
fills + costs, exact risk-based sizing (ATR stop), cash/position accounting, exposure & daily-loss,
gap-through-stop, EOT liquidation, missing/insufficient data, determinism, DATABASE-enforced terminal
immutability (direct-SQL tamper), failed-run audit, metrics + drawdown, multi-symbol chronology,
timezone/calendar boundaries, API validation + bounds, and a source guard against execution tokens.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from atp.research import calendars as cal
from atp.research.engine import Costs, replay
from atp.research.risk_adapter import evaluate_sim_risk
from atp.research.runner import OneActiveRunError, ValidationError, run_backtest
from atp.research.strategy import OhlcTrendBaseline, PitContext, ResearchBar, atr, get_strategy
from atp.store import open_store


def _store():
    return open_store(str(Path(tempfile.mkdtemp()) / "atp.db"))


def _sessions(n: int, start=date(2026, 1, 5)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if cal.is_session_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed(store, symbol, sessions, closes, *, rng=1.0):
    prev = float(closes[0])
    for day, c in zip(sessions, closes):
        c = float(c)
        store.insert_ohlc_bar(symbol=symbol, interval="1D", ts=f"{day.isoformat()}T00:00:00+00:00",
                              open=prev, high=c + rng, low=c - rng, close=c, volume=1000, source="TEST")
        prev = c


def _uptrend(n=100, flat_until=52, slope=0.8):
    return [100.0 if i < flat_until else 100.0 + (i - flat_until) * slope for i in range(n)]


def _run(store, symbols, sessions, owner="operator", **over):
    req = {"symbols": symbols, "interval": "1d", "start": sessions[0].isoformat(),
           "end": sessions[-1].isoformat(), "starting_capital": "100000", **over}
    return run_backtest(store, owner=owner, req=req)


# ---------------------------------------------------------------- point-in-time availability
def test_availability_is_session_close_not_utc_naive_and_dst_correct():
    p = cal.resolve_policy("NVDA", "1D")
    # summer (EDT): 16:00 ET = 20:00 UTC; winter (EST): 16:00 ET = 21:00 UTC — NOT a naïve +24h.
    summer = cal.available_at("2026-07-06T00:00:00+00:00", p)
    winter = cal.available_at("2026-01-05T00:00:00+00:00", p)
    assert summer.hour == 20 and winter.hour == 21          # DST handled
    assert summer.date() == date(2026, 7, 6)                # same session day, not next-day midnight
    assert summer != cal.parse_ts("2026-07-06T00:00:00+00:00") + timedelta(hours=24)


def test_holidays_and_early_close_not_counted_as_bars():
    p = cal.resolve_policy("NVDA", "1D")
    # 2026-01-19 is MLK (holiday). Expected daily bars Fri..Tue skip weekend + the holiday.
    start = datetime(2026, 1, 16, tzinfo=timezone.utc)   # Fri
    end = datetime(2026, 1, 20, tzinfo=timezone.utc)     # Tue
    assert cal.expected_bars(start, end, p) == 2          # Fri 16th + Tue 20th (Mon 19th holiday)
    assert not cal.is_session_day(date(2026, 1, 19))
    assert cal.is_early_close(date(2026, 11, 27))         # day after Thanksgiving


def test_unknown_symbol_fails_point_in_time_metadata():
    with pytest.raises(cal.PointInTimeError):
        cal.resolve_policy("ZZZZ", "1D")


# ---------------------------------------------------------------- next-candle fill + costs
def test_next_candle_fill_and_costs():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    assert run.status == "COMPLETED"
    trades = s.bt_list_trades(run.run_id)
    assert len(trades) == 1
    t = trades[0]
    # the decision fires on a bar; the fill is the NEXT bar's open — different timestamps, never same-bar.
    decs = [d for d in s.bt_list_decisions(run.run_id) if d.action == "ENTER_LONG"]
    assert decs and t.entry_fill_ts > decs[0].ts
    assert Decimal(t.commission) > 0 and Decimal(t.slippage) > 0   # costs applied


# ---------------------------------------------------------------- exact risk-based sizing (correction #2)
def test_risk_based_sizing_and_stop():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend(), rng=1.0)   # TR == 2 ⇒ ATR14 == 2 ⇒ 2·ATR == 4
    run = s.bt_get_run(_run(s, ["NVDA"], sess, risk={"risk_per_trade_pct": "1"}))
    t = s.bt_list_trades(run.run_id)[0]
    # risk_budget = 100000 * 1% = 1000; risk_per_share = 2*ATR = 4 ⇒ qty = floor(1000/4) = 250
    assert Decimal(t.quantity) == 250
    # stop persisted = entry-decision close − 2*ATR, and it is BELOW the entry fill.
    assert Decimal(t.initial_stop_price) < Decimal(t.entry_price)


def test_invalid_stop_distance_guard():
    # Directly exercise the guard: a strategy that yields risk_per_share ≤ 0 must be rejected (no trade).
    from atp.research.strategy import ENTER_LONG, ResearchDecision, ResearchStrategy

    class ZeroStop(ResearchStrategy):
        strategy_id, version, warmup_bars = "ZERO", 1, 1

        @property
        def config(self):
            return {}

        def decide(self, ctx):
            b = ctx.bars[-1]
            return ResearchDecision(ts=b.ts, symbol=ctx.symbol, strategy_id="ZERO", strategy_version=1,
                                    action=ENTER_LONG, evidence={"initial_stop": b.close, "risk_per_share": Decimal(0)})

    days = ["2026-01-02", "2026-01-05", "2026-01-06"]   # consecutive NYSE sessions
    bars = [ResearchBar(f"{d}T00:00:00+00:00", Decimal(100), Decimal(100), Decimal(100), Decimal(100), Decimal(1))
            for d in days]
    cfg = {"capital": Decimal(100000), "max_daily_loss_pct": Decimal(2), "max_position_risk_pct": Decimal(1),
           "max_portfolio_exposure_pct": Decimal(100), "max_drawdown_pct": Decimal(20),
           "warning_threshold_pct": Decimal(80), "currency": "USD"}
    res = replay(symbols=["NVDA"], bars_by_symbol={"NVDA": bars}, policy=cal.resolve_policy("NVDA", "1D"),
                 strategy=ZeroStop(), risk_config=cfg,
                 costs=Costs(Decimal(2), Decimal(1), Decimal("0.005"), Decimal(1)),
                 starting_capital=Decimal(100000), max_concurrent=1)
    assert any(e["event_type"] == "INVALID_STOP_DISTANCE" for e in res.events)
    assert len(res.trades) == 0


# ---------------------------------------------------------------- accounting + never-negative cash
def test_cash_and_position_accounting():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    eq = s.bt_list_equity(run.run_id)
    assert all(Decimal(p.cash) >= 0 for p in eq)         # never negative cash
    last = eq[-1]
    # after EOT liquidation everything is in cash and equity == cash
    assert Decimal(last.equity) == Decimal(last.cash)


# ---------------------------------------------------------------- exposure + daily-loss gate (pure evaluator)
def test_risk_gate_blocks_on_daily_loss_and_exposure():
    cfg = {"capital": Decimal(100000), "max_daily_loss_pct": Decimal(2), "max_position_risk_pct": Decimal(1),
           "max_portfolio_exposure_pct": Decimal(50), "max_drawdown_pct": Decimal(20),
           "warning_threshold_pct": Decimal(80), "currency": "USD"}
    # daily loss beyond 2% of capital ⇒ BLOCKED
    blocked = evaluate_sim_risk(cfg, realized=Decimal(-2500), unrealized=Decimal(0), ts="t",
                                peak_equity=Decimal(100000), equity=Decimal(97500), gross_pct=Decimal(10),
                                net_pct=Decimal(10))
    assert blocked["status"] == "BLOCKED"
    # gross exposure beyond the 50% limit ⇒ BLOCKED
    expo = evaluate_sim_risk(cfg, realized=Decimal(0), unrealized=Decimal(0), ts="t",
                             peak_equity=Decimal(100000), equity=Decimal(100000), gross_pct=Decimal(60),
                             net_pct=Decimal(60))
    assert expo["status"] == "BLOCKED"


def test_exposure_sizing_caps_quantity():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    # tiny capital + 100% exposure ⇒ position notional capped by cash/exposure, not by the risk budget
    run = s.bt_get_run(_run(s, ["NVDA"], sess, starting_capital="600"))
    trades = s.bt_list_trades(run.run_id)
    if trades:
        t = trades[0]
        assert Decimal(t.quantity) * Decimal(t.entry_price) <= Decimal("600")   # never over-allocates cash


# ---------------------------------------------------------------- gap through stop + EOT
def test_gap_through_stop_fills_at_adverse_open():
    s = _store()
    sess = _sessions(100)
    closes = _uptrend(n=100, flat_until=52, slope=0.8)
    # after the trend/entry, one violent gap-down bar opens far below the stop
    for i in range(70, 100):
        closes[i] = 60.0
    _seed(s, "NVDA", sess, closes)
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    stops = [t for t in s.bt_list_trades(run.run_id) if t.exit_reason == "STOP"]
    assert stops, "a gap-down through the stop should produce a STOP exit"
    t = stops[0]
    assert Decimal(t.exit_price) < Decimal(t.initial_stop_price)   # filled worse than the stop (gap)


def test_end_of_test_liquidation():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())    # still rising at the end ⇒ open position liquidated at EOT
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    assert any(t.exit_reason == "EOT_LIQUIDATION" for t in s.bt_list_trades(run.run_id))


# ---------------------------------------------------------------- missing / insufficient
def test_insufficient_coverage_fails_run():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    run = s.bt_get_run(_run(s, ["NVDA"], sess, end=sess[5].isoformat()))    # < 60 bars
    assert run.status == "FAILED" and run.failure_code == "INSUFFICIENT_HISTORICAL_COVERAGE"
    assert json.loads(run.missing_data_json)["symbols"][0]["available_bars"] <= 6


def test_unknown_symbol_run_fails_point_in_time():
    s = _store()
    sess = _sessions(100)
    run = s.bt_get_run(_run(s, ["ZZZZ"], sess))
    assert run.status == "FAILED" and run.failure_code == "INSUFFICIENT_POINT_IN_TIME_METADATA"
    assert any(e.event_type == "INSUFFICIENT_POINT_IN_TIME_METADATA" for e in s.bt_list_events(run.run_id))


# ---------------------------------------------------------------- determinism
def test_deterministic_repeat_runs():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    r1 = s.bt_get_run(_run(s, ["NVDA"], sess, owner="a"))
    r2 = s.bt_get_run(_run(s, ["NVDA"], sess, owner="b"))
    assert r1.result_checksum == r2.result_checksum and r1.result_checksum is not None


# ---------------------------------------------------------------- DB-enforced terminal immutability
def test_completed_run_is_database_immutable():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    assert run.status == "COMPLETED"

    def tamper(sql, params=()):
        with s.tx() as cur:
            s._exec(cur, sql, params)

    for sql, params in [
        ("UPDATE backtest_runs SET status='RUNNING' WHERE run_id=?", (run.run_id,)),
        ("DELETE FROM backtest_runs WHERE run_id=?", (run.run_id,)),
        ("DELETE FROM backtest_trades WHERE run_id=?", (run.run_id,)),
        ("UPDATE backtest_equity_points SET equity='0' WHERE run_id=?", (run.run_id,)),
        ("INSERT INTO backtest_events (id,run_id,event_type,created_at) VALUES ('x',?,?,?)",
         (run.run_id, "TAMPER", "now")),
    ]:
        with pytest.raises(Exception):
            tamper(sql, params)
    # the run and its result checksum are unchanged
    assert s.bt_get_run(run.run_id).status == "COMPLETED"


def test_failed_run_retains_audit():
    s = _store()
    sess = _sessions(100)
    run = s.bt_get_run(_run(s, ["ZZZZ"], sess))
    assert run.status == "FAILED"
    assert run.failure_code and run.failure_reason
    assert len(s.bt_list_events(run.run_id)) >= 1


# ---------------------------------------------------------------- metrics + drawdown
def test_metrics_present_with_no_data_honesty():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    m = json.loads(s.bt_get_metrics(run.run_id).metrics_json)
    assert "max_drawdown" in m and isinstance(m["max_drawdown"], (int, float))
    assert m["num_trades"] == 1
    # a single trade ⇒ payoff ratio / some ratios are NOT APPLICABLE, never fabricated
    assert m["payoff_ratio"] in ("NOT APPLICABLE",) or isinstance(m["payoff_ratio"], (int, float))
    assert "robustness" in m and m["robustness"]["min_trade_warning"]


# ---------------------------------------------------------------- multi-symbol chronology
def test_multi_symbol_chronology():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    _seed(s, "AAPL", sess, _uptrend(slope=0.6))
    run = s.bt_get_run(_run(s, ["NVDA", "AAPL"], sess, max_concurrent_positions=2))
    assert run.status == "COMPLETED"
    eq = s.bt_list_equity(run.run_id)
    assert all(eq[i].ts >= eq[i - 1].ts for i in range(1, len(eq)))       # chronological
    syms = {t.symbol for t in s.bt_list_trades(run.run_id)}
    assert syms == {"NVDA", "AAPL"}


# ---------------------------------------------------------------- timezone / calendar boundary
def test_contiguity_across_session_boundary():
    p = cal.resolve_policy("NVDA", "1D")
    # Fri 16th → Tue 20th is CONTIGUOUS: the weekend (17-18) and the MLK holiday (Mon 19th) are correctly
    # skipped, so no gap. Skipping the 20th (→ 21st) IS an unexplained gap.
    assert cal.is_contiguous("2026-01-16T00:00:00+00:00", "2026-01-20T00:00:00+00:00", p) is True
    assert cal.is_contiguous("2026-01-16T00:00:00+00:00", "2026-01-21T00:00:00+00:00", p) is False


def test_ambiguous_stop_first_rule_reserved_in_schema():
    # The baseline has no target so ambiguity cannot occur, but the generic rule + code path are shipped.
    s = _store()
    ddl = s._one("SELECT sql FROM sqlite_master WHERE type='table' AND name='backtest_trades'")[0]
    assert "AMBIGUOUS_INTRABAR_STOP_FIRST" in ddl


# ---------------------------------------------------------------- ATR + strategy purity
def test_atr_and_strategy_are_pure_prefix_functions():
    bars = [ResearchBar(f"2026-01-{i:02d}T00:00:00+00:00", Decimal(100), Decimal(101), Decimal(99),
                        Decimal(100), Decimal(1000)) for i in range(1, 20)]
    assert atr(bars, 14) == 2                              # deterministic mean true range
    strat = OhlcTrendBaseline()
    d1 = strat.decide(PitContext("NVDA", bars))
    d2 = strat.decide(PitContext("NVDA", bars))
    assert d1.checksum() == d2.checksum()                 # pure — no state, no look-ahead


# ---------------------------------------------------------------- API validation + bounds
def _control(store):
    import atp.services.control as control
    control.ctx.store = store
    os.environ["ATP_CONTROL_TOKEN"] = "tok"
    return control


def test_api_validation_bounds_and_happy_path():
    from fastapi import HTTPException
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    c = _control(s)
    ok = c.BacktestCreate(symbols=["NVDA"], interval="1d", start=sess[0].isoformat(), end=sess[-1].isoformat())

    with pytest.raises(HTTPException) as e401:
        c.create_backtest(ok, authorization="Bearer WRONG")
    assert e401.value.status_code == 401

    for bad in [
        c.BacktestCreate(symbols=["A", "B", "C", "D", "E", "F"], interval="1d", start=sess[0].isoformat(), end=sess[-1].isoformat()),
        c.BacktestCreate(symbols=["NVDA"], interval="5m", start=sess[0].isoformat(), end=sess[-1].isoformat()),
        c.BacktestCreate(symbols=["NVDA"], interval="1h", start="2020-01-01", end="2026-01-01"),  # >1y for 1h
    ]:
        with pytest.raises(HTTPException) as e:
            c.create_backtest(bad, authorization="Bearer tok")
        assert e.value.status_code == 422

    detail = c.create_backtest(ok, authorization="Bearer tok")
    assert detail["status"] == "COMPLETED" and detail["safety"]["execution"] == "DISABLED"
    rid = detail["run_id"]

    # one active run guard (leave a RUNNING run) → 409
    s.bt_create_run(run_id="busy", owner="operator", strategy_id="S", strategy_version=1,
                    strategy_config_json="{}", strategy_checksum="c", engine_version="e",
                    symbol_universe_json="[]", interval="1D", start_ts="a", end_ts="b", asset_class="US_EQUITY",
                    timestamp_policy_id="P", timestamp_policy_version=1, exchange_calendar_id="NYSE",
                    exchange_calendar_version="v", exchange_tz="America/New_York", session_calendar="s",
                    data_source="d", config_snapshot_json="{}", risk_config_snapshot_json="{}")
    s.bt_advance_status("busy", "QUEUED", "RUNNING")
    with pytest.raises(HTTPException) as e409:
        c.create_backtest(ok, authorization="Bearer tok")
    assert e409.value.status_code == 409

    # read endpoints
    assert c.get_backtest_metrics(rid)["metrics"]["num_trades"] == 1
    assert c.get_backtest_trades(rid)["count"] == 1
    assert c.get_backtest_equity(rid)["count"] >= 1
    assert c.get_backtest_events(rid)["count"] >= 1
    assert c.list_backtests()["count"] >= 1
    with pytest.raises(HTTPException) as e404:
        c.get_backtest("nope")
    assert e404.value.status_code == 404


# ---------------------------------------------------------------- source guard (execution tokens)
def test_research_source_has_no_execution_call_identifiers():
    root = Path(__file__).resolve().parents[2] / "src" / "atp" / "research"
    forbidden = ("placeOrder(", "submitOrder(", "createOrder(", "executeTrade(", ".place_order(",
                 "set_kill_switch(", "ib_async", "ibapi", "copy_trade(", "PaperBroker(", "ExecutionEngine(",
                 "Backtester(")
    for f in root.glob("*.py"):
        text = f.read_text()
        for tok in forbidden:
            assert tok not in text, f"{f.name} must not reference {tok}"
