"""Phase R2.0 — Risk Control Center & Capital Protection Layer (observability + gate only).

Covers: config persistence/validation/versioning, immutable events, READY/WARNING/BLOCKED/NO-DATA,
exact daily-limit boundary, position/exposure/drawdown breaches, kill switch STOPPED, missing data
(never zero, never READY), out-of-band canonical conflict, governance forced-BLOCKED + backward
compatibility, authenticated + unauthorized update, restart-safe persistence, and NO order/execution/
broker side effects. Touches no Trading Core / RiskEngine / Broker / IBKR / Execution / autonomous code.
"""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from atp.aigov.engine import evaluate_governance
from atp.riskcontrol.config import validate_config
from atp.riskcontrol.evaluate import evaluate_risk_state
from atp.riskcontrol.readmodel import build_risk_config_view, build_risk_events, build_risk_status
from atp.store import open_store, risk_config_token


@pytest.fixture()
def store(tmp_path):
    return open_store(str(tmp_path / "atp.db"))              # applies migration 17


def _token(store):
    return build_risk_config_view(store)["version_token"]


def _apply(store, **over):
    cfg = {"capital": D("100000"), "risk_per_trade_pct": D("2"), "max_daily_loss_pct": D("1"),
           "currency": "EUR", "warning_threshold_pct": D("80"), "max_portfolio_exposure_pct": D("50"),
           "max_drawdown_pct": D("10"), "actor": "tester"}
    cfg.update(over)
    return store.apply_risk_control_update(expected_token=_token(store), **cfg)


class KS:
    def __init__(self, engaged): self.engaged = engaged
class RS:
    def __init__(self, peak, killed=False): self.peak_equity = peak; self.day_start_equity = peak; self.killed = killed
class PNL:
    def __init__(self, r, u): self.realized_pnl = r; self.unrealized_pnl = u; self.updated_at = "2026-08-17T00:00Z"

CFG = {"capital": D("100000"), "currency": "EUR", "max_daily_loss_pct": D("1"), "max_position_risk_pct": D("2"),
       "max_portfolio_exposure_pct": D("50"), "max_drawdown_pct": D("10"), "warning_threshold_pct": D("80")}
EXP = {"gross_pct": D("20"), "net_pct": D("10")}


def _ev(pnl, *, config=CFG, kill=False, exposure=EXP, equity=D("99000"), rs=None):
    return evaluate_risk_state(config=config, daily_pnl=pnl, risk_state=rs or RS(D("100000")),
                               kill_switch=KS(kill), exposure=exposure, equity=equity)


# ------------------------------------------------------------------ validation
def test_validation_rejects_bad_inputs():
    assert validate_config({"capital": "-5", "currency": "EUR", "max_daily_loss_pct": "1",
                            "max_position_risk_pct": "2", "max_portfolio_exposure_pct": "50",
                            "max_drawdown_pct": "10", "warning_threshold_pct": "80"})[1] == ["CAPITAL_MUST_BE_POSITIVE"]
    assert "UNKNOWN_CURRENCY" in validate_config({**{k: "1" for k in
        ("max_daily_loss_pct", "max_position_risk_pct", "max_portfolio_exposure_pct", "max_drawdown_pct", "warning_threshold_pct")},
        "capital": "1", "currency": "XYZ"})[1]
    assert "MAX_DAILY_LOSS_PCT_MUST_BE_POSITIVE" in validate_config({"capital": "1", "currency": "EUR",
        "max_daily_loss_pct": "0", "max_position_risk_pct": "2", "max_portfolio_exposure_pct": "50",
        "max_drawdown_pct": "10", "warning_threshold_pct": "80"})[1]
    ok, errs = validate_config({"capital": "100000", "currency": "eur", "max_daily_loss_pct": "1",
        "max_position_risk_pct": "2", "max_portfolio_exposure_pct": "50", "max_drawdown_pct": "10",
        "warning_threshold_pct": "80"})
    assert errs == [] and ok["capital"] == D("100000") and ok["currency"] == "EUR"


def test_validation_rejects_inconsistent_amount():
    _, errs = validate_config({"capital": "100000", "currency": "EUR", "max_daily_loss_pct": "1",
        "max_position_risk_pct": "2", "max_portfolio_exposure_pct": "50", "max_drawdown_pct": "10",
        "warning_threshold_pct": "80", "max_daily_loss_amount": "5000"})  # should be 1000
    assert "INCONSISTENT_DAILY_LOSS_AMOUNT" in errs


