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
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from .money import QUANT, D, money_str, to_decimal


class _ReclaimSkipped(Exception):
    """Internal sentinel: a stale-reclaim tx whose guarded terminal flip matched 0 rows (a fresh heartbeat
    won the race) — used to roll the whole transaction back so the reclaim event is never left behind."""


class PaperCanaryError(RuntimeError):
    """Base error for the durable PAPER-only canary ledger."""


class PaperCanaryConflict(PaperCanaryError):
    """An idempotency key or immutable identity was reused with different canonical content."""


class PaperCanaryStateError(PaperCanaryConflict):
    """A version/status compare-and-swap or state-machine precondition failed."""


class PaperCanarySafetyError(PaperCanaryError):
    """A durable runtime, kill, risk, capital, or exposure guard refused execution."""


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


def _paper_exact_money(value, *, field: str, positive: bool = False,
                       nonnegative: bool = False) -> Decimal:
    if type(value) is not Decimal:
        raise TypeError(f"{field} must be an exact Decimal")
    if not value.is_finite() or D(money_str(value)) != value:
        raise ValueError(f"{field} must be finite and exactly representable at 8 decimal places")
    if value == 0 and value.is_signed():
        raise ValueError(f"{field} must not use signed zero")
    if positive and value <= 0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def paper_canary_money_str(value: Decimal) -> str:
    """Canonical fixed-point 8dp text for Paper Canary hashes and config snapshots."""
    exact = _paper_exact_money(value, field="paper money")
    canonical = exact.quantize(QUANT, rounding=ROUND_HALF_EVEN)
    if canonical == 0:
        canonical = abs(canonical)
    return format(canonical, "f")


def _paper_timestamp(value: str, *, field: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{field} must be a non-empty aware ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError(f"{field} must be UTC")
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc) == f"{field} must be UTC":
            raise
        raise ValueError(f"{field} must be a UTC ISO-8601 timestamp") from exc


def _paper_json(value, *, field: str, require_object: bool = True) -> tuple[dict, str]:
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"{field} contains duplicate key {key!r}")
            result[key] = item
        return result

    try:
        decoded = json.loads(value, object_pairs_hook=unique_object) if type(value) is str else value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if require_object and type(decoded) is not dict:
        raise ValueError(f"{field} must be a JSON object")
    try:
        canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                               allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be canonicalizable JSON") from exc
    return decoded, canonical


