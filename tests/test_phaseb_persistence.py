"""Phase B — durable persistence foundation acceptance tests.

Backend under test is SQLite (file-backed, real transactions); a "restart" is a fresh Store opened
over the SAME file. Every safety-critical guarantee the spec names is proven here."""

from decimal import Decimal

import pytest

from atp.runtime import (
    LifecycleError,
    LifecycleManager,
    OrderManager,
    RuntimeStatus,
    TradingGate,
    apply_fill_to_position,
    enforce_daily_loss,
    reconcile,
    reconstruct_positions,
    remaining_daily_budget,
)
from atp.runtime.lifecycle import RECOVERY_STEPS
from atp.store import D, open_store
from atp.store.base import FillRow, utcnow_iso


def _db(tmp_path):
    return open_store(str(tmp_path / "atp.db"))


def _reopen(tmp_path, store):
    store.close()
    return _db(tmp_path)


# ------------------------------------------------------------------ migrations + money
def test_migrations_apply_and_are_idempotent(tmp_path):
    s = _db(tmp_path)
    assert s.ping()
    applied = sorted(r[0] for r in s._all("SELECT version FROM schema_migrations"))
    assert applied == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    # tables exist
    for t in ("runtime_state", "orders", "fills", "positions", "kill_switch", "daily_pnl",
              "audit_events", "service_heartbeats", "market_data_health", "ohlc_bars", "news_items",
              "traders", "trader_performance", "trader_positions",
              "companies", "financial_metrics", "valuation", "analyst_estimates",
              "options_snapshot", "options_flow", "ai_assessments", "ai_assessment_components",
              "ai_predictions", "ai_prediction_outcomes", "ai_governance_results",
              "data_completeness_snapshots", "macro_snapshots",
              "institutional_position_changes", "insider_transactions", "insider_clusters"):
        s._one(f"SELECT COUNT(*) FROM {t}")
    # migration 002 money columns exist
    s._one("SELECT notional, stop, target, monetary_risk, risk_pct FROM orders")
    # migration 003 ohlc_bars columns exist
    s._one("SELECT symbol, interval, ts, open, high, low, close, volume, source, created_at FROM ohlc_bars")
    s._one("SELECT slippage, fees FROM fills")
    # migration 004 news_items columns exist
    s._one("SELECT id, symbol, title, source, url, published_at, content_summary, sentiment_score, impact_level, created_at FROM news_items")
    # migration 005 trader-intelligence columns exist
    s._one("SELECT id, name, source, market_focus, strategy_type, track_record_days, created_at FROM traders")
    s._one("SELECT trader_id, total_return, annualized_return, win_rate, max_drawdown, sharpe_ratio, sortino_ratio, average_holding_period, number_of_trades, updated_at FROM trader_performance")
    s._one("SELECT trader_id, symbol, direction, entry_price, position_size, timestamp FROM trader_positions")
    # migration 006 fundamentals columns exist
    s._one("SELECT symbol, company_name, sector, industry, exchange, country, updated_at FROM companies")
    s._one("SELECT symbol, period, revenue, revenue_growth, gross_margin, operating_margin, net_margin, eps, eps_growth, free_cash_flow, debt, cash, updated_at FROM financial_metrics")
    s._one("SELECT symbol, market_cap, pe_ratio, forward_pe, price_sales, enterprise_value, updated_at FROM valuation")
    s._one("SELECT symbol, rating, target_price, analyst_count, upgrade_count, downgrade_count, updated_at FROM analyst_estimates")
    # migration 007 options columns exist
    s._one("SELECT symbol, expiration_date, strike, option_type, timestamp, bid, ask, last, volume, open_interest, implied_volatility, source, created_at FROM options_snapshot")
    s._one("SELECT symbol, timestamp, call_volume, put_volume, call_put_ratio, implied_volatility, open_interest, unusual_activity_score, large_trade_count, premium_volume, sentiment, updated_at FROM options_flow")
    # migration 008 ai-consensus columns exist
    s._one("SELECT id, symbol, timestamp, overall_score, direction_bias, confidence, status, created_at FROM ai_assessments")
    s._one("SELECT assessment_id, component_name, score, weight, direction, reason, risk_flags FROM ai_assessment_components")
    # migration 009 ai-evaluation columns exist
    s._one("SELECT id, symbol, timestamp, score, direction, confidence, status, price_at_prediction, components_snapshot, created_at FROM ai_predictions")
    # migration 010 outcome-lifecycle columns exist
    s._one("SELECT prediction_id, time_horizon, price_at_prediction, future_price, return_percentage, direction_correct, evaluated_at, direction_expected, direction_actual, status FROM ai_prediction_outcomes")
    # migration 011 ai-governance columns exist
    s._one("SELECT id, prediction_id, symbol, status, score, confidence, data_completeness, reason_codes, created_at FROM ai_governance_results")
    # migration 012 data-completeness columns exist
    s._one("SELECT id, symbol, timestamp, overall_score, state, available_sources, missing_sources, created_at FROM data_completeness_snapshots")
    # migration 013 macro columns exist
    s._one("SELECT id, timestamp, fed_rate, treasury_10y, treasury_2y, cpi, core_cpi, unemployment, vix, dxy, oil, gold, source, created_at FROM macro_snapshots")
    # migration 014 institutional columns exist
    s._one("SELECT id, institution, symbol, previous_shares, current_shares, share_change, percentage_change, direction, filing_period, created_at FROM institutional_position_changes")
    s._one("SELECT id, symbol, insider_name, title, transaction_type, shares, price, transaction_date, created_at FROM insider_transactions")
    # migration 015 insider-cluster columns exist
    s._one("SELECT id, symbol, time_window, cluster_type, insider_count, weighted_score, total_shares, total_value, created_at FROM insider_clusters")
    s2 = _reopen(tmp_path, s)                     # re-open re-runs migrator → no-op
    assert sorted(r[0] for r in s2._all("SELECT version FROM schema_migrations")) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]