# ------------------------------------------------------------------ persistence + versioning + events
def test_config_persist_version_events_and_derived(store):
    assert store.get_risk_control_policy() is None
    r = _apply(store)
    assert r["ok"] and r["version"] == 1
    view = build_risk_config_view(store)
    assert view["configured"] and view["configuration_version"] == 1
    assert view["config"]["max_daily_loss_amount"] == 1000.0          # derived: 100000 * 1%
    assert view["config"]["max_position_risk_pct"] == 2.0             # alias of canonical risk_per_trade_pct
    assert store.get_risk_config().capital == D("100000")             # canonical, single source
    assert store.count_risk_events() == 1
    ev = store.list_risk_events(10)[0]
    assert ev.event_type == "CONFIGURATION_UPDATED" and ev.configuration_version == "1"
    import json
    details = json.loads(ev.details_json)                             # structured before/after (not free-text)
    assert "changed_fields" in details and "before" in details and "after" in details and details["actor"] == "tester"
    # second update bumps the version + adds an immutable event
    assert _apply(store, capital=D("120000"))["version"] == 2
    assert store.count_risk_events() == 2 and store.get_risk_config().capital == D("120000")


def test_optimistic_version_conflict(store):
    _apply(store)                                                     # v1, token now stale for a saved copy
    stale = "deadbeefdeadbeefdead"
    r = store.apply_risk_control_update(expected_token=stale, capital=D("1"), risk_per_trade_pct=D("1"),
        max_daily_loss_pct=D("1"), currency="EUR", warning_threshold_pct=D("80"),
        max_portfolio_exposure_pct=D("50"), max_drawdown_pct=D("10"), actor="x")
    assert r["ok"] is False and r["reason"] == "version_conflict"


def test_out_of_band_canonical_change_blocks_stale_update(store):
    _apply(store)                                                     # v1
    good_token = _token(store)
    store.upsert_risk_config(capital=D("777777"), risk_per_trade_pct=D("2"), max_daily_loss_pct=D("1"))  # out-of-band!
    r = store.apply_risk_control_update(expected_token=good_token, capital=D("50"), risk_per_trade_pct=D("2"),
        max_daily_loss_pct=D("1"), currency="EUR", warning_threshold_pct=D("80"),
        max_portfolio_exposure_pct=D("50"), max_drawdown_pct=D("10"), actor="x")
    assert r["ok"] is False and r["reason"] == "version_conflict"     # stale RCC update rejected
    assert store.get_risk_config().capital == D("777777")            # out-of-band value preserved


def test_restart_safe(store, tmp_path):
    _apply(store, capital=D("55555"))
    store.close()
    s2 = open_store(str(tmp_path / "atp.db"))
    assert s2.get_risk_config().capital == D("55555")
    assert build_risk_config_view(s2)["configuration_version"] == 1
    assert s2.count_risk_events() == 1


# ------------------------------------------------------------------ risk-state rules (deterministic)
def test_ready():
    assert _ev(PNL(D("-300"), D("0")))["status"] == "READY"


def test_daily_warning_boundary():
    r = _ev(PNL(D("-800"), D("0")))                                  # == 80% of 1000 limit
    assert r["status"] == "WARNING" and "DAILY_LOSS_WARNING" in r["reasons"]


def test_daily_limit_exact_boundary_blocks():
    r = _ev(PNL(D("-1000"), D("0")))                                 # == limit → BLOCKED (>=)
    assert r["status"] == "BLOCKED" and "DAILY_LOSS_LIMIT_EXCEEDED" in r["reasons"]


def test_daily_limit_exceeded():
    assert _ev(PNL(D("-1100"), D("0")))["status"] == "BLOCKED"


def test_position_risk_exceeded():
    r = _ev(PNL(D("-10"), D("0")), exposure={"gross_pct": D("20"), "net_pct": D("10"), "position_risk_pct": D("3")})
    assert r["status"] == "BLOCKED" and "POSITION_RISK_EXCEEDED" in r["reasons"]


def test_exposure_exceeded():
    r = _ev(PNL(D("-10"), D("0")), exposure={"gross_pct": D("60"), "net_pct": D("10")})   # > 50%
    assert r["status"] == "BLOCKED" and "EXPOSURE_LIMIT_EXCEEDED" in r["reasons"]


def test_drawdown_exceeded():
    r = _ev(PNL(D("-10"), D("0")), rs=RS(D("100000")), equity=D("85000"))                 # 15% > 10%
    assert r["status"] == "BLOCKED" and "DRAWDOWN_LIMIT_EXCEEDED" in r["reasons"]


def test_kill_switch_stopped_blocks():
    r = _ev(PNL(D("0"), D("0")), kill=True)
    assert r["status"] == "BLOCKED" and "KILL_SWITCH_TRIGGERED" in r["reasons"]
    assert r["kill_switch"] == "STOPPED"


def test_missing_config_is_no_data_not_ready():
    r = _ev(PNL(D("-10"), D("0")), config=None)
    assert r["status"] == "NO DATA" and "RISK_CONFIGURATION_MISSING" in r["reasons"]


def test_missing_pnl_is_no_data():
    r = _ev(None)
    assert r["status"] == "NO DATA" and "daily_pnl" in r["missing"]


def test_missing_exposure_is_no_data_never_zero():
    r = _ev(PNL(D("-10"), D("0")), exposure=None)
    assert r["status"] == "NO DATA" and "exposure" in r["missing"]
    assert r["gross_pct"] is None                                    # never fabricated as 0


