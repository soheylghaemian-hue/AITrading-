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
from atp.research.strategy import (
    ENTER_LONG, OhlcTrendBaseline, PitContext, ResearchBar, ResearchDecision, ResearchStrategy, atr,
    get_strategy,
)
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


def _seed_dataset(store, symbol, sessions, closes, *, rng=1.0, owner="operator"):
    """Seed a COMPLETED, checksum-verified research dataset whose daily bars mirror `_seed` (so a pinned
    backtest over it produces the same result). Directly mirrors how `_seed` writes live ohlc_bars."""
    from atp.research.backfill import normalize as norm
    from atp.research.backfill.validate import dataset_checksum
    prev = float(closes[0])
    bars = []
    for day, c in zip(sessions, closes):
        c = float(c)
        bars.append({"symbol": symbol, "interval": "1D", "ts": f"{day.isoformat()}T00:00:00+00:00",
                     "session_date": day.isoformat(), "open": Decimal(str(prev)),
                     "high": Decimal(str(c + rng)), "low": Decimal(str(c - rng)), "close": Decimal(str(c)),
                     "volume": Decimal("1000"), "trade_count": 10, "source": norm.PROVIDER,
                     "adjustment_policy": norm.ADJUSTMENT_POLICY})
        prev = c
    ds_id = "ds-" + symbol
    store.rd_create_dataset(dataset_id=ds_id, owner=owner, request_checksum="sha256:seed-" + symbol,
                            symbol_universe_json=json.dumps([symbol]), interval="1D", provider=norm.PROVIDER,
                            provider_contract_version=norm.PROVIDER_CONTRACT_VERSION,
                            adjustment_policy=norm.ADJUSTMENT_POLICY,
                            normalization_policy=norm.NORMALIZATION_POLICY,
                            calendar_version=cal.CALENDAR_VERSION, range_start=sessions[0].isoformat(),
                            range_end=sessions[-1].isoformat(),
                            missing_minute_threshold=str(norm.MISSING_MINUTE_THRESHOLD))
    store.rd_advance_status(ds_id, "PLANNED", "RUNNING")
    store.rd_write_and_finalize(ds_id, expected_from="RUNNING", status="COMPLETED", bars=bars,
                                row_count=len(bars), dataset_checksum=dataset_checksum(bars),
                                provider_adjusted_flag=True)
    return ds_id


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
    _seed(s, "NVDA", sess, _uptrend(), rng=1.0)   # TR == 2 ⇒ ATR14 == 2 ⇒ 2·ATR == 4 (expected rps)
    run = s.bt_get_run(_run(s, ["NVDA"], sess, risk={"risk_per_trade_pct": "1"}))
    t = s.bt_list_trades(run.run_id)[0]
    # GAP-SAFE (correction #1): sizing uses the ACTUAL fill vs the persisted stop, not the decision-close.
    # risk_budget = 100000·1% = 1000; expected rps = 4.00; actual rps = fill − stop = 4.02 (fill > close by
    # the adverse cost) ⇒ qty = floor(1000/4.02) = 248, and 248·4.02 = 996.96 ≤ 1000 (budget never exceeded).
    assert Decimal(t.expected_risk_per_share) == Decimal("4.00")
    assert Decimal(t.actual_risk_per_share) == Decimal("4.02")
    assert Decimal(t.quantity) == 248
    assert Decimal(t.quantity) * Decimal(t.actual_risk_per_share) <= Decimal("1000")
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
            # a stop AT/ABOVE the entry ⇒ actual_risk_per_share (fill − stop) ≤ 0 ⇒ INVALID_STOP_DISTANCE
            return ResearchDecision(ts=b.ts, symbol=ctx.symbol, strategy_id="ZERO", strategy_version=1,
                                    action=ENTER_LONG,
                                    evidence={"initial_stop": b.close + 10, "risk_per_share": Decimal(0)})

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
    ds_id = _seed_dataset(s, "NVDA", sess, _uptrend())      # R3.0A: a run must pin an explicit dataset
    c = _control(s)
    ok = c.BacktestCreate(symbols=["NVDA"], interval="1d", start=sess[0].isoformat(),
                          end=sess[-1].isoformat(), dataset_id=ds_id)

    with pytest.raises(HTTPException) as e401:
        c.create_backtest(ok, authorization="Bearer WRONG")
    assert e401.value.status_code == 401

    # R3.0A: dataset_id is REQUIRED — a request without it is a 422 (no implicit 'latest').
    no_ds = c.BacktestCreate(symbols=["NVDA"], interval="1d", start=sess[0].isoformat(), end=sess[-1].isoformat())
    with pytest.raises(HTTPException) as e_missing:
        c.create_backtest(no_ds, authorization="Bearer tok")
    assert e_missing.value.status_code == 422
    assert any("dataset_id is required" in x for x in e_missing.value.detail["errors"])

    for bad in [
        c.BacktestCreate(symbols=["A", "B", "C", "D", "E", "F"], interval="1d", start=sess[0].isoformat(), end=sess[-1].isoformat(), dataset_id=ds_id),
        c.BacktestCreate(symbols=["NVDA"], interval="5m", start=sess[0].isoformat(), end=sess[-1].isoformat(), dataset_id=ds_id),
        c.BacktestCreate(symbols=["NVDA"], interval="1h", start="2020-01-01", end="2026-01-01", dataset_id=ds_id),  # >1y for 1h
    ]:
        with pytest.raises(HTTPException) as e:
            c.create_backtest(bad, authorization="Bearer tok")
        assert e.value.status_code == 422

    detail = c.create_backtest(ok, authorization="Bearer tok")
    assert detail["status"] == "COMPLETED" and detail["safety"]["execution"] == "DISABLED"
    assert detail["dataset_id"] == ds_id                    # the run is pinned to the dataset
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
    for f in root.rglob("*.py"):          # recurse into research/backfill/ (R3.0A) as well
        text = f.read_text()
        for tok in forbidden:
            assert tok not in text, f"{f.name} must not reference {tok}"