def test_postgres_ddl_declares_numeric_money():
    """Static (no live PG): every authoritative money field is NUMERIC(20,8) in the PG schema —
    never binary float. SQLite stores the same values as canonical decimal TEXT."""
    from atp.store.schema import _migration_002, _statements
    ddl = _statements("postgres") + _migration_002("postgres")
    money_columns = ["capital", "cash", "equity", "notional", "stop", "target", "monetary_risk",
                     "realized_pnl", "unrealized_pnl", "avg_price", "price", "commission",
                     "slippage", "fees", "quantity", "day_start_equity", "entry_price", "exit_price"]
    for col in money_columns:
        assert any(col in s and "NUMERIC(20,8)" in s for s in ddl), f"{col} is not NUMERIC in PG DDL"
    assert any("idempotency_key" in s and "UNIQUE" in s for s in ddl)   # idempotency constraint
    # no authoritative money column uses a binary-float type (precise token check, not substring)
    import re
    for s in ddl:
        assert not re.search(r"\b(FLOAT|DOUBLE\s+PRECISION|REAL)\b", s, re.IGNORECASE)


def test_money_is_exact_decimal(tmp_path):
    s = _db(tmp_path)
    s.upsert_risk_config(capital=D("1000000.00"), risk_per_trade_pct=D("0.01"),
                         max_daily_loss_pct=D("0.03"))
    cfg = s.get_risk_config()
    assert cfg.capital == Decimal("1000000.00000000")
    assert isinstance(cfg.capital, Decimal)
    # a value that is not exact in binary float survives exactly
    s.upsert_daily_pnl(trade_date="2026-08-14", day_start_equity=D("1000000"),
                       realized_pnl=D("-0.10"), unrealized_pnl=D("0"))
    assert s.get_daily_pnl("2026-08-14").realized_pnl == Decimal("-0.10000000")


