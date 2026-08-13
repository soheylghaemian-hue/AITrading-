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
    applied = s._all("SELECT version FROM schema_migrations")
    assert [r[0] for r in applied] == [1]
    # tables exist
    for t in ("runtime_state", "orders", "fills", "positions", "kill_switch", "daily_pnl",
              "audit_events", "service_heartbeats", "market_data_health"):
        s._one(f"SELECT COUNT(*) FROM {t}")
    s2 = _reopen(tmp_path, s)                     # re-open re-runs migrator → no-op
    assert [r[0] for r in s2._all("SELECT version FROM schema_migrations")] == [1]


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