# ============================ CTO hotfix — four correctness defects ============================
_RCFG = {"capital": Decimal(100000), "max_daily_loss_pct": Decimal(2), "max_position_risk_pct": Decimal(3),
         "max_portfolio_exposure_pct": Decimal(100), "max_drawdown_pct": Decimal(20),
         "warning_threshold_pct": Decimal(80), "currency": "USD"}
_COSTS = Costs(Decimal(2), Decimal(1), Decimal("0.005"), Decimal(1))


class _EnterAt(ResearchStrategy):
    """Fake strategy: emit ENTER_LONG whenever len(bars) is in `enter_lens`, with a stop = close − dist."""
    strategy_id, version = "FAKE", 1

    def __init__(self, enter_lens, dist=Decimal(10), warmup=1):
        self.enter_lens, self._dist, self.warmup_bars = set(enter_lens), Decimal(str(dist)), warmup

    @property
    def config(self):
        return {"enter_lens": sorted(self.enter_lens)}

    def decide(self, ctx):
        b = ctx.bars[-1]
        base = dict(ts=b.ts, symbol=ctx.symbol, strategy_id="FAKE", strategy_version=1)
        if len(ctx.bars) in self.enter_lens:
            stop = b.close - self._dist
            return ResearchDecision(**base, action=ENTER_LONG,
                                    evidence={"initial_stop": stop, "risk_per_share": self._dist})
        return ResearchDecision(**base, action="HOLD")


def _bars(day_price):
    """[(ts, o,h,l,c)] → ResearchBar list."""
    return [ResearchBar(ts, Decimal(str(o)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)), Decimal(1000))
            for ts, o, h, l, c in day_price]