# ------------------------------------------------------------------ KILL SWITCH durability
def test_kill_switch_survives_restart(tmp_path):
    s = _db(tmp_path)
    life = LifecycleManager(s)
    life.recover()
    life.mark_ready(); life.arm(); life.start(confirm=True)
    assert life.status is RuntimeStatus.RUNNING
    life.kill(actor="user", reason="panic")
    assert life.status is RuntimeStatus.KILLED

    s2 = _reopen(tmp_path, s)                     # crash + restart
    life2 = LifecycleManager(s2)
    assert life2.recover() is RuntimeStatus.KILLED      # NOT reset by restart
    assert s2.get_kill_switch().engaged is True
    # only explicit authenticated reset clears it
    with pytest.raises(LifecycleError):
        life2.arm()
    life2.reset_kill(actor="owner")
    assert life2.status is RuntimeStatus.DISABLED
    assert s2.get_kill_switch().engaged is False


# ------------------------------------------------------------------ DAILY LOSS durability
def test_daily_loss_survives_restart(tmp_path):
    s = _db(tmp_path)
    s.upsert_risk_config(capital=D("1000000"), risk_per_trade_pct=D("0.01"),
                         max_daily_loss_pct=D("0.03"))
    s.upsert_daily_pnl(trade_date="2026-08-14", day_start_equity=D("1000000"),
                       realized_pnl=D("-28000"), unrealized_pnl=D("0"))
    assert remaining_daily_budget(s, trade_date="2026-08-14") == Decimal("2000")

    s2 = _reopen(tmp_path, s)                     # crash + restart
    pnl = s2.get_daily_pnl("2026-08-14")
    assert pnl.realized_pnl == Decimal("-28000.00000000")           # NOT reset to 0
    assert remaining_daily_budget(s2, trade_date="2026-08-14") == Decimal("2000")   # NOT 30000

    # crossing the limit engages the durable lock; it survives another restart
    s2.upsert_daily_pnl(trade_date="2026-08-14", day_start_equity=D("1000000"),
                        realized_pnl=D("-30000"), unrealized_pnl=D("0"))
    assert enforce_daily_loss(s2, trade_date="2026-08-14") is True
    s3 = _reopen(tmp_path, s2)
    assert s3.get_daily_loss_lock("2026-08-14").engaged is True


# ------------------------------------------------------------------ RECOVERY LIFECYCLE
def test_unexpected_restart_from_running_requires_recovery(tmp_path):
    s = _db(tmp_path)
    life = LifecycleManager(s)
    life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    assert life.status is RuntimeStatus.RUNNING

    s2 = _reopen(tmp_path, s)                     # crash while RUNNING
    life2 = LifecycleManager(s2)
    assert life2.recover() is RuntimeStatus.RECOVERY_REQUIRED     # never auto-RUNNING
    assert life2.status is RuntimeStatus.RECOVERY_REQUIRED


def test_recovery_passes_to_ready_never_running(tmp_path):
    s = _db(tmp_path)
    life = LifecycleManager(s)
    life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    s2 = _reopen(tmp_path, s); life2 = LifecycleManager(s2); life2.recover()
    checks = {step: (lambda: True) for step in RECOVERY_STEPS}
    ok, results = life2.run_recovery(checks)
    assert ok is True
    assert life2.status is RuntimeStatus.READY_FOR_ARM           # NOT RUNNING
    assert all(passed for _, passed in results)


def test_recovery_failure_stays_blocked(tmp_path):
    s = _db(tmp_path)
    life = LifecycleManager(s)
    life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    s2 = _reopen(tmp_path, s); life2 = LifecycleManager(s2); life2.recover()
    checks = {step: (lambda: True) for step in RECOVERY_STEPS}
    checks["reconcile"] = lambda: False          # a reconciliation mismatch
    ok, results = life2.run_recovery(checks)
    assert ok is False
    assert life2.status is RuntimeStatus.RECOVERY_REQUIRED       # stays blocked
    assert any(a == "RECOVERY_FAIL" for a in [e.action for e in s2.recent_audit()])


def test_clean_disabled_restart_stays_disabled(tmp_path):
    s = _db(tmp_path)
    LifecycleManager(s).recover()                # first boot → DISABLED
    s2 = _reopen(tmp_path, s)
    assert LifecycleManager(s2).recover() is RuntimeStatus.DISABLED