def paper_canary_config_checksum(config_json) -> str:
    """Checksum the canonical, lossless PAPER-canary config snapshot."""
    _, canonical = _paper_json(config_json, field="config_json")
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def paper_canary_request_checksum(*, run_id: str, decision_id: str, client_order_id: str,
                                  instrument: str, side: str, quantity: Decimal, order_type: str,
                                  quote_bid: Decimal, quote_ask: Decimal, quote_ts: str,
                                  risk_config_checksum: str, config_checksum: str,
                                  asset_class: str = "EQUITY",
                                  multiplier: Decimal = Decimal("1")) -> str:
    """Canonical full-request binding shared by the runtime and durable idempotency boundary."""
    q = _paper_exact_money(quantity, field="quantity", positive=True)
    bid = _paper_exact_money(quote_bid, field="quote_bid", positive=True)
    ask = _paper_exact_money(quote_ask, field="quote_ask", positive=True)
    mult = _paper_exact_money(multiplier, field="multiplier", positive=True)
    if not all(type(v) is str and v for v in (
        run_id, decision_id, client_order_id, instrument, side, order_type,
        risk_config_checksum, config_checksum, asset_class,
    )):
        raise ValueError("paper request string fields must be non-empty")
    payload = {
        "asset_class": asset_class,
        "client_order_id": client_order_id,
        "config_checksum": config_checksum,
        "decision_id": decision_id,
        "instrument": instrument,
        "multiplier": paper_canary_money_str(mult),
        "order_type": order_type,
        "quantity": paper_canary_money_str(q),
        "quote": {"ask": paper_canary_money_str(ask), "bid": paper_canary_money_str(bid),
                  "ts": _paper_timestamp(quote_ts, field="quote_ts")},
        "risk_config_checksum": risk_config_checksum,
        "run_id": run_id,
        "side": side,
        "tag": "atp.paper-canary.request.v1",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


# --------------------------------------------------------------------------- rows
@dataclass(frozen=True, slots=True)
class PaperCanaryRunRow:
    run_id: str
    status: str
    active_slot: int | None
    version: int
    config_json: str
    config_checksum: str
    risk_config_checksum: str
    commit_sha: str
    reason: str | None
    created_at: str
    started_at: str | None
    heartbeat_at: str | None
    ended_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class PaperCanaryAccountRow:
    run_id: str
    starting_cash: Decimal
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    version: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class PaperCanaryOrderRow:
    client_order_id: str
    run_id: str
    idempotency_key: str
    decision_id: str
    instrument: str
    side: str
    quantity: Decimal
    order_type: str
    state: str
    request_checksum: str
    risk_config_checksum: str
    quote_bid: Decimal
    quote_ask: Decimal
    quote_ts: str
    broker_order_id: str | None
    reason: str | None
    version: int
    correlation_id: str | None
    created_at: str
    authorized_at: str | None
    terminal_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class PaperCanaryFillRow:
    fill_id: str
    client_order_id: str
    broker_fill_id: str
    ledger_seq: int
    instrument: str
    side: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    multiplier: Decimal
    quote_ts: str
    ts: str


@dataclass(frozen=True, slots=True)
class PaperCanaryPositionRow:
    run_id: str
    instrument: str
    quantity: Decimal
    avg_price: Decimal
    mark_price: Decimal
    realized_pnl: Decimal
    version: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class PaperCanaryOrderEventRow:
    event_id: str
    client_order_id: str
    seq: int
    ts: str
    event_type: str
    previous_state: str | None
    new_state: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class PaperCanaryReconciliationRow:
    reconciliation_id: str
    run_id: str
    status: str
    fills_checksum: str
    positions_checksum: str
    account_checksum: str
    open_order_count: int
    breaks_json: str
    checked_at: str


@dataclass(frozen=True, slots=True)
class PaperCanaryFillCommitResult:
    order: PaperCanaryOrderRow
    fill: PaperCanaryFillRow
    account: PaperCanaryAccountRow
    position: PaperCanaryPositionRow


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


# --------------------------------------------------------------------------- § R3.1A intel/validation rows
@dataclass(slots=True)
class ResearchIntelSnapshotRow:
    snapshot_id: str
    universe_id: str
    universe_version: str
    sampling_policy_version: str
    outcome_policy_version: str
    symbol: str
    asset_class: str
    exchange: str
    currency: str
    exchange_tz: str
    calendar_id: str
    calendar_version: str
    scheduled_target_ts: str
    computation_started_ts: str
    decision_ts: str
    decision_session_date: str
    is_early_close: bool | None
    decision_price: str | None
    decision_price_source: str | None
    decision_price_provenance_status: str | None
    decision_price_bar_ts: str | None
    consensus_score: str | None
    consensus_direction: str | None
    consensus_confidence: str | None
    consensus_status: str | None
    governance_status: str | None
    governance_reasons_json: str | None
    data_completeness: str | None
    expected_outcome_contract_json: str
    adjustment_policy: str
    horizons_json: str
    inputs_checksum: str
    snapshot_checksum: str
    commit_sha: str
    supersedes_snapshot_id: str | None
    status: str
    created_at: str


@dataclass(slots=True)
class ResearchIntelInputRow:
    snapshot_id: str
    component_name: str
    canonical_value_json: str | None
    component_score: str | None
    component_status: str | None
    source_provider: str | None
    source_event_ts: str | None
    source_published_or_filed_ts: str | None
    source_observed_ts: str | None
    source_available_ts: str | None
    provenance_status: str
    missing_data_reason: str | None
    freshness_state: str | None
    created_at: str


@dataclass(slots=True)
class ResearchIntelOutcomeRow:
    snapshot_id: str
    horizon_sessions: int
    snapshot_checksum: str
    dataset_id: str | None
    dataset_checksum: str | None
    provider_contract_version: str | None
    adjustment_policy: str | None
    decision_bar_ts: str | None
    decision_price: str | None
    outcome_bar_ts: str | None
    outcome_price: str | None
    return_pct: str | None
    direction_expected: str | None
    direction_actual: str | None
    direction_correct: bool | None
    classification: str | None
    neutral_threshold_pct: str | None
    outcome_policy_version: str
    decision_price_bar_ts: str | None
    decision_price_reconciliation: str | None
    outcome_checksum: str | None
    status: str
    failure_code: str | None
    evaluation_ts: str
    commit_sha: str
    created_at: str


@dataclass(slots=True)
class ResearchValidationRunRow:
    run_id: str
    universe_id: str
    universe_version: str
    validation_policy_version: str
    outcome_policy_version: str
    sampling_policy_version: str
    gate_id: str
    snapshot_set_checksum: str | None
    outcome_set_checksum: str | None
    dataset_ids_json: str | None
    commit_sha: str
    result_checksum: str | None
    status: str
    gate_report_json: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    updated_at: str


# --------------------------------------------------------------------------- § WP2 instrument model rows
@dataclass(slots=True)
class InstrumentRow:
    instrument_id: str
    natural_key: str
    con_id: int | None
    isin: str | None
    figi: str | None
    cusip: str | None
    sedol: str | None
    local_symbol: str | None
    symbol: str
    description: str | None
    region: str | None
    country: str | None
    exchange: str
    primary_exchange: str | None
    trading_currency: str
    settlement_currency: str | None
    timezone: str | None
    trading_calendar: str | None
    calendar_version: str | None
    asset_class: str
    sub_class: str | None
    underlying_symbol: str | None
    underlying_instrument_id: str | None
    tick_size: str | None
    multiplier: str | None
    lot_size: str | None
    min_size: str | None
    expiry: str | None
    strike: str | None
    option_right: str | None
    tradability_status: str
    market_data_status: str
    source_status: str
    verification_status: str
    source: str | None
    last_verified_at: str | None
    content_checksum: str
    created_at: str
    updated_at: str
    # § WP3 — IBKR qualification lifecycle (NULL/default for a freshly imported instrument)
    qualification_status: str = "DISCOVERED"
    qualification_reason: str | None = None
    qualification_run_id: str | None = None
    qualification_attempts: int = 0
    last_qualified_at: str | None = None


@dataclass(slots=True)
class InstrumentImportRunRow:
    run_id: str
    request_checksum: str
    source_label: str
    planned_markets_json: str
    completed_markets_json: str
    failed_markets_json: str
    status: str
    discovered_count: int
    inserted_count: int
    updated_count: int
    unchanged_count: int
    skipped_count: int
    failed_market_count: int
    failure_code: str | None
    failure_reason: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    updated_at: str


@dataclass(slots=True)
class InstrumentImportEventRow:
    id: str
    run_id: str
    seq: int | None
    ts: str | None
    market: str | None
    event_type: str
    severity: str | None
    details_json: str | None
    created_at: str


# --------------------------------------------------------------------------- § WP3 IBKR qualification rows
@dataclass(slots=True)
class InstrumentQualificationRunRow:
    run_id: str
    request_checksum: str
    run_label: str
    exchange: str | None
    batch_size: int
    pause_seconds: str
    status: str
    planned_markets_json: str
    completed_markets_json: str
    failed_markets_json: str
    processed_count: int
    verified_count: int
    ambiguous_count: int
    not_tradable_count: int
    mdne_count: int
    error_retryable_count: int
    error_permanent_count: int
    failure_code: str | None
    failure_reason: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    updated_at: str


@dataclass(slots=True)
class InstrumentQualificationEventRow:
    id: str
    run_id: str
    seq: int | None
    ts: str | None
    instrument_id: str | None
    market: str | None
    event_type: str
    severity: str | None
    status: str | None
    con_id: int | None
    candidate_count: int | None
    reason: str | None
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
        """Persist one compare-and-swap runtime transition and its audit event atomically.

        Risk-increasing states re-check the durable kill latch while the runtime singleton is
        locked.  This closes the gap between a LifecycleManager read and the write: an emergency
        KILL that wins that race can never be overwritten by a stale ARM/START/READY transition.
        """
        cid = correlation_id or new_id()
        now = utcnow_iso()
        with self.tx() as cur:
            # SQLite has no FOR UPDATE.  A value-preserving DML takes its writer lock before the
            # read that drives this write; PostgreSQL locks the singleton row explicitly below.
            if self.MONEY_AS_TEXT:
                self._exec(cur, "UPDATE runtime_state SET updated_at=updated_at WHERE id=1")
            self._exec(
                cur,
                f"SELECT status FROM runtime_state WHERE id=1{self.LOCK_CLAUSE}",
            )
            row = cur.fetchone()
            actual_previous = row[0] if row else None
            if previous is not None and actual_previous != previous:
                raise PaperCanaryStateError(
                    f"runtime transition lost compare-and-swap: expected {previous}, "
                    f"found {actual_previous}",
                )

            if new_status in {"READY_FOR_ARM", "ARMED", "RUNNING"}:
                self._exec(
                    cur,
                    "INSERT INTO kill_switch (id,engaged,actor,reason,updated_at) "
                    "VALUES (1,0,NULL,NULL,?) ON CONFLICT(id) DO NOTHING",
                    (now,),
                )
                self._exec(
                    cur,
                    f"SELECT engaged FROM kill_switch WHERE id=1{self.LOCK_CLAUSE}",
                )
                kill = cur.fetchone()
                if not kill or bool(kill[0]):
                    raise PaperCanarySafetyError(
                        "runtime transition blocked: kill switch is engaged",
                    )

            evt = AuditEventRow(
                new_id(), now, actor, action or f"TRANSITION:{new_status}",
                actual_previous, new_status, reason, cid,
            )
            self._exec(cur,
                "INSERT INTO runtime_state (id,status,updated_at,correlation_id,reason) VALUES (1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, "
                "correlation_id=excluded.correlation_id, reason=excluded.reason, "
                "paper_commit_sha=CASE WHEN excluded.status IN ('DISABLED','KILLED','RECOVERY_REQUIRED') "
                "THEN NULL ELSE runtime_state.paper_commit_sha END, "
                "paper_config_checksum=CASE WHEN excluded.status IN "
                "('DISABLED','KILLED','RECOVERY_REQUIRED') THEN NULL "
                "ELSE runtime_state.paper_config_checksum END, "
                "paper_risk_config_checksum=CASE WHEN excluded.status IN "
                "('DISABLED','KILLED','RECOVERY_REQUIRED') THEN NULL "
                "ELSE runtime_state.paper_risk_config_checksum END, "
                "paper_prepared_at=CASE WHEN excluded.status IN "
                "('DISABLED','KILLED','RECOVERY_REQUIRED') THEN NULL "
                "ELSE runtime_state.paper_prepared_at END, "
                "paper_run_id=CASE WHEN excluded.status IN "
                "('DISABLED','KILLED','RECOVERY_REQUIRED') THEN NULL "
                "ELSE runtime_state.paper_run_id END",
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

    def list_orders(self) -> list[OrderRow]:
        """Read the complete durable legacy-order set for restart recovery."""
        rows = self._all(
            "SELECT client_order_id,idempotency_key,instrument,side,quantity,order_type,state,"
            "broker_order_id,correlation_id,reason,created_at,updated_at FROM orders "
            "ORDER BY created_at,client_order_id",
        )
        return [self._order_row(row) for row in rows]

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

    def upsert_md_health(self, *, symbol: str, source: str, status: str, latency_ms, ts: str,
                         quote_ts: str | None = None) -> None:
        with self.tx() as cur:
            self._exec(cur,
                "INSERT INTO market_data_health (symbol,source,status,latency_ms,updated_at,quote_ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET source=excluded.source, "
                "status=excluded.status, latency_ms=excluded.latency_ms, "
                "updated_at=excluded.updated_at, quote_ts=excluded.quote_ts",
                (symbol, source, status, latency_ms, ts, quote_ts))

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

    def rd_reclaim_stale_running(self, cutoff_iso: str, *, failure_code: str, failure_reason: str,
                                 _probe=None) -> list[str]:
        """R3.0A.2 — ATOMICALLY reclaim crashed/stale RUNNING datasets as FAILED. The `updated_at < cutoff`
        staleness predicate is RE-CHECKED at the actual terminal transition (not only when the candidate was
        first selected), so a legitimate worker that writes a fresh heartbeat before the flip is NOT reclaimed.
        The immutable RECLAIM event is written in the SAME transaction as the FAILED transition (event first,
        while the parent is still RUNNING → the child trigger allows it; then the guarded flip; if the flip
        matches 0 rows the whole tx rolls back, so the event is never left behind). Returns ONLY the ids that
        were actually transitioned. `_probe` is a test-only seam invoked per candidate before its tx (used to
        deterministically inject a concurrent heartbeat); production callers never pass it."""
        now = utcnow_iso()
        candidates = [r[0] for r in self._all("SELECT dataset_id FROM research_datasets WHERE status='RUNNING' "
                                              "AND updated_at < ?", (cutoff_iso,))]
        reclaimed: list[str] = []
        for ds_id in candidates:
            if _probe is not None:
                _probe(ds_id)                    # test seam: may write a fresh heartbeat via another conn
            try:
                with self.tx() as cur:
                    self._exec(cur, "INSERT INTO research_dataset_events (id,dataset_id,seq,ts,event_type,"
                               "severity,symbol,details_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                               (f"{ds_id}-reclaim", ds_id, None, now, "RECLAIM", "ERROR", None,
                                json.dumps({"failure_code": failure_code, "reason": failure_reason}), now))
                    self._exec(cur, "UPDATE research_datasets SET status='FAILED', failure_code=?, "
                               "failure_reason=?, ended_at=?, updated_at=? WHERE dataset_id=? AND "
                               "status='RUNNING' AND updated_at < ?",
                               (failure_code, failure_reason, now, now, ds_id, cutoff_iso))
                    if cur.rowcount <= 0:
                        raise _ReclaimSkipped()  # fresh heartbeat won → rollback (event undone), stays RUNNING
                reclaimed.append(ds_id)
            except _ReclaimSkipped:
                continue
        return reclaimed

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

    # ---- § R3.1A immutable point-in-time intelligence snapshots + outcomes + validation ----
    _RI_SNAP_COLS = (
        "snapshot_id,universe_id,universe_version,sampling_policy_version,outcome_policy_version,symbol,"
        "asset_class,exchange,currency,exchange_tz,calendar_id,calendar_version,scheduled_target_ts,"
        "computation_started_ts,decision_ts,decision_session_date,is_early_close,decision_price,"
        "decision_price_source,decision_price_provenance_status,decision_price_bar_ts,consensus_score,"
        "consensus_direction,consensus_confidence,consensus_status,governance_status,governance_reasons_json,"
        "data_completeness,expected_outcome_contract_json,adjustment_policy,horizons_json,inputs_checksum,"
        "snapshot_checksum,commit_sha,supersedes_snapshot_id,status,created_at")
    _RI_IN_COLS = (
        "snapshot_id,component_name,canonical_value_json,component_score,component_status,source_provider,"
        "source_event_ts,source_published_or_filed_ts,source_observed_ts,source_available_ts,"
        "provenance_status,missing_data_reason,freshness_state,created_at")
    _RI_OUT_COLS = (
        "snapshot_id,horizon_sessions,snapshot_checksum,dataset_id,dataset_checksum,provider_contract_version,"
        "adjustment_policy,decision_bar_ts,decision_price,outcome_bar_ts,outcome_price,return_pct,"
        "direction_expected,direction_actual,direction_correct,classification,neutral_threshold_pct,"
        "outcome_policy_version,decision_price_bar_ts,decision_price_reconciliation,outcome_checksum,status,"
        "failure_code,evaluation_ts,commit_sha,created_at")
    _RV_RUN_COLS = (
        "run_id,universe_id,universe_version,validation_policy_version,outcome_policy_version,"
        "sampling_policy_version,gate_id,snapshot_set_checksum,outcome_set_checksum,dataset_ids_json,"
        "commit_sha,result_checksum,status,gate_report_json,created_at,started_at,ended_at,updated_at")

    def ri_write_snapshot(self, *, snapshot: dict, inputs: list[dict], event: dict) -> bool:
        """ATOMIC forward-only write of one immutable snapshot + its canonical input envelope + a collection
        event, in ONE transaction. Idempotent: if the snapshot already exists (deterministic snapshot_id →
        one per symbol/session/policy) nothing is written and False is returned. The inputs FK + single
        transaction make an input/decision without a persisted snapshot impossible."""
        s, now = snapshot, utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO research_intel_snapshots ({self._RI_SNAP_COLS}) "
                       f"VALUES ({','.join(['?'] * 37)}) ON CONFLICT(snapshot_id) DO NOTHING",
                       (s["snapshot_id"], s["universe_id"], s["universe_version"], s["sampling_policy_version"],
                        s["outcome_policy_version"], s["symbol"], s["asset_class"], s["exchange"], s["currency"],
                        s["exchange_tz"], s["calendar_id"], s["calendar_version"], s["scheduled_target_ts"],
                        s["computation_started_ts"], s["decision_ts"], s["decision_session_date"],
                        (1 if s.get("is_early_close") else 0), s.get("decision_price"),
                        s.get("decision_price_source"), s.get("decision_price_provenance_status"),
                        s.get("decision_price_bar_ts"), s.get("consensus_score"), s.get("consensus_direction"),
                        s.get("consensus_confidence"), s.get("consensus_status"), s.get("governance_status"),
                        s.get("governance_reasons_json"), s.get("data_completeness"),
                        s["expected_outcome_contract_json"], s["adjustment_policy"], s["horizons_json"],
                        s["inputs_checksum"], s["snapshot_checksum"], s["commit_sha"],
                        s.get("supersedes_snapshot_id"), s.get("status", "COLLECTED"), now))
            if cur.rowcount <= 0:
                return False
            for c in inputs:
                self._exec(cur, f"INSERT INTO research_intel_snapshot_inputs ({self._RI_IN_COLS}) "
                           f"VALUES ({','.join(['?'] * 14)})",
                           (s["snapshot_id"], c["component_name"], c.get("canonical_value_json"),
                            c.get("component_score"), c.get("component_status"), c.get("source_provider"),
                            c.get("source_event_ts"), c.get("source_published_or_filed_ts"),
                            c.get("source_observed_ts"), c.get("source_available_ts"), c["provenance_status"],
                            c.get("missing_data_reason"), c.get("freshness_state"), now))
            self._add_intel_event(cur, event, now)
            return True

    def ri_add_event(self, event: dict) -> None:
        with self.tx() as cur:
            self._add_intel_event(cur, event, utcnow_iso())

    def _add_intel_event(self, cur, event: dict, now: str) -> None:
        self._exec(cur, "INSERT INTO research_intel_collection_events (id,snapshot_id,event_type,severity,ts,"
                   "symbol,session_date,details_json,commit_sha,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (event.get("id") or new_id(), event.get("snapshot_id"), event["event_type"],
                    event.get("severity"), event.get("ts") or now, event.get("symbol"),
                    event.get("session_date"), json.dumps(event.get("details") or {}),
                    event.get("commit_sha"), now))

    def _snap_row(self, r) -> ResearchIntelSnapshotRow:
        r = list(r)
        r[16] = None if r[16] is None else bool(r[16])   # is_early_close → bool
        return ResearchIntelSnapshotRow(*r)

    def ri_get_snapshot(self, snapshot_id: str) -> ResearchIntelSnapshotRow | None:
        r = self._one(f"SELECT {self._RI_SNAP_COLS} FROM research_intel_snapshots WHERE snapshot_id=?",
                      (snapshot_id,))
        return self._snap_row(r) if r else None

    def ri_list_snapshots(self, *, universe_id: str | None = None, symbol: str | None = None,
                          limit: int = 2000) -> list[ResearchIntelSnapshotRow]:
        n = max(1, min(20000, int(limit)))
        where, params = [], []
        if universe_id:
            where.append("universe_id=?"); params.append(universe_id)
        if symbol:
            where.append("symbol=?"); params.append(symbol.upper())
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(f"SELECT {self._RI_SNAP_COLS} FROM research_intel_snapshots{clause} "
                         "ORDER BY decision_session_date ASC, symbol ASC LIMIT ?", (*params, n))
        return [self._snap_row(r) for r in rows]

    def ri_list_inputs(self, snapshot_id: str) -> list[ResearchIntelInputRow]:
        rows = self._all(f"SELECT {self._RI_IN_COLS} FROM research_intel_snapshot_inputs WHERE snapshot_id=? "
                         "ORDER BY component_name ASC", (snapshot_id,))
        return [ResearchIntelInputRow(*r) for r in rows]

    def ri_write_outcome(self, outcome: dict) -> bool:
        """Write ONE immutable terminal outcome (MATURED|FAILED) for (snapshot, horizon). Idempotent (a row
        is written once; a matured/failed outcome is never rewritten). Never reads live `ohlc_bars`."""
        o, now = outcome, utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO research_intel_outcomes ({self._RI_OUT_COLS}) "
                       f"VALUES ({','.join(['?'] * 26)}) ON CONFLICT(snapshot_id,horizon_sessions) DO NOTHING",
                       (o["snapshot_id"], int(o["horizon_sessions"]), o["snapshot_checksum"], o.get("dataset_id"),
                        o.get("dataset_checksum"), o.get("provider_contract_version"), o.get("adjustment_policy"),
                        o.get("decision_bar_ts"), o.get("decision_price"), o.get("outcome_bar_ts"),
                        o.get("outcome_price"), o.get("return_pct"), o.get("direction_expected"),
                        o.get("direction_actual"),
                        (None if o.get("direction_correct") is None else (1 if o["direction_correct"] else 0)),
                        o.get("classification"), o.get("neutral_threshold_pct"), o["outcome_policy_version"],
                        o.get("decision_price_bar_ts"), o.get("decision_price_reconciliation"),
                        o.get("outcome_checksum"), o["status"], o.get("failure_code"), now, o["commit_sha"], now))
            return cur.rowcount > 0

    def _out_row(self, r) -> ResearchIntelOutcomeRow:
        r = list(r)
        r[14] = None if r[14] is None else bool(r[14])
        return ResearchIntelOutcomeRow(*r)

    def ri_list_outcomes(self, *, snapshot_id: str | None = None, status: str | None = None,
                         limit: int = 20000) -> list[ResearchIntelOutcomeRow]:
        n = max(1, min(200000, int(limit)))
        where, params = [], []
        if snapshot_id:
            where.append("snapshot_id=?"); params.append(snapshot_id)
        if status:
            where.append("status=?"); params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(f"SELECT {self._RI_OUT_COLS} FROM research_intel_outcomes{clause} LIMIT ?",
                         (*params, n))
        return [self._out_row(r) for r in rows]

    def ri_existing_outcome_keys(self) -> set:
        """(snapshot_id, horizon) pairs already terminal (MATURED|FAILED) — so the evaluator skips them."""
        return {(r[0], int(r[1])) for r in self._all(
            "SELECT snapshot_id,horizon_sessions FROM research_intel_outcomes")}

    def ri_count_events(self, event_type: str | None = None) -> int:
        if event_type:
            r = self._one("SELECT COUNT(*) FROM research_intel_collection_events WHERE event_type=?",
                          (event_type,))
        else:
            r = self._one("SELECT COUNT(*) FROM research_intel_collection_events")
        return int(r[0]) if r else 0

    # ---- validation runs ----
    def rv_create_run(self, *, run_id, universe_id, universe_version, validation_policy_version,
                      outcome_policy_version, sampling_policy_version, gate_id, commit_sha) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO research_validation_runs ({self._RV_RUN_COLS}) "
                       f"VALUES ({','.join(['?'] * 18)})",
                       (run_id, universe_id, universe_version, validation_policy_version, outcome_policy_version,
                        sampling_policy_version, gate_id, None, None, None, commit_sha, None, "RUNNING", None,
                        now, now, None, now))

    def rv_finalize_run(self, run_id: str, *, expected_from: str, status: str, snapshot_set_checksum=None,
                        outcome_set_checksum=None, dataset_ids_json=None, result_checksum=None,
                        gate_report_json=None, metrics: list[dict] = ()) -> bool:
        now = utcnow_iso()
        with self.tx() as cur:
            for m in metrics:
                self._exec(cur, "INSERT INTO research_validation_metrics (run_id,metric_group,metrics_json,"
                           "created_at) VALUES (?,?,?,?)", (run_id, m["metric_group"], m["metrics_json"], now))
            self._exec(cur, "UPDATE research_validation_runs SET status=?, snapshot_set_checksum=?, "
                       "outcome_set_checksum=?, dataset_ids_json=?, result_checksum=?, gate_report_json=?, "
                       "ended_at=?, updated_at=? WHERE run_id=? AND status=?",
                       (status, snapshot_set_checksum, outcome_set_checksum, dataset_ids_json, result_checksum,
                        gate_report_json, now, now, run_id, expected_from))
            return cur.rowcount > 0

    def rv_get_run(self, run_id: str) -> ResearchValidationRunRow | None:
        r = self._one(f"SELECT {self._RV_RUN_COLS} FROM research_validation_runs WHERE run_id=?", (run_id,))
        return ResearchValidationRunRow(*r) if r else None

    def rv_list_runs(self, limit: int = 50) -> list[ResearchValidationRunRow]:
        n = max(1, min(200, int(limit)))
        rows = self._all(f"SELECT {self._RV_RUN_COLS} FROM research_validation_runs "
                         "ORDER BY created_at DESC LIMIT ?", (n,))
        return [ResearchValidationRunRow(*r) for r in rows]

    def rv_list_metrics(self, run_id: str) -> list[tuple]:
        return self._all("SELECT metric_group,metrics_json FROM research_validation_metrics WHERE run_id=? "
                         "ORDER BY metric_group ASC", (run_id,))

    # ---- § WP2 unified persistent global-instrument model (REFERENCE DATA ONLY) ----
    # Idempotent, collision-safe instrument catalogue + resumable/observable import runs. No trading, no
    # orders/execution/broker, no market-data subscription, no IBKR qualification.
    _IM_MUTABLE_COLS = (
        "con_id", "isin", "figi", "cusip", "sedol", "local_symbol", "symbol", "description",
        "region", "country", "exchange", "primary_exchange", "trading_currency", "settlement_currency",
        "timezone", "trading_calendar", "calendar_version", "asset_class", "sub_class",
        "underlying_symbol", "underlying_instrument_id", "tick_size", "multiplier", "lot_size", "min_size",
        "expiry", "strike", "option_right", "tradability_status", "market_data_status",
        "source_status", "verification_status", "source", "last_verified_at", "content_checksum",
    )
    _IM_INSTR_COLS = ("instrument_id,natural_key," + ",".join(_IM_MUTABLE_COLS) + ",created_at,updated_at,"
                      "qualification_status,qualification_reason,qualification_run_id,"
                      "qualification_attempts,last_qualified_at")
    _IM_RUN_COLS = (
        "run_id,request_checksum,source_label,planned_markets_json,completed_markets_json,"
        "failed_markets_json,status,discovered_count,inserted_count,updated_count,unchanged_count,"
        "skipped_count,failed_market_count,failure_code,failure_reason,created_at,started_at,ended_at,updated_at"
    )
    _IM_EVENT_COLS = "id,run_id,seq,ts,market,event_type,severity,details_json,created_at"

    def im_upsert_instrument(self, record: dict) -> str:
        """Idempotent, collision-safe upsert keyed by the stable ``instrument_id`` (derived from the
        venue-anchored natural key). Returns 'inserted' | 'updated' | 'unchanged' (unchanged when the
        record's ``content_checksum`` already matches — a genuine no-op re-import). Atomic per call."""
        iid = record["instrument_id"]
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "SELECT content_checksum FROM instruments WHERE instrument_id=?", (iid,))
            existing = cur.fetchone()
            values = tuple(self._im_value(record, c) for c in self._IM_MUTABLE_COLS)
            if existing is None:
                cols = "instrument_id,natural_key," + ",".join(self._IM_MUTABLE_COLS) + ",created_at,updated_at"
                ph = ",".join(["?"] * (len(self._IM_MUTABLE_COLS) + 4))
                self._exec(cur, f"INSERT INTO instruments ({cols}) VALUES ({ph})",
                           (iid, record["natural_key"], *values, now, now))
                return "inserted"
            if existing[0] == record.get("content_checksum"):
                return "unchanged"
            set_clause = ",".join(f"{c}=?" for c in self._IM_MUTABLE_COLS) + ",updated_at=?"
            self._exec(cur, f"UPDATE instruments SET {set_clause} WHERE instrument_id=?",
                       (*values, now, iid))
            return "updated"

    @staticmethod
    def _im_value(record: dict, col: str):
        v = record.get(col)
        if col == "con_id":
            return None if v is None else int(v)
        return v

    def im_get_instrument(self, instrument_id: str) -> InstrumentRow | None:
        r = self._one(f"SELECT {self._IM_INSTR_COLS} FROM instruments WHERE instrument_id=?", (instrument_id,))
        return self._im_row(r) if r else None

    def im_get_by_natural_key(self, natural_key: str) -> InstrumentRow | None:
        r = self._one(f"SELECT {self._IM_INSTR_COLS} FROM instruments WHERE natural_key=?", (natural_key,))
        return self._im_row(r) if r else None

    @staticmethod
    def _im_row(r) -> InstrumentRow:
        r = list(r)
        r[2] = None if r[2] is None else int(r[2])   # con_id → int
        return InstrumentRow(*r)

    def im_list_instruments(self, *, asset_class: str | None = None, exchange: str | None = None,
                            region: str | None = None, verification_status: str | None = None,
                            limit: int = 100, offset: int = 0) -> list[InstrumentRow]:
        n, off = max(1, min(1000, int(limit))), max(0, int(offset))
        where, params = [], []
        if asset_class:
            where.append("asset_class=?"); params.append(asset_class)
        if exchange:
            where.append("exchange=?"); params.append(exchange)
        if region:
            where.append("region=?"); params.append(region)
        if verification_status:
            where.append("verification_status=?"); params.append(verification_status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._all(f"SELECT {self._IM_INSTR_COLS} FROM instruments{clause} "
                         "ORDER BY symbol ASC, exchange ASC, instrument_id ASC LIMIT ? OFFSET ?",
                         (*params, n, off))
        return [self._im_row(r) for r in rows]

    def im_count_instruments(self, *, asset_class: str | None = None, exchange: str | None = None) -> int:
        where, params = [], []
        if asset_class:
            where.append("asset_class=?"); params.append(asset_class)
        if exchange:
            where.append("exchange=?"); params.append(exchange)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        r = self._one(f"SELECT COUNT(*) FROM instruments{clause}", tuple(params))
        return int(r[0]) if r else 0

    def im_create_import_run(self, *, run_id: str, request_checksum: str, source_label: str,
                             planned_markets: list) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO instrument_import_runs ({self._IM_RUN_COLS}) "
                       f"VALUES ({','.join(['?'] * 19)})",
                       (run_id, request_checksum, source_label, json.dumps(list(planned_markets)),
                        json.dumps([]), json.dumps([]), "PLANNED", 0, 0, 0, 0, 0, 0, None, None,
                        now, None, None, now))

    def im_find_run_by_request_checksum(self, request_checksum: str):
        """Idempotency/resumability lookup → (completed_run_or_None, running_run_or_None). ``completed`` is a
        fully COMPLETED run — a true no-op, re-running is pointless; ``running`` is a RUNNING or PLANNED run
        to resume. PARTIAL and FAILED runs are intentionally NOT returned: a re-run starts a fresh attempt
        that re-imports every market idempotently (so previously-failed markets get retried, and already-
        imported ones simply resolve to 'unchanged')."""
        rows = self._all(f"SELECT {self._IM_RUN_COLS} FROM instrument_import_runs WHERE request_checksum=? "
                         "ORDER BY created_at DESC, run_id DESC", (request_checksum,))
        completed = running = None
        for raw in rows:
            row = InstrumentImportRunRow(*raw)
            if row.status == "COMPLETED" and completed is None:
                completed = row
            elif row.status in ("RUNNING", "PLANNED") and running is None:
                running = row
        return completed, running

    def im_advance_run_status(self, run_id: str, expected_from: str, to: str) -> bool:
        now = utcnow_iso()
        with self.tx() as cur:
            if to == "RUNNING":
                self._exec(cur, "UPDATE instrument_import_runs SET status=?, started_at=?, updated_at=? "
                           "WHERE run_id=? AND status=?", (to, now, now, run_id, expected_from))
            else:
                self._exec(cur, "UPDATE instrument_import_runs SET status=?, updated_at=? "
                           "WHERE run_id=? AND status=?", (to, now, run_id, expected_from))
            return cur.rowcount > 0

    def im_record_market_progress(self, run_id: str, *, market: str, market_status: str, counts: dict,
                                  event: dict | None = None) -> bool:
        """Persist one market's outcome AND its progress event in ONE transaction, guarded on the run still
        being RUNNING (so a reclaimed/terminal run is never mutated — returns False and writes nothing).
        ``market_status`` is 'COMPLETED' (→ completed list, removed from failed) or 'FAILED' (→ failed list).
        Run-level counters are incremented by ``counts``. Bumps updated_at as a liveness heartbeat."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "SELECT completed_markets_json,failed_markets_json,discovered_count,"
                       "inserted_count,updated_count,unchanged_count,skipped_count,failed_market_count "
                       "FROM instrument_import_runs WHERE run_id=? AND status='RUNNING'", (run_id,))
            row = cur.fetchone()
            if row is None:
                return False
            completed = list(json.loads(row[0]))
            failed = list(json.loads(row[1]))
            if market_status == "COMPLETED":
                if market not in completed:
                    completed.append(market)
                failed = [m for m in failed if m != market]
            elif market_status == "FAILED":
                if market not in failed:
                    failed.append(market)
            discovered = int(row[2]) + int(counts.get("discovered", 0))
            inserted = int(row[3]) + int(counts.get("inserted", 0))
            updated = int(row[4]) + int(counts.get("updated", 0))
            unchanged = int(row[5]) + int(counts.get("unchanged", 0))
            skipped = int(row[6]) + int(counts.get("skipped", 0))
            failed_markets = len(failed)
            self._exec(cur, "UPDATE instrument_import_runs SET completed_markets_json=?, failed_markets_json=?, "
                       "discovered_count=?, inserted_count=?, updated_count=?, unchanged_count=?, "
                       "skipped_count=?, failed_market_count=?, updated_at=? WHERE run_id=? AND status='RUNNING'",
                       (json.dumps(completed), json.dumps(failed), discovered, inserted, updated, unchanged,
                        skipped, failed_markets, now, run_id))
            if cur.rowcount <= 0:
                return False
            if event is not None:
                self._exec(cur, f"INSERT INTO instrument_import_events ({self._IM_EVENT_COLS}) "
                           "VALUES (?,?,?,?,?,?,?,?,?)",
                           (event["id"], run_id, event.get("seq"), event.get("ts", now), market,
                            event["event_type"], event.get("severity"),
                            json.dumps(event.get("details") or {}), now))
            return True

    def im_append_import_event(self, run_id: str, *, event_id: str, event_type: str, market: str | None = None,
                               seq: int | None = None, severity: str | None = None,
                               details: dict | None = None) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO instrument_import_events ({self._IM_EVENT_COLS}) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (event_id, run_id, seq, now, market, event_type, severity,
                        json.dumps(details or {}), now))

    def im_finalize_run(self, run_id: str, *, expected_from: str = "RUNNING", status: str,
                        failure_code: str | None = None, failure_reason: str | None = None) -> bool:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "UPDATE instrument_import_runs SET status=?, failure_code=?, failure_reason=?, "
                       "ended_at=?, updated_at=? WHERE run_id=? AND status=?",
                       (status, failure_code, failure_reason, now, now, run_id, expected_from))
            return cur.rowcount > 0

    def im_get_run(self, run_id: str) -> InstrumentImportRunRow | None:
        r = self._one(f"SELECT {self._IM_RUN_COLS} FROM instrument_import_runs WHERE run_id=?", (run_id,))
        return InstrumentImportRunRow(*r) if r else None

    def im_list_runs(self, *, status: str | None = None, limit: int = 50,
                     offset: int = 0) -> list[InstrumentImportRunRow]:
        n, off = max(1, min(200, int(limit))), max(0, int(offset))
        clause, params = "", []
        if status:
            clause, params = " WHERE status=?", [status]
        rows = self._all(f"SELECT {self._IM_RUN_COLS} FROM instrument_import_runs{clause} "
                         "ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?", (*params, n, off))
        return [InstrumentImportRunRow(*r) for r in rows]

    def im_list_run_events(self, run_id: str, *, limit: int = 500,
                           offset: int = 0) -> list[InstrumentImportEventRow]:
        n, off = max(1, min(2000, int(limit))), max(0, int(offset))
        rows = self._all(f"SELECT {self._IM_EVENT_COLS} FROM instrument_import_events WHERE run_id=? "
                         "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?", (run_id, n, off))
        return [InstrumentImportEventRow(*r) for r in rows]

    def im_reclaim_stale_running(self, cutoff_iso: str, *, failure_code: str, failure_reason: str,
                                 _probe=None) -> list[str]:
        """Atomically reclaim crashed/stale RUNNING import runs as FAILED. The ``updated_at < cutoff``
        staleness predicate is RE-CHECKED at the terminal flip, so a run that writes a fresh heartbeat first
        is NOT reclaimed. The immutable RECLAIM event is written in the SAME transaction as the FAILED
        transition (event first, while the run is still RUNNING → allowed; then the guarded flip; if it
        matches 0 rows the whole tx rolls back). Returns only the ids actually transitioned."""
        now = utcnow_iso()
        candidates = [r[0] for r in self._all(
            "SELECT run_id FROM instrument_import_runs WHERE status='RUNNING' AND updated_at < ?",
            (cutoff_iso,))]
        reclaimed: list[str] = []
        for run_id in candidates:
            if _probe is not None:
                _probe(run_id)
            try:
                with self.tx() as cur:
                    self._exec(cur, f"INSERT INTO instrument_import_events ({self._IM_EVENT_COLS}) "
                               "VALUES (?,?,?,?,?,?,?,?,?)",
                               (f"{run_id}-reclaim", run_id, None, now, None, "RECLAIM", "ERROR",
                                json.dumps({"failure_code": failure_code, "reason": failure_reason}), now))
                    self._exec(cur, "UPDATE instrument_import_runs SET status='FAILED', failure_code=?, "
                               "failure_reason=?, ended_at=?, updated_at=? WHERE run_id=? AND "
                               "status='RUNNING' AND updated_at < ?",
                               (failure_code, failure_reason, now, now, run_id, cutoff_iso))
                    if cur.rowcount <= 0:
                        raise _ReclaimSkipped()
                reclaimed.append(run_id)
            except _ReclaimSkipped:
                continue
        return reclaimed

    # ---- § WP3 read-only IBKR qualification of the persistent instrument catalogue ----
    _IQ_RUN_COLS = (
        "run_id,request_checksum,run_label,exchange,batch_size,pause_seconds,status,"
        "planned_markets_json,completed_markets_json,failed_markets_json,processed_count,verified_count,"
        "ambiguous_count,not_tradable_count,mdne_count,error_retryable_count,error_permanent_count,"
        "failure_code,failure_reason,created_at,started_at,ended_at,updated_at"
    )
    _IQ_EVENT_COLS = ("id,run_id,seq,ts,instrument_id,market,event_type,severity,status,con_id,"
                      "candidate_count,reason,details_json,created_at")

    def _iq_insert_event(self, cur, run_id: str, event: dict, now: str) -> None:
        self._exec(cur, f"INSERT INTO instrument_qualification_events ({self._IQ_EVENT_COLS}) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (event["id"], run_id, event.get("seq"), event.get("ts", now), event.get("instrument_id"),
                    event.get("market"), event["event_type"], event.get("severity"), event.get("status"),
                    (None if event.get("con_id") is None else int(event["con_id"])),
                    event.get("candidate_count"), event.get("reason"),
                    json.dumps(event.get("details") or {}), now))

    def iq_select_instruments(self, *, statuses, exchange: str | None = None,
                              limit: int = 500) -> list[InstrumentRow]:
        n = max(1, min(5000, int(limit)))
        status_list = list(statuses) or ["DISCOVERED"]
        ph = ",".join(["?"] * len(status_list))
        where = f"qualification_status IN ({ph})"
        params = list(status_list)
        if exchange:
            where += " AND exchange=?"
            params.append(exchange)
        rows = self._all(f"SELECT {self._IM_INSTR_COLS} FROM instruments WHERE {where} "
                         "ORDER BY exchange ASC, instrument_id ASC LIMIT ?", (*params, n))
        return [self._im_row(r) for r in rows]

    def iq_find_instrument_by_conid(self, con_id: int) -> InstrumentRow | None:
        r = self._one(f"SELECT {self._IM_INSTR_COLS} FROM instruments WHERE con_id=?", (int(con_id),))
        return self._im_row(r) if r else None

    def iq_mark_pending(self, instrument_id: str, run_id: str) -> int:
        """Claim an instrument for qualification (→ QUALIFICATION_PENDING) and return its CURRENT attempt
        count. Attempts are incremented only when an outcome is actually recorded (see iq_apply_outcome), so
        a crash between the claim and the outcome burns no attempt — a resumed run does not over-escalate."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "UPDATE instruments SET qualification_status='QUALIFICATION_PENDING', "
                       "qualification_run_id=?, updated_at=? WHERE instrument_id=?",
                       (run_id, now, instrument_id))
            self._exec(cur, "SELECT qualification_attempts FROM instruments WHERE instrument_id=?",
                       (instrument_id,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def iq_max_event_seq(self, run_id: str) -> int:
        """Highest event seq recorded for a run (0 if none). Seeds the next event id on resume so a gap left
        by a rolled-back outcome (or a very large run) never collides with an already-persisted event id."""
        r = self._one("SELECT MAX(seq) FROM instrument_qualification_events WHERE run_id=?", (run_id,))
        return int(r[0]) if r and r[0] is not None else 0

    def iq_apply_outcome(self, instrument_id: str, *, run_id: str, qualification_status: str, reason: str,
                         verification_status: str | None = None, tradability_status: str | None = None,
                         market_data_status: str | None = None, con_id=None, set_last_verified: bool = False,
                         count_attempt: bool = True, event: dict) -> None:
        """Persist one instrument's qualification outcome AND its audit event in ONE transaction, and bump the
        run's liveness heartbeat. Increments the instrument's attempt count when ``count_attempt`` is True
        (attempts track genuine RECORDED qualification attempts, not claims). A broker-outage outcome
        (ConnectionUnavailableError) passes ``count_attempt=False`` so an IBKR outage never consumes an
        instrument's retry budget. Fail-closed: coarse fields (verification/tradability/market_data/con_id/
        last_verified_at) are written only when explicitly provided by the caller for a proven outcome. The
        run's per-status counters are derived authoritatively at finalize, so resume never double-counts."""
        now = utcnow_iso()
        sets = ["qualification_status=?", "qualification_reason=?", "qualification_run_id=?",
                "last_qualified_at=?", "updated_at=?"]
        if count_attempt:
            sets.append("qualification_attempts=qualification_attempts+1")
        params = [qualification_status, reason, run_id, now, now]
        if verification_status is not None:
            sets.append("verification_status=?"); params.append(verification_status)
        if tradability_status is not None:
            sets.append("tradability_status=?"); params.append(tradability_status)
        if market_data_status is not None:
            sets.append("market_data_status=?"); params.append(market_data_status)
        if con_id is not None:
            sets.append("con_id=?"); params.append(int(con_id))
        if set_last_verified:
            sets.append("last_verified_at=?"); params.append(now)
        params.append(instrument_id)
        with self.tx() as cur:
            self._exec(cur, f"UPDATE instruments SET {','.join(sets)} WHERE instrument_id=?", tuple(params))
            self._iq_insert_event(cur, run_id, event, now)
            self._exec(cur, "UPDATE instrument_qualification_runs SET updated_at=? "
                       "WHERE run_id=? AND status='RUNNING'", (now, run_id))

    def iq_create_run(self, *, run_id: str, request_checksum: str, run_label: str, exchange: str | None,
                      batch_size: int, pause_seconds: float) -> None:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, f"INSERT INTO instrument_qualification_runs ({self._IQ_RUN_COLS}) "
                       f"VALUES ({','.join(['?'] * 23)})",
                       (run_id, request_checksum, run_label, exchange, int(batch_size), str(pause_seconds),
                        "PLANNED", json.dumps([]), json.dumps([]), json.dumps([]), 0, 0, 0, 0, 0, 0, 0,
                        None, None, now, None, None, now))

    def iq_advance_run_status(self, run_id: str, expected_from: str, to: str) -> bool:
        now = utcnow_iso()
        with self.tx() as cur:
            if to == "RUNNING":
                self._exec(cur, "UPDATE instrument_qualification_runs SET status=?, started_at=?, updated_at=? "
                           "WHERE run_id=? AND status=?", (to, now, now, run_id, expected_from))
            else:
                self._exec(cur, "UPDATE instrument_qualification_runs SET status=?, updated_at=? "
                           "WHERE run_id=? AND status=?", (to, now, run_id, expected_from))
            return cur.rowcount > 0

    def iq_set_planned_markets(self, run_id: str, markets) -> bool:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "UPDATE instrument_qualification_runs SET planned_markets_json=?, updated_at=? "
                       "WHERE run_id=? AND status='RUNNING'", (json.dumps(list(markets)), now, run_id))
            return cur.rowcount > 0

    def iq_record_market(self, run_id: str, *, market: str, market_status: str, event: dict | None = None) -> bool:
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "SELECT completed_markets_json,failed_markets_json FROM "
                       "instrument_qualification_runs WHERE run_id=? AND status='RUNNING'", (run_id,))
            row = cur.fetchone()
            if row is None:
                return False
            completed = list(json.loads(row[0]))
            failed = list(json.loads(row[1]))
            if market_status == "COMPLETED":
                if market not in completed:
                    completed.append(market)
                failed = [m for m in failed if m != market]
            elif market_status == "FAILED":
                if market not in failed:
                    failed.append(market)
                completed = [m for m in completed if m != market]   # a market is never both
            self._exec(cur, "UPDATE instrument_qualification_runs SET completed_markets_json=?, "
                       "failed_markets_json=?, updated_at=? WHERE run_id=? AND status='RUNNING'",
                       (json.dumps(completed), json.dumps(failed), now, run_id))
            if cur.rowcount <= 0:
                return False
            if event is not None:
                self._iq_insert_event(cur, run_id, event, now)
            return True

    def iq_finalize_run(self, run_id: str, *, expected_from: str = "RUNNING", status: str,
                        failure_code: str | None = None, failure_reason: str | None = None) -> bool:
        """Finalize a run, deriving the per-status counters AUTHORITATIVELY from the instruments this run
        last touched (grouped by qualification_status). Deriving-not-accumulating makes the counts exact and
        immune to resume/re-processing double-counting; the snapshot is then frozen with the terminal flip."""
        now = utcnow_iso()
        with self.tx() as cur:
            self._exec(cur, "SELECT qualification_status, COUNT(*) FROM instruments "
                       "WHERE qualification_run_id=? GROUP BY qualification_status", (run_id,))
            counts = {row[0]: int(row[1]) for row in cur.fetchall()}
            v = counts.get("VERIFIED", 0)
            a = counts.get("AMBIGUOUS", 0)
            nt = counts.get("NOT_TRADABLE", 0)
            md = counts.get("MARKET_DATA_NOT_ENTITLED", 0)
            er = counts.get("ERROR_RETRYABLE", 0)
            ep = counts.get("ERROR_PERMANENT", 0)
            processed = v + a + nt + md + er + ep
            self._exec(cur, "UPDATE instrument_qualification_runs SET status=?, verified_count=?, "
                       "ambiguous_count=?, not_tradable_count=?, mdne_count=?, error_retryable_count=?, "
                       "error_permanent_count=?, processed_count=?, failure_code=?, failure_reason=?, "
                       "ended_at=?, updated_at=? WHERE run_id=? AND status=?",
                       (status, v, a, nt, md, er, ep, processed, failure_code, failure_reason, now, now,
                        run_id, expected_from))
            return cur.rowcount > 0

    def iq_get_run(self, run_id: str) -> InstrumentQualificationRunRow | None:
        r = self._one(f"SELECT {self._IQ_RUN_COLS} FROM instrument_qualification_runs WHERE run_id=?",
                      (run_id,))
        return InstrumentQualificationRunRow(*r) if r else None

    def iq_list_runs(self, *, status: str | None = None, limit: int = 50,
                     offset: int = 0) -> list[InstrumentQualificationRunRow]:
        n, off = max(1, min(200, int(limit))), max(0, int(offset))
        clause, params = "", []
        if status:
            clause, params = " WHERE status=?", [status]
        rows = self._all(f"SELECT {self._IQ_RUN_COLS} FROM instrument_qualification_runs{clause} "
                         "ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?", (*params, n, off))
        return [InstrumentQualificationRunRow(*r) for r in rows]

    def iq_list_run_events(self, run_id: str, *, limit: int = 500,
                           offset: int = 0) -> list[InstrumentQualificationEventRow]:
        n, off = max(1, min(5000, int(limit))), max(0, int(offset))
        rows = self._all(f"SELECT {self._IQ_EVENT_COLS} FROM instrument_qualification_events WHERE run_id=? "
                         "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?", (run_id, n, off))
        return [InstrumentQualificationEventRow(*r) for r in rows]

    def iq_reclaim_stale_running(self, cutoff_iso: str, *, failure_code: str, failure_reason: str,
                                 _probe=None) -> list[str]:
        """Atomically reclaim crashed/stale RUNNING qualification runs as FAILED, re-checking the staleness
        predicate at the terminal flip (a fresh heartbeat is not reclaimed). Instruments left
        QUALIFICATION_PENDING remain selectable, so the next run re-qualifies them."""
        now = utcnow_iso()
        candidates = [r[0] for r in self._all(
            "SELECT run_id FROM instrument_qualification_runs WHERE status='RUNNING' AND updated_at < ?",
            (cutoff_iso,))]
        reclaimed: list[str] = []
        for run_id in candidates:
            if _probe is not None:
                _probe(run_id)
            try:
                with self.tx() as cur:
                    self._exec(cur, f"INSERT INTO instrument_qualification_events ({self._IQ_EVENT_COLS}) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (f"{run_id}-reclaim", run_id, None, now, None, None, "RECLAIM", "ERROR", None,
                                None, None, failure_reason,
                                json.dumps({"failure_code": failure_code, "reason": failure_reason}), now))
                    # best-effort: snapshot the counts this crashed run managed to produce, so a reclaimed
                    # run's summary reflects what it actually did rather than defaulting to all-zero.
                    self._exec(cur, "SELECT qualification_status, COUNT(*) FROM instruments "
                               "WHERE qualification_run_id=? GROUP BY qualification_status", (run_id,))
                    c = {row[0]: int(row[1]) for row in cur.fetchall()}
                    v = c.get("VERIFIED", 0); a = c.get("AMBIGUOUS", 0); nt = c.get("NOT_TRADABLE", 0)
                    md = c.get("MARKET_DATA_NOT_ENTITLED", 0); er = c.get("ERROR_RETRYABLE", 0)
                    ep = c.get("ERROR_PERMANENT", 0)
                    self._exec(cur, "UPDATE instrument_qualification_runs SET status='FAILED', verified_count=?, "
                               "ambiguous_count=?, not_tradable_count=?, mdne_count=?, error_retryable_count=?, "
                               "error_permanent_count=?, processed_count=?, failure_code=?, failure_reason=?, "
                               "ended_at=?, updated_at=? WHERE run_id=? AND status='RUNNING' AND updated_at < ?",
                               (v, a, nt, md, er, ep, v + a + nt + md + er + ep, failure_code, failure_reason,
                                now, now, run_id, cutoff_iso))
                    if cur.rowcount <= 0:
                        raise _ReclaimSkipped()
                reclaimed.append(run_id)
            except _ReclaimSkipped:
                continue
        return reclaimed

    # -- durable PAPER-canary lifecycle + ledger -------------------------
    _PAPER_RUN_COLS = (
        "run_id,status,active_slot,version,config_json,config_checksum,risk_config_checksum,"
        "commit_sha,reason,created_at,started_at,heartbeat_at,ended_at,updated_at"
    )
    _PAPER_ACCOUNT_COLS = (
        "run_id,starting_cash,cash,equity,realized_pnl,gross_exposure,net_exposure,version,updated_at"
    )
    _PAPER_ORDER_COLS = (
        "client_order_id,run_id,idempotency_key,decision_id,instrument,side,quantity,order_type,state,"
        "request_checksum,risk_config_checksum,quote_bid,quote_ask,quote_ts,broker_order_id,reason,"
        "version,correlation_id,created_at,authorized_at,terminal_at,updated_at"
    )
    _PAPER_FILL_COLS = (
        "fill_id,client_order_id,broker_fill_id,ledger_seq,instrument,side,quantity,price,commission,"
        "multiplier,quote_ts,ts"
    )
    _PAPER_POSITION_COLS = (
        "run_id,instrument,quantity,avg_price,mark_price,realized_pnl,version,updated_at"
    )
    _PAPER_EVENT_COLS = (
        "event_id,client_order_id,seq,ts,event_type,previous_state,new_state,reason"
    )
    _PAPER_RECON_COLS = (
        "reconciliation_id,run_id,status,fills_checksum,positions_checksum,account_checksum,"
        "open_order_count,breaks_json,checked_at"
    )
    _PAPER_ACTIVE_RUN_STATES = frozenset({
        "CREATED", "READY_FOR_ARM", "RUNNING", "RECOVERY_REQUIRED",
    })
    _PAPER_TERMINAL_RUN_STATES = frozenset({"STOPPED", "FAILED", "COMPLETED"})
    _PAPER_RUN_TRANSITIONS = {
        "CREATED": frozenset({"READY_FOR_ARM", "STOPPED", "FAILED"}),
        "READY_FOR_ARM": frozenset({"RUNNING", "STOPPED", "FAILED"}),
        "RUNNING": frozenset({"RECOVERY_REQUIRED", "STOPPED", "FAILED", "COMPLETED"}),
        "RECOVERY_REQUIRED": frozenset({"READY_FOR_ARM", "STOPPED", "FAILED"}),
        "STOPPED": frozenset(),
        "FAILED": frozenset(),
        "COMPLETED": frozenset(),
    }
    _PAPER_ORDER_TRANSITIONS = {
        "INTENT": frozenset({"AUTHORIZED", "REJECTED", "CANCELLED"}),
        "AUTHORIZED": frozenset({"REJECTED", "CANCELLED"}),
    }

    @staticmethod
    def _paper_run_row(row) -> PaperCanaryRunRow:
        values = list(row)
        values[2] = None if values[2] is None else int(values[2])
        values[3] = int(values[3])
        return PaperCanaryRunRow(*values)

    @staticmethod
    def _paper_account_row(row) -> PaperCanaryAccountRow:
        values = list(row)
        for index in range(1, 7):
            values[index] = to_decimal(values[index])
        values[7] = int(values[7])
        return PaperCanaryAccountRow(*values)

    @staticmethod
    def _paper_order_row(row) -> PaperCanaryOrderRow:
        values = list(row)
        values[6] = to_decimal(values[6])
        values[11] = to_decimal(values[11])
        values[12] = to_decimal(values[12])
        values[16] = int(values[16])
        return PaperCanaryOrderRow(*values)

    @staticmethod
    def _paper_fill_row(row) -> PaperCanaryFillRow:
        values = list(row)
        values[3] = int(values[3])
        for index in range(6, 10):
            values[index] = to_decimal(values[index])
        return PaperCanaryFillRow(*values)

    @staticmethod
    def _paper_position_row(row) -> PaperCanaryPositionRow:
        values = list(row)
        for index in range(2, 6):
            values[index] = to_decimal(values[index])
        values[6] = int(values[6])
        return PaperCanaryPositionRow(*values)

    @staticmethod
    def _paper_event_row(row) -> PaperCanaryOrderEventRow:
        values = list(row)
        values[2] = int(values[2])
        return PaperCanaryOrderEventRow(*values)

    @staticmethod
    def _paper_reconciliation_row(row) -> PaperCanaryReconciliationRow:
        values = list(row)
        values[6] = int(values[6])
        return PaperCanaryReconciliationRow(*values)

    @staticmethod
    def _paper_cap(value, *, field: str) -> Decimal:
        if type(value) is not str:
            raise ValueError(f"config {field} must be a canonical decimal string")
        try:
            parsed = Decimal(str(value))
        except Exception as exc:
            raise ValueError(f"config {field} is not a decimal") from exc
        exact = _paper_exact_money(parsed, field=field, positive=True)
        if value != paper_canary_money_str(exact):
            raise ValueError(f"config {field} is not canonical 8dp money")
        return exact

    @staticmethod
    def _paper_config_amount(value, *, field: str, positive: bool = False) -> Decimal:
        if type(value) is not str:
            raise ValueError(f"config {field} must be a canonical decimal string")
        try:
            parsed = Decimal(value)
        except Exception as exc:
            raise ValueError(f"config {field} is not a decimal") from exc
        exact = _paper_exact_money(
            parsed, field=field, positive=positive, nonnegative=not positive,
        )
        if value != paper_canary_money_str(exact):
            raise ValueError(f"config {field} is not canonical 8dp money")
        return exact

    @classmethod
    def _paper_config(cls, config_json) -> tuple[dict, str, str]:
        config, canonical = _paper_json(config_json, field="config_json")
        required = frozenset({
            "asset_class", "commission_per_unit", "instrument", "max_daily_turnover",
            "max_gross_notional", "max_order_notional", "max_orders", "min_commission",
            "mode", "quote_max_age_s", "slippage_bps", "starting_cash", "tag",
        })
        if frozenset(config) != required:
            raise ValueError("config_json has an invalid durable Paper Canary shape")
        if config["tag"] != "atp.paper-canary.config.v1" or config["mode"] != "paper":
            raise ValueError("config_json has an invalid Paper Canary scope tag/mode")
        instrument = config.get("instrument")
        if (
            type(instrument) is not str
            or not instrument
            or instrument != instrument.strip()
            or instrument != instrument.upper()
            or len(instrument) > 128
            or any(ord(character) < 32 for character in instrument)
        ):
            raise ValueError("config instrument must be one non-empty uppercase symbol")
        if config.get("asset_class", "EQUITY") != "EQUITY":
            raise ValueError("Paper Canary supports only EQUITY")
        for name in (
            "max_order_notional", "max_gross_notional", "max_daily_turnover",
        ):
            cls._paper_cap(config.get(name), field=name)
        starting_cash = cls._paper_config_amount(
            config.get("starting_cash"), field="starting_cash", positive=True,
        )
        commission_per_unit = cls._paper_config_amount(
            config.get("commission_per_unit"), field="commission_per_unit",
        )
        min_commission = cls._paper_config_amount(
            config.get("min_commission"), field="min_commission",
        )
        slippage_bps = cls._paper_config_amount(config.get("slippage_bps"), field="slippage_bps")
        cls._paper_config_amount(config.get("quote_max_age_s"), field="quote_max_age_s", positive=True)
        max_orders = config.get("max_orders")
        if type(max_orders) is not int or max_orders <= 0:
            raise ValueError("config max_orders must be a positive integer")
        if cls._paper_cap(config["max_order_notional"], field="max_order_notional") > cls._paper_cap(
            config["max_gross_notional"], field="max_gross_notional",
        ):
            raise ValueError("max_order_notional cannot exceed max_gross_notional")
        if cls._paper_cap(config["max_gross_notional"], field="max_gross_notional") > starting_cash:
            raise ValueError("max_gross_notional cannot exceed starting_cash")
        if slippage_bps >= Decimal("10000"):
            raise ValueError("slippage_bps must be below 10000")
        if commission_per_unit < 0 or min_commission < 0:  # defensive after exact validation
            raise ValueError("commission values must be nonnegative")
        checksum = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        return config, canonical, checksum

    def _paper_risk_checksum_in_tx(self, cur) -> str:
        self._exec(
            cur,
            "SELECT capital,risk_per_trade_pct,max_daily_loss_pct,updated_at "
            f"FROM risk_config WHERE id=1{self.LOCK_CLAUSE}",
        )
        risk = cur.fetchone()
        if not risk:
            raise PaperCanarySafetyError("canonical risk_config is missing")
        self._exec(
            cur,
            "SELECT risk_config_id,currency,warning_threshold_pct,max_portfolio_exposure_pct,max_drawdown_pct,"
            f"config_version FROM risk_control_policy WHERE id='policy'{self.LOCK_CLAUSE}",
        )
        policy = cur.fetchone()
        capital = to_decimal(risk[0])
        risk_per_trade = to_decimal(risk[1])
        max_daily_loss = to_decimal(risk[2])
        try:
            capital = _paper_exact_money(capital, field="risk capital", positive=True)
            risk_per_trade = _paper_exact_money(
                risk_per_trade, field="risk_per_trade_pct", positive=True,
            )
            max_daily_loss = _paper_exact_money(
                max_daily_loss, field="max_daily_loss_pct", positive=True,
            )
        except (TypeError, ValueError) as exc:
            raise PaperCanarySafetyError("canonical risk_config is not exact 8dp money") from exc
        if (
            capital is None
            or risk_per_trade is None
            or max_daily_loss is None
            or not all(value.is_finite() for value in (capital, risk_per_trade, max_daily_loss))
            or capital <= 0
            or not Decimal("0") < risk_per_trade <= max_daily_loss <= Decimal("100")
        ):
            raise PaperCanarySafetyError("canonical risk_config is incomplete or out of bounds")
        if (
            not policy
            or int(policy[0]) != 1
            or type(policy[1]) is not str
            or not policy[1]
            or policy[1] != policy[1].strip()
            or any(policy[index] is None for index in (2, 3, 4))
            or int(policy[5]) <= 0
        ):
            raise PaperCanarySafetyError("risk_control_policy is incomplete")
        warning = to_decimal(policy[2])
        exposure = to_decimal(policy[3])
        drawdown = to_decimal(policy[4])
        try:
            warning = _paper_exact_money(warning, field="warning_threshold_pct", positive=True)
            exposure = _paper_exact_money(
                exposure, field="max_portfolio_exposure_pct", positive=True,
            )
            drawdown = _paper_exact_money(drawdown, field="max_drawdown_pct", positive=True)
        except (TypeError, ValueError) as exc:
            raise PaperCanarySafetyError("risk_control_policy is not exact 8dp money") from exc
        if (
            warning is None
            or exposure is None
            or drawdown is None
            or not all(value.is_finite() for value in (warning, exposure, drawdown))
            or not Decimal("0") < warning <= Decimal("100")
            or not Decimal("0") < exposure <= Decimal("100")
            or not Decimal("0") < drawdown <= Decimal("100")
        ):
            raise PaperCanarySafetyError("risk_control_policy percentages are out of bounds")
        return risk_config_token(
            capital=capital,
            risk_per_trade_pct=risk_per_trade,
            max_daily_loss_pct=max_daily_loss,
            rc_updated_at=risk[3],
            config_version=int(policy[5]),
            currency=policy[1],
            warning_threshold_pct=warning,
            max_portfolio_exposure_pct=exposure,
            max_drawdown_pct=drawdown,
        )

    def _paper_risk_capital_in_tx(self, cur) -> Decimal:
        """Read the already transaction-locked canonical risk capital as exact Paper money."""
        self._exec(
            cur,
            f"SELECT capital FROM risk_config WHERE id=1{self.LOCK_CLAUSE}",
        )
        row = cur.fetchone()
        if not row:
            raise PaperCanarySafetyError("canonical risk_config is missing")
        try:
            return _paper_exact_money(
                to_decimal(row[0]), field="risk capital", positive=True,
            )
        except (TypeError, ValueError) as exc:
            raise PaperCanarySafetyError("canonical risk capital is invalid") from exc

    def _paper_serialize_sqlite_write(self, cur) -> None:
        """Acquire SQLite's single-writer lock before any read that drives a later write.

        PostgreSQL uses the explicit row locks on the selected run/order/runtime rows. SQLite has
        no `FOR UPDATE`; without an early write, two deferred read transactions can deadlock while
        upgrading. This value-preserving singleton update makes one writer wait before it observes
        idempotency or ledger state.
        """
        if self.MONEY_AS_TEXT:
            self._exec(cur, "UPDATE risk_config SET updated_at=updated_at WHERE id=1")
            if cur.rowcount != 1:
                raise PaperCanarySafetyError("canonical risk_config is missing")

    def current_paper_risk_config_checksum(self) -> str:
        with self.tx() as cur:
            return self._paper_risk_checksum_in_tx(cur)

    def prepare_paper_runtime(
        self,
        *,
        config_json: str,
        commit_sha: str,
        expected_config_checksum: str,
        expected_risk_config_checksum: str,
        actor: str,
        reason: str,
    ) -> dict:
        """Atomically validate the PAPER-only pre-arm boundary and move DISABLED -> READY_FOR_ARM.

        This is deliberately narrower than the generic lifecycle primitive.  It binds the transition
        to the exact deployed commit, canonical Paper Canary config and full Risk Control token; proves
        the kill/risk/daily-loss/market-data boundary; initializes only a missing healthy risk baseline;
        and writes the transition plus its audit event in the same transaction.  It never arms, starts,
        creates a run, writes P&L, or touches an order/fill/broker path.
        """
        if type(commit_sha) is not str or re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
            raise PaperCanarySafetyError("deployed commit must be an exact 40-lowerhex SHA")
        if type(expected_config_checksum) is not str or re.fullmatch(
            r"sha256:[0-9a-f]{64}", expected_config_checksum,
        ) is None:
            raise PaperCanarySafetyError("expected Paper Canary config checksum is invalid")
        if type(expected_risk_config_checksum) is not str or re.fullmatch(
            r"[0-9a-f]{20}", expected_risk_config_checksum,
        ) is None:
            raise PaperCanarySafetyError("expected Risk Control checksum is invalid")
        if type(actor) is not str or not actor or type(reason) is not str or not reason:
            raise PaperCanarySafetyError("prepare actor and reason must be non-empty strings")

        config, canonical_config, config_checksum = self._paper_config(config_json)
        if canonical_config != config_json or config_checksum != expected_config_checksum:
            raise PaperCanarySafetyError("Paper Canary server config binding changed")
        started_at = datetime.fromisoformat(
            _paper_timestamp(utcnow_iso(), field="paper prepare time"),
        )
        today = started_at.date().isoformat()

        with self.tx() as cur:
            # SQLite has no FOR UPDATE.  Take its single-writer lock before any read that drives a write.
            self._paper_serialize_sqlite_write(cur)

            self._exec(
                cur,
                f"SELECT status FROM runtime_state WHERE id=1{self.LOCK_CLAUSE}",
            )
            runtime = cur.fetchone()
            if not runtime or runtime[0] != "DISABLED":
                raise PaperCanaryStateError("Paper Canary prepare requires global DISABLED")

            self._exec(
                cur,
                "SELECT run_id,status FROM paper_canary_runs WHERE active_slot=1"
                + self.LOCK_CLAUSE,
            )
            if cur.fetchall():
                raise PaperCanaryStateError("an active Paper Canary run requires recovery or stop")

            # Materialize the default-off latch before locking it.  On PostgreSQL, SELECT FOR UPDATE
            # cannot lock a missing row; this insert makes a concurrent first-ever KILL serialize on
            # the singleton instead of racing the READY transition.
            self._exec(
                cur,
                "INSERT INTO kill_switch (id,engaged,actor,reason,updated_at) "
                "VALUES (1,0,NULL,NULL,?) ON CONFLICT(id) DO NOTHING",
                (started_at.isoformat(),),
            )
            self._exec(
                cur,
                f"SELECT engaged FROM kill_switch WHERE id=1{self.LOCK_CLAUSE}",
            )
            kill = cur.fetchone()
            if not kill or bool(kill[0]):
                raise PaperCanarySafetyError("kill switch is engaged")

            risk_checksum = self._paper_risk_checksum_in_tx(cur)
            if risk_checksum != expected_risk_config_checksum:
                raise PaperCanarySafetyError("Risk Control configuration changed before prepare")
            risk_capital = self._paper_risk_capital_in_tx(cur)
            starting_cash = self._paper_config_amount(
                config["starting_cash"], field="starting_cash", positive=True,
            )
            if starting_cash > risk_capital:
                raise PaperCanarySafetyError(
                    "Paper Canary starting_cash exceeds canonical risk capital",
                )

            self._exec(
                cur,
                "INSERT INTO risk_state (id,day_start_equity,peak_equity,halted,killed,updated_at) "
                "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (self._m(risk_capital), self._m(risk_capital), 0, 0, started_at.isoformat()),
            )
            initialized = cur.rowcount == 1
            self._exec(
                cur,
                "SELECT day_start_equity,peak_equity,halted,killed,updated_at FROM risk_state WHERE id=1"
                + self.LOCK_CLAUSE,
            )
            risk_state = cur.fetchone()
            if risk_state is None:
                raise PaperCanarySafetyError("durable risk state is missing")
            try:
                day_start = _paper_exact_money(
                    to_decimal(risk_state[0]), field="risk_state.day_start_equity", positive=True,
                )
                peak = _paper_exact_money(
                    to_decimal(risk_state[1]), field="risk_state.peak_equity", positive=True,
                )
            except (TypeError, ValueError) as exc:
                raise PaperCanarySafetyError("durable risk state is invalid") from exc
            if bool(risk_state[2]) or bool(risk_state[3]) or peak < day_start:
                raise PaperCanarySafetyError("durable risk state is halted, killed, or inconsistent")
            try:
                risk_ts = datetime.fromisoformat(str(risk_state[4]).replace("Z", "+00:00"))
                if risk_ts.tzinfo is None:
                    raise ValueError
                risk_day = risk_ts.astimezone(timezone.utc).date().isoformat()
            except Exception as exc:
                raise PaperCanarySafetyError("durable risk-state timestamp is invalid") from exc
            if risk_day != today:
                raise PaperCanarySafetyError("durable risk state is not from the current UTC day")

            self._exec(
                cur,
                "INSERT INTO daily_loss_lock (trade_date,engaged,reason,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(trade_date) DO NOTHING",
                (today, 0, "Paper Canary pre-arm baseline", started_at.isoformat()),
            )
            self._exec(
                cur,
                "SELECT engaged FROM daily_loss_lock WHERE trade_date=?" + self.LOCK_CLAUSE,
                (today,),
            )
            daily_lock = cur.fetchone()
            if not daily_lock or bool(daily_lock[0]):
                raise PaperCanarySafetyError("daily loss lock is engaged")

            # A canary account is reset for every run; the loss budget is not.  Materialize and
            # lock the one UTC-day aggregate while the canonical Risk row is locked, then require
            # its immutable capital baseline to match that canonical authority.  This makes a
            # second run inherit the first run's loss instead of silently receiving a fresh budget.
            self._exec(
                cur,
                "INSERT INTO paper_daily_loss_state "
                "(trade_date,risk_capital_baseline,cumulative_equity_delta,version,updated_at) "
                "VALUES (?,?,?,0,?) ON CONFLICT(trade_date) DO NOTHING",
                (today, self._m(risk_capital), self._m(Decimal("0")), started_at.isoformat()),
            )
            self._exec(
                cur,
                "SELECT risk_capital_baseline,cumulative_equity_delta,version "
                "FROM paper_daily_loss_state WHERE trade_date=?" + self.LOCK_CLAUSE,
                (today,),
            )
            paper_daily = cur.fetchone()
            if not paper_daily:
                raise PaperCanarySafetyError("durable Paper daily-loss aggregate is missing")
            try:
                paper_daily_capital = _paper_exact_money(
                    to_decimal(paper_daily[0]),
                    field="paper_daily_loss_state.risk_capital_baseline",
                    positive=True,
                )
                paper_daily_delta = _paper_exact_money(
                    to_decimal(paper_daily[1]),
                    field="paper_daily_loss_state.cumulative_equity_delta",
                )
            except (TypeError, ValueError) as exc:
                raise PaperCanarySafetyError("durable Paper daily-loss aggregate is invalid") from exc
            if paper_daily_capital != risk_capital:
                raise PaperCanarySafetyError(
                    "durable Paper daily-loss capital baseline changed during the UTC day",
                )
            self._exec(
                cur,
                f"SELECT max_daily_loss_pct FROM risk_config WHERE id=1{self.LOCK_CLAUSE}",
            )
            max_loss_row = cur.fetchone()
            try:
                max_loss_pct = _paper_exact_money(
                    to_decimal(max_loss_row[0]) if max_loss_row else None,
                    field="max_daily_loss_pct",
                    positive=True,
                )
            except (TypeError, ValueError) as exc:
                raise PaperCanarySafetyError("canonical daily loss limit is invalid") from exc
            paper_daily_limit = paper_daily_capital * max_loss_pct / Decimal(100)
            if max(Decimal("0"), -paper_daily_delta) >= paper_daily_limit:
                raise PaperCanarySafetyError("durable Paper daily loss limit is exhausted")

            self._exec(
                cur,
                "SELECT day_start_equity,realized_pnl,unrealized_pnl FROM daily_pnl WHERE trade_date=?"
                + self.LOCK_CLAUSE,
                (today,),
            )
            pnl = cur.fetchone()
            pnl_observed = pnl is not None
            if pnl is not None:
                try:
                    observed_equity = _paper_exact_money(
                        to_decimal(pnl[0]), field="daily_pnl.day_start_equity", positive=True,
                    )
                    realized = _paper_exact_money(
                        to_decimal(pnl[1]), field="daily_pnl.realized_pnl",
                    )
                    unrealized = _paper_exact_money(
                        to_decimal(pnl[2]), field="daily_pnl.unrealized_pnl",
                    )
                except (TypeError, ValueError) as exc:
                    raise PaperCanarySafetyError("daily P&L state is invalid") from exc
                if observed_equity <= 0:  # defensive after exact validation
                    raise PaperCanarySafetyError("daily P&L equity is invalid")
                if observed_equity != day_start:
                    raise PaperCanarySafetyError(
                        "daily P&L and durable risk-state baselines are inconsistent",
                    )
                daily_limit = risk_capital * max_loss_pct / Decimal(100)
                if max(Decimal(0), -(realized + unrealized)) >= daily_limit:
                    raise PaperCanarySafetyError("daily loss limit is exhausted")

            instrument = config["instrument"]
            self._exec(
                cur,
                "SELECT source,status,updated_at,quote_ts FROM market_data_health WHERE symbol=?"
                + self.LOCK_CLAUSE,
                (instrument,),
            )
            md = cur.fetchone()
            if not md or md[0] != "MASSIVE" or md[1] != "READY":
                raise PaperCanarySafetyError("Paper Canary market data is not MASSIVE/READY")
            checked_at = datetime.fromisoformat(
                _paper_timestamp(utcnow_iso(), field="paper prepare checked_at"),
            )
            if checked_at.date().isoformat() != today:
                raise PaperCanarySafetyError("UTC trading day changed during Paper Canary prepare")
            try:
                health_ts = datetime.fromisoformat(str(md[2]).replace("Z", "+00:00"))
                quote_ts = datetime.fromisoformat(str(md[3]).replace("Z", "+00:00"))
                if health_ts.tzinfo is None or quote_ts.tzinfo is None:
                    raise ValueError
                health_ts = health_ts.astimezone(timezone.utc)
                quote_ts = quote_ts.astimezone(timezone.utc)
                health_age = Decimal(str((checked_at - health_ts).total_seconds()))
                quote_age = Decimal(str((checked_at - quote_ts).total_seconds()))
            except Exception as exc:
                raise PaperCanarySafetyError("Paper Canary market-data timestamps are invalid") from exc
            max_age = self._paper_config_amount(
                config["quote_max_age_s"], field="quote_max_age_s", positive=True,
            )
            if (
                health_age < 0
                or health_age > max_age
                or quote_age < 0
                or quote_age > max_age
                or health_ts < quote_ts
            ):
                raise PaperCanarySafetyError(
                    "Paper Canary quote/health is stale, future-dated, or inconsistent",
                )

            cid = new_id()
            prepared_at = checked_at.isoformat()
            audit_reason = (
                f"{reason}; commit={commit_sha}; config={config_checksum}; risk={risk_checksum}; "
                f"daily_pnl={'observed' if pnl_observed else 'no-data'}"
            )
            event = AuditEventRow(
                new_id(), prepared_at, actor, "PAPER_CANARY_READY", "DISABLED", "READY_FOR_ARM",
                audit_reason, cid,
            )
            self._exec(
                cur,
                "UPDATE runtime_state SET status=?,updated_at=?,correlation_id=?,reason=?,"
                "paper_commit_sha=?,paper_config_checksum=?,paper_risk_config_checksum=?,"
                "paper_prepared_at=?,paper_run_id=NULL "
                "WHERE id=1 AND status='DISABLED'",
                (
                    "READY_FOR_ARM", prepared_at, cid, audit_reason, commit_sha, config_checksum,
                    risk_checksum, prepared_at,
                ),
            )
            if cur.rowcount != 1:
                raise PaperCanaryStateError("Paper Canary prepare lost the runtime state race")
            self._insert_audit(cur, event)
            return {
                "status": "READY_FOR_ARM",
                "commit_sha": commit_sha,
                "config_checksum": config_checksum,
                "risk_config_checksum": risk_checksum,
                "risk_state_initialized": initialized,
                "daily_pnl_observed": pnl_observed,
                "market_data_symbol": instrument,
            }

    def get_paper_runtime_binding(self) -> dict | None:
        row = self._one(
            "SELECT status,paper_commit_sha,paper_config_checksum,paper_risk_config_checksum,"
            "paper_prepared_at,paper_run_id FROM runtime_state WHERE id=1",
        )
        if row is None:
            return None
        return {
            "status": row[0],
            "commit_sha": row[1],
            "config_checksum": row[2],
            "risk_config_checksum": row[3],
            "prepared_at": row[4],
            "run_id": row[5],
        }

    def disable_paper_runtime_if_no_active(
        self, *, actor: str, reason: str, expected_run_id: str | None = None,
    ) -> dict:
        """Atomically move the safe global runtime to DISABLED only when no Paper run is active.

        The runtime singleton is the serialization point shared with prepared run creation. On
        PostgreSQL this row lock prevents an owner create from committing between the active-run
        check and disable; SQLite takes its writer lock before the check. KILLED and recovery states
        remain explicit and cannot be reset through this risk-reducing endpoint.
        """
        if type(actor) is not str or not actor or type(reason) is not str or not reason:
            raise PaperCanarySafetyError("disable actor and reason must be non-empty strings")
        if expected_run_id is not None and (
            type(expected_run_id) is not str or not expected_run_id
        ):
            raise PaperCanarySafetyError("expected_run_id must be None or a non-empty string")
        now = utcnow_iso()
        with self.tx() as cur:
            if self.MONEY_AS_TEXT:
                self._exec(
                    cur,
                    "UPDATE runtime_state SET updated_at=updated_at WHERE id=1",
                )
                if cur.rowcount != 1:
                    raise PaperCanaryStateError("global runtime state is missing")
            self._exec(
                cur,
                "SELECT status,paper_run_id FROM runtime_state WHERE id=1"
                + self.LOCK_CLAUSE,
            )
            runtime = cur.fetchone()
            if not runtime:
                raise PaperCanaryStateError("global runtime state is missing")
            previous = runtime[0]
            bound_run_id = runtime[1]
            if previous not in {"DISABLED", "READY_FOR_ARM", "ARMED", "RUNNING", "HALTED"}:
                raise PaperCanaryStateError(
                    f"global Paper disable is not allowed from {previous}",
                )
            if not (previous == "DISABLED" and bound_run_id is None):
                if expected_run_id is None:
                    if bound_run_id is not None:
                        raise PaperCanarySafetyError(
                            "run-less disable is allowed only before any Paper run consumed the binding",
                        )
                elif bound_run_id != expected_run_id:
                    raise PaperCanarySafetyError(
                        "Paper disable proof does not match the run bound to this runtime",
                    )

            self._exec(
                cur,
                "INSERT INTO kill_switch (id,engaged,actor,reason,updated_at) "
                "VALUES (1,0,NULL,NULL,?) ON CONFLICT(id) DO NOTHING",
                (now,),
            )
            self._exec(
                cur,
                f"SELECT engaged FROM kill_switch WHERE id=1{self.LOCK_CLAUSE}",
            )
            kill = cur.fetchone()
            if not kill or bool(kill[0]):
                raise PaperCanarySafetyError("kill switch is engaged; manual RESET is required")

            self._exec(
                cur,
                "SELECT run_id FROM paper_canary_runs WHERE active_slot=1" + self.LOCK_CLAUSE,
            )
            if cur.fetchall():
                raise PaperCanaryStateError("an active Paper Canary run prevents global disable")
            if previous == "DISABLED":
                return {"status": "DISABLED", "previous_status": previous, "changed": False}

            cid = new_id()
            event = AuditEventRow(
                new_id(), now, actor, "PAPER_CANARY_DISABLE", previous, "DISABLED", reason, cid,
            )
            self._exec(
                cur,
                "UPDATE runtime_state SET status='DISABLED',updated_at=?,correlation_id=?,reason=?,"
                "paper_commit_sha=NULL,paper_config_checksum=NULL,paper_risk_config_checksum=NULL,"
                "paper_prepared_at=NULL,paper_run_id=NULL WHERE id=1 AND status=?",
                (now, cid, reason, previous),
            )
            if cur.rowcount != 1:
                raise PaperCanaryStateError("global Paper disable lost the runtime state race")
            self._insert_audit(cur, event)
            return {"status": "DISABLED", "previous_status": previous, "changed": True}

    def create_paper_run(self, *, run_id: str, config_json, risk_config_checksum: str,
                         commit_sha: str, starting_cash: Decimal,
                         status: str = "CREATED", reason: str | None = None,
                         require_prepared: bool = False) -> PaperCanaryRunRow:
        if not all(type(value) is str and value for value in (run_id, risk_config_checksum, commit_sha)):
            raise ValueError("run_id, risk_config_checksum, and commit_sha must be non-empty strings")
        if status not in {"CREATED", "READY_FOR_ARM"}:
            raise PaperCanaryStateError("new run must start CREATED or READY_FOR_ARM")
        if type(require_prepared) is not bool:
            raise TypeError("require_prepared must be an exact bool")
        cash = _paper_exact_money(starting_cash, field="starting_cash", positive=True)
        _, canonical, config_checksum = self._paper_config(config_json)
        config, _, _ = self._paper_config(canonical)
        if self._paper_config_amount(
            config["starting_cash"], field="starting_cash", positive=True,
        ) != cash:
            raise PaperCanaryConflict("starting_cash does not match the immutable config snapshot")
        now = utcnow_iso()
        with self.tx() as cur:
            self._paper_serialize_sqlite_write(cur)
            # Every creation path serializes on the runtime singleton first. This is the common
            # PostgreSQL lock order shared with prepare/disable/fill and also prevents an
            # unprepared direct Store caller from appearing after a successful no-active disable.
            self._exec(
                cur,
                "SELECT status,paper_commit_sha,paper_config_checksum,"
                "paper_risk_config_checksum,paper_prepared_at,paper_run_id "
                "FROM runtime_state WHERE id=1"
                + self.LOCK_CLAUSE,
            )
            prepared = cur.fetchone()
            if not prepared or prepared[0] != "RUNNING":
                raise PaperCanarySafetyError("global runtime is not RUNNING")

            self._exec(
                cur,
                "INSERT INTO kill_switch (id,engaged,actor,reason,updated_at) "
                "VALUES (1,0,NULL,NULL,?) ON CONFLICT(id) DO NOTHING",
                (now,),
            )
            self._exec(
                cur,
                f"SELECT engaged FROM kill_switch WHERE id=1{self.LOCK_CLAUSE}",
            )
            kill = cur.fetchone()
            if not kill or bool(kill[0]):
                raise PaperCanarySafetyError("kill switch is engaged")

            if require_prepared:
                if (
                    prepared[1] != commit_sha
                    or prepared[2] != config_checksum
                    or prepared[3] != risk_config_checksum
                    or not prepared[4]
                ):
                    raise PaperCanarySafetyError(
                        "global runtime is not RUNNING with the exact prepared Paper binding",
                    )
                try:
                    prepared_dt = datetime.fromisoformat(
                        str(prepared[4]).replace("Z", "+00:00"),
                    )
                    if prepared_dt.tzinfo is None:
                        raise ValueError
                    prepared_day = prepared_dt.astimezone(timezone.utc).date()
                except (TypeError, ValueError) as exc:
                    raise PaperCanarySafetyError(
                        "prepared Paper binding timestamp is invalid",
                    ) from exc
                if prepared_day != datetime.fromisoformat(now).date():
                    raise PaperCanarySafetyError(
                        "prepared Paper binding is from another UTC trading day",
                    )
            elif any(prepared[index] is not None for index in range(1, 5)):
                raise PaperCanarySafetyError(
                    "a prepared Paper runtime requires the exact prepared creation path",
                )

            if prepared[5] not in {None, run_id}:
                raise PaperCanarySafetyError("Paper runtime binding was consumed by another run")
            self._exec(
                cur,
                "UPDATE runtime_state SET paper_run_id=? "
                "WHERE id=1 AND (paper_run_id IS NULL OR paper_run_id=?)",
                (run_id, run_id),
            )
            if cur.rowcount != 1:
                raise PaperCanarySafetyError("Paper runtime binding was consumed by another run")

            # A retry locks its existing run before risk rows. Intent and fill use the same
            # run-before-risk order, eliminating the run/risk cycle while the runtime singleton
            # continues to serialize create/disable/fill globally.
            self._exec(
                cur,
                f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs "
                f"WHERE run_id=?{self.LOCK_CLAUSE}",
                (run_id,),
            )
            raw_run = cur.fetchone()
            if self._paper_risk_checksum_in_tx(cur) != risk_config_checksum:
                raise PaperCanarySafetyError("risk configuration changed before run creation")
            if cash > self._paper_risk_capital_in_tx(cur):
                raise PaperCanarySafetyError(
                    "Paper Canary starting_cash exceeds canonical risk capital",
                )
            created = False
            if raw_run is None:
                self._exec(
                    cur,
                    "INSERT INTO paper_canary_runs "
                    "(run_id,status,active_slot,version,config_json,config_checksum,risk_config_checksum,"
                    "commit_sha,reason,created_at,started_at,heartbeat_at,ended_at,updated_at) "
                    "VALUES (?,?,1,0,?,?,?,?,?,?,NULL,NULL,NULL,?) ON CONFLICT DO NOTHING",
                    (
                        run_id, status, canonical, config_checksum, risk_config_checksum,
                        commit_sha, reason, now, now,
                    ),
                )
                created = cur.rowcount == 1
                self._exec(
                    cur,
                    f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs "
                    f"WHERE run_id=?{self.LOCK_CLAUSE}",
                    (run_id,),
                )
                raw_run = cur.fetchone()
            if not raw_run:
                raise PaperCanaryConflict("another active Paper Canary run already owns the slot")
            run = self._paper_run_row(raw_run)
            if created:
                zero = self._m(Decimal("0"))
                self._exec(
                    cur,
                    "INSERT INTO paper_accounts "
                    "(run_id,starting_cash,cash,equity,realized_pnl,gross_exposure,net_exposure,version,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,0,?)",
                    (run_id, self._m(cash), self._m(cash), self._m(cash), zero, zero, zero, now),
                )
            self._exec(cur, f"SELECT {self._PAPER_ACCOUNT_COLS} FROM paper_accounts WHERE run_id=?",
                       (run_id,))
            raw_account = cur.fetchone()
            if (
                run.status != status
                or run.config_json != canonical
                or run.config_checksum != config_checksum
                or run.risk_config_checksum != risk_config_checksum
                or run.commit_sha != commit_sha
                or run.reason != reason
                or not raw_account
                or self._paper_account_row(raw_account).starting_cash != cash
            ):
                raise PaperCanaryConflict("run_id already exists with different immutable content")
            return run

    def get_paper_run(self, run_id: str) -> PaperCanaryRunRow | None:
        row = self._one(f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs WHERE run_id=?", (run_id,))
        return self._paper_run_row(row) if row else None

    def list_paper_runs(self, *, status: str | None = None,
                        limit: int = 100) -> list[PaperCanaryRunRow]:
        count = max(1, min(1000, int(limit)))
        if status is None:
            rows = self._all(
                f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs "
                "ORDER BY created_at DESC,run_id DESC LIMIT ?", (count,),
            )
        else:
            rows = self._all(
                f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs WHERE status=? "
                "ORDER BY created_at DESC,run_id DESC LIMIT ?", (status, count),
            )
        return [self._paper_run_row(row) for row in rows]

    def transition_paper_run(self, *, run_id: str, expected_status: str,
                             expected_version: int, new_status: str,
                             reason: str | None = None) -> PaperCanaryRunRow:
        if new_status not in self._PAPER_RUN_TRANSITIONS.get(expected_status, frozenset()):
            raise PaperCanaryStateError(f"invalid Paper Canary transition {expected_status}->{new_status}")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        active_slot = 1 if new_status in self._PAPER_ACTIVE_RUN_STATES else None
        now = utcnow_iso()
        terminal = new_status in self._PAPER_TERMINAL_RUN_STATES
        with self.tx() as cur:
            self._exec(
                cur,
                "UPDATE paper_canary_runs SET status=?,active_slot=?,version=version+1,reason=?,"
                "started_at=CASE WHEN ?='RUNNING' THEN COALESCE(started_at,?) ELSE started_at END,"
                "heartbeat_at=CASE WHEN ?='RUNNING' THEN ? ELSE heartbeat_at END,"
                "ended_at=CASE WHEN ?=1 THEN ? ELSE ended_at END,updated_at=? "
                "WHERE run_id=? AND status=? AND version=?",
                (new_status, active_slot, reason, new_status, now, new_status, now,
                 1 if terminal else 0, now, now, run_id, expected_status, expected_version),
            )
            if cur.rowcount != 1:
                raise PaperCanaryStateError("run status/version compare-and-swap failed")
            self._exec(cur, f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs WHERE run_id=?",
                       (run_id,))
            return self._paper_run_row(cur.fetchone())

    def get_paper_account(self, run_id: str) -> PaperCanaryAccountRow | None:
        row = self._one(f"SELECT {self._PAPER_ACCOUNT_COLS} FROM paper_accounts WHERE run_id=?", (run_id,))
        return self._paper_account_row(row) if row else None

    def _paper_append_order_event(self, cur, *, client_order_id: str, event_type: str,
                                  previous_state: str | None, new_state: str | None,
                                  reason: str | None, now: str) -> PaperCanaryOrderEventRow:
        self._exec(cur, "SELECT COALESCE(MAX(seq),0) FROM paper_order_events WHERE client_order_id=?",
                   (client_order_id,))
        seq = int(cur.fetchone()[0]) + 1
        event_id = "pce_" + hashlib.sha256(
            f"{client_order_id}\0{seq}\0{event_type}".encode()
        ).hexdigest()[:24]
        self._exec(
            cur,
            "INSERT INTO paper_order_events "
            "(event_id,client_order_id,seq,ts,event_type,previous_state,new_state,reason) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (event_id, client_order_id, seq, now, event_type, previous_state, new_state, reason),
        )
        return PaperCanaryOrderEventRow(
            event_id, client_order_id, seq, now, event_type, previous_state, new_state, reason,
        )

    def get_or_create_paper_intent(
        self, *, run_id: str, idempotency_key: str, decision_id: str, instrument: str,
        side: str, quantity: Decimal, quote_bid: Decimal, quote_ask: Decimal, quote_ts: str,
        risk_config_checksum: str, correlation_id: str | None = None,
        client_order_id: str | None = None, order_type: str = "MARKET",
    ) -> PaperCanaryOrderRow:
        if not all(type(value) is str and value for value in (
            run_id, idempotency_key, decision_id, instrument, side, risk_config_checksum,
        )):
            raise ValueError("paper intent identity fields must be non-empty strings")
        if correlation_id is not None and (type(correlation_id) is not str or not correlation_id):
            raise ValueError("correlation_id must be None or a non-empty string")
        if side not in {"BUY", "SELL"} or order_type != "MARKET":
            raise ValueError("Paper Canary supports BUY/SELL MARKET orders only")
        qty = _paper_exact_money(quantity, field="quantity", positive=True)
        bid = _paper_exact_money(quote_bid, field="quote_bid", positive=True)
        ask = _paper_exact_money(quote_ask, field="quote_ask", positive=True)
        if ask < bid:
            raise ValueError("quote_ask must be greater than or equal to quote_bid")
        quote_time = _paper_timestamp(quote_ts, field="quote_ts")
        order_id = client_order_id or (
            "pco_" + hashlib.sha256(f"{run_id}\0{idempotency_key}".encode()).hexdigest()[:24]
        )
        if type(order_id) is not str or not order_id:
            raise ValueError("client_order_id must be a non-empty string")
        now = utcnow_iso()
        with self.tx() as cur:
            self._paper_serialize_sqlite_write(cur)
            self._exec(
                cur,
                f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs WHERE run_id=?{self.LOCK_CLAUSE}",
                (run_id,),
            )
            raw_run = cur.fetchone()
            if not raw_run:
                raise PaperCanaryStateError("Paper Canary run does not exist")
            run = self._paper_run_row(raw_run)
            if run.status != "RUNNING" or run.active_slot != 1:
                raise PaperCanaryStateError("Paper Canary run is not active RUNNING")
            config, canonical_config, config_checksum = self._paper_config(run.config_json)
            if canonical_config != run.config_json or config_checksum != run.config_checksum:
                raise PaperCanarySafetyError("Paper Canary config snapshot was altered")
            if instrument != config["instrument"]:
                raise PaperCanarySafetyError("order instrument is outside the one-instrument run config")
            current_risk = self._paper_risk_checksum_in_tx(cur)
            if risk_config_checksum != run.risk_config_checksum or current_risk != run.risk_config_checksum:
                raise PaperCanarySafetyError("risk configuration token does not match the run snapshot")
            request_checksum = paper_canary_request_checksum(
                run_id=run_id,
                decision_id=decision_id,
                client_order_id=order_id,
                instrument=instrument,
                side=side,
                quantity=qty,
                order_type=order_type,
                quote_bid=bid,
                quote_ask=ask,
                quote_ts=quote_time,
                risk_config_checksum=risk_config_checksum,
                config_checksum=config_checksum,
                asset_class=config.get("asset_class", "EQUITY"),
            )
            self._exec(
                cur,
                "INSERT INTO paper_orders "
                "(client_order_id,run_id,idempotency_key,decision_id,instrument,side,quantity,order_type,"
                "state,request_checksum,risk_config_checksum,quote_bid,quote_ask,quote_ts,broker_order_id,"
                "reason,version,correlation_id,created_at,authorized_at,terminal_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'INTENT',?,?,?,?,?,NULL,NULL,0,?,?,NULL,NULL,?) "
                "ON CONFLICT DO NOTHING",
                (order_id, run_id, idempotency_key, decision_id, instrument, side, self._m(qty), order_type,
                 request_checksum, risk_config_checksum, self._m(bid), self._m(ask), quote_time,
                 correlation_id, now, now),
            )
            created = cur.rowcount == 1
            if created:
                self._paper_append_order_event(
                    cur,
                    client_order_id=order_id,
                    event_type="INTENT",
                    previous_state=None,
                    new_state="INTENT",
                    reason=None,
                    now=now,
                )
            self._exec(
                cur,
                f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders "
                "WHERE client_order_id=? OR idempotency_key=? OR (run_id=? AND decision_id=?) "
                f"ORDER BY client_order_id{self.LOCK_CLAUSE}",
                (order_id, idempotency_key, run_id, decision_id),
            )
            matches = cur.fetchall()
            if len(matches) != 1:
                raise PaperCanaryConflict("intent identities resolve to conflicting durable orders")
            order = self._paper_order_row(matches[0])
            expected = (
                order.client_order_id == order_id
                and order.run_id == run_id
                and order.idempotency_key == idempotency_key
                and order.decision_id == decision_id
                and order.instrument == instrument
                and order.side == side
                and order.quantity == qty
                and order.order_type == order_type
                and order.request_checksum == request_checksum
                and order.risk_config_checksum == risk_config_checksum
                and order.quote_bid == bid
                and order.quote_ask == ask
                and order.quote_ts == quote_time
                and order.correlation_id == correlation_id
            )
            if not expected:
                raise PaperCanaryConflict("idempotency key or decision was reused with another request")
            return order

    def get_paper_order(self, client_order_id: str) -> PaperCanaryOrderRow | None:
        row = self._one(
            f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders WHERE client_order_id=?",
            (client_order_id,),
        )
        return self._paper_order_row(row) if row else None

    def list_paper_orders(self, *, run_id: str, state: str | None = None,
                          limit: int = 1000) -> list[PaperCanaryOrderRow]:
        count = max(1, min(10000, int(limit)))
        if state is None:
            rows = self._all(
                f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders WHERE run_id=? "
                "ORDER BY created_at,client_order_id LIMIT ?", (run_id, count),
            )
        else:
            rows = self._all(
                f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders WHERE run_id=? AND state=? "
                "ORDER BY created_at,client_order_id LIMIT ?", (run_id, state, count),
            )
        return [self._paper_order_row(row) for row in rows]

    def transition_paper_order(self, *, client_order_id: str, expected_status: str,
                               expected_version: int, new_status: str,
                               reason: str | None = None,
                               broker_order_id: str | None = None) -> PaperCanaryOrderRow:
        allowed = self._PAPER_ORDER_TRANSITIONS.get(expected_status, frozenset())
        if new_status not in allowed:
            raise PaperCanaryStateError(f"invalid paper order transition {expected_status}->{new_status}")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        if broker_order_id is not None and (type(broker_order_id) is not str or not broker_order_id):
            raise ValueError("broker_order_id must be None or a non-empty string")
        now = utcnow_iso()
        terminal = new_status in {"REJECTED", "CANCELLED"}
        with self.tx() as cur:
            self._exec(
                cur,
                "UPDATE paper_orders SET state=?,broker_order_id=COALESCE(?,broker_order_id),reason=?,"
                "version=version+1,authorized_at=CASE WHEN ?='AUTHORIZED' THEN ? ELSE authorized_at END,"
                "terminal_at=CASE WHEN ?=1 THEN ? ELSE terminal_at END,updated_at=? "
                "WHERE client_order_id=? AND state=? AND version=?",
                (new_status, broker_order_id, reason, new_status, now, 1 if terminal else 0,
                 now, now, client_order_id, expected_status, expected_version),
            )
            if cur.rowcount != 1:
                raise PaperCanaryStateError("order status/version compare-and-swap failed")
            self._paper_append_order_event(
                cur,
                client_order_id=client_order_id,
                event_type=new_status,
                previous_state=expected_status,
                new_state=new_status,
                reason=reason,
                now=now,
            )
            self._exec(
                cur,
                f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders WHERE client_order_id=?",
                (client_order_id,),
            )
            return self._paper_order_row(cur.fetchone())

    def list_paper_order_events(self, client_order_id: str) -> list[PaperCanaryOrderEventRow]:
        rows = self._all(
            f"SELECT {self._PAPER_EVENT_COLS} FROM paper_order_events WHERE client_order_id=? ORDER BY seq",
            (client_order_id,),
        )
        return [self._paper_event_row(row) for row in rows]

    def get_paper_fill(self, client_order_id: str | None = None, *,
                       fill_id: str | None = None) -> PaperCanaryFillRow | None:
        if (client_order_id is None) == (fill_id is None):
            raise ValueError("provide exactly one of client_order_id or fill_id")
        if client_order_id is not None:
            row = self._one(
                f"SELECT {self._PAPER_FILL_COLS} FROM paper_fills WHERE client_order_id=?",
                (client_order_id,),
            )
        else:
            row = self._one(f"SELECT {self._PAPER_FILL_COLS} FROM paper_fills WHERE fill_id=?", (fill_id,))
        return self._paper_fill_row(row) if row else None

    def list_paper_fills(self, *, run_id: str, limit: int = 10000) -> list[PaperCanaryFillRow]:
        count = max(1, min(100000, int(limit)))
        rows = self._all(
            f"SELECT {','.join(f'f.{name}' for name in self._PAPER_FILL_COLS.split(','))} "
            "FROM paper_fills f JOIN paper_orders o ON o.client_order_id=f.client_order_id "
            "WHERE o.run_id=? ORDER BY f.ledger_seq LIMIT ?",
            (run_id, count),
        )
        return [self._paper_fill_row(row) for row in rows]

    def get_paper_position(self, *, run_id: str,
                           instrument: str) -> PaperCanaryPositionRow | None:
        row = self._one(
            f"SELECT {self._PAPER_POSITION_COLS} FROM paper_positions WHERE run_id=? AND instrument=?",
            (run_id, instrument),
        )
        return self._paper_position_row(row) if row else None

    def list_paper_positions(self, *, run_id: str) -> list[PaperCanaryPositionRow]:
        rows = self._all(
            f"SELECT {self._PAPER_POSITION_COLS} FROM paper_positions WHERE run_id=? ORDER BY instrument",
            (run_id,),
        )
        return [self._paper_position_row(row) for row in rows]

    @staticmethod
    def _paper_require_ledger_money(value: Decimal, *, field: str,
                                    nonnegative: bool = False) -> Decimal:
        try:
            return _paper_exact_money(value, field=field, nonnegative=nonnegative)
        except (TypeError, ValueError) as exc:
            raise PaperCanarySafetyError(
                f"{field} is not exactly representable in the durable 8dp ledger"
            ) from exc

    def _paper_existing_fill_result(
        self, cur, *, run: PaperCanaryRunRow, order: PaperCanaryOrderRow,
        existing: PaperCanaryFillRow, fill_id: str, broker_order_id: str,
        broker_fill_id: str, instrument: str, side: str, quantity: Decimal,
        price: Decimal, commission: Decimal, multiplier: Decimal, quote_ts: str, ts: str,
    ) -> PaperCanaryFillCommitResult:
        if (
            order.run_id != run.run_id
            or order.state != "FILLED"
            or order.broker_order_id != broker_order_id
            or existing.fill_id != fill_id
            or existing.broker_fill_id != broker_fill_id
            or existing.instrument != instrument
            or existing.side != side
            or existing.quantity != quantity
            or existing.price != price
            or existing.commission != commission
            or existing.multiplier != multiplier
            or existing.quote_ts != quote_ts
            or existing.ts != ts
        ):
            raise PaperCanaryConflict("fill retry does not exactly match the immutable committed fill")
        self._exec(
            cur,
            f"SELECT {self._PAPER_ACCOUNT_COLS} FROM paper_accounts WHERE run_id=?{self.LOCK_CLAUSE}",
            (run.run_id,),
        )
        raw_account = cur.fetchone()
        self._exec(
            cur,
            f"SELECT {self._PAPER_POSITION_COLS} FROM paper_positions "
            f"WHERE run_id=? AND instrument=?{self.LOCK_CLAUSE}",
            (run.run_id, instrument),
        )
        raw_position = cur.fetchone()
        if not raw_account or not raw_position:
            raise PaperCanarySafetyError("committed fill is missing its account or position projection")
        return PaperCanaryFillCommitResult(
            order,
            existing,
            self._paper_account_row(raw_account),
            self._paper_position_row(raw_position),
        )

    def commit_paper_fill_atomic(
        self, *, run_id: str, client_order_id: str, expected_order_version: int,
        fill_id: str, broker_order_id: str, broker_fill_id: str, instrument: str,
        side: str, quantity: Decimal, price: Decimal, commission: Decimal,
        multiplier: Decimal, quote_ts: str, ts: str,
    ) -> PaperCanaryFillCommitResult:
        """Commit one full PAPER fill and every ledger projection in one serializable transaction.

        The exact existing fill is returned for an identical retry. Any mismatch, unsafe runtime
        state, stale configuration, cap breach, short sale, or partial-ledger inconsistency fails
        closed and rolls the transaction back.
        """
        if not all(type(value) is str and value for value in (
            run_id, client_order_id, fill_id, broker_order_id, broker_fill_id, instrument, side,
        )):
            raise ValueError("fill identity fields must be non-empty strings")
        if side not in {"BUY", "SELL"}:
            raise ValueError("fill side must be BUY or SELL")
        if type(expected_order_version) is not int or expected_order_version < 0:
            raise ValueError("expected_order_version must be a nonnegative integer")
        qty = _paper_exact_money(quantity, field="quantity", positive=True)
        px = _paper_exact_money(price, field="price", positive=True)
        fee = _paper_exact_money(commission, field="commission", nonnegative=True)
        mult = _paper_exact_money(multiplier, field="multiplier", positive=True)
        quote_time = _paper_timestamp(quote_ts, field="quote_ts")
        fill_time = _paper_timestamp(ts, field="ts")
        if datetime.fromisoformat(fill_time) < datetime.fromisoformat(quote_time):
            raise ValueError("fill timestamp cannot precede the bound quote")
        transaction_started_at = utcnow_iso()
        with self.tx() as cur:
            self._paper_serialize_sqlite_write(cur)
            # The global runtime singleton is the first PostgreSQL row lock for every fill,
            # create, prepare, and disable transaction. Keeping this one common root removes the
            # runtime/run inversion that could otherwise deadlock a direct concurrent Store call.
            self._exec(
                cur,
                "SELECT status,paper_prepared_at,paper_run_id FROM runtime_state WHERE id=1"
                + self.LOCK_CLAUSE,
            )
            runtime = cur.fetchone()
            self._exec(
                cur,
                "INSERT INTO kill_switch (id,engaged,actor,reason,updated_at) "
                "VALUES (1,0,NULL,NULL,?) ON CONFLICT(id) DO NOTHING",
                (transaction_started_at,),
            )
            self._exec(
                cur,
                f"SELECT engaged FROM kill_switch WHERE id=1{self.LOCK_CLAUSE}",
            )
            kill = cur.fetchone()
            self._exec(
                cur,
                f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs WHERE run_id=?{self.LOCK_CLAUSE}",
                (run_id,),
            )
            raw_run = cur.fetchone()
            if not raw_run:
                raise PaperCanaryStateError("Paper Canary run does not exist")
            run = self._paper_run_row(raw_run)
            self._exec(
                cur,
                f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders "
                f"WHERE client_order_id=?{self.LOCK_CLAUSE}",
                (client_order_id,),
            )
            raw_order = cur.fetchone()
            if not raw_order:
                raise PaperCanaryStateError("paper order does not exist")
            order = self._paper_order_row(raw_order)
            self._exec(
                cur,
                f"SELECT {self._PAPER_FILL_COLS} FROM paper_fills "
                f"WHERE client_order_id=?{self.LOCK_CLAUSE}",
                (client_order_id,),
            )
            raw_existing = cur.fetchone()
            if raw_existing:
                return self._paper_existing_fill_result(
                    cur,
                    run=run,
                    order=order,
                    existing=self._paper_fill_row(raw_existing),
                    fill_id=fill_id,
                    broker_order_id=broker_order_id,
                    broker_fill_id=broker_fill_id,
                    instrument=instrument,
                    side=side,
                    quantity=qty,
                    price=px,
                    commission=fee,
                    multiplier=mult,
                    quote_ts=quote_time,
                    ts=fill_time,
                )
            if not runtime or runtime[0] != "RUNNING":
                raise PaperCanarySafetyError("global runtime is not RUNNING")
            if runtime[2] != run_id:
                raise PaperCanarySafetyError("Paper run does not own the global runtime binding")
            if not kill or bool(kill[0]):
                raise PaperCanarySafetyError("kill switch is engaged")
            # The prepared-day attestation is an admission gate for risk-increasing BUYs.  A
            # durable long-reducing SELL must remain available after UTC midnight (and even if
            # old prepared metadata is malformed), otherwise an overnight run can be stranded.
            # The SELL is still proven against the locked durable position below.
            prepared_dt = None
            if side == "BUY" and runtime[1] is not None:
                try:
                    prepared_dt = datetime.fromisoformat(
                        _paper_timestamp(runtime[1], field="paper_prepared_at"),
                    )
                except (TypeError, ValueError) as exc:
                    raise PaperCanarySafetyError(
                        "Paper runtime prepared timestamp is invalid",
                    ) from exc
            if run.status != "RUNNING" or run.active_slot != 1:
                raise PaperCanarySafetyError("Paper Canary run is not active RUNNING")
            if order.run_id != run_id:
                raise PaperCanaryConflict("order is not bound to the requested Paper Canary run")
            if order.state != "AUTHORIZED" or order.version != expected_order_version:
                raise PaperCanaryStateError("order is not AUTHORIZED at the expected version")
            if order.broker_order_id not in {None, broker_order_id}:
                raise PaperCanaryConflict("broker_order_id does not match the authorized order")
            if (
                order.instrument != instrument
                or order.side != side
                or order.quantity != qty
                or order.quote_ts != quote_time
            ):
                raise PaperCanaryConflict("fill does not match the full authorized order binding")
            config, canonical_config, config_checksum = self._paper_config(run.config_json)
            if canonical_config != run.config_json or config_checksum != run.config_checksum:
                raise PaperCanarySafetyError("Paper Canary config snapshot was altered")
            if instrument != config["instrument"]:
                raise PaperCanarySafetyError("fill instrument is outside the one-instrument run config")
            quote_age = datetime.fromisoformat(fill_time) - datetime.fromisoformat(quote_time)
            quote_age_us = (
                (quote_age.days * 86_400 + quote_age.seconds) * 1_000_000
                + quote_age.microseconds
            )
            quote_age_seconds = Decimal(quote_age_us) / Decimal(1_000_000)
            if quote_age_seconds > self._paper_config_amount(
                config["quote_max_age_s"], field="quote_max_age_s", positive=True,
            ):
                raise PaperCanarySafetyError("bound quote is stale at atomic fill commit")
            slip = self._paper_config_amount(config["slippage_bps"], field="slippage_bps") / Decimal("10000")
            raw_price = order.quote_ask * (Decimal("1") + slip) if side == "BUY" else (
                order.quote_bid * (Decimal("1") - slip)
            )
            expected_price = raw_price.quantize(QUANT, rounding=ROUND_HALF_EVEN)
            expected_commission = max(
                self._paper_config_amount(config["min_commission"], field="min_commission"),
                self._paper_config_amount(
                    config["commission_per_unit"], field="commission_per_unit",
                ) * qty,
            ).quantize(QUANT, rounding=ROUND_HALF_EVEN)
            if px != expected_price or fee != expected_commission or mult != Decimal("1"):
                raise PaperCanaryConflict(
                    "fill price/commission/multiplier does not match deterministic config terms"
                )

            self._exec(
                cur,
                f"SELECT halted,killed FROM risk_state WHERE id=1{self.LOCK_CLAUSE}",
            )
            risk_state = cur.fetchone()
            if not risk_state or bool(risk_state[0]) or bool(risk_state[1]):
                raise PaperCanarySafetyError("durable risk state is halted, killed, or missing")
            current_risk = self._paper_risk_checksum_in_tx(cur)
            if (
                current_risk != run.risk_config_checksum
                or order.risk_config_checksum != run.risk_config_checksum
            ):
                raise PaperCanarySafetyError("risk configuration changed after authorization")
            configured_starting_cash = self._paper_config_amount(
                config["starting_cash"], field="starting_cash", positive=True,
            )
            risk_capital = self._paper_risk_capital_in_tx(cur)
            if configured_starting_cash > risk_capital:
                raise PaperCanarySafetyError(
                    "Paper Canary starting_cash exceeds canonical risk capital",
                )
            self._exec(
                cur,
                f"SELECT max_daily_loss_pct FROM risk_config WHERE id=1{self.LOCK_CLAUSE}",
            )
            max_loss_row = cur.fetchone()
            try:
                paper_max_loss_pct = _paper_exact_money(
                    to_decimal(max_loss_row[0]) if max_loss_row else None,
                    field="max_daily_loss_pct",
                    positive=True,
                )
            except (TypeError, ValueError) as exc:
                raise PaperCanarySafetyError("canonical daily loss limit is invalid") from exc

            self._exec(
                cur,
                f"SELECT {self._PAPER_ACCOUNT_COLS} FROM paper_accounts "
                f"WHERE run_id=?{self.LOCK_CLAUSE}",
                (run_id,),
            )
            raw_account = cur.fetchone()
            if not raw_account:
                raise PaperCanarySafetyError("Paper Canary account is missing")
            account = self._paper_account_row(raw_account)
            if account.starting_cash != configured_starting_cash:
                raise PaperCanarySafetyError("account capital does not match the immutable run config")
            self._exec(
                cur,
                f"SELECT {self._PAPER_POSITION_COLS} FROM paper_positions "
                f"WHERE run_id=? AND instrument=?{self.LOCK_CLAUSE}",
                (run_id, instrument),
            )
            raw_position = cur.fetchone()
            position = self._paper_position_row(raw_position) if raw_position else None

            # The first Store-owned time selects only the candidate UTC safety day. Lock that
            # day's loss row before the final market-health attestation; a wait here must never
            # leave quote/health freshness frozen at a pre-wait instant.
            candidate_now = _paper_timestamp(utcnow_iso(), field="candidate_commit_now")
            candidate_dt = datetime.fromisoformat(candidate_now)
            trade_date = candidate_dt.date().isoformat()
            self._exec(
                cur,
                "INSERT INTO daily_loss_lock (trade_date,engaged,reason,updated_at) "
                "VALUES (?,0,NULL,?) ON CONFLICT(trade_date) DO NOTHING",
                (trade_date, candidate_now),
            )
            self._exec(
                cur,
                f"SELECT engaged FROM daily_loss_lock WHERE trade_date=?{self.LOCK_CLAUSE}",
                (trade_date,),
            )
            daily_lock = cur.fetchone()
            if not daily_lock:
                raise PaperCanarySafetyError("daily loss lock is missing")
            daily_lock_engaged = bool(daily_lock[0])
            # The loss latch blocks risk-increasing BUYs, but it must never strand a long Paper
            # position.  A valid long-reducing SELL remains available so the canary can flatten.
            if daily_lock_engaged and side == "BUY":
                raise PaperCanarySafetyError("daily loss lock is engaged")

            self._exec(
                cur,
                "INSERT INTO paper_daily_loss_state "
                "(trade_date,risk_capital_baseline,cumulative_equity_delta,version,updated_at) "
                "VALUES (?,?,?,0,?) ON CONFLICT(trade_date) DO NOTHING",
                (trade_date, self._m(risk_capital), self._m(Decimal("0")), candidate_now),
            )
            self._exec(
                cur,
                "SELECT risk_capital_baseline,cumulative_equity_delta,version "
                "FROM paper_daily_loss_state WHERE trade_date=?" + self.LOCK_CLAUSE,
                (trade_date,),
            )
            paper_daily = cur.fetchone()
            if not paper_daily:
                raise PaperCanarySafetyError("durable Paper daily-loss aggregate is missing")
            try:
                paper_daily_capital = _paper_exact_money(
                    to_decimal(paper_daily[0]),
                    field="paper_daily_loss_state.risk_capital_baseline",
                    positive=True,
                )
                paper_daily_delta = _paper_exact_money(
                    to_decimal(paper_daily[1]),
                    field="paper_daily_loss_state.cumulative_equity_delta",
                )
                paper_daily_version = int(paper_daily[2])
            except (TypeError, ValueError) as exc:
                raise PaperCanarySafetyError("durable Paper daily-loss aggregate is invalid") from exc
            if paper_daily_capital != risk_capital:
                raise PaperCanarySafetyError(
                    "durable Paper daily-loss capital baseline changed during the UTC day",
                )

            # Re-attest the authoritative symbol row only after every safety row governing this
            # commit is locked. PostgreSQL holds it through commit; SQLite's early writer lock
            # provides equivalent serialization. `READY` is the durable representation emitted
            # only by the REALTIME quality gate, so MASSIVE/READY is the database-side attestation.
            self._exec(
                cur,
                "SELECT symbol,source,status,updated_at,quote_ts FROM market_data_health "
                f"WHERE symbol=?{self.LOCK_CLAUSE}",
                (instrument,),
            )
            raw_health = cur.fetchone()
            if (
                not raw_health
                or raw_health[0] != instrument
                or raw_health[1] != "MASSIVE"
                or raw_health[2] != "READY"
            ):
                raise PaperCanarySafetyError(
                    "market-data health is not exact MASSIVE/READY/REALTIME for the fill symbol",
                )
            try:
                health_time = _paper_timestamp(
                    raw_health[3], field="market-data health updated_at",
                )
                durable_quote_time = _paper_timestamp(
                    raw_health[4], field="market-data health quote_ts",
                )
            except (TypeError, ValueError) as exc:
                raise PaperCanarySafetyError(
                    "market-data health updated_at/quote_ts timestamps are invalid or missing",
                ) from exc

            # Capture the authoritative time only after the daily and health locks. The candidate
            # time above never authorizes freshness or a day rollover.
            now = _paper_timestamp(utcnow_iso(), field="commit_now")
            quote_dt = datetime.fromisoformat(quote_time)
            fill_dt = datetime.fromisoformat(fill_time)
            commit_dt = datetime.fromisoformat(now)
            health_dt = datetime.fromisoformat(health_time)
            durable_quote_dt = datetime.fromisoformat(durable_quote_time)
            if (
                quote_dt > commit_dt
                or fill_dt > commit_dt
                or health_dt > commit_dt
                or durable_quote_dt > commit_dt
                or (prepared_dt is not None and prepared_dt > commit_dt)
            ):
                raise PaperCanarySafetyError(
                    "prepared/quote/fill/market-data timestamp is in the future at atomic fill commit"
                )
            if commit_dt.date() != candidate_dt.date() or fill_dt.date() != commit_dt.date():
                raise PaperCanarySafetyError(
                    "candidate, fill evidence, and atomic commit must share one UTC trading day",
                )
            if prepared_dt is not None and prepared_dt.date() != commit_dt.date():
                raise PaperCanarySafetyError(
                    "Paper run cannot fill outside its prepared UTC trading day",
                )
            if durable_quote_dt < quote_dt:
                raise PaperCanarySafetyError(
                    "durable current quote predates the bound order quote",
                )
            if health_dt < durable_quote_dt:
                raise PaperCanarySafetyError(
                    "market-data health predates its durable current REALTIME quote",
                )
            health_age = commit_dt - health_dt
            health_age_us = (
                (health_age.days * 86_400 + health_age.seconds) * 1_000_000
                + health_age.microseconds
            )
            if Decimal(health_age_us) / Decimal(1_000_000) > self._paper_config_amount(
                config["quote_max_age_s"], field="quote_max_age_s", positive=True,
            ):
                raise PaperCanarySafetyError(
                    "market-data health became stale before the atomic fill commit",
                )
            durable_quote_age = commit_dt - durable_quote_dt
            durable_quote_age_us = (
                (durable_quote_age.days * 86_400 + durable_quote_age.seconds) * 1_000_000
                + durable_quote_age.microseconds
            )
            if Decimal(durable_quote_age_us) / Decimal(1_000_000) > self._paper_config_amount(
                config["quote_max_age_s"], field="quote_max_age_s", positive=True,
            ):
                raise PaperCanarySafetyError(
                    "durable current quote became stale before the atomic fill commit",
                )
            commit_age = commit_dt - quote_dt
            commit_age_us = (
                (commit_age.days * 86_400 + commit_age.seconds) * 1_000_000
                + commit_age.microseconds
            )
            if Decimal(commit_age_us) / Decimal(1_000_000) > self._paper_config_amount(
                config["quote_max_age_s"], field="quote_max_age_s", positive=True,
            ):
                raise PaperCanarySafetyError(
                    "bound quote became stale while waiting to commit the atomic fill"
                )

            old_quantity = Decimal("0") if position is None else position.quantity
            old_average = Decimal("0") if position is None else position.avg_price
            old_mark = Decimal("0") if position is None else position.mark_price
            old_realized = Decimal("0") if position is None else position.realized_pnl
            old_net = self._paper_require_ledger_money(
                old_quantity * old_mark * mult, field="existing net exposure", nonnegative=True,
            )
            if (
                old_quantity < 0
                or account.realized_pnl != old_realized
                or account.gross_exposure != abs(old_net)
                or account.net_exposure != old_net
                or account.equity != account.cash + old_net
            ):
                raise PaperCanarySafetyError("account and position projections are inconsistent")
            if side == "SELL" and qty > old_quantity:
                raise PaperCanarySafetyError("Paper Canary is long-only; SELL exceeds the long position")

            notional = self._paper_require_ledger_money(
                qty * px * mult, field="order notional", nonnegative=True,
            )
            # Admission caps constrain BUYs.  A SELL proven against the locked durable long
            # position is an exit, so activity/notional limits must never strand it.
            if side == "BUY" and notional > self._paper_cap(
                config["max_order_notional"], field="max_order_notional",
            ):
                raise PaperCanarySafetyError("max_order_notional exceeded")
            new_quantity = old_quantity + qty if side == "BUY" else old_quantity - qty
            projected_gross = self._paper_require_ledger_money(
                abs(new_quantity) * px * mult, field="projected gross exposure", nonnegative=True,
            )
            if side == "BUY" and projected_gross > self._paper_cap(
                config["max_gross_notional"], field="max_gross_notional",
            ):
                raise PaperCanarySafetyError("max_gross_notional exceeded")
            if side == "SELL":
                pre_sell_gross_at_fill_price = self._paper_require_ledger_money(
                    old_quantity * px * mult,
                    field="pre-sell gross exposure at fill price",
                    nonnegative=True,
                )
                if projected_gross >= pre_sell_gross_at_fill_price:
                    raise PaperCanarySafetyError(
                        "SELL must strictly reduce the locked long exposure",
                    )
            self._exec(
                cur,
                "SELECT f.quantity,f.price,f.multiplier,f.ts FROM paper_fills f "
                "JOIN paper_orders o ON o.client_order_id=f.client_order_id "
                "WHERE o.run_id=? ORDER BY f.ledger_seq",
                (run_id,),
            )
            prior_fills = cur.fetchall()
            turnover = sum(
                (to_decimal(row[0]) * to_decimal(row[1]) * to_decimal(row[2])
                 for row in prior_fills if row[3][:10] == trade_date),
                Decimal("0"),
            )
            turnover = self._paper_require_ledger_money(
                turnover + notional, field="daily turnover", nonnegative=True,
            )
            if side == "BUY" and turnover > self._paper_cap(
                config["max_daily_turnover"], field="max_daily_turnover",
            ):
                raise PaperCanarySafetyError("max_daily_turnover exceeded")
            daily_fill_count = sum(1 for row in prior_fills if row[3][:10] == trade_date)
            if side == "BUY" and daily_fill_count + 1 > int(config["max_orders"]):
                raise PaperCanarySafetyError("max_orders exceeded")

            if side == "BUY":
                new_cash = account.cash - notional - fee
                weighted_cost = old_quantity * old_average + qty * px
                new_average = weighted_cost / new_quantity
                realized_delta = Decimal("0")
            else:
                new_cash = account.cash + notional - fee
                new_average = Decimal("0") if new_quantity == 0 else old_average
                realized_delta = (px - old_average) * qty * mult - fee
            new_cash = self._paper_require_ledger_money(
                new_cash, field="cash", nonnegative=side == "BUY",
            )
            new_average = self._paper_require_ledger_money(
                new_average, field="average price", nonnegative=True,
            )
            new_realized = self._paper_require_ledger_money(
                old_realized + realized_delta, field="realized PnL",
            )
            new_net = projected_gross
            new_equity = self._paper_require_ledger_money(
                new_cash + new_net, field="equity",
            )
            # Apply this fill's exact account-equity delta to the UTC-day aggregate shared by every
            # run.  BUY is risk-increasing and cannot start or cross the canonical loss boundary.
            # SELL is risk-reducing and may flatten even when its commission/slippage crosses the
            # boundary; in that case the durable daily latch is engaged in this same transaction.
            account_equity_delta = self._paper_require_ledger_money(
                new_equity - account.equity,
                field="Paper account equity delta",
            )
            projected_daily_delta = self._paper_require_ledger_money(
                paper_daily_delta + account_equity_delta,
                field="Paper cumulative daily equity delta",
            )
            paper_loss_limit = paper_daily_capital * paper_max_loss_pct / Decimal(100)
            current_daily_loss = max(Decimal("0"), -paper_daily_delta)
            projected_daily_loss = max(Decimal("0"), -projected_daily_delta)
            # Keep the stricter per-run account drawdown boundary as well.  Without it, a tiny
            # canary account could spend the much larger shared Risk-capital budget in one run.
            # The shared aggregate prevents sequential-run resets; this bound limits each account.
            run_loss_limit = (
                min(risk_capital, account.starting_cash)
                * paper_max_loss_pct
                / Decimal(100)
            )
            current_run_loss = max(
                Decimal("0"), account.starting_cash - account.equity,
            )
            projected_run_loss = max(
                Decimal("0"), account.starting_cash - new_equity,
            )
            if side == "BUY" and (
                current_daily_loss >= paper_loss_limit
                or projected_daily_loss >= paper_loss_limit
                or current_run_loss >= run_loss_limit
                or projected_run_loss >= run_loss_limit
            ):
                raise PaperCanarySafetyError("durable Paper daily loss limit would be reached")
            engage_daily_loss = side == "SELL" and (
                current_daily_loss >= paper_loss_limit
                or projected_daily_loss >= paper_loss_limit
                or current_run_loss >= run_loss_limit
                or projected_run_loss >= run_loss_limit
            )

            self._exec(cur, "SELECT COALESCE(MAX(ledger_seq),0) FROM paper_fills")
            ledger_seq = int(cur.fetchone()[0]) + 1
            self._exec(
                cur,
                "UPDATE paper_orders SET state='FILLED',broker_order_id=?,reason=NULL,version=version+1,"
                "terminal_at=?,updated_at=? WHERE client_order_id=? AND state='AUTHORIZED' AND version=?",
                (broker_order_id, fill_time, now, client_order_id, expected_order_version),
            )
            if cur.rowcount != 1:
                raise PaperCanaryStateError("order authorization was consumed concurrently")
            self._exec(
                cur,
                "INSERT INTO paper_fills "
                "(fill_id,client_order_id,broker_fill_id,ledger_seq,instrument,side,quantity,price,commission,"
                "multiplier,quote_ts,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (fill_id, client_order_id, broker_fill_id, ledger_seq, instrument, side, self._m(qty),
                 self._m(px), self._m(fee), self._m(mult), quote_time, fill_time),
            )
            if position is None:
                self._exec(
                    cur,
                    "INSERT INTO paper_positions "
                    "(run_id,instrument,quantity,avg_price,mark_price,realized_pnl,version,updated_at) "
                    "VALUES (?,?,?,?,?,?,0,?)",
                    (run_id, instrument, self._m(new_quantity), self._m(new_average), self._m(px),
                     self._m(new_realized), now),
                )
            else:
                self._exec(
                    cur,
                    "UPDATE paper_positions SET quantity=?,avg_price=?,mark_price=?,realized_pnl=?,"
                    "version=version+1,updated_at=? WHERE run_id=? AND instrument=? AND version=?",
                    (self._m(new_quantity), self._m(new_average), self._m(px), self._m(new_realized),
                     now, run_id, instrument, position.version),
                )
                if cur.rowcount != 1:
                    raise PaperCanaryStateError("position version compare-and-swap failed")
            self._exec(
                cur,
                "UPDATE paper_accounts SET cash=?,equity=?,realized_pnl=?,gross_exposure=?,net_exposure=?,"
                "version=version+1,updated_at=? WHERE run_id=? AND version=?",
                (self._m(new_cash), self._m(new_equity), self._m(new_realized), self._m(projected_gross),
                 self._m(new_net), now, run_id, account.version),
            )
            if cur.rowcount != 1:
                raise PaperCanaryStateError("account version compare-and-swap failed")
            self._exec(
                cur,
                "UPDATE paper_daily_loss_state SET cumulative_equity_delta=?,version=version+1,"
                "updated_at=? WHERE trade_date=? AND version=?",
                (self._m(projected_daily_delta), now, trade_date, paper_daily_version),
            )
            if cur.rowcount != 1:
                raise PaperCanaryStateError("Paper daily-loss aggregate compare-and-swap failed")
            if engage_daily_loss and not daily_lock_engaged:
                loss_reason = "Paper Canary daily loss limit reached"
                self._exec(
                    cur,
                    "UPDATE daily_loss_lock SET engaged=1,reason=?,updated_at=? "
                    "WHERE trade_date=? AND engaged=0",
                    (loss_reason, now, trade_date),
                )
                if cur.rowcount != 1:
                    raise PaperCanaryStateError("Paper daily-loss latch compare-and-swap failed")
                self._insert_audit(
                    cur,
                    AuditEventRow(
                        new_id(), now, "paper-canary", "DAILY_LOSS_LOCK", None, "HALTED",
                        f"{loss_reason}; trade_date={trade_date}; run_id={run_id}", new_id(),
                    ),
                )
            self._paper_append_order_event(
                cur,
                client_order_id=client_order_id,
                event_type="FILLED",
                previous_state="AUTHORIZED",
                new_state="FILLED",
                reason=None,
                now=fill_time,
            )
            self._exec(
                cur,
                f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders WHERE client_order_id=?",
                (client_order_id,),
            )
            committed_order = self._paper_order_row(cur.fetchone())
            self._exec(cur, f"SELECT {self._PAPER_FILL_COLS} FROM paper_fills WHERE fill_id=?", (fill_id,))
            committed_fill = self._paper_fill_row(cur.fetchone())
            self._exec(cur, f"SELECT {self._PAPER_ACCOUNT_COLS} FROM paper_accounts WHERE run_id=?", (run_id,))
            committed_account = self._paper_account_row(cur.fetchone())
            self._exec(
                cur,
                f"SELECT {self._PAPER_POSITION_COLS} FROM paper_positions "
                "WHERE run_id=? AND instrument=?",
                (run_id, instrument),
            )
            committed_position = self._paper_position_row(cur.fetchone())
            return PaperCanaryFillCommitResult(
                committed_order, committed_fill, committed_account, committed_position,
            )

    def cancel_paper_nonterminal_orders(self, *, run_id: str,
                                        reason: str) -> list[PaperCanaryOrderRow]:
        if type(reason) is not str or not reason:
            raise ValueError("recovery cancellation reason must be a non-empty string")
        now = utcnow_iso()
        cancelled: list[PaperCanaryOrderRow] = []
        with self.tx() as cur:
            self._paper_serialize_sqlite_write(cur)
            self._exec(
                cur,
                f"SELECT {self._PAPER_RUN_COLS} FROM paper_canary_runs WHERE run_id=?{self.LOCK_CLAUSE}",
                (run_id,),
            )
            raw_run = cur.fetchone()
            if not raw_run:
                raise PaperCanaryStateError("Paper Canary run does not exist")
            run = self._paper_run_row(raw_run)
            if run.status not in {"RECOVERY_REQUIRED", "STOPPED", "FAILED"}:
                raise PaperCanaryStateError("nonterminal orders may be bulk-cancelled only during recovery")
            self._exec(
                cur,
                f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders "
                "WHERE run_id=? AND state IN ('INTENT','AUTHORIZED') ORDER BY created_at,client_order_id"
                f"{self.LOCK_CLAUSE}",
                (run_id,),
            )
            orders = [self._paper_order_row(row) for row in cur.fetchall()]
            for order in orders:
                self._exec(
                    cur,
                    "UPDATE paper_orders SET state='CANCELLED',reason=?,version=version+1,terminal_at=?,"
                    "updated_at=? WHERE client_order_id=? AND state=? AND version=?",
                    (reason, now, now, order.client_order_id, order.state, order.version),
                )
                if cur.rowcount != 1:
                    raise PaperCanaryStateError("recovery order cancellation compare-and-swap failed")
                self._paper_append_order_event(
                    cur,
                    client_order_id=order.client_order_id,
                    event_type="CANCELLED",
                    previous_state=order.state,
                    new_state="CANCELLED",
                    reason=reason,
                    now=now,
                )
                self._exec(
                    cur,
                    f"SELECT {self._PAPER_ORDER_COLS} FROM paper_orders WHERE client_order_id=?",
                    (order.client_order_id,),
                )
                cancelled.append(self._paper_order_row(cur.fetchone()))
        return cancelled

    def record_paper_reconciliation(
        self, *, run_id: str, status: str, fills_checksum: str,
        positions_checksum: str, account_checksum: str, open_order_count: int,
        breaks_json, reconciliation_id: str | None = None,
        checked_at: str | None = None,
    ) -> PaperCanaryReconciliationRow:
        if status not in {"PASS", "FAIL"}:
            raise ValueError("reconciliation status must be PASS or FAIL")
        if not all(type(value) is str and value for value in (
            run_id, fills_checksum, positions_checksum, account_checksum,
        )):
            raise ValueError("reconciliation identities/checksums must be non-empty strings")
        if type(open_order_count) is not int or open_order_count < 0:
            raise ValueError("open_order_count must be a nonnegative integer")
        try:
            breaks = json.loads(breaks_json) if type(breaks_json) is str else breaks_json
            if type(breaks) is not list or any(type(item) is not str or not item for item in breaks):
                raise ValueError("breaks_json must be a JSON list of non-empty strings")
            canonical_breaks = json.dumps(
                breaks, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("breaks_json must be a canonicalizable JSON list") from exc
        checked = _paper_timestamp(checked_at or utcnow_iso(), field="checked_at")
        recon_id = reconciliation_id or (
            "pcr_" + hashlib.sha256(
                "\0".join((run_id, status, fills_checksum, positions_checksum, account_checksum,
                            str(open_order_count), canonical_breaks, checked)).encode()
            ).hexdigest()[:24]
        )
        if type(recon_id) is not str or not recon_id:
            raise ValueError("reconciliation_id must be a non-empty string")
        with self.tx() as cur:
            self._paper_serialize_sqlite_write(cur)
            self._exec(
                cur,
                f"SELECT run_id FROM paper_canary_runs WHERE run_id=?{self.LOCK_CLAUSE}",
                (run_id,),
            )
            if not cur.fetchone():
                raise PaperCanaryStateError("Paper Canary run does not exist")
            self._exec(
                cur,
                "INSERT INTO paper_reconciliations "
                "(reconciliation_id,run_id,status,fills_checksum,positions_checksum,account_checksum,"
                "open_order_count,breaks_json,checked_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(reconciliation_id) DO NOTHING",
                (recon_id, run_id, status, fills_checksum, positions_checksum, account_checksum,
                 open_order_count, canonical_breaks, checked),
            )
            self._exec(
                cur,
                f"SELECT {self._PAPER_RECON_COLS} FROM paper_reconciliations "
                "WHERE reconciliation_id=?",
                (recon_id,),
            )
            row = self._paper_reconciliation_row(cur.fetchone())
            if row != PaperCanaryReconciliationRow(
                recon_id, run_id, status, fills_checksum, positions_checksum, account_checksum,
                open_order_count, canonical_breaks, checked,
            ):
                raise PaperCanaryConflict("reconciliation_id already exists with different content")
            return row

    def get_paper_reconciliation(self, reconciliation_id: str) -> PaperCanaryReconciliationRow | None:
        row = self._one(
            f"SELECT {self._PAPER_RECON_COLS} FROM paper_reconciliations WHERE reconciliation_id=?",
            (reconciliation_id,),
        )
        return self._paper_reconciliation_row(row) if row else None

    def list_paper_reconciliations(self, *, run_id: str,
                                   limit: int = 1000) -> list[PaperCanaryReconciliationRow]:
        count = max(1, min(10000, int(limit)))
        rows = self._all(
            f"SELECT {self._PAPER_RECON_COLS} FROM paper_reconciliations WHERE run_id=? "
            "ORDER BY checked_at,reconciliation_id LIMIT ?",
            (run_id, count),
        )
        return [self._paper_reconciliation_row(row) for row in rows]