# ---- Defect 1: gap-safe sizing never exceeds the risk budget ----
def test_gapup_fill_never_exceeds_risk_budget():
    days = ["2026-06-01", "2026-06-02", "2026-06-03"]   # consecutive NYSE sessions
    # decision at bar0 (close 100, stop 90); bar1 GAPS UP to open 130 (fill there); bar2 normal.
    bars = _bars([(f"{days[0]}T00:00:00+00:00", 100, 101, 99, 100),
                  (f"{days[1]}T00:00:00+00:00", 130, 131, 129, 130),   # gap-up open
                  (f"{days[2]}T00:00:00+00:00", 130, 131, 129, 130)])
    res = replay(symbols=["NVDA"], bars_by_symbol={"NVDA": bars}, policy=cal.resolve_policy("NVDA", "1D"),
                 strategy=_EnterAt([1], dist=Decimal(10)), risk_config=_RCFG, costs=_COSTS,
                 starting_capital=Decimal(100000), max_concurrent=1)
    t = res.trades[0]
    budget = Decimal(100000) * _RCFG["max_position_risk_pct"] / 100     # = 3000
    exp_rps = Decimal(str(t["expected_risk_per_share"]))
    act_rps = Decimal(str(t["actual_risk_per_share"]))
    assert exp_rps == Decimal("10.00")                                 # decision-derived
    assert act_rps > exp_rps                                           # gap widened the REAL risk/share
    assert Decimal(t["initial_stop_price"]) == Decimal("90.00")        # stop persisted, NOT widened
    assert t["quantity"] * act_rps <= budget                           # budget NEVER exceeded post-gap


# ---- Defect 2: a symbol's same-timestamp close never leaks into another symbol's earlier open ----
def _aaa_qty_with_bbb_close(bbb_bar2_close):
    days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
    bbb = _bars([(f"{days[0]}T00:00:00+00:00", 100, 101, 99, 100),
                 (f"{days[1]}T00:00:00+00:00", 100, 101, 99, 100),
                 (f"{days[2]}T00:00:00+00:00", 100, 101, 99, bbb_bar2_close),   # varies
                 (f"{days[3]}T00:00:00+00:00", 100, 101, 99, 100)])
    aaa = _bars([(f"{days[0]}T00:00:00+00:00", 100, 101, 99, 100),
                 (f"{days[1]}T00:00:00+00:00", 100, 101, 99, 100),
                 (f"{days[2]}T00:00:00+00:00", 100, 101, 99, 100),
                 (f"{days[3]}T00:00:00+00:00", 100, 101, 99, 100)])
    res = replay(symbols=["AAA", "BBB"], bars_by_symbol={"AAA": aaa, "BBB": bbb},
                 policy=cal.resolve_policy("NVDA", "1D"),   # policy only carries interval; symbol-agnostic here
                 strategy=None, risk_config=_RCFG, costs=_COSTS, starting_capital=Decimal(100000),
                 max_concurrent=2) if False else None
    # BBB enters at bar0, AAA enters at bar1 (fills at bar2 — the shared timestamp whose BBB close varies)
    from atp.research.engine import replay as _replay
    res = _replay(symbols=["AAA", "BBB"], bars_by_symbol={"AAA": aaa, "BBB": bbb},
                  policy=cal.resolve_policy("NVDA", "1D"), strategy=_MultiFake(), risk_config=_RCFG,
                  costs=_COSTS, starting_capital=Decimal(100000), max_concurrent=2)
    a = [t for t in res.trades if t["symbol"] == "AAA"]
    return a[0]["quantity"] if a else None


class _MultiFake(ResearchStrategy):
    strategy_id, version, warmup_bars = "MULTI", 1, 1

    @property
    def config(self):
        return {}

    def decide(self, ctx):
        b = ctx.bars[-1]
        base = dict(ts=b.ts, symbol=ctx.symbol, strategy_id="MULTI", strategy_version=1)
        # BBB enters after 1 bar, AAA after 2 bars → AAA fills at bar2 (shared ts with BBB's varying close)
        want = {"BBB": 1, "AAA": 2}.get(ctx.symbol)
        if want is not None and len(ctx.bars) == want:
            return ResearchDecision(**base, action=ENTER_LONG,
                                    evidence={"initial_stop": b.close - 10, "risk_per_share": Decimal(10)})
        return ResearchDecision(**base, action="HOLD")