def test_ready_for_arm_restart_is_preserved(tmp_path):
    s = _db(tmp_path)
    life = LifecycleManager(s); life.recover(); life.mark_ready()
    s2 = _reopen(tmp_path, s)
    assert LifecycleManager(s2).recover() is RuntimeStatus.READY_FOR_ARM


# ------------------------------------------------------------------ ARM/START guards
def test_start_requires_confirmation_and_arm(tmp_path):
    s = _db(tmp_path); life = LifecycleManager(s); life.recover()
    with pytest.raises(LifecycleError):
        life.start(confirm=True)                 # not ARMED
    life.mark_ready(); life.arm()
    with pytest.raises(LifecycleError):
        life.start(confirm=False)                # wrong confirmation
    assert life.start(confirm="YES, START PAPER TRADING") is RuntimeStatus.RUNNING


def test_cannot_start_from_recovery_required(tmp_path):
    s = _db(tmp_path); life = LifecycleManager(s)
    life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True)
    s2 = _reopen(tmp_path, s); life2 = LifecycleManager(s2); life2.recover()
    assert life2.status is RuntimeStatus.RECOVERY_REQUIRED
    with pytest.raises(LifecycleError):
        life2.arm()                              # cannot arm from RECOVERY_REQUIRED
    with pytest.raises(LifecycleError):
        life2.start(confirm=True)


# ------------------------------------------------------------------ IDEMPOTENT ORDERS
class _IdempotentPaperBroker:
    """Deterministic paper fill, idempotent by client_order_id (broker-level dedup)."""
    def __init__(self):
        self.calls = 0
        self._filled: dict[str, dict] = {}

    def fill(self, coid):
        if coid in self._filled:
            return self._filled[coid]            # already filled — no second execution
        self.calls += 1
        ack = {"broker_order_id": "bk_" + coid, "price": "100.00", "commission": "1.00",
               "filled_qty": "10"}
        self._filled[coid] = ack
        return ack


def test_order_is_not_submitted_twice(tmp_path):
    s = _db(tmp_path)
    om = OrderManager(s)
    broker = _IdempotentPaperBroker()
    kw = dict(idempotency_key="idem-1", instrument="AAPL", side="BUY", quantity=D("10"),
              correlation_id="c1", authorize=lambda: (True, "ok"), fill=broker.fill)
    o1 = om.place(**kw)
    o2 = om.place(**kw)                          # simulated retry / restart with same key
    assert o1.client_order_id == o2.client_order_id
    assert o2.state == "FILLED"
    assert broker.calls == 1                     # submitted exactly once
    assert len(s.list_fills("AAPL")) == 1        # exactly one fill persisted
    pos = s.get_position("AAPL")
    assert pos.quantity == Decimal("10")


def test_resume_after_crash_between_authorize_and_fill(tmp_path):
    s = _db(tmp_path)
    om = OrderManager(s)
    broker = _IdempotentPaperBroker()
    # place with an authorize that we accept, then simulate a crash by re-opening BEFORE fill:
    # emulate the AUTHORIZED-but-not-filled state directly, then resume.
    om._store.insert_order_intent(client_order_id="co_x", idempotency_key="idem-2",
                                  instrument="NVDA", side="BUY", quantity=D("5"),
                                  order_type="MARKET", correlation_id="c2")
    s.update_order_state(client_order_id="co_x", state="AUTHORIZED", reason="risk approved")
    s2 = _reopen(tmp_path, s)
    om2 = OrderManager(s2)
    o = om2.place(idempotency_key="idem-2", instrument="NVDA", side="BUY", quantity=D("5"),
                  correlation_id="c2", authorize=lambda: (True, "ok"), fill=broker.fill)
    assert o.state == "FILLED"
    assert broker.calls == 1                     # resumed and filled exactly once
    assert len(s2.list_fills("NVDA")) == 1