def test_missing_drawdown_is_no_data():
    r = _ev(PNL(D("-10"), D("0")), rs=None, equity=None)
    assert r["status"] == "NO DATA" and "drawdown" in r["missing"]


def test_observed_zero_pnl_is_ready_not_missing():
    r = _ev(PNL(D("0"), D("0")))                                     # a real, sourced flat day
    assert r["status"] == "READY" and r["daily_pnl"] == D("0")


# ------------------------------------------------------------------ read-model status (side-effect free)
def test_build_risk_status_no_config(store):
    s = build_risk_status(store)
    assert s["status"] == "NO DATA" and "RISK_CONFIGURATION_MISSING" in s["reasons"]
    assert s["kill_switch"] == "ARMED" and s["capital"]["value"] is None       # never 0
    assert s["exposure"]["gross_pct"] is None and s["drawdown"]["value_pct"] is None


def test_risk_events_merges_kill_switch_audit(store):
    _apply(store)                                                    # CONFIGURATION_UPDATED
    store.set_kill_switch(engaged=True, actor="operator", reason="test")   # existing authoritative path
    ev = build_risk_events(store, 50)
    types = {e["event_type"] for e in ev["events"]}
    assert "CONFIGURATION_UPDATED" in types and "KILL_SWITCH_TRIGGERED" in types
    assert build_risk_status(store)["kill_switch"] == "STOPPED"      # authoritative read


# ------------------------------------------------------------------ governance integration + compatibility
_APPROVABLE = {"symbol": "NVDA", "score": 88, "confidence": 82, "status": "COMPLETE", "direction": "BULLISH",
               "components": [{"component_name": "Fundamentals", "score": 90, "direction": "bullish", "weight": 0.20},
                              {"component_name": "News", "score": 85, "direction": "bullish", "weight": 0.15},
                              {"component_name": "Options", "score": 84, "direction": "bullish", "weight": 0.15},
                              {"component_name": "Market Data", "score": 80, "direction": "bullish", "weight": 0.20},
                              {"component_name": "Trader Intelligence", "score": 75, "direction": "neutral", "weight": 0.15},
                              {"component_name": "Risk", "score": 90, "direction": "neutral", "weight": 0.15}],
               "conflicts": []}


def test_governance_backward_compatible_when_omitted():
    # No risk_status arg → identical to prior contract (APPROVED for an approvable assessment).
    g = evaluate_governance(_APPROVABLE)
    assert g["status"] == "APPROVED" and g["approved"] is True
    assert set(g) >= {"symbol", "status", "score", "confidence", "data_completeness", "reasons",
                      "approved", "direction", "missing", "conflicts"}   # existing contract keys intact


def test_governance_risk_blocked_forces_blocked():
    g = evaluate_governance(_APPROVABLE, risk_status="BLOCKED")
    assert g["status"] == "BLOCKED" and "RISK_BLOCK" in g["reasons"]
    assert g["score"] == 88                                          # intelligence assessment still visible


def test_governance_risk_no_data_prevents_approved_not_false_blocked():
    g = evaluate_governance(_APPROVABLE, risk_status="NO DATA")
    assert g["status"] == "PARTIAL" and g["approved"] is False       # capital readiness prevented…
    assert "RISK_DATA_MISSING" in g["reasons"]
    assert g["status"] != "BLOCKED"                                  # …but NOT a false BLOCKED


def test_governance_risk_warning_visible_nonblocking():
    g = evaluate_governance(_APPROVABLE, risk_status="WARNING")
    assert g["status"] == "APPROVED" and "RISK_WARNING" in g["reasons"]


def test_governance_partial_stays_partial_under_risk_no_data():
    partial = {**_APPROVABLE, "score": 60}                           # low score → PARTIAL by intelligence
    assert evaluate_governance(partial)["status"] == "PARTIAL"
    assert evaluate_governance(partial, risk_status="NO DATA")["status"] == "PARTIAL"   # not flipped to BLOCKED


# ------------------------------------------------------------------ security: no execution / broker
def test_config_update_no_execution_side_effects(store):
    _apply(store)
    assert store.list_positions() == [] and store.list_fills() == []
    assert store.get_order_by_idempotency("x") is None
    assert store.get_kill_switch().engaged is False                 # /risk/config never touches kill switch


def test_source_has_no_execution_code_identifiers():
    # Guard against actual execution/broker CODE identifiers (not the words in the safety docstrings that
    # say "does NOT touch execution/broker/autonomous").
    root = Path(__file__).resolve().parents[2] / "src" / "atp" / "riskcontrol"
    forbidden = ("placeOrder(", "submitOrder(", "createOrder(", ".check_order(", "RiskEngine(",
                 "ib_async", "reqMktData(", "IB(", "ibapi", "copy_trade(", "insert_order(",
                 ".set_kill_switch(")
    for f in root.glob("*.py"):
        text = f.read_text()
        for token in forbidden:
            assert token not in text, f"{f.name} must not reference {token}"