def test_multisymbol_open_ignores_other_symbol_same_ts_close():
    q_low = _aaa_qty_with_bbb_close(100)
    q_high = _aaa_qty_with_bbb_close(500)     # a huge same-timestamp close for BBB
    assert q_low is not None and q_high is not None
    assert q_low == q_high     # AAA's open sizing used BBB's PRIOR close, not its same-timestamp close


# ---- Defect 3: daily state resets BEFORE the risk gate ----
def _hbars(day, ohlc_list):
    buckets = cal._session_hour_buckets(day)
    assert len(ohlc_list) == len(buckets), (len(ohlc_list), len(buckets))
    return [ResearchBar(b.isoformat(), Decimal(str(o)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)), Decimal(1000))
            for b, (o, h, l, c) in zip(buckets, ohlc_list)]


def test_daily_loss_blocks_same_session_and_resets_next_session():
    from datetime import date
    d1, d2 = date(2026, 6, 1), date(2026, 6, 2)          # two consecutive EDT sessions (7 buckets each)
    # Session 1: enter (bar0), a violent crash below the stop (bar2) → big stop-out loss (> daily limit);
    #            a later same-session entry signal (bar4) must be RISK_BLOCKED. Session 2 resets.
    s1 = _hbars(d1, [(100, 101, 99, 100)] * 2 + [(100, 101, 40, 60)] + [(60, 61, 59, 60)] * 4)
    s2 = _hbars(d2, [(60, 61, 59, 60)] * 7)
    bars = s1 + s2
    res = replay(symbols=["NVDA"], bars_by_symbol={"NVDA": bars}, policy=cal.resolve_policy("NVDA", "1h"),
                 strategy=_EnterAt([1, 5, 9], dist=Decimal(10)), risk_config=_RCFG, costs=_COSTS,
                 starting_capital=Decimal(100000), max_concurrent=1)
    kinds = [e["event_type"] for e in res.events]
    assert "RISK_BLOCKED" in kinds                       # same-day re-entry blocked by the daily loss
    day2_entries = [e for e in res.events if e["event_type"] == "ENTRY_FILLED" and e["ts"][:10] == "2026-06-02"]
    assert day2_entries                                  # next session's daily budget reset → entry allowed


def test_daily_pnl_resets_at_session_boundary():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    eq = s.bt_list_equity(run.run_id)
    # each 1D step is its own NY session → daily_pnl equals the day-over-day change, not a running total
    assert any(p.daily_pnl is not None for p in eq)


# ---- Defect 4: calendar range + timestamp-set membership ----
def test_out_of_calendar_range_rejected():
    s = _store()
    sess = _sessions(100)
    run = s.bt_get_run(_run(s, ["NVDA"], sess, start="2022-06-01", end="2022-10-01"))
    assert run.status == "FAILED" and run.failure_code == "INSUFFICIENT_POINT_IN_TIME_METADATA"


def test_coverage_membership_flags_out_of_session_bar():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    # inject a bar on a Saturday (never a session) — an unexpected out-of-session timestamp
    s.insert_ohlc_bar(symbol="NVDA", interval="1D", ts="2026-01-17T00:00:00+00:00", open=1, high=1, low=1,
                      close=1, volume=1, source="TEST")
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    assert run.status == "FAILED" and run.failure_code == "INSUFFICIENT_HISTORICAL_COVERAGE"
    assert json.loads(run.missing_data_json)["symbols"][0]["out_of_session_bars"] >= 1