def test_risk_veto_rejects_order(tmp_path):
    s = _db(tmp_path)
    om = OrderManager(s)
    o = om.place(idempotency_key="idem-3", instrument="SPY", side="BUY", quantity=D("10"),
                 correlation_id="c3", authorize=lambda: (False, "monetary risk > 1%"),
                 fill=lambda coid: pytest.fail("must not submit a vetoed order"))
    assert o.state == "REJECTED"
    assert s.get_position("SPY") is None


# ------------------------------------------------------------------ TRANSACTIONAL ATOMICITY
def test_fill_and_position_are_atomic(tmp_path):
    s = _db(tmp_path)
    # a deliberate error inside the transaction must roll BOTH the fill and position back
    fill = FillRow("fl1", "coX", "AAPL", "BUY", D("10"), D("100"), D("1"), utcnow_iso())
    bad_pos = type("BadPos", (), {"instrument": "AAPL", "quantity": D("10"),
                                  "avg_price": D("100"), "realized_pnl": object()})()  # non-numeric
    with pytest.raises(Exception):
        s.apply_fill(fill=fill, position=bad_pos)
    assert s.list_fills("AAPL") == []            # fill rolled back
    assert s.get_position("AAPL") is None        # position rolled back


# ------------------------------------------------------------------ FAIL CLOSED
def _running(s):
    life = LifecycleManager(s); life.recover(); life.mark_ready(); life.arm()
    life.start(confirm=True); return life


def test_gate_blocks_when_not_running(tmp_path):
    s = _db(tmp_path); life = LifecycleManager(s); life.recover()
    assert TradingGate(s, life).can_trade().allowed is False


def test_gate_blocks_without_risk_state(tmp_path):
    s = _db(tmp_path); life = _running(s)
    r = TradingGate(s, life).can_trade()
    assert r.allowed is False and "risk state" in r.reason


def test_gate_allows_when_all_healthy(tmp_path):
    s = _db(tmp_path); life = _running(s)
    s.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1000000"),
                        halted=False, killed=False)
    assert TradingGate(s, life).can_trade().allowed is True


def test_gate_blocks_when_db_down(tmp_path):
    s = _db(tmp_path); life = _running(s)
    s.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1000000"),
                        halted=False, killed=False)
    gate = TradingGate(s, life)
    assert gate.can_trade().allowed is True
    s.close()                                    # database now unavailable
    r = gate.can_trade()
    assert r.allowed is False and "database" in r.reason


def test_gate_blocks_on_daily_loss_and_kill(tmp_path):
    s = _db(tmp_path); life = _running(s)
    s.upsert_risk_state(day_start_equity=D("1000000"), peak_equity=D("1000000"),
                        halted=False, killed=False)
    d = "2026-08-14"
    s.set_daily_loss_lock(trade_date=d, engaged=True, reason="limit")
    assert TradingGate(s, life).can_trade(trade_date=d).allowed is False
    life.kill(actor="user", reason="panic")
    assert TradingGate(s, life).can_trade(trade_date=d).allowed is False


# ------------------------------------------------------------------ POSITIONS + RECONCILE
def test_position_reconstruction_and_reconcile(tmp_path):
    s = _db(tmp_path)
    om = OrderManager(s); broker = _IdempotentPaperBroker()
    om.place(idempotency_key="k1", instrument="AAPL", side="BUY", quantity=D("10"),
             correlation_id="c", authorize=lambda: (True, "ok"), fill=broker.fill)
    rebuilt = reconstruct_positions(s)
    assert rebuilt["AAPL"].quantity == Decimal("10")
    # DB says 10, broker says 9 → mismatch → not ok
    res = reconcile({"AAPL": D("10")}, {"AAPL": D("9")})
    assert res.ok is False and res.diffs[0][0] == "AAPL"
    assert reconcile({"AAPL": D("10")}, {"AAPL": D("10")}).ok is True


def test_audit_trail_has_correlation_ids(tmp_path):
    s = _db(tmp_path); life = LifecycleManager(s)
    life.recover(); life.mark_ready(); life.arm(); life.start(confirm=True); life.kill()
    actions = [e.action for e in s.recent_audit(50)]
    for a in ("ARM", "START", "KILL"):
        assert a in actions
    assert all(e.correlation_id for e in s.recent_audit(50))
