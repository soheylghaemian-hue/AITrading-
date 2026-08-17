"""Durable source-of-truth Store (§ Phase B).

A single transactional persistence surface for all safety-critical trading state. Two backends share
one SQL implementation (`SqlStore`): SQLite (local/test, file-backed) and PostgreSQL (production).
PostgreSQL is authoritative in production; Redis is NEVER authoritative for trading state.

Design rules honored here:
  * money is exact Decimal (stored NUMERIC in PG, canonical TEXT in SQLite) — never binary float;
  * timestamps are ISO-8601 UTC text everywhere;
  * every control transition writes runtime_state AND an audit_event inside ONE transaction;
  * a fill and its position update commit inside ONE transaction (crash-atomic);
  * if the database is unavailable, callers must fail closed (see runtime.gate).
"""

from __future__ import annotations

import abc
import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .money import D, money_str, opt_money_str, to_decimal


def risk_config_token(*, capital, risk_per_trade_pct, max_daily_loss_pct, rc_updated_at, config_version,
                      currency, warning_threshold_pct, max_portfolio_exposure_pct, max_drawdown_pct) -> str:
    """§ R2.0 optimistic-concurrency token over the FULL combined canonical config view (canonical
    risk_config + risk_control_policy). Deterministic. ANY change to ANY field — including an out-of-band
    risk_config write (which bumps rc_updated_at) — changes the token, so a stale Risk Control Center
    update is rejected. Read-only; no trading."""
    def n(v):
        return "∅" if v is None else money_str(v) if isinstance(v, Decimal) else str(v)
    payload = {"capital": n(capital), "rpt": n(risk_per_trade_pct), "mdl": n(max_daily_loss_pct),
               "rc_updated_at": n(rc_updated_at), "version": n(config_version), "currency": n(currency),
               "warn": n(warning_threshold_pct), "expo": n(max_portfolio_exposure_pct), "dd": n(max_drawdown_pct)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- rows
@dataclass(slots=True)
class RuntimeStateRow:
    status: str
    updated_at: str
    correlation_id: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class KillSwitchRow:
    engaged: bool
    actor: str | None
    reason: str | None
    updated_at: str | None


@dataclass(slots=True)
class DailyPnlRow:
    trade_date: str
    day_start_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    updated_at: str


@dataclass(slots=True)
class DailyLossLockRow:
    trade_date: str
    engaged: bool
    reason: str | None
    updated_at: str | None


@dataclass(slots=True)
class RiskConfigRow:
    capital: Decimal
    risk_per_trade_pct: Decimal
    max_daily_loss_pct: Decimal
    updated_at: str


@dataclass(slots=True)
class RiskStateRow:
    day_start_equity: Decimal
    peak_equity: Decimal
    halted: bool
    killed: bool
    updated_at: str


@dataclass(slots=True)
class RiskControlPolicyRow:
    # § R2.0 — ONLY the Risk-Control fields not already in the canonical risk_config. References the
    # canonical singleton via risk_config_id. Never stores capital / risk_per_trade_pct /
    # max_daily_loss_pct (those stay canonical) nor the kill switch (that stays in KillSwitchRow).
    id: str
    risk_config_id: int
    currency: str | None
    warning_threshold_pct: Decimal | None
    max_portfolio_exposure_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    config_version: int
    updated_at: str | None
    updated_by: str | None


@dataclass(slots=True)
class RiskEventRow:
    id: str
    timestamp: str | None
    event_type: str
    severity: str | None
    description: str | None
    reason_code: str | None
    observed_value: str | None
    configured_limit: str | None
    configuration_version: str | None
    details_json: str | None
    created_at: str


@dataclass(slots=True)
class OrderRow:
    client_order_id: str
    idempotency_key: str
    instrument: str
    side: str
    quantity: Decimal
    order_type: str
    state: str                       # INTENT/AUTHORIZED/REJECTED/SUBMITTED/FILLED/CANCELLED
    broker_order_id: str | None
    correlation_id: str | None
    reason: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class FillRow:
    fill_id: str
    client_order_id: str
    instrument: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    ts: str


@dataclass(slots=True)
class PositionRow:
    instrument: str
    quantity: Decimal
    avg_price: Decimal
    realized_pnl: Decimal
    updated_at: str


@dataclass(slots=True)
class AuditEventRow:
    event_id: str
    ts: str
    actor: str
    action: str
    previous_state: str | None
    new_state: str | None
    reason: str | None
    correlation_id: str | None


@dataclass(slots=True)
class DecisionRow:
    decision_id: str
    ts: str
    instrument: str
    final_decision: str | None
    payload: str | None
    correlation_id: str | None


@dataclass(slots=True)
class NewsItemRow:
    id: str
    symbol: str
    title: str
    source: str | None
    url: str | None
    published_at: str
    content_summary: str | None
    sentiment_score: float | None
    impact_level: str | None
    created_at: str


@dataclass(slots=True)
class TraderRow:
    id: str
    name: str
    source: str
    market_focus: str | None
    strategy_type: str | None
    track_record_days: int | None
    created_at: str


@dataclass(slots=True)
class TraderPerformanceRow:
    trader_id: str
    total_return: float | None
    annualized_return: float | None
    win_rate: float | None
    max_drawdown: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    average_holding_period: float | None
    number_of_trades: int | None
    updated_at: str


@dataclass(slots=True)
class TraderPositionRow:
    trader_id: str
    symbol: str
    direction: str             # LONG / SHORT / NEUTRAL
    entry_price: float | None
    position_size: float | None
    timestamp: str


@dataclass(slots=True)
class CompanyRow:
    symbol: str
    company_name: str | None
    sector: str | None
    industry: str | None
    exchange: str | None
    country: str | None
    updated_at: str


@dataclass(slots=True)
class FinancialMetricsRow:
    symbol: str
    period: str | None
    revenue: float | None
    revenue_growth: float | None
    gross_margin: float | None
    operating_margin: float | None
    net_margin: float | None
    eps: float | None
    eps_growth: float | None
    free_cash_flow: float | None
    debt: float | None
    cash: float | None
    updated_at: str


@dataclass(slots=True)
class ValuationRow:
    symbol: str
    market_cap: float | None
    pe_ratio: float | None
    forward_pe: float | None
    price_sales: float | None
    enterprise_value: float | None
    updated_at: str


@dataclass(slots=True)
class AnalystEstimatesRow:
    symbol: str
    rating: str | None
    target_price: float | None
    analyst_count: int | None
    upgrade_count: int | None
    downgrade_count: int | None
    updated_at: str


@dataclass(slots=True)
class OptionsSnapshotRow:
    symbol: str
    expiration_date: str
    strike: float | None
    option_type: str
    timestamp: str | None
    bid: float | None
    ask: float | None
    last: float | None
    volume: int | None
    open_interest: int | None
    implied_volatility: float | None
    source: str | None
    created_at: str


@dataclass(slots=True)
class OptionsFlowRow:
    symbol: str
    timestamp: str | None
    call_volume: int | None
    put_volume: int | None
    call_put_ratio: float | None
    implied_volatility: float | None
    open_interest: int | None
    unusual_activity_score: float | None
    large_trade_count: int | None
    premium_volume: float | None
    sentiment: str | None
    updated_at: str


@dataclass(slots=True)
class AiAssessmentRow:
    id: str
    symbol: str
    timestamp: str | None
    overall_score: float | None
    direction_bias: str | None
    confidence: float | None
    status: str | None
    created_at: str


@dataclass(slots=True)
class AiAssessmentComponentRow:
    assessment_id: str
    component_name: str
    score: float | None
    weight: float | None
    direction: str | None
    reason: str | None
    risk_flags: str | None


@dataclass(slots=True)
class AiPredictionRow:
    id: str
    symbol: str
    timestamp: str | None
    score: float | None
    direction: str | None
    confidence: float | None
    status: str | None
    price_at_prediction: float | None
    components_snapshot: str | None
    created_at: str


@dataclass(slots=True)
class AiPredictionOutcomeRow:
    prediction_id: str
    time_horizon: int
    price_at_prediction: float | None
    future_price: float | None
    return_percentage: float | None
    direction_correct: bool | None
    evaluated_at: str
    direction_expected: str | None = None
    direction_actual: str | None = None
    status: str | None = None


@dataclass(slots=True)
class AiGovernanceResultRow:
    id: str
    prediction_id: str
    symbol: str
    status: str
    score: float | None
    confidence: float | None
    data_completeness: float | None
    reason_codes: str | None
    created_at: str


@dataclass(slots=True)
class DataCompletenessRow:
    id: str
    symbol: str
    timestamp: str | None
    overall_score: float | None
    state: str | None
    available_sources: str | None
    missing_sources: str | None
    created_at: str


@dataclass(slots=True)
class MacroSnapshotRow:
    id: str
    timestamp: str | None
    fed_rate: float | None
    treasury_10y: float | None
    treasury_2y: float | None
    cpi: float | None
    core_cpi: float | None
    unemployment: float | None
    vix: float | None
    dxy: float | None
    oil: float | None
    gold: float | None
    source: str | None
    created_at: str


@dataclass(slots=True)
class InstitutionalChangeRow:
    id: str
    institution: str
    symbol: str
    previous_shares: float | None
    current_shares: float | None
    share_change: float | None
    percentage_change: float | None
    direction: str | None
    filing_period: str | None
    created_at: str


@dataclass(slots=True)
class InsiderTransactionRow:
    id: str
    symbol: str
    insider_name: str | None
    title: str | None
    transaction_type: str | None
    shares: float | None
    price: float | None
    transaction_date: str | None
    created_at: str


@dataclass(slots=True)
class InsiderClusterRow:
    id: str
    symbol: str
    time_window: str
    cluster_type: str | None
    insider_count: float | None
    weighted_score: float | None
    total_shares: float | None
    total_value: float | None
    created_at: str


@dataclass(slots=True)
class OhlcBarRow:
    symbol: str
    interval: str
    ts: str                    # ISO-8601 UTC bar-open, interval-aligned
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    created_at: str


# --------------------------------------------------------------------------- interface
class Store(abc.ABC):
    """Abstract durable store. Concrete backends: SqliteStore, PostgresStore."""

    @abc.abstractmethod
    def ping(self) -> bool: ...
    @abc.abstractmethod
    def close(self) -> None: ...


# --------------------------------------------------------------------------- § R3.0 backtest rows
@dataclass(slots=True)
class BacktestRunRow:
    run_id: str
    owner: str
    strategy_id: str
    strategy_version: int
    strategy_config_json: str
    strategy_checksum: str
    engine_version: str
    symbol_universe_json: str
    interval: str
    start_ts: str
    end_ts: str
    asset_class: str
    timestamp_policy_id: str
    timestamp_policy_version: int
    exchange_calendar_id: str
    exchange_calendar_version: str
    exchange_tz: str
    session_calendar: str
    data_source: str
    config_snapshot_json: str
    risk_config_snapshot_json: str
    status: str
    failure_code: str | None
    failure_reason: str | None
    warnings_json: str | None
    missing_data_json: str | None
    commit_ref: str | None
    result_checksum: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    updated_at: str
    # § R3.0A — nullable dataset pin (NULL for legacy/fixture runs)
    dataset_id: str | None = None
    dataset_provider: str | None = None
    dataset_provider_contract_version: str | None = None
    dataset_adjustment_policy: str | None = None
    dataset_normalization_policy: str | None = None
    dataset_calendar_version: str | None = None
    dataset_checksum: str | None = None


@dataclass(slots=True)
class BacktestDecisionRow:
    id: str
    run_id: str
    seq: int
    ts: str
    symbol: str
    strategy_id: str
    strategy_version: int
    action: str
    confidence: str | None
    evidence_json: str | None
    missing_inputs_json: str | None
    reason: str | None
    decision_checksum: str
    created_at: str


@dataclass(slots=True)
class BacktestTradeRow:
    id: str
    run_id: str
    symbol: str
    side: str
    entry_decision_id: str | None
    exit_decision_id: str | None
    entry_ts: str
    entry_fill_ts: str
    entry_price: str
    initial_stop_price: str | None
    exit_ts: str | None
    exit_fill_ts: str | None
    exit_price: str | None
    quantity: str
    gross_pnl: str | None
    commission: str
    slippage: str
    net_pnl: str | None
    return_pct: str | None
    bars_held: int | None
    exit_reason: str | None
    ambiguous: bool
    created_at: str
    expected_risk_per_share: str | None = None
    actual_risk_per_share: str | None = None


@dataclass(slots=True)
class BacktestEquityPointRow:
    run_id: str
    seq: int
    ts: str
    cash: str
    equity: str
    realized_pnl: str
    unrealized_pnl: str
    daily_pnl: str | None
    gross_exposure_pct: str | None
    net_exposure_pct: str | None
    drawdown_pct: str | None


@dataclass(slots=True)
class BacktestEventRow:
    id: str
    run_id: str
    seq: int | None
    ts: str | None
    event_type: str
    severity: str | None
    symbol: str | None
    details_json: str | None
    created_at: str


@dataclass(slots=True)
class BacktestMetricsRow:
    run_id: str
    metrics_json: str
    computed_at: str


@dataclass(slots=True)
class ResearchDatasetRow:
    dataset_id: str
    owner: str
    request_checksum: str
    supersedes_dataset_id: str | None
    retry_of_dataset_id: str | None
    symbol_universe_json: str
    interval: str
    provider: str
    provider_contract_version: str
    adjustment_policy: str
    normalization_policy: str
    calendar_version: str
    range_start: str
    range_end: str
    status: str
    row_count: int | None
    missing_minute_threshold: str | None
    raw_pages_checksum: str | None
    dataset_checksum: str | None
    provider_adjusted_flag: bool | None
    warnings_json: str | None
    missing_data_json: str | None
    failure_code: str | None
    failure_reason: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    updated_at: str


@dataclass(slots=True)
class ResearchDatasetEventRow:
    id: str
    dataset_id: str
    seq: int | None
    ts: str | None
    event_type: str
    severity: str | None
    symbol: str | None
    details_json: str | None
    created_at: str


# --------------------------------------------------------------------------- shared SQL impl
class SqlStore(Store):
    """DB-agnostic SQL implementation. Subclasses supply a DB-API connection, the parameter
    placeholder, and money adaptation. All writes go through short explicit transactions."""

    PLACEHOLDER = "?"          # SQLite; PostgresStore overrides with "%s"
    MONEY_AS_TEXT = True       # SQLite stores money as canonical TEXT; PG stores NUMERIC
    LOCK_CLAUSE = ""           # SQLite serializes writers; PostgresStore uses " FOR UPDATE"

    def __init__(self, conn):
        self._conn = conn

    # -- low level ---------------------------------------------------------
    def _q(self, sql: str) -> str:
        return sql if self.PLACEHOLDER == "?" else sql.replace("?", self.PLACEHOLDER)

    def _m(self, value):
        """Adapt a Decimal for storage."""
        if value is None:
            return None
        return money_str(value) if self.MONEY_AS_TEXT else Decimal(str(value))

    @contextmanager
    def tx(self):
        """One explicit transaction. Commit on success, rollback on any error."""
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def _exec(self, cur, sql, params=()):
        cur.execute(self._q(sql), params)

    def _one(self, sql, params=()):
        cur = self._conn.cursor()
        try:
            cur.execute(self._q(sql), params)
            return cur.fetchone()
        finally:
            cur.close()

    def _all(self, sql, params=()):
        cur = self._conn.cursor()
        try:
            cur.execute(self._q(sql), params)
            return cur.fetchall()
        finally:
            cur.close()

    def ping(self) -> bool:
        try:
            self._one("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # -- runtime state + transitions (atomic with audit) -------------------
    def get_runtime_state(self) -> RuntimeStateRow | None:
        r = self._one("SELECT status, updated_at, correlation_id, reason FROM runtime_state WHERE id=1")
        return RuntimeStateRow(r[0], r[1], r[2], r[3]) if r else None

    def transition(self, *, new_status: str, actor: str, reason: str | None,
                   correlation_id: str | None = None, previous: str | None = None,
                   action: str | None = None) -> AuditEventRow:
        """Persist runtime_state AND an audit_event in ONE transaction."""
        cid = correlation_id or new_id()
        now = utcnow_iso()
        prev = previous if previous is not None else (self.get_runtime_state().status
                                                      if self.get_runtime_state() else None)
        evt = AuditEventRow(new_id(), now, actor, action or f"TRANSITION:{new_status}",
                            prev, new_status, reason, cid)
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO runtime_state (id,status,updated_at,correlation_id,reason) VALUES (1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, "
                "correlation_id=excluded.correlation_id, reason=excluded.reason",
                (new_status, now, cid, reason))
            self._insert_audit(cur, evt)
        return evt

    # -- audit -------------------------------------------------------------
    def _insert_audit(self, cur, evt: AuditEventRow):
        self._exec(cur,
            "INSERT INTO audit_events (event_id,ts,actor,action,previous_state,new_state,reason,correlation_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (evt.event_id, evt.ts, evt.actor, evt.action, evt.previous_state,
             evt.new_state, evt.reason, evt.correlation_id))

    def audit(self, *, actor: str, action: str, reason: str | None = None,
              previous_state: str | None = None, new_state: str | None = None,
              correlation_id: str | None = None) -> AuditEventRow:
        evt = AuditEventRow(new_id(), utcnow_iso(), actor, action, previous_state,
                            new_state, reason, correlation_id or new_id())
        with self.tx() as cur:
            self._insert_audit(cur, evt)
        return evt

    def recent_audit(self, limit: int = 50) -> list[AuditEventRow]:
        rows = self._all("SELECT event_id,ts,actor,action,previous_state,new_state,reason,correlation_id "
                         "FROM audit_events ORDER BY ts DESC LIMIT ?", (limit,))
        return [AuditEventRow(*r) for r in rows]

    # -- kill switch (durable latch) --------------------------------------
    def get_kill_switch(self) -> KillSwitchRow:
        r = self._one("SELECT engaged, actor, reason, updated_at FROM kill_switch WHERE id=1")
        if not r:
            return KillSwitchRow(False, None, None, None)
        return KillSwitchRow(bool(r[0]), r[1], r[2], r[3])

    def set_kill_switch(self, *, engaged: bool, actor: str, reason: str | None,
                        correlation_id: str | None = None) -> AuditEventRow:
        cid = correlation_id or new_id()
        now = utcnow_iso()
        evt = AuditEventRow(new_id(), now, actor, "KILL" if engaged else "RESET",
                            None, "KILLED" if engaged else "RESET", reason, cid)
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO kill_switch (id,engaged,actor,reason,updated_at) VALUES (1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET engaged=excluded.engaged, actor=excluded.actor, "
                "reason=excluded.reason, updated_at=excluded.updated_at",
                (1 if engaged else 0, actor, reason, now))
            self._insert_audit(cur, evt)
        return evt

    # -- daily P&L + loss lock (durable) ----------------------------------
    def get_daily_pnl(self, trade_date: str) -> DailyPnlRow | None:
        r = self._one("SELECT trade_date, day_start_equity, realized_pnl, unrealized_pnl, updated_at "
                      "FROM daily_pnl WHERE trade_date=?", (trade_date,))
        if not r:
            return None
        return DailyPnlRow(r[0], to_decimal(r[1]), to_decimal(r[2]), to_decimal(r[3]), r[4])

    def upsert_daily_pnl(self, *, trade_date: str, day_start_equity, realized_pnl, unrealized_pnl) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO daily_pnl (trade_date,day_start_equity,realized_pnl,unrealized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                "day_start_equity=excluded.day_start_equity, realized_pnl=excluded.realized_pnl, "
                "unrealized_pnl=excluded.unrealized_pnl, updated_at=excluded.updated_at",
                (trade_date, self._m(day_start_equity), self._m(realized_pnl),
                 self._m(unrealized_pnl), now))

    def try_reserve_daily_risk(self, *, trade_date: str, amount, limit) -> bool:
        """Concurrency-safe risk-budget reservation: lock today's daily_pnl row, and only if the
        requested `amount` still fits the remaining budget (`limit` − loss-so-far) reserve it by
        booking it as realized loss. Two racing authorizations cannot jointly exceed the budget:
        the second waits on the row lock and then sees the reduced remaining. Fails closed if there
        is no budget context."""
        with self.tx() as cur:
            self._exec(cur, "SELECT realized_pnl, unrealized_pnl FROM daily_pnl WHERE trade_date=?"
                       + self.LOCK_CLAUSE, (trade_date,))
            row = cur.fetchone()
            if row is None:
                return False
            realized, unreal = to_decimal(row[0]), to_decimal(row[1])
            loss_so_far = max(D(0), -(realized + unreal))
            remaining = D(limit) - loss_so_far
            if D(amount) > remaining:
                return False
            self._exec(cur, "UPDATE daily_pnl SET realized_pnl=?, updated_at=? WHERE trade_date=?",
                       (self._m(realized - D(amount)), utcnow_iso(), trade_date))
            return True

    def get_daily_loss_lock(self, trade_date: str) -> DailyLossLockRow:
        r = self._one("SELECT trade_date, engaged, reason, updated_at FROM daily_loss_lock WHERE trade_date=?",
                      (trade_date,))
        if not r:
            return DailyLossLockRow(trade_date, False, None, None)
        return DailyLossLockRow(r[0], bool(r[1]), r[2], r[3])

    def set_daily_loss_lock(self, *, trade_date: str, engaged: bool, reason: str | None,
                            actor: str = "risk", correlation_id: str | None = None) -> AuditEventRow:
        now = utcnow_iso()
        evt = AuditEventRow(new_id(), now, actor, "DAILY_LOSS_LOCK" if engaged else "DAILY_LOSS_UNLOCK",
                            None, "HALTED" if engaged else None, reason, correlation_id or new_id())
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO daily_loss_lock (trade_date,engaged,reason,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(trade_date) DO UPDATE SET engaged=excluded.engaged, reason=excluded.reason, "
                "updated_at=excluded.updated_at",
                (trade_date, 1 if engaged else 0, reason, now))
            self._insert_audit(cur, evt)
        return evt

    # -- risk config + state ----------------------------------------------
    def get_risk_config(self) -> RiskConfigRow | None:
        r = self._one("SELECT capital, risk_per_trade_pct, max_daily_loss_pct, updated_at "
                      "FROM risk_config WHERE id=1")
        return RiskConfigRow(to_decimal(r[0]), to_decimal(r[1]), to_decimal(r[2]), r[3]) if r else None

    def upsert_risk_config(self, *, capital, risk_per_trade_pct, max_daily_loss_pct) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO risk_config (id,capital,risk_per_trade_pct,max_daily_loss_pct,updated_at) "
                "VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET capital=excluded.capital, "
                "risk_per_trade_pct=excluded.risk_per_trade_pct, "
                "max_daily_loss_pct=excluded.max_daily_loss_pct, updated_at=excluded.updated_at",
                (self._m(capital), self._m(risk_per_trade_pct), self._m(max_daily_loss_pct), now))

    def get_risk_state(self) -> RiskStateRow | None:
        r = self._one("SELECT day_start_equity, peak_equity, halted, killed, updated_at "
                      "FROM risk_state WHERE id=1")
        return RiskStateRow(to_decimal(r[0]), to_decimal(r[1]), bool(r[2]), bool(r[3]), r[4]) if r else None

    def upsert_risk_state(self, *, day_start_equity, peak_equity, halted: bool, killed: bool) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO risk_state (id,day_start_equity,peak_equity,halted,killed,updated_at) "
                "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET day_start_equity=excluded.day_start_equity, "
                "peak_equity=excluded.peak_equity, halted=excluded.halted, killed=excluded.killed, "
                "updated_at=excluded.updated_at",
                (self._m(day_start_equity), self._m(peak_equity), 1 if halted else 0,
                 1 if killed else 0, now))

    # -- Risk Control Center (§ R2.0): policy companion + immutable events. Canonical capital /
    #    risk_per_trade_pct / max_daily_loss_pct stay in risk_config; kill switch stays in kill_switch.
    _RCP_COLS = ("id,risk_config_id,currency,warning_threshold_pct,max_portfolio_exposure_pct,"
                 "max_drawdown_pct,config_version,updated_at,updated_by")
    _RISK_EVENT_COLS = ("id,timestamp,event_type,severity,description,reason_code,observed_value,"
                        "configured_limit,configuration_version,details_json,created_at")

    def get_risk_control_policy(self) -> RiskControlPolicyRow | None:
        r = self._one(f"SELECT {self._RCP_COLS} FROM risk_control_policy WHERE id='policy'")
        if not r:
            return None
        g = lambda v: None if v is None else to_decimal(v)  # noqa: E731
        return RiskControlPolicyRow(r[0], int(r[1]), r[2], g(r[3]), g(r[4]), g(r[5]), int(r[6]), r[7], r[8])

    def insert_risk_event(self, *, id: str, timestamp, event_type: str, severity: str | None = None,
                          description: str | None = None, reason_code: str | None = None,
                          observed_value: str | None = None, configured_limit: str | None = None,
                          configuration_version: str | None = None, details_json: str | None = None) -> None:
        """Append an immutable risk event. ON CONFLICT DO NOTHING → never rewritten (never fabricated)."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                f"INSERT INTO risk_events ({self._RISK_EVENT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (id, timestamp, event_type, severity, description, reason_code, observed_value,
                 configured_limit, configuration_version, details_json, now))

    def list_risk_events(self, limit: int = 100) -> list[RiskEventRow]:
        n = max(1, min(1000, int(limit)))
        rows = self._all(f"SELECT {self._RISK_EVENT_COLS} FROM risk_events ORDER BY created_at DESC LIMIT ?", (n,))
        return [RiskEventRow(*r) for r in rows]

    def count_risk_events(self) -> int:
        r = self._one("SELECT COUNT(*) FROM risk_events")
        return int(r[0]) if r else 0

    def apply_risk_control_update(self, *, expected_token: str, capital, risk_per_trade_pct,
                                  max_daily_loss_pct, currency, warning_threshold_pct,
                                  max_portfolio_exposure_pct, max_drawdown_pct, actor: str,
                                  event_id: str | None = None) -> dict:
        """§ R2.0 atomic, version-checked Risk-Control config update. In ONE transaction it (1) locks BOTH
        singleton rows and verifies the optimistic token (rejecting a stale update, incl. an out-of-band
        risk_config change), (2) writes the canonical risk_config + the risk_control_policy, and (3)
        appends an immutable CONFIGURATION_UPDATED risk_event with structured before/after details_json.
        If the event write fails the whole tx rolls back. It calls NO order/execution/broker code and
        NEVER mutates the kill switch. Returns {ok, reason, version, current_token, details_json}."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "SELECT capital,risk_per_trade_pct,max_daily_loss_pct,updated_at FROM risk_config "
                       "WHERE id=1" + self.LOCK_CLAUSE)
            rc = cur.fetchone()
            self._exec(cur, f"SELECT {self._RCP_COLS} FROM risk_control_policy WHERE id='policy'" + self.LOCK_CLAUSE)
            pol = cur.fetchone()
            d = lambda v: None if v is None else to_decimal(v)  # noqa: E731
            cur_token = risk_config_token(
                capital=d(rc[0]) if rc else None, risk_per_trade_pct=d(rc[1]) if rc else None,
                max_daily_loss_pct=d(rc[2]) if rc else None, rc_updated_at=(rc[3] if rc else None),
                config_version=(int(pol[6]) if pol else 0), currency=(pol[2] if pol else None),
                warning_threshold_pct=(d(pol[3]) if pol else None),
                max_portfolio_exposure_pct=(d(pol[4]) if pol else None),
                max_drawdown_pct=(d(pol[5]) if pol else None))
            if expected_token != cur_token:
                return {"ok": False, "reason": "version_conflict", "current_token": cur_token}
            new_version = (int(pol[6]) if pol else 0) + 1
            ms = lambda v: None if v is None else money_str(v if isinstance(v, Decimal) else D(str(v)))  # noqa: E731
            before = {"capital": ms(d(rc[0])) if rc else None,
                      "risk_per_trade_pct": ms(d(rc[1])) if rc else None,
                      "max_daily_loss_pct": ms(d(rc[2])) if rc else None,
                      "currency": (pol[2] if pol else None),
                      "warning_threshold_pct": ms(d(pol[3])) if pol else None,
                      "max_portfolio_exposure_pct": ms(d(pol[4])) if pol else None,
                      "max_drawdown_pct": ms(d(pol[5])) if pol else None,
                      "config_version": (int(pol[6]) if pol else 0)}
            self._exec(cur,
                "INSERT INTO risk_config (id,capital,risk_per_trade_pct,max_daily_loss_pct,updated_at) "
                "VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET capital=excluded.capital, "
                "risk_per_trade_pct=excluded.risk_per_trade_pct, max_daily_loss_pct=excluded.max_daily_loss_pct, "
                "updated_at=excluded.updated_at",
                (self._m(capital), self._m(risk_per_trade_pct), self._m(max_daily_loss_pct), now))
            self._exec(cur,
                "INSERT INTO risk_control_policy (id,risk_config_id,currency,warning_threshold_pct,"
                "max_portfolio_exposure_pct,max_drawdown_pct,config_version,updated_at,updated_by) "
                "VALUES ('policy',1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET currency=excluded.currency, "
                "warning_threshold_pct=excluded.warning_threshold_pct, "
                "max_portfolio_exposure_pct=excluded.max_portfolio_exposure_pct, "
                "max_drawdown_pct=excluded.max_drawdown_pct, config_version=excluded.config_version, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (currency, self._m(warning_threshold_pct), self._m(max_portfolio_exposure_pct),
                 self._m(max_drawdown_pct), new_version, now, actor))
            after = {"capital": ms(capital), "risk_per_trade_pct": ms(risk_per_trade_pct),
                     "max_daily_loss_pct": ms(max_daily_loss_pct), "currency": currency,
                     "warning_threshold_pct": ms(warning_threshold_pct),
                     "max_portfolio_exposure_pct": ms(max_portfolio_exposure_pct),
                     "max_drawdown_pct": ms(max_drawdown_pct), "config_version": new_version}
            changed = [k for k in after if str(before.get(k)) != str(after.get(k))]
            details = json.dumps({"changed_fields": changed, "before": before, "after": after,
                                  "actor": actor, "configuration_version": new_version, "timestamp": now},
                                 sort_keys=True)
            self._exec(cur,
                f"INSERT INTO risk_events ({self._RISK_EVENT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event_id or new_id(), now, "CONFIGURATION_UPDATED", "INFO",
                 f"Risk configuration updated to v{new_version} by {actor}", "CONFIGURATION_UPDATED",
                 None, None, str(new_version), details, now))
            return {"ok": True, "version": new_version, "details_json": details}

    # -- orders (idempotent lifecycle) ------------------------------------
    def get_order_by_idempotency(self, key: str) -> OrderRow | None:
        r = self._one("SELECT client_order_id,idempotency_key,instrument,side,quantity,order_type,state,"
                      "broker_order_id,correlation_id,reason,created_at,updated_at FROM orders "
                      "WHERE idempotency_key=?", (key,))
        return self._order_row(r) if r else None

    def get_order(self, client_order_id: str) -> OrderRow | None:
        r = self._one("SELECT client_order_id,idempotency_key,instrument,side,quantity,order_type,state,"
                      "broker_order_id,correlation_id,reason,created_at,updated_at FROM orders "
                      "WHERE client_order_id=?", (client_order_id,))
        return self._order_row(r) if r else None

    def _order_row(self, r) -> OrderRow:
        return OrderRow(r[0], r[1], r[2], r[3], to_decimal(r[4]), r[5], r[6], r[7], r[8], r[9], r[10], r[11])

    def insert_order_intent(self, *, client_order_id: str, idempotency_key: str, instrument: str,
                            side: str, quantity, order_type: str, correlation_id: str,
                            reason: str | None = None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO orders (client_order_id,idempotency_key,instrument,side,quantity,order_type,"
                "state,broker_order_id,correlation_id,reason,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_order_id, idempotency_key, instrument, side, self._m(quantity), order_type,
                 "INTENT", None, correlation_id, reason, now, now))

    def update_order_state(self, *, client_order_id: str, state: str,
                           broker_order_id: str | None = None, reason: str | None = None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            if broker_order_id is not None:
                self._exec(cur, "UPDATE orders SET state=?, broker_order_id=?, reason=?, updated_at=? "
                                "WHERE client_order_id=?",
                           (state, broker_order_id, reason, now, client_order_id))
            else:
                self._exec(cur, "UPDATE orders SET state=?, reason=?, updated_at=? WHERE client_order_id=?",
                           (state, reason, now, client_order_id))

    # -- fill + position update in ONE transaction (crash-atomic) ---------
    def apply_fill(self, *, fill: FillRow, position: PositionRow, order_state: str = "FILLED",
                   broker_order_id: str | None = None) -> None:
        """Insert the fill, upsert the resulting position, and advance the order (with its broker
        acknowledgement) — all in ONE transaction. A crash cannot leave a fill recorded without its
        position update, nor mark an order filled without persisting the fill."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO fills (fill_id,client_order_id,instrument,side,quantity,price,commission,ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fill.fill_id, fill.client_order_id, fill.instrument, fill.side,
                 self._m(fill.quantity), self._m(fill.price), self._m(fill.commission), fill.ts))
            self._exec(cur,
                "INSERT INTO positions (instrument,quantity,avg_price,realized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(instrument) DO UPDATE SET quantity=excluded.quantity, "
                "avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at",
                (position.instrument, self._m(position.quantity), self._m(position.avg_price),
                 self._m(position.realized_pnl), now))
            if broker_order_id is not None:
                self._exec(cur, "UPDATE orders SET state=?, broker_order_id=?, updated_at=? "
                                "WHERE client_order_id=?",
                           (order_state, broker_order_id, now, fill.client_order_id))
            else:
                self._exec(cur, "UPDATE orders SET state=?, updated_at=? WHERE client_order_id=?",
                           (order_state, now, fill.client_order_id))

    def apply_fill_atomic(self, *, fill: FillRow, compute, order_state: str = "FILLED",
                          broker_order_id: str | None = None) -> PositionRow:
        """Concurrency-safe fill application. Inside ONE transaction: ensure the position row exists
        (a zero row is inserted if absent — semantically identical to "no position"), lock it
        (SELECT … FOR UPDATE on PostgreSQL; SQLite serializes writers), recompute the position from
        the locked value via `compute(current_row_or_None) -> PositionRow`, then persist fill +
        position + order together. Two concurrent fills on the same instrument cannot interleave —
        including the FIRST fill on a brand-new instrument, since FOR UPDATE cannot lock a row that
        does not yet exist."""
        now = utcnow_iso()
        with self.tx() as cur:
            # Guarantee a lockable row BEFORE FOR UPDATE. Without this, two concurrent first fills on a
            # new instrument both see "no row", both compute from zero, and one update is lost. A zero
            # row folds identically to None in compute() (qty/avg/realized = 0), so this is safe.
            self._exec(cur,
                "INSERT INTO positions (instrument,quantity,avg_price,realized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(instrument) DO NOTHING",
                (fill.instrument, self._m(D(0)), self._m(D(0)), self._m(D(0)), now))
            self._exec(cur,
                "SELECT instrument,quantity,avg_price,realized_pnl,updated_at FROM positions "
                "WHERE instrument=?" + self.LOCK_CLAUSE, (fill.instrument,))
            row = cur.fetchone()
            current = (PositionRow(row[0], to_decimal(row[1]), to_decimal(row[2]),
                                   to_decimal(row[3]), row[4]) if row else None)
            new_pos: PositionRow = compute(current)
            self._exec(cur,
                "INSERT INTO fills (fill_id,client_order_id,instrument,side,quantity,price,commission,ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fill.fill_id, fill.client_order_id, fill.instrument, fill.side,
                 self._m(fill.quantity), self._m(fill.price), self._m(fill.commission), fill.ts))
            self._exec(cur,
                "INSERT INTO positions (instrument,quantity,avg_price,realized_pnl,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(instrument) DO UPDATE SET quantity=excluded.quantity, "
                "avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at",
                (new_pos.instrument, self._m(new_pos.quantity), self._m(new_pos.avg_price),
                 self._m(new_pos.realized_pnl), now))
            if broker_order_id is not None:
                self._exec(cur, "UPDATE orders SET state=?, broker_order_id=?, updated_at=? "
                                "WHERE client_order_id=?",
                           (order_state, broker_order_id, now, fill.client_order_id))
            else:
                self._exec(cur, "UPDATE orders SET state=?, updated_at=? WHERE client_order_id=?",
                           (order_state, now, fill.client_order_id))
        return new_pos

    def get_position(self, instrument: str) -> PositionRow | None:
        r = self._one("SELECT instrument,quantity,avg_price,realized_pnl,updated_at FROM positions "
                      "WHERE instrument=?", (instrument,))
        return PositionRow(r[0], to_decimal(r[1]), to_decimal(r[2]), to_decimal(r[3]), r[4]) if r else None

    def list_positions(self) -> list[PositionRow]:
        rows = self._all("SELECT instrument,quantity,avg_price,realized_pnl,updated_at FROM positions")
        return [PositionRow(r[0], to_decimal(r[1]), to_decimal(r[2]), to_decimal(r[3]), r[4]) for r in rows]

    def list_fills(self, instrument: str | None = None) -> list[FillRow]:
        if instrument:
            rows = self._all("SELECT fill_id,client_order_id,instrument,side,quantity,price,commission,ts "
                             "FROM fills WHERE instrument=? ORDER BY ts", (instrument,))
        else:
            rows = self._all("SELECT fill_id,client_order_id,instrument,side,quantity,price,commission,ts "
                             "FROM fills ORDER BY ts")
        return [FillRow(r[0], r[1], r[2], r[3], to_decimal(r[4]), to_decimal(r[5]), to_decimal(r[6]), r[7])
                for r in rows]

    # -- OHLC bars (§ Phase G1) -------------------------------------------
    def upsert_ohlc_bar(self, *, symbol: str, interval: str, ts: str, open: float, high: float,
                        low: float, close: float, volume: float, source: str) -> None:
        """Insert or update the (forming) bar for (symbol, interval, ts). Idempotent: re-writing the same
        bucket updates high/low/close/volume; a duplicate never creates a second row."""
        now = utcnow_iso()
        def m(v):  # float -> exact decimal in the shared money encoding
            return self._m(D(str(v)))
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ohlc_bars (symbol,interval,ts,open,high,low,close,volume,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,interval,ts) DO UPDATE SET open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, volume=excluded.volume, source=excluded.source",
                (symbol, interval, ts, m(open), m(high), m(low), m(close), m(volume), source, now))

    def insert_ohlc_bar(self, *, symbol: str, interval: str, ts: str, open: float, high: float,
                        low: float, close: float, volume: float, source: str) -> None:
        """Strict insert — RAISES on a duplicate (symbol, interval, ts) via the PK/unique constraint."""
        now = utcnow_iso()
        def m(v):
            return self._m(D(str(v)))
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ohlc_bars (symbol,interval,ts,open,high,low,close,volume,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, interval, ts, m(open), m(high), m(low), m(close), m(volume), source, now))

    def list_ohlc_bars(self, symbol: str, interval: str, limit: int = 500) -> list[OhlcBarRow]:
        """Most-recent `limit` bars for (symbol, interval), returned oldest→newest (chart order)."""
        n = max(1, min(5000, int(limit)))
        rows = self._all(
            "SELECT symbol,interval,ts,open,high,low,close,volume,source,created_at FROM ohlc_bars "
            "WHERE symbol=? AND interval=? ORDER BY ts DESC LIMIT ?", (symbol, interval, n))
        out = [OhlcBarRow(r[0], r[1], r[2], to_decimal(r[3]), to_decimal(r[4]), to_decimal(r[5]),
                          to_decimal(r[6]), to_decimal(r[7]), r[8], r[9]) for r in rows]
        out.reverse()
        return out

    def count_ohlc_bars(self, symbol: str, interval: str) -> int:
        r = self._one("SELECT COUNT(*) FROM ohlc_bars WHERE symbol=? AND interval=?", (symbol, interval))
        return int(r[0]) if r else 0

    def latest_ohlc_bars(self) -> list[OhlcBarRow]:
        """The most-recent bar for every (symbol, interval) — used to resume forming bars after a
        service restart, so an in-progress bar is never reset/corrupted by a restart."""
        rows = self._all(
            "SELECT o.symbol,o.interval,o.ts,o.open,o.high,o.low,o.close,o.volume,o.source,o.created_at "
            "FROM ohlc_bars o JOIN (SELECT symbol,interval,MAX(ts) AS mx FROM ohlc_bars "
            "GROUP BY symbol,interval) g ON o.symbol=g.symbol AND o.interval=g.interval AND o.ts=g.mx")
        return [OhlcBarRow(r[0], r[1], r[2], to_decimal(r[3]), to_decimal(r[4]), to_decimal(r[5]),
                           to_decimal(r[6]), to_decimal(r[7]), r[8], r[9]) for r in rows]

    # -- decisions / heartbeats / market-data health ----------------------
    def insert_decision(self, *, decision_id: str, ts: str, instrument: str, payload_json: str,
                        final_decision: str | None, correlation_id: str | None = None) -> None:
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO decisions (decision_id,ts,instrument,final_decision,payload,correlation_id) "
                "VALUES (?,?,?,?,?,?)",
                (decision_id, ts, instrument, final_decision, payload_json, correlation_id))

    # -- news items (§ Phase G2.1, read-only intelligence) ----------------
    def upsert_news_item(self, *, id: str, symbol: str, title: str, source: str | None, url: str | None,
                         published_at: str, content_summary: str | None, sentiment_score: float | None,
                         impact_level: str | None) -> None:
        """Insert or update a news item (idempotent on the deterministic `id`). Stores ONLY article
        fields + the derived sentiment/impact — never a provider key or secret."""
        now = utcnow_iso()
        ss = None if sentiment_score is None else str(float(sentiment_score))
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO news_items (id,symbol,title,source,url,published_at,content_summary,"
                "sentiment_score,impact_level,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, source=excluded.source, "
                "url=excluded.url, published_at=excluded.published_at, "
                "content_summary=excluded.content_summary, sentiment_score=excluded.sentiment_score, "
                "impact_level=excluded.impact_level",
                (id, symbol, title, source, url, published_at, content_summary, ss, impact_level, now))

    def list_news(self, symbol: str, limit: int = 50) -> list[NewsItemRow]:
        """Most-recent `limit` news items for a symbol (newest first). Empty when none collected."""
        n = max(1, min(200, int(limit)))
        rows = self._all(
            "SELECT id,symbol,title,source,url,published_at,content_summary,sentiment_score,"
            "impact_level,created_at FROM news_items WHERE symbol=? ORDER BY published_at DESC LIMIT ?",
            (symbol, n))
        return [NewsItemRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                            (float(r[7]) if r[7] is not None else None), r[8], r[9]) for r in rows]

    def count_news(self, symbol: str | None = None) -> int:
        if symbol:
            r = self._one("SELECT COUNT(*) FROM news_items WHERE symbol=?", (symbol,))
        else:
            r = self._one("SELECT COUNT(*) FROM news_items")
        return int(r[0]) if r else 0

    # -- AI evaluation & performance (§ Phase G3.1, IMMUTABLE history) -----
    def insert_ai_prediction(self, *, id: str, symbol: str, timestamp, score, direction, confidence,
                             status, price_at_prediction, components_snapshot: str | None) -> None:
        """Append an immutable prediction snapshot. ON CONFLICT DO NOTHING → a prediction is NEVER
        rewritten (history is honest; old scores never change)."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ai_predictions (id,symbol,timestamp,score,direction,confidence,status,"
                "price_at_prediction,components_snapshot,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (id, symbol.upper(), timestamp, f(score), direction, f(confidence), status,
                 f(price_at_prediction), components_snapshot, now))

    def insert_ai_prediction_outcome(self, *, prediction_id: str, time_horizon: int, price_at_prediction,
                                     future_price, return_percentage, direction_correct: bool | None,
                                     direction_expected: str | None = None, direction_actual: str | None = None,
                                     status: str | None = "EVALUATED") -> None:
        """Record a measured outcome once. ON CONFLICT DO NOTHING → outcomes are never overwritten or
        removed (failed predictions stay on the record)."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        dc = None if direction_correct is None else (1 if direction_correct else 0)
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ai_prediction_outcomes (prediction_id,time_horizon,price_at_prediction,"
                "future_price,return_percentage,direction_correct,evaluated_at,direction_expected,"
                "direction_actual,status) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(prediction_id,time_horizon) DO NOTHING",
                (prediction_id, int(time_horizon), f(price_at_prediction), f(future_price),
                 f(return_percentage), dc, now, direction_expected, direction_actual, status))

    def get_ai_prediction(self, prediction_id: str) -> AiPredictionRow | None:
        r = self._one("SELECT id,symbol,timestamp,score,direction,confidence,status,price_at_prediction,"
                      "components_snapshot,created_at FROM ai_predictions WHERE id=?", (prediction_id,))
        return self._pred_row(r) if r else None

    def _pred_row(self, r) -> AiPredictionRow:
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return AiPredictionRow(r[0], r[1], r[2], g(r[3]), r[4], g(r[5]), r[6], g(r[7]), r[8], r[9])

    def list_ai_predictions(self, symbol: str | None = None, limit: int = 500) -> list[AiPredictionRow]:
        n = max(1, min(5000, int(limit)))
        if symbol:
            rows = self._all("SELECT id,symbol,timestamp,score,direction,confidence,status,price_at_prediction,"
                             "components_snapshot,created_at FROM ai_predictions WHERE symbol=? "
                             "ORDER BY timestamp DESC LIMIT ?", (symbol.upper(), n))
        else:
            rows = self._all("SELECT id,symbol,timestamp,score,direction,confidence,status,price_at_prediction,"
                             "components_snapshot,created_at FROM ai_predictions ORDER BY timestamp DESC LIMIT ?", (n,))
        return [self._pred_row(r) for r in rows]

    def list_ai_prediction_outcomes(self, prediction_id: str | None = None) -> list[AiPredictionOutcomeRow]:
        cols = ("SELECT prediction_id,time_horizon,price_at_prediction,future_price,return_percentage,"
                "direction_correct,evaluated_at,direction_expected,direction_actual,status "
                "FROM ai_prediction_outcomes")
        rows = (self._all(cols + " WHERE prediction_id=?", (prediction_id,)) if prediction_id
                else self._all(cols))
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return [AiPredictionOutcomeRow(x[0], int(x[1]), g(x[2]), g(x[3]), g(x[4]),
                                       (None if x[5] is None else bool(x[5])), x[6], x[7], x[8], x[9]) for x in rows]

    def count_ai_prediction_outcomes(self) -> int:
        r = self._one("SELECT COUNT(*) FROM ai_prediction_outcomes")
        return int(r[0]) if r else 0

    def count_ai_predictions(self) -> int:
        r = self._one("SELECT COUNT(*) FROM ai_predictions")
        return int(r[0]) if r else 0

    # -- AI governance results (§ Phase G3.3, read-only IMMUTABLE decision verdicts) ------
    _GOV_COLS = ("id,prediction_id,symbol,status,score,confidence,data_completeness,reason_codes,created_at")

    def insert_governance_result(self, *, id: str, prediction_id: str, symbol: str, status: str,
                                 score, confidence, data_completeness, reason_codes: str | None) -> None:
        """Record a governance verdict once. ON CONFLICT DO NOTHING → a governance decision is NEVER
        rewritten (old verdicts stay exactly as decided)."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ai_governance_results (id,prediction_id,symbol,status,score,confidence,"
                "data_completeness,reason_codes,created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (id, prediction_id, symbol.upper(), status, f(score), f(confidence),
                 f(data_completeness), reason_codes, now))

    def _gov_row(self, r) -> AiGovernanceResultRow:
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return AiGovernanceResultRow(r[0], r[1], r[2], r[3], g(r[4]), g(r[5]), g(r[6]), r[7], r[8])

    def get_governance_result(self, prediction_id: str) -> AiGovernanceResultRow | None:
        r = self._one(f"SELECT {self._GOV_COLS} FROM ai_governance_results WHERE id=?", (prediction_id,))
        return self._gov_row(r) if r else None

    def list_governance_results(self, symbol: str | None = None, limit: int = 200) -> list[AiGovernanceResultRow]:
        n = max(1, min(2000, int(limit)))
        if symbol:
            rows = self._all(f"SELECT {self._GOV_COLS} FROM ai_governance_results WHERE symbol=? "
                             "ORDER BY created_at DESC LIMIT ?", (symbol.upper(), n))
        else:
            rows = self._all(f"SELECT {self._GOV_COLS} FROM ai_governance_results "
                             "ORDER BY created_at DESC LIMIT ?", (n,))
        return [self._gov_row(r) for r in rows]

    def count_governance_results(self) -> int:
        r = self._one("SELECT COUNT(*) FROM ai_governance_results")
        return int(r[0]) if r else 0

    # -- Data completeness snapshots (§ Phase C1, read-only IMMUTABLE reliability history) ------
    _DC_COLS = "id,symbol,timestamp,overall_score,state,available_sources,missing_sources,created_at"

    def insert_data_completeness(self, *, id: str, symbol: str, timestamp, overall_score, state: str | None,
                                 available_sources: str | None, missing_sources: str | None) -> None:
        """Record a completeness snapshot once. ON CONFLICT DO NOTHING → snapshots are never rewritten."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO data_completeness_snapshots (id,symbol,timestamp,overall_score,state,"
                "available_sources,missing_sources,created_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (id, symbol.upper(), timestamp, f(overall_score), state, available_sources,
                 missing_sources, now))

    def _dc_row(self, r) -> DataCompletenessRow:
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return DataCompletenessRow(r[0], r[1], r[2], g(r[3]), r[4], r[5], r[6], r[7])

    def get_data_completeness(self, snapshot_id: str) -> DataCompletenessRow | None:
        r = self._one(f"SELECT {self._DC_COLS} FROM data_completeness_snapshots WHERE id=?", (snapshot_id,))
        return self._dc_row(r) if r else None

    def latest_data_completeness(self, symbol: str) -> DataCompletenessRow | None:
        r = self._one(f"SELECT {self._DC_COLS} FROM data_completeness_snapshots WHERE symbol=? "
                      "ORDER BY timestamp DESC LIMIT 1", (symbol.upper(),))
        return self._dc_row(r) if r else None

    def list_data_completeness(self, symbol: str | None = None, limit: int = 200) -> list[DataCompletenessRow]:
        n = max(1, min(2000, int(limit)))
        if symbol:
            rows = self._all(f"SELECT {self._DC_COLS} FROM data_completeness_snapshots WHERE symbol=? "
                             "ORDER BY timestamp DESC LIMIT ?", (symbol.upper(), n))
        else:
            rows = self._all(f"SELECT {self._DC_COLS} FROM data_completeness_snapshots "
                             "ORDER BY timestamp DESC LIMIT ?", (n,))
        return [self._dc_row(r) for r in rows]

    def count_data_completeness(self) -> int:
        r = self._one("SELECT COUNT(*) FROM data_completeness_snapshots")
        return int(r[0]) if r else 0

    # -- Macro snapshots (§ Phase R1.2, read-only IMMUTABLE macro-environment history) ------
    _MACRO_COLS = ("id,timestamp,fed_rate,treasury_10y,treasury_2y,cpi,core_cpi,unemployment,vix,dxy,"
                   "oil,gold,source,created_at")

    def insert_macro_snapshot(self, *, id: str, timestamp, fed_rate=None, treasury_10y=None,
                              treasury_2y=None, cpi=None, core_cpi=None, unemployment=None, vix=None,
                              dxy=None, oil=None, gold=None, source: str | None = None) -> None:
        """Record a macro snapshot once. ON CONFLICT DO NOTHING → snapshots are never rewritten. Missing
        metrics stay NULL (NO DATA) — never fabricated."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO macro_snapshots (id,timestamp,fed_rate,treasury_10y,treasury_2y,cpi,core_cpi,"
                "unemployment,vix,dxy,oil,gold,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (id, timestamp, f(fed_rate), f(treasury_10y), f(treasury_2y), f(cpi), f(core_cpi),
                 f(unemployment), f(vix), f(dxy), f(oil), f(gold), source, now))

    def _macro_row(self, r) -> MacroSnapshotRow:
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return MacroSnapshotRow(r[0], r[1], g(r[2]), g(r[3]), g(r[4]), g(r[5]), g(r[6]), g(r[7]),
                                g(r[8]), g(r[9]), g(r[10]), g(r[11]), r[12], r[13])

    def latest_macro_snapshot(self) -> MacroSnapshotRow | None:
        r = self._one(f"SELECT {self._MACRO_COLS} FROM macro_snapshots ORDER BY timestamp DESC LIMIT 1")
        return self._macro_row(r) if r else None

    def list_macro_snapshots(self, limit: int = 200) -> list[MacroSnapshotRow]:
        n = max(1, min(2000, int(limit)))
        rows = self._all(f"SELECT {self._MACRO_COLS} FROM macro_snapshots ORDER BY timestamp DESC LIMIT ?", (n,))
        return [self._macro_row(r) for r in rows]

    def count_macro_snapshots(self) -> int:
        r = self._one("SELECT COUNT(*) FROM macro_snapshots")
        return int(r[0]) if r else 0

    # -- Institutional position changes (§ Phase R1.3, read-only IMMUTABLE 13F QoQ history) ------
    _INSTCHG_COLS = ("id,institution,symbol,previous_shares,current_shares,share_change,"
                     "percentage_change,direction,filing_period,created_at")

    def insert_institutional_change(self, *, id: str, institution: str, symbol: str, previous_shares,
                                    current_shares, share_change, percentage_change, direction: str | None,
                                    filing_period: str | None) -> None:
        """Record a 13F position change once. ON CONFLICT DO NOTHING → never rewritten (never fabricated)."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO institutional_position_changes (id,institution,symbol,previous_shares,"
                "current_shares,share_change,percentage_change,direction,filing_period,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (id, institution, symbol.upper(), f(previous_shares), f(current_shares), f(share_change),
                 f(percentage_change), direction, filing_period, now))

    def _instchg_row(self, r) -> InstitutionalChangeRow:
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return InstitutionalChangeRow(r[0], r[1], r[2], g(r[3]), g(r[4]), g(r[5]), g(r[6]), r[7], r[8], r[9])

    def list_institutional_changes(self, symbol: str | None = None, limit: int = 500) -> list[InstitutionalChangeRow]:
        n = max(1, min(5000, int(limit)))
        if symbol:
            rows = self._all(f"SELECT {self._INSTCHG_COLS} FROM institutional_position_changes WHERE symbol=? "
                             "ORDER BY filing_period DESC LIMIT ?", (symbol.upper(), n))
        else:
            rows = self._all(f"SELECT {self._INSTCHG_COLS} FROM institutional_position_changes "
                             "ORDER BY filing_period DESC LIMIT ?", (n,))
        return [self._instchg_row(r) for r in rows]

    def count_institutional_changes(self) -> int:
        r = self._one("SELECT COUNT(*) FROM institutional_position_changes")
        return int(r[0]) if r else 0

    # -- Insider transactions (§ Phase R1.3, read-only IMMUTABLE Form 4 history) ------
    _INSIDER_COLS = "id,symbol,insider_name,title,transaction_type,shares,price,transaction_date,created_at"

    def insert_insider_transaction(self, *, id: str, symbol: str, insider_name: str | None,
                                   title: str | None, transaction_type: str | None, shares, price,
                                   transaction_date: str | None) -> None:
        """Record an insider transaction once. ON CONFLICT DO NOTHING → never rewritten (never fabricated)."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO insider_transactions (id,symbol,insider_name,title,transaction_type,shares,"
                "price,transaction_date,created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (id, symbol.upper(), insider_name, title, transaction_type, f(shares), f(price),
                 transaction_date, now))

    def _insider_row(self, r) -> InsiderTransactionRow:
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return InsiderTransactionRow(r[0], r[1], r[2], r[3], r[4], g(r[5]), g(r[6]), r[7], r[8])

    def list_insider_transactions(self, symbol: str | None = None, limit: int = 500) -> list[InsiderTransactionRow]:
        n = max(1, min(5000, int(limit)))
        if symbol:
            rows = self._all(f"SELECT {self._INSIDER_COLS} FROM insider_transactions WHERE symbol=? "
                             "ORDER BY transaction_date DESC LIMIT ?", (symbol.upper(), n))
        else:
            rows = self._all(f"SELECT {self._INSIDER_COLS} FROM insider_transactions "
                             "ORDER BY transaction_date DESC LIMIT ?", (n,))
        return [self._insider_row(r) for r in rows]

    def count_insider_transactions(self) -> int:
        r = self._one("SELECT COUNT(*) FROM insider_transactions")
        return int(r[0]) if r else 0

    # -- Insider clusters (§ Phase R1.4, read-only IMMUTABLE cluster snapshots) ------
    _CLUSTER_COLS = ("id,symbol,time_window,cluster_type,insider_count,weighted_score,total_shares,"
                     "total_value,created_at")

    def insert_insider_cluster(self, *, id: str, symbol: str, time_window: str, cluster_type: str | None,
                               insider_count, weighted_score, total_shares, total_value) -> None:
        """Record an insider cluster snapshot once. ON CONFLICT DO NOTHING → never rewritten (never fabricated)."""
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO insider_clusters (id,symbol,time_window,cluster_type,insider_count,"
                "weighted_score,total_shares,total_value,created_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (id, symbol.upper(), time_window, cluster_type, f(insider_count), f(weighted_score),
                 f(total_shares), f(total_value), now))

    def _cluster_row(self, r) -> InsiderClusterRow:
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return InsiderClusterRow(r[0], r[1], r[2], r[3], g(r[4]), g(r[5]), g(r[6]), g(r[7]), r[8])

    def list_insider_clusters(self, symbol: str | None = None, limit: int = 500) -> list[InsiderClusterRow]:
        n = max(1, min(5000, int(limit)))
        if symbol:
            rows = self._all(f"SELECT {self._CLUSTER_COLS} FROM insider_clusters WHERE symbol=? "
                             "ORDER BY created_at DESC LIMIT ?", (symbol.upper(), n))
        else:
            rows = self._all(f"SELECT {self._CLUSTER_COLS} FROM insider_clusters ORDER BY created_at DESC LIMIT ?", (n,))
        return [self._cluster_row(r) for r in rows]

    def count_insider_clusters(self) -> int:
        r = self._one("SELECT COUNT(*) FROM insider_clusters")
        return int(r[0]) if r else 0

    # -- AI consensus (§ Phase G3, read-only orchestration snapshot) ------
    def upsert_ai_assessment(self, *, symbol: str, overall_score, direction_bias, confidence,
                             status: str | None) -> None:
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ai_assessments (id,symbol,timestamp,overall_score,direction_bias,confidence,"
                "status,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "timestamp=excluded.timestamp, overall_score=excluded.overall_score, "
                "direction_bias=excluded.direction_bias, confidence=excluded.confidence, status=excluded.status",
                (symbol.upper(), symbol.upper(), now, f(overall_score), direction_bias, f(confidence), status, now))

    def upsert_ai_assessment_component(self, *, assessment_id: str, component_name: str, score, weight,
                                       direction: str | None, reason: str | None, risk_flags: str | None) -> None:
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO ai_assessment_components (assessment_id,component_name,score,weight,direction,"
                "reason,risk_flags) VALUES (?,?,?,?,?,?,?) ON CONFLICT(assessment_id,component_name) DO UPDATE SET "
                "score=excluded.score, weight=excluded.weight, direction=excluded.direction, "
                "reason=excluded.reason, risk_flags=excluded.risk_flags",
                (assessment_id.upper(), component_name, f(score), f(weight), direction, reason, risk_flags))

    def get_ai_assessment(self, symbol: str) -> AiAssessmentRow | None:
        r = self._one("SELECT id,symbol,timestamp,overall_score,direction_bias,confidence,status,created_at "
                      "FROM ai_assessments WHERE id=?", (symbol.upper(),))
        if not r:
            return None
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return AiAssessmentRow(r[0], r[1], r[2], g(r[3]), r[4], g(r[5]), r[6], r[7])

    def list_ai_assessment_components(self, assessment_id: str) -> list[AiAssessmentComponentRow]:
        rows = self._all("SELECT assessment_id,component_name,score,weight,direction,reason,risk_flags "
                         "FROM ai_assessment_components WHERE assessment_id=?", (assessment_id.upper(),))
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return [AiAssessmentComponentRow(x[0], x[1], g(x[2]), g(x[3]), x[4], x[5], x[6]) for x in rows]

    def count_ai_assessments(self) -> int:
        r = self._one("SELECT COUNT(*) FROM ai_assessments")
        return int(r[0]) if r else 0

    # -- options intelligence (§ Phase G2.3, read-only) -------------------
    def upsert_options_snapshot(self, *, symbol: str, expiration_date: str, strike, option_type: str,
                                timestamp, bid, ask, last, volume, open_interest, implied_volatility,
                                source: str) -> None:
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO options_snapshot (symbol,expiration_date,strike,option_type,timestamp,bid,ask,"
                "last,volume,open_interest,implied_volatility,source,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,expiration_date,strike,option_type) DO UPDATE SET timestamp=excluded.timestamp, "
                "bid=excluded.bid, ask=excluded.ask, last=excluded.last, volume=excluded.volume, "
                "open_interest=excluded.open_interest, implied_volatility=excluded.implied_volatility, "
                "source=excluded.source",
                (symbol.upper(), expiration_date, f(strike), option_type.lower(), timestamp, f(bid), f(ask),
                 f(last), volume, open_interest, f(implied_volatility), source, now))

    def upsert_options_flow(self, *, symbol: str, timestamp, call_volume, put_volume, call_put_ratio,
                            implied_volatility, open_interest, unusual_activity_score, large_trade_count,
                            premium_volume, sentiment) -> None:
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO options_flow (symbol,timestamp,call_volume,put_volume,call_put_ratio,"
                "implied_volatility,open_interest,unusual_activity_score,large_trade_count,premium_volume,"
                "sentiment,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "timestamp=excluded.timestamp, call_volume=excluded.call_volume, put_volume=excluded.put_volume, "
                "call_put_ratio=excluded.call_put_ratio, implied_volatility=excluded.implied_volatility, "
                "open_interest=excluded.open_interest, unusual_activity_score=excluded.unusual_activity_score, "
                "large_trade_count=excluded.large_trade_count, premium_volume=excluded.premium_volume, "
                "sentiment=excluded.sentiment, updated_at=excluded.updated_at",
                (symbol.upper(), timestamp, call_volume, put_volume, f(call_put_ratio), f(implied_volatility),
                 open_interest, f(unusual_activity_score), large_trade_count, f(premium_volume), sentiment, now))

    def get_options_flow(self, symbol: str) -> OptionsFlowRow | None:
        r = self._one("SELECT symbol,timestamp,call_volume,put_volume,call_put_ratio,implied_volatility,"
                      "open_interest,unusual_activity_score,large_trade_count,premium_volume,sentiment,updated_at "
                      "FROM options_flow WHERE symbol=?", (symbol.upper(),))
        if not r:
            return None
        g = lambda v: None if v is None else float(v)  # noqa: E731
        i = lambda v: None if v is None else int(v)    # noqa: E731
        return OptionsFlowRow(r[0], r[1], i(r[2]), i(r[3]), g(r[4]), g(r[5]), i(r[6]), g(r[7]), i(r[8]),
                              g(r[9]), r[10], r[11])

    def list_options_snapshots(self, symbol: str, limit: int = 100) -> list[OptionsSnapshotRow]:
        n = max(1, min(500, int(limit)))
        rows = self._all("SELECT symbol,expiration_date,strike,option_type,timestamp,bid,ask,last,volume,"
                         "open_interest,implied_volatility,source,created_at FROM options_snapshot "
                         "WHERE symbol=? ORDER BY volume DESC LIMIT ?", (symbol.upper(), n))
        g = lambda v: None if v is None else float(v)  # noqa: E731
        i = lambda v: None if v is None else int(v)    # noqa: E731
        return [OptionsSnapshotRow(x[0], x[1], g(x[2]), x[3], x[4], g(x[5]), g(x[6]), g(x[7]), i(x[8]),
                                   i(x[9]), g(x[10]), x[11], x[12]) for x in rows]

    def count_options_flow(self) -> int:
        r = self._one("SELECT COUNT(*) FROM options_flow")
        return int(r[0]) if r else 0

    # -- fundamentals intelligence (§ Phase G2.2, read-only) --------------
    def upsert_company(self, *, symbol: str, company_name, sector, industry, exchange, country) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO companies (symbol,company_name,sector,industry,exchange,country,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET company_name=excluded.company_name, "
                "sector=excluded.sector, industry=excluded.industry, exchange=excluded.exchange, "
                "country=excluded.country, updated_at=excluded.updated_at",
                (symbol.upper(), company_name, sector, industry, exchange, country, now))

    def upsert_financial_metrics(self, *, symbol: str, period, revenue, revenue_growth, gross_margin,
                                 operating_margin, net_margin, eps, eps_growth, free_cash_flow, debt, cash) -> None:
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731 — canonical-decimal TEXT
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO financial_metrics (symbol,period,revenue,revenue_growth,gross_margin,"
                "operating_margin,net_margin,eps,eps_growth,free_cash_flow,debt,cash,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET period=excluded.period, "
                "revenue=excluded.revenue, revenue_growth=excluded.revenue_growth, "
                "gross_margin=excluded.gross_margin, operating_margin=excluded.operating_margin, "
                "net_margin=excluded.net_margin, eps=excluded.eps, eps_growth=excluded.eps_growth, "
                "free_cash_flow=excluded.free_cash_flow, debt=excluded.debt, cash=excluded.cash, "
                "updated_at=excluded.updated_at",
                (symbol.upper(), period, f(revenue), f(revenue_growth), f(gross_margin), f(operating_margin),
                 f(net_margin), f(eps), f(eps_growth), f(free_cash_flow), f(debt), f(cash), now))

    def upsert_valuation(self, *, symbol: str, market_cap, pe_ratio, forward_pe, price_sales, enterprise_value) -> None:
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO valuation (symbol,market_cap,pe_ratio,forward_pe,price_sales,enterprise_value,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET market_cap=excluded.market_cap, "
                "pe_ratio=excluded.pe_ratio, forward_pe=excluded.forward_pe, price_sales=excluded.price_sales, "
                "enterprise_value=excluded.enterprise_value, updated_at=excluded.updated_at",
                (symbol.upper(), f(market_cap), f(pe_ratio), f(forward_pe), f(price_sales), f(enterprise_value), now))

    def upsert_analyst_estimates(self, *, symbol: str, rating, target_price, analyst_count,
                                 upgrade_count, downgrade_count) -> None:
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO analyst_estimates (symbol,rating,target_price,analyst_count,upgrade_count,"
                "downgrade_count,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "rating=excluded.rating, target_price=excluded.target_price, analyst_count=excluded.analyst_count, "
                "upgrade_count=excluded.upgrade_count, downgrade_count=excluded.downgrade_count, "
                "updated_at=excluded.updated_at",
                (symbol.upper(), rating, f(target_price), analyst_count, upgrade_count, downgrade_count, now))

    def get_company(self, symbol: str) -> CompanyRow | None:
        r = self._one("SELECT symbol,company_name,sector,industry,exchange,country,updated_at "
                      "FROM companies WHERE symbol=?", (symbol.upper(),))
        return CompanyRow(*r) if r else None

    def get_financial_metrics(self, symbol: str) -> FinancialMetricsRow | None:
        r = self._one("SELECT symbol,period,revenue,revenue_growth,gross_margin,operating_margin,net_margin,"
                      "eps,eps_growth,free_cash_flow,debt,cash,updated_at FROM financial_metrics WHERE symbol=?",
                      (symbol.upper(),))
        if not r:
            return None
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return FinancialMetricsRow(r[0], r[1], g(r[2]), g(r[3]), g(r[4]), g(r[5]), g(r[6]), g(r[7]), g(r[8]),
                                   g(r[9]), g(r[10]), g(r[11]), r[12])

    def get_valuation(self, symbol: str) -> ValuationRow | None:
        r = self._one("SELECT symbol,market_cap,pe_ratio,forward_pe,price_sales,enterprise_value,updated_at "
                      "FROM valuation WHERE symbol=?", (symbol.upper(),))
        if not r:
            return None
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return ValuationRow(r[0], g(r[1]), g(r[2]), g(r[3]), g(r[4]), g(r[5]), r[6])

    def get_analyst_estimates(self, symbol: str) -> AnalystEstimatesRow | None:
        r = self._one("SELECT symbol,rating,target_price,analyst_count,upgrade_count,downgrade_count,updated_at "
                      "FROM analyst_estimates WHERE symbol=?", (symbol.upper(),))
        if not r:
            return None
        return AnalystEstimatesRow(r[0], r[1], (float(r[2]) if r[2] is not None else None),
                                   (int(r[3]) if r[3] is not None else None),
                                   (int(r[4]) if r[4] is not None else None),
                                   (int(r[5]) if r[5] is not None else None), r[6])

    def count_companies(self) -> int:
        r = self._one("SELECT COUNT(*) FROM companies")
        return int(r[0]) if r else 0

    # -- trader intelligence (§ Phase G2.5, read-only) --------------------
    def upsert_trader(self, *, id: str, name: str, source: str, market_focus: str | None,
                      strategy_type: str | None, track_record_days: int | None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO traders (id,name,source,market_focus,strategy_type,track_record_days,created_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "source=excluded.source, market_focus=excluded.market_focus, "
                "strategy_type=excluded.strategy_type, track_record_days=excluded.track_record_days",
                (id, name, source, market_focus, strategy_type, track_record_days, now))

    def upsert_trader_performance(self, *, trader_id: str, total_return, annualized_return, win_rate,
                                  max_drawdown, sharpe_ratio, sortino_ratio, average_holding_period,
                                  number_of_trades) -> None:
        now = utcnow_iso()
        f = lambda v: None if v is None else str(float(v))  # noqa: E731 — canonical-decimal TEXT
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO trader_performance (trader_id,total_return,annualized_return,win_rate,"
                "max_drawdown,sharpe_ratio,sortino_ratio,average_holding_period,number_of_trades,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trader_id) DO UPDATE SET "
                "total_return=excluded.total_return, annualized_return=excluded.annualized_return, "
                "win_rate=excluded.win_rate, max_drawdown=excluded.max_drawdown, "
                "sharpe_ratio=excluded.sharpe_ratio, sortino_ratio=excluded.sortino_ratio, "
                "average_holding_period=excluded.average_holding_period, "
                "number_of_trades=excluded.number_of_trades, updated_at=excluded.updated_at",
                (trader_id, f(total_return), f(annualized_return), f(win_rate), f(max_drawdown),
                 f(sharpe_ratio), f(sortino_ratio), f(average_holding_period), number_of_trades, now))

    def upsert_trader_position(self, *, trader_id: str, symbol: str, direction: str,
                               entry_price, position_size, timestamp: str) -> None:
        f = lambda v: None if v is None else str(float(v))  # noqa: E731
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO trader_positions (trader_id,symbol,direction,entry_price,position_size,timestamp) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(trader_id,symbol) DO UPDATE SET direction=excluded.direction, "
                "entry_price=excluded.entry_price, position_size=excluded.position_size, timestamp=excluded.timestamp",
                (trader_id, symbol.upper(), direction.upper(), f(entry_price), f(position_size), timestamp))

    def get_trader(self, trader_id: str) -> TraderRow | None:
        r = self._one("SELECT id,name,source,market_focus,strategy_type,track_record_days,created_at "
                      "FROM traders WHERE id=?", (trader_id,))
        return TraderRow(r[0], r[1], r[2], r[3], r[4], (int(r[5]) if r[5] is not None else None), r[6]) if r else None

    def list_traders(self, limit: int = 200) -> list[TraderRow]:
        n = max(1, min(1000, int(limit)))
        rows = self._all("SELECT id,name,source,market_focus,strategy_type,track_record_days,created_at "
                         "FROM traders ORDER BY created_at DESC LIMIT ?", (n,))
        return [TraderRow(r[0], r[1], r[2], r[3], r[4], (int(r[5]) if r[5] is not None else None), r[6]) for r in rows]

    def get_trader_performance(self, trader_id: str) -> TraderPerformanceRow | None:
        r = self._one("SELECT trader_id,total_return,annualized_return,win_rate,max_drawdown,sharpe_ratio,"
                      "sortino_ratio,average_holding_period,number_of_trades,updated_at "
                      "FROM trader_performance WHERE trader_id=?", (trader_id,))
        if not r:
            return None
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return TraderPerformanceRow(r[0], g(r[1]), g(r[2]), g(r[3]), g(r[4]), g(r[5]), g(r[6]), g(r[7]),
                                    (int(r[8]) if r[8] is not None else None), r[9])

    def list_trader_positions_for_symbol(self, symbol: str) -> list[TraderPositionRow]:
        rows = self._all("SELECT trader_id,symbol,direction,entry_price,position_size,timestamp "
                         "FROM trader_positions WHERE symbol=?", (symbol.upper(),))
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return [TraderPositionRow(r[0], r[1], r[2], g(r[3]), g(r[4]), r[5]) for r in rows]

    def list_trader_positions_for_trader(self, trader_id: str) -> list[TraderPositionRow]:
        rows = self._all("SELECT trader_id,symbol,direction,entry_price,position_size,timestamp "
                         "FROM trader_positions WHERE trader_id=?", (trader_id,))
        g = lambda v: None if v is None else float(v)  # noqa: E731
        return [TraderPositionRow(r[0], r[1], r[2], g(r[3]), g(r[4]), r[5]) for r in rows]

    def count_traders(self) -> int:
        r = self._one("SELECT COUNT(*) FROM traders")
        return int(r[0]) if r else 0

    def list_decisions(self, limit: int = 50) -> list[DecisionRow]:
        """Most-recent `limit` AI decisions (newest first) for the dashboard read-model. Read-only;
        returns an empty list when none have been recorded (the UI shows NO DATA)."""
        n = max(1, min(500, int(limit)))
        rows = self._all(
            "SELECT decision_id,ts,instrument,final_decision,payload,correlation_id FROM decisions "
            "ORDER BY ts DESC LIMIT ?", (n,))
        return [DecisionRow(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]

    def upsert_heartbeat(self, *, service: str, status: str, detail: str | None = None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO service_heartbeats (service,status,detail,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(service) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
                "updated_at=excluded.updated_at",
                (service, status, detail, now))

    def list_heartbeats(self) -> list[tuple]:
        return self._all("SELECT service,status,detail,updated_at FROM service_heartbeats ORDER BY service")

    def upsert_md_health(self, *, symbol: str, source: str, status: str, latency_ms, ts: str) -> None:
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO market_data_health (symbol,source,status,latency_ms,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET source=excluded.source, "
                "status=excluded.status, latency_ms=excluded.latency_ms, updated_at=excluded.updated_at",
                (symbol, source, status, latency_ms, ts))

    def list_md_health(self) -> list[tuple]:
        return self._all("SELECT symbol,source,status,latency_ms,updated_at FROM market_data_health "
                         "ORDER BY symbol")

    # ---- § R3.0 backtesting (RESEARCH ONLY; terminal-immutable via DB triggers) -----------------
    def list_ohlc_bars_range(self, symbol: str, interval: str, start_ts: str, end_ts: str,
                             limit: int = 60000) -> list[OhlcBarRow]:
        """Bars for (symbol, interval) with event-time ts in [start_ts, end_ts], oldest→newest. Both
        the event time (ts) and the ingest time (created_at) are returned so the caller can enforce a
        point-in-time availability policy. Read-only."""
        n = max(1, min(60000, int(limit)))
        rows = self._all(
            "SELECT symbol,interval,ts,open,high,low,close,volume,source,created_at FROM ohlc_bars "
            "WHERE symbol=? AND interval=? AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT ?",
            (symbol, interval, start_ts, end_ts, n))
        return [OhlcBarRow(r[0], r[1], r[2], to_decimal(r[3]), to_decimal(r[4]), to_decimal(r[5]),
                           to_decimal(r[6]), to_decimal(r[7]), r[8], r[9]) for r in rows]

    _BT_RUN_COLS = (
        "run_id,owner,strategy_id,strategy_version,strategy_config_json,strategy_checksum,engine_version,"
        "symbol_universe_json,interval,start_ts,end_ts,asset_class,timestamp_policy_id,"
        "timestamp_policy_version,exchange_calendar_id,exchange_calendar_version,exchange_tz,"
        "session_calendar,data_source,config_snapshot_json,risk_config_snapshot_json,status,failure_code,"
        "failure_reason,warnings_json,missing_data_json,commit_ref,result_checksum,created_at,started_at,"
        "ended_at,updated_at,dataset_id,dataset_provider,dataset_provider_contract_version,"
        "dataset_adjustment_policy,dataset_normalization_policy,dataset_calendar_version,dataset_checksum")

    def bt_create_run(self, *, run_id, owner, strategy_id, strategy_version, strategy_config_json,
                      strategy_checksum, engine_version, symbol_universe_json, interval, start_ts, end_ts,
                      asset_class, timestamp_policy_id, timestamp_policy_version, exchange_calendar_id,
                      exchange_calendar_version, exchange_tz, session_calendar, data_source,
                      config_snapshot_json, risk_config_snapshot_json, commit_ref=None, dataset_pin=None) -> None:
        """Create a run in QUEUED. The run row is never deleted (DB trigger); it only transitions.
        `dataset_pin` (optional) = {dataset_id, provider, provider_contract_version, adjustment_policy,
        normalization_policy, calendar_version, checksum} for a research-dataset-pinned run; NULL otherwise."""
        now = utcnow_iso()
        p = dataset_pin or {}
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO backtest_runs ({self._BT_RUN_COLS}) VALUES ({','.join(['?'] * 39)})",
                       (run_id, owner, strategy_id, int(strategy_version), strategy_config_json,
                        strategy_checksum, engine_version, symbol_universe_json, interval, start_ts, end_ts,
                        asset_class, timestamp_policy_id, int(timestamp_policy_version), exchange_calendar_id,
                        exchange_calendar_version, exchange_tz, session_calendar, data_source,
                        config_snapshot_json, risk_config_snapshot_json, "QUEUED", None, None, None, None,
                        commit_ref, None, now, None, None, now,
                        p.get("dataset_id"), p.get("provider"), p.get("provider_contract_version"),
                        p.get("adjustment_policy"), p.get("normalization_policy"), p.get("calendar_version"),
                        p.get("checksum")))

    def bt_advance_status(self, run_id: str, expected_from: str, to: str) -> bool:
        """Optimistic state transition. Returns False if the run is not in `expected_from` (or the DB
        trigger rejected a terminal mutation). The legal-transition map is validated by the caller."""
        now = utcnow_iso()
        with self.tx() as cur:
            if to == "RUNNING":
                self._exec(cur, "UPDATE backtest_runs SET status=?, started_at=?, updated_at=? "
                           "WHERE run_id=? AND status=?", (to, now, now, run_id, expected_from))
            else:
                self._exec(cur, "UPDATE backtest_runs SET status=?, updated_at=? WHERE run_id=? AND status=?",
                           (to, now, run_id, expected_from))
            return cur.rowcount > 0

    def bt_finalize_run(self, run_id: str, expected_from: str, status: str, *, result_checksum=None,
                        warnings_json=None, missing_data_json=None, failure_code=None,
                        failure_reason=None) -> bool:
        """Terminal transition (→ COMPLETED / FAILED / CANCELLED) with result fields, atomically."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "UPDATE backtest_runs SET status=?, result_checksum=?, warnings_json=?, "
                       "missing_data_json=?, failure_code=?, failure_reason=?, ended_at=?, updated_at=? "
                       "WHERE run_id=? AND status=?",
                       (status, result_checksum, warnings_json, missing_data_json, failure_code,
                        failure_reason, now, now, run_id, expected_from))
            return cur.rowcount > 0

    def bt_insert_decision(self, *, id, run_id, seq, ts, symbol, strategy_id, strategy_version, action,
                           confidence=None, evidence_json=None, missing_inputs_json=None, reason=None,
                           decision_checksum) -> None:
        with self.tx() as cur:
            self._exec(cur, "INSERT INTO backtest_decisions (id,run_id,seq,ts,symbol,strategy_id,"
                       "strategy_version,action,confidence,evidence_json,missing_inputs_json,reason,"
                       "decision_checksum,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (id, run_id, int(seq), ts, symbol, strategy_id, int(strategy_version), action,
                        confidence, evidence_json, missing_inputs_json, reason, decision_checksum, utcnow_iso()))

    def bt_insert_trade(self, *, id, run_id, symbol, entry_decision_id, exit_decision_id, entry_ts,
                        entry_fill_ts, entry_price, initial_stop_price, exit_ts, exit_fill_ts, exit_price,
                        quantity, gross_pnl, commission, slippage, net_pnl, return_pct, bars_held,
                        exit_reason, ambiguous) -> None:
        def om(v):
            return None if v is None else money_str(v)
        with self.tx() as cur:
            self._exec(cur, "INSERT INTO backtest_trades (id,run_id,symbol,side,entry_decision_id,"
                       "exit_decision_id,entry_ts,entry_fill_ts,entry_price,initial_stop_price,exit_ts,"
                       "exit_fill_ts,exit_price,quantity,gross_pnl,commission,slippage,net_pnl,return_pct,"
                       "bars_held,exit_reason,ambiguous,created_at) "
                       "VALUES (?,?,?,'LONG',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (id, run_id, symbol, entry_decision_id, exit_decision_id, entry_ts, entry_fill_ts,
                        money_str(entry_price), om(initial_stop_price), exit_ts, exit_fill_ts, om(exit_price),
                        money_str(quantity), om(gross_pnl), money_str(commission), money_str(slippage),
                        om(net_pnl), (None if return_pct is None else str(return_pct)),
                        (None if bars_held is None else int(bars_held)), exit_reason,
                        1 if ambiguous else 0, utcnow_iso()))

    def bt_insert_equity(self, *, run_id, seq, ts, cash, equity, realized_pnl, unrealized_pnl,
                         daily_pnl=None, gross_exposure_pct=None, net_exposure_pct=None,
                         drawdown_pct=None) -> None:
        def om(v):
            return None if v is None else money_str(v)
        with self.tx() as cur:
            self._exec(cur, "INSERT INTO backtest_equity_points (run_id,seq,ts,cash,equity,realized_pnl,"
                       "unrealized_pnl,daily_pnl,gross_exposure_pct,net_exposure_pct,drawdown_pct) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, int(seq), ts, money_str(cash), money_str(equity), money_str(realized_pnl),
                        money_str(unrealized_pnl), om(daily_pnl),
                        (None if gross_exposure_pct is None else str(gross_exposure_pct)),
                        (None if net_exposure_pct is None else str(net_exposure_pct)),
                        (None if drawdown_pct is None else str(drawdown_pct))))

    def bt_insert_event(self, *, id, run_id, seq=None, ts=None, event_type, severity=None, symbol=None,
                        details_json=None) -> None:
        with self.tx() as cur:
            self._exec(cur, "INSERT INTO backtest_events (id,run_id,seq,ts,event_type,severity,symbol,"
                       "details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (id, run_id, (None if seq is None else int(seq)), ts, event_type, severity, symbol,
                        details_json, utcnow_iso()))

    def bt_insert_metrics(self, *, run_id, metrics_json) -> None:
        with self.tx() as cur:
            self._exec(cur, "INSERT INTO backtest_metrics (run_id,metrics_json,computed_at) VALUES (?,?,?)",
                       (run_id, metrics_json, utcnow_iso()))

    def bt_get_run(self, run_id: str) -> BacktestRunRow | None:
        r = self._one(f"SELECT {self._BT_RUN_COLS} FROM backtest_runs WHERE run_id=?", (run_id,))
        return BacktestRunRow(*r) if r else None

    def bt_count_active(self, owner: str) -> int:
        r = self._one("SELECT COUNT(*) FROM backtest_runs WHERE owner=? AND status IN ('QUEUED','RUNNING')",
                      (owner,))
        return int(r[0]) if r else 0

    def bt_list_runs(self, *, owner: str | None = None, status: str | None = None, limit: int = 50,
                     offset: int = 0) -> list[BacktestRunRow]:
        n, off = max(1, min(100, int(limit))), max(0, int(offset))
        where, params = [], []
        if owner:
            where.append("owner=?"); params.append(owner)
        if status:
            where.append("status=?"); params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(f"SELECT {self._BT_RUN_COLS} FROM backtest_runs{clause} "
                         "ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, n, off))
        return [BacktestRunRow(*r) for r in rows]

    def bt_list_decisions(self, run_id: str, limit: int = 500, offset: int = 0) -> list[BacktestDecisionRow]:
        n, off = max(1, min(2000, int(limit))), max(0, int(offset))
        rows = self._all("SELECT id,run_id,seq,ts,symbol,strategy_id,strategy_version,action,confidence,"
                         "evidence_json,missing_inputs_json,reason,decision_checksum,created_at "
                         "FROM backtest_decisions WHERE run_id=? ORDER BY seq ASC LIMIT ? OFFSET ?",
                         (run_id, n, off))
        return [BacktestDecisionRow(*r) for r in rows]

    def bt_list_trades(self, run_id: str, limit: int = 1000, offset: int = 0) -> list[BacktestTradeRow]:
        n, off = max(1, min(2000, int(limit))), max(0, int(offset))
        rows = self._all("SELECT id,run_id,symbol,side,entry_decision_id,exit_decision_id,entry_ts,"
                         "entry_fill_ts,entry_price,initial_stop_price,exit_ts,exit_fill_ts,exit_price,"
                         "quantity,gross_pnl,commission,slippage,net_pnl,return_pct,bars_held,exit_reason,"
                         "ambiguous,created_at,expected_risk_per_share,actual_risk_per_share "
                         "FROM backtest_trades WHERE run_id=? ORDER BY entry_ts ASC LIMIT ? OFFSET ?",
                         (run_id, n, off))
        return [BacktestTradeRow(*r[:21], bool(r[21]), r[22], r[23], r[24]) for r in rows]

    def bt_list_equity(self, run_id: str, limit: int = 50000) -> list[BacktestEquityPointRow]:
        n = max(1, min(50000, int(limit)))
        rows = self._all("SELECT run_id,seq,ts,cash,equity,realized_pnl,unrealized_pnl,daily_pnl,"
                         "gross_exposure_pct,net_exposure_pct,drawdown_pct FROM backtest_equity_points "
                         "WHERE run_id=? ORDER BY seq ASC LIMIT ?", (run_id, n))
        return [BacktestEquityPointRow(*r) for r in rows]

    def bt_list_events(self, run_id: str, limit: int = 500, offset: int = 0) -> list[BacktestEventRow]:
        n, off = max(1, min(2000, int(limit))), max(0, int(offset))
        rows = self._all("SELECT id,run_id,seq,ts,event_type,severity,symbol,details_json,created_at "
                         "FROM backtest_events WHERE run_id=? ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                         (run_id, n, off))
        return [BacktestEventRow(*r) for r in rows]

    def bt_get_metrics(self, run_id: str) -> BacktestMetricsRow | None:
        r = self._one("SELECT run_id,metrics_json,computed_at FROM backtest_metrics WHERE run_id=?", (run_id,))
        return BacktestMetricsRow(*r) if r else None

    def bt_write_results(self, run_id: str, *, expected_from: str, status: str, decisions=(), trades=(),
                         equity_points=(), events=(), metrics_json=None, result_checksum=None,
                         warnings_json=None, missing_data_json=None, failure_code=None,
                         failure_reason=None) -> bool:
        """Persist ALL result rows + finalize the run in ONE transaction. Child inserts run while the
        parent is still `expected_from` (non-terminal → DB triggers allow); the terminal transition is
        the last statement (RUNNING→terminal → allowed), so the whole result set + failure info commit
        atomically and are frozen thereafter. Returns True iff the run was in `expected_from`."""
        now = utcnow_iso()

        def om(v):
            return None if v is None else money_str(v)

        def scoped(local):
            return None if local is None else f"{run_id}:{local}"

        with self.tx() as cur:
            for d in decisions:
                self._exec(cur, "INSERT INTO backtest_decisions (id,run_id,seq,ts,symbol,strategy_id,"
                           "strategy_version,action,confidence,evidence_json,missing_inputs_json,reason,"
                           "decision_checksum,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (scoped(d["id"]), run_id, int(d["seq"]), d["ts"], d["symbol"], d["strategy_id"],
                            int(d["strategy_version"]), d["action"],
                            (None if d.get("confidence") is None else str(d["confidence"])),
                            json.dumps(d.get("evidence") or {}), json.dumps(d.get("missing_inputs") or []),
                            d.get("reason"), d["checksum"], now))
            for t in trades:
                self._exec(cur, "INSERT INTO backtest_trades (id,run_id,symbol,side,entry_decision_id,"
                           "exit_decision_id,entry_ts,entry_fill_ts,entry_price,initial_stop_price,exit_ts,"
                           "exit_fill_ts,exit_price,quantity,gross_pnl,commission,slippage,net_pnl,return_pct,"
                           "bars_held,exit_reason,ambiguous,created_at,expected_risk_per_share,"
                           "actual_risk_per_share) "
                           "VALUES (?,?,?,'LONG',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (scoped(t["id"]), run_id, t["symbol"], scoped(t.get("entry_decision_id")),
                            scoped(t.get("exit_decision_id")),
                            t["entry_ts"], t["entry_fill_ts"], money_str(t["entry_price"]),
                            om(t.get("initial_stop_price")), t.get("exit_ts"), t.get("exit_fill_ts"),
                            om(t.get("exit_price")), money_str(t["quantity"]), om(t.get("gross_pnl")),
                            money_str(t["commission"]), money_str(t["slippage"]), om(t.get("net_pnl")),
                            (None if t.get("return_pct") is None else str(t["return_pct"])),
                            (None if t.get("bars_held") is None else int(t["bars_held"])), t.get("exit_reason"),
                            1 if t.get("ambiguous") else 0, now,
                            om(t.get("expected_risk_per_share")), om(t.get("actual_risk_per_share"))))
            for e in equity_points:
                self._exec(cur, "INSERT INTO backtest_equity_points (run_id,seq,ts,cash,equity,realized_pnl,"
                           "unrealized_pnl,daily_pnl,gross_exposure_pct,net_exposure_pct,drawdown_pct) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, int(e["seq"]), e["ts"], money_str(e["cash"]), money_str(e["equity"]),
                            money_str(e["realized_pnl"]), money_str(e["unrealized_pnl"]), om(e.get("daily_pnl")),
                            (None if e.get("gross_exposure_pct") is None else str(e["gross_exposure_pct"])),
                            (None if e.get("net_exposure_pct") is None else str(e["net_exposure_pct"])),
                            (None if e.get("drawdown_pct") is None else str(e["drawdown_pct"]))))
            for ev in events:
                self._exec(cur, "INSERT INTO backtest_events (id,run_id,seq,ts,event_type,severity,symbol,"
                           "details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (f"{run_id}-e{ev['seq']}", run_id, int(ev["seq"]), ev.get("ts"), ev["event_type"],
                            ev.get("severity"), ev.get("symbol"), json.dumps(ev.get("details") or {}), now))
            if metrics_json is not None:
                self._exec(cur, "INSERT INTO backtest_metrics (run_id,metrics_json,computed_at) VALUES (?,?,?)",
                           (run_id, metrics_json, now))
            self._exec(cur, "UPDATE backtest_runs SET status=?, result_checksum=?, warnings_json=?, "
                       "missing_data_json=?, failure_code=?, failure_reason=?, ended_at=?, updated_at=? "
                       "WHERE run_id=? AND status=?",
                       (status, result_checksum, warnings_json, missing_data_json, failure_code,
                        failure_reason, now, now, run_id, expected_from))
            return cur.rowcount > 0

    # ---- § R3.0A research OHLC datasets (immutable, versioned; NEVER touch live ohlc_bars) ----
    _RD_DS_COLS = (
        "dataset_id,owner,request_checksum,supersedes_dataset_id,retry_of_dataset_id,symbol_universe_json,"
        "interval,provider,provider_contract_version,adjustment_policy,normalization_policy,calendar_version,"
        "range_start,range_end,status,row_count,missing_minute_threshold,raw_pages_checksum,dataset_checksum,"
        "provider_adjusted_flag,warnings_json,missing_data_json,failure_code,failure_reason,created_at,"
        "started_at,ended_at,updated_at")

    def rd_create_dataset(self, *, dataset_id, owner, request_checksum, symbol_universe_json, interval,
                          provider, provider_contract_version, adjustment_policy, normalization_policy,
                          calendar_version, range_start, range_end, missing_minute_threshold=None,
                          supersedes_dataset_id=None, retry_of_dataset_id=None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO research_datasets ({self._RD_DS_COLS}) VALUES ({','.join(['?'] * 28)})",
                       (dataset_id, owner, request_checksum, supersedes_dataset_id, retry_of_dataset_id,
                        symbol_universe_json, interval, provider, provider_contract_version, adjustment_policy,
                        normalization_policy, calendar_version, range_start, range_end, "PLANNED", None,
                        (None if missing_minute_threshold is None else str(missing_minute_threshold)),
                        None, None, None, None, None, None, None, now, None, None, now))

    def rd_advance_status(self, dataset_id: str, expected_from: str, to: str) -> bool:
        now = utcnow_iso()
        with self.tx() as cur:
            if to == "RUNNING":
                self._exec(cur, "UPDATE research_datasets SET status=?, started_at=?, updated_at=? "
                           "WHERE dataset_id=? AND status=?", (to, now, now, dataset_id, expected_from))
            else:
                self._exec(cur, "UPDATE research_datasets SET status=?, updated_at=? WHERE dataset_id=? AND status=?",
                           (to, now, dataset_id, expected_from))
            return cur.rowcount > 0

    def rd_write_and_finalize(self, dataset_id: str, *, expected_from: str, status: str, bars=(), events=(),
                              row_count=None, raw_pages_checksum=None, dataset_checksum=None,
                              provider_adjusted_flag=None, warnings_json=None, missing_data_json=None,
                              failure_code=None, failure_reason=None) -> bool:
        """Persist all normalized bars + events AND finalize the dataset in ONE transaction (child inserts
        while non-terminal → triggers allow; terminal transition last). Atomic + then frozen."""
        now = utcnow_iso()
        with self.tx() as cur:
            for barr in bars:
                self._exec(cur, "INSERT INTO research_ohlc_bars (dataset_id,symbol,interval,ts,session_date,"
                           "open,high,low,close,volume,trade_count,source,adjustment_policy,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (dataset_id, barr["symbol"], barr["interval"], barr["ts"], barr["session_date"],
                            money_str(barr["open"]), money_str(barr["high"]), money_str(barr["low"]),
                            money_str(barr["close"]), money_str(barr["volume"]),
                            (None if barr.get("trade_count") is None else int(barr["trade_count"])),
                            barr["source"], barr["adjustment_policy"], now))
            for e in events:
                self._exec(cur, "INSERT INTO research_dataset_events (id,dataset_id,seq,ts,event_type,severity,"
                           "symbol,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (f"{dataset_id}-e{e['seq']}", dataset_id, int(e["seq"]), e.get("ts"), e["event_type"],
                            e.get("severity"), e.get("symbol"), json.dumps(e.get("details") or {}), now))
            self._exec(cur, "UPDATE research_datasets SET status=?, row_count=?, raw_pages_checksum=?, "
                       "dataset_checksum=?, provider_adjusted_flag=?, warnings_json=?, missing_data_json=?, "
                       "failure_code=?, failure_reason=?, ended_at=?, updated_at=? WHERE dataset_id=? AND status=?",
                       (status, (None if row_count is None else int(row_count)), raw_pages_checksum,
                        dataset_checksum, (None if provider_adjusted_flag is None else (1 if provider_adjusted_flag else 0)),
                        warnings_json, missing_data_json, failure_code, failure_reason, now, now,
                        dataset_id, expected_from))
            return cur.rowcount > 0

    def rd_append_bars(self, dataset_id: str, bars=(), events=()) -> bool:
        """R3.0A.1 — incrementally persist ONE bounded chunk's normalized bars + events while the dataset is
        RUNNING (never finalizes). Bumps updated_at as a liveness heartbeat (so a crashed worker's RUNNING
        row is detectably stale). The heartbeat UPDATE is guarded on status='RUNNING' FIRST — if the dataset
        is no longer RUNNING (reclaimed/terminal) it returns False and inserts nothing (all in one tx)."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "UPDATE research_datasets SET updated_at=? WHERE dataset_id=? AND status='RUNNING'",
                       (now, dataset_id))
            if cur.rowcount <= 0:
                return False
            for barr in bars:
                self._exec(cur, "INSERT INTO research_ohlc_bars (dataset_id,symbol,interval,ts,session_date,"
                           "open,high,low,close,volume,trade_count,source,adjustment_policy,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (dataset_id, barr["symbol"], barr["interval"], barr["ts"], barr["session_date"],
                            money_str(barr["open"]), money_str(barr["high"]), money_str(barr["low"]),
                            money_str(barr["close"]), money_str(barr["volume"]),
                            (None if barr.get("trade_count") is None else int(barr["trade_count"])),
                            barr["source"], barr["adjustment_policy"], now))
            for e in events:
                self._exec(cur, "INSERT INTO research_dataset_events (id,dataset_id,seq,ts,event_type,severity,"
                           "symbol,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (f"{dataset_id}-e{e['seq']}", dataset_id, int(e["seq"]), e.get("ts"), e["event_type"],
                            e.get("severity"), e.get("symbol"), json.dumps(e.get("details") or {}), now))
            return True

    def rd_reclaim_stale_running(self, cutoff_iso: str, *, failure_code: str, failure_reason: str) -> list[str]:
        """R3.0A.1 — a RUNNING dataset whose heartbeat (updated_at) is older than `cutoff_iso` is a crashed
        worker; record it honestly as FAILED (terminal) BEFORE any retry creates a NEW dataset. Returns the
        reclaimed dataset_ids. RUNNING→FAILED is a legal terminal transition (triggers allow it)."""
        now = utcnow_iso()
        ids = [r[0] for r in self._all("SELECT dataset_id FROM research_datasets WHERE status='RUNNING' "
                                       "AND updated_at < ?", (cutoff_iso,))]
        for ds_id in ids:
            with self.tx() as cur:
                self._exec(cur, "UPDATE research_datasets SET status='FAILED', failure_code=?, failure_reason=?, "
                           "ended_at=?, updated_at=? WHERE dataset_id=? AND status='RUNNING'",
                           (failure_code, failure_reason, now, now, ds_id))
        return ids

    def rd_get_dataset(self, dataset_id: str) -> ResearchDatasetRow | None:
        r = self._one(f"SELECT {self._RD_DS_COLS} FROM research_datasets WHERE dataset_id=?", (dataset_id,))
        if not r:
            return None
        r = list(r)
        r[19] = None if r[19] is None else bool(r[19])   # provider_adjusted_flag → bool
        return ResearchDatasetRow(*r)

    def rd_find_by_request_checksum(self, request_checksum: str):
        """Idempotency lookup → (completed_row_or_None, running_row_or_None). Never returns FAILED."""
        rows = self._all(f"SELECT {self._RD_DS_COLS} FROM research_datasets WHERE request_checksum=? "
                         "ORDER BY created_at DESC", (request_checksum,))
        completed = running = None
        for raw in rows:
            raw = list(raw)
            raw[19] = None if raw[19] is None else bool(raw[19])
            row = ResearchDatasetRow(*raw)
            if row.status == "COMPLETED" and completed is None:
                completed = row
            elif row.status in ("RUNNING", "PLANNED") and running is None:
                running = row
        return completed, running

    def rd_list_datasets(self, *, owner: str | None = None, status: str | None = None, limit: int = 50,
                         offset: int = 0) -> list[ResearchDatasetRow]:
        n, off = max(1, min(100, int(limit))), max(0, int(offset))
        where, params = [], []
        if owner:
            where.append("owner=?"); params.append(owner)
        if status:
            where.append("status=?"); params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(f"SELECT {self._RD_DS_COLS} FROM research_datasets{clause} "
                         "ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, n, off))
        out = []
        for raw in rows:
            raw = list(raw)
            raw[19] = None if raw[19] is None else bool(raw[19])
            out.append(ResearchDatasetRow(*raw))
        return out

    def rd_superseded_by(self, dataset_id: str) -> list[str]:
        """Derived (not a mutation): dataset_ids that declare this one as their `supersedes_dataset_id`."""
        return [r[0] for r in self._all(
            "SELECT dataset_id FROM research_datasets WHERE supersedes_dataset_id=? ORDER BY created_at",
            (dataset_id,))]

    def rd_list_bars(self, dataset_id: str, symbol: str | None = None, limit: int = 5000) -> list[tuple]:
        n = max(1, min(60000, int(limit)))
        if symbol:
            return self._all("SELECT dataset_id,symbol,interval,ts,session_date,open,high,low,close,volume,"
                             "trade_count,source,adjustment_policy FROM research_ohlc_bars "
                             "WHERE dataset_id=? AND symbol=? ORDER BY symbol, ts ASC LIMIT ?",
                             (dataset_id, symbol, n))
        return self._all("SELECT dataset_id,symbol,interval,ts,session_date,open,high,low,close,volume,"
                         "trade_count,source,adjustment_policy FROM research_ohlc_bars "
                         "WHERE dataset_id=? ORDER BY symbol, ts ASC LIMIT ?", (dataset_id, n))

    def rd_list_bars_range(self, dataset_id: str, symbol: str, interval: str, start_ts: str, end_ts: str,
                           limit: int = 60000) -> list[OhlcBarRow]:
        """Read a pinned dataset's bars in [start_ts, end_ts] as OhlcBarRow (so R3's replay reuses the same
        shape). Read-only."""
        n = max(1, min(60000, int(limit)))
        rows = self._all("SELECT symbol,interval,ts,open,high,low,close,volume,source,created_at "
                         "FROM research_ohlc_bars WHERE dataset_id=? AND symbol=? AND interval=? "
                         "AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT ?",
                         (dataset_id, symbol, interval, start_ts, end_ts, n))
        return [OhlcBarRow(r[0], r[1], r[2], to_decimal(r[3]), to_decimal(r[4]), to_decimal(r[5]),
                           to_decimal(r[6]), to_decimal(r[7]), r[8], r[9]) for r in rows]

    def rd_count_bars(self, dataset_id: str, symbol: str | None = None) -> int:
        if symbol:
            r = self._one("SELECT COUNT(*) FROM research_ohlc_bars WHERE dataset_id=? AND symbol=?",
                          (dataset_id, symbol))
        else:
            r = self._one("SELECT COUNT(*) FROM research_ohlc_bars WHERE dataset_id=?", (dataset_id,))
        return int(r[0]) if r else 0

    def rd_list_events(self, dataset_id: str, limit: int = 500, offset: int = 0) -> list[ResearchDatasetEventRow]:
        n, off = max(1, min(2000, int(limit))), max(0, int(offset))
        rows = self._all("SELECT id,dataset_id,seq,ts,event_type,severity,symbol,details_json,created_at "
                         "FROM research_dataset_events WHERE dataset_id=? ORDER BY created_at ASC, id ASC "
                         "LIMIT ? OFFSET ?", (dataset_id, n, off))
        return [ResearchDatasetEventRow(*r) for r in rows]