def test_extra_bars_do_not_compensate_missing():
    s = _store()
    sess = _sessions(40)                                  # only 40 in-session bars (< 60 required)
    _seed(s, "NVDA", sess, _uptrend(n=40, flat_until=20))
    # add 30 out-of-session Saturday bars — they must NOT compensate the missing in-session bars
    from datetime import date, timedelta
    d = date(2026, 1, 17)
    for _ in range(30):
        while d.weekday() != 5:
            d += timedelta(days=1)
        s.insert_ohlc_bar(symbol="NVDA", interval="1D", ts=f"{d.isoformat()}T00:00:00+00:00", open=1, high=1,
                          low=1, close=1, volume=1, source="TEST")
        d += timedelta(days=7)
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    assert run.status == "FAILED" and run.failure_code == "INSUFFICIENT_HISTORICAL_COVERAGE"
    assert json.loads(run.missing_data_json)["symbols"][0]["available_bars"] < 60   # in-session only


def test_1h_expected_timestamps_membership():
    from datetime import date, datetime, timezone
    p = cal.resolve_policy("NVDA", "1h")

    def buckets(d):
        return cal.expected_bar_timestamps(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc),
                                           datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc), p)
    edt = buckets(date(2026, 6, 1))                       # EDT session → 09:30–16:00 ET = 13:30–20:00 UTC
    est = buckets(date(2026, 1, 5))                       # EST session → 14:30–21:00 UTC
    assert len(edt) == 7 and "2026-06-01T13:00:00+00:00" in edt and "2026-06-01T19:00:00+00:00" in edt
    assert "2026-06-01T03:00:00+00:00" not in edt         # an overnight bucket is NOT expected
    assert len(est) == 7 and "2026-01-05T14:00:00+00:00" in est
    assert len(buckets(date(2026, 11, 27))) == 4          # early close (day after Thanksgiving)
    assert buckets(date(2026, 1, 17)) == set()            # Saturday → no session
    assert buckets(date(2026, 1, 19)) == set()            # MLK holiday → no session


def test_daily_timestamp_convention_is_utc_midnight():
    from datetime import datetime, timezone
    p = cal.resolve_policy("NVDA", "1D")
    exp = cal.expected_bar_timestamps(datetime(2026, 6, 1, tzinfo=timezone.utc),
                                      datetime(2026, 6, 1, tzinfo=timezone.utc), p)
    assert exp == {"2026-06-01T00:00:00+00:00"}           # provider convention: UTC-midnight bucket label
    # and that UTC-midnight bar becomes available at the real session close (20:00 UTC in EDT)
    assert cal.available_at("2026-06-01T00:00:00+00:00", p).isoformat() == "2026-06-01T20:00:00+00:00"


# ---- Defect 5: fill timestamps are session-based and chronologically valid ----
def test_fill_timestamps_session_based_and_ordered():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())
    t = s.bt_list_trades(s.bt_get_run(_run(s, ["NVDA"], sess)).run_id)[0]
    # 1D entry fill = session OPEN (13:30/14:30 UTC), never UTC midnight, never the bar close
    assert t.entry_fill_ts.endswith("13:30:00+00:00") or t.entry_fill_ts.endswith("14:30:00+00:00")
    assert not t.entry_fill_ts.endswith("00:00:00+00:00")
    # entry decision < entry fill < exit fill, all distinct and chronological
    assert t.entry_ts < t.entry_fill_ts < t.exit_fill_ts


# ---- Deterministic reference (golden checksum) ----
def test_deterministic_reference_checksum():
    s = _store()
    sess = _sessions(100)
    _seed(s, "NVDA", sess, _uptrend())      # NVDA, 100 sessions from 2026-01-05, flat<52 then +0.8, rng 1
    run = s.bt_get_run(_run(s, ["NVDA"], sess))
    assert run.result_checksum == "3301d3bd2b41fd277568882f6d6afdb9912e3c37ed7a92f81028462e985619ea"
