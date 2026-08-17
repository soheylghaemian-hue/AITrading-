"""§ R3.0 — run orchestration: validate → snapshot → replay → immutable persist (RESEARCH ONLY).

Creating a backtest starts an INTERNAL historical research run. It never enables trading, never creates
a broker order, never touches execution/autonomous/live/F2/kill-switch. Inputs are validated and bounded
BEFORE persistence; the run is created QUEUED, advanced to RUNNING, and finalized atomically (results +
terminal transition in one transaction) into a database-immutable record with a deterministic checksum.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ..store import new_id
from . import ENGINE_VERSION, calendars as cal
from .engine import Costs, coverage_preflight, replay
from .metrics import compute_metrics, robustness_report
from .strategy import ResearchBar, get_strategy

# Fixed R3.0 resource limits.
MAX_SYMBOLS = 5
MAX_INPUT_BARS = 50000
MAX_EQUITY_POINTS = 50000
MAX_YEARS_1D = 5
MAX_YEARS_1H = 1
SUPPORTED_INTERVALS = ("1h", "1D")

_DEFAULT_COSTS = {"spread_bps": "2", "slippage_bps": "1", "commission_per_share": "0.005", "min_commission": "1"}
_DEFAULT_RISK = {"risk_per_trade_pct": "1", "max_daily_loss_pct": "2", "max_portfolio_exposure_pct": "100",
                 "max_drawdown_pct": "20", "warning_threshold_pct": "80"}


class ValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class OneActiveRunError(Exception):
    pass


def _parse_dt(s: str) -> datetime:
    txt = str(s).strip()
    if len(txt) == 10:
        txt += "T00:00:00+00:00"
    dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _dec(v) -> Decimal:
    return Decimal(str(v))


def validate_request(req: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    symbols = [str(s).upper() for s in (req.get("symbols") or [])]
    if not symbols:
        errors.append("at least one symbol is required")
    if len(symbols) > MAX_SYMBOLS:
        errors.append(f"too many symbols ({len(symbols)} > {MAX_SYMBOLS})")
    interval = cal.normalize_interval(req.get("interval") or "")
    if interval not in SUPPORTED_INTERVALS:
        errors.append(f"unsupported interval '{req.get('interval')}' (supported: 1h, 1D)")
    try:
        start, end = _parse_dt(req["start"]), _parse_dt(req["end"])
        if start >= end:
            errors.append("start must be before end")
        else:
            span_years = (end - start).days / 365.25
            cap = MAX_YEARS_1D if interval == "1D" else MAX_YEARS_1H
            if span_years > cap:
                errors.append(f"date range {span_years:.1f}y exceeds the {cap}y cap for {interval}")
    except (KeyError, ValueError, TypeError):
        errors.append("start/end must be ISO-8601 dates")
        start = end = None
    try:
        capital = _dec(req.get("starting_capital", "100000"))
        if capital <= 0:
            errors.append("starting_capital must be > 0")
    except (InvalidOperation, TypeError):
        errors.append("starting_capital must be numeric"); capital = Decimal(0)
    strategy_id = req.get("strategy_id", "OHLC_TREND_BASELINE")
    strategy_version = int(req.get("strategy_version", 1))
    try:
        get_strategy(strategy_id, strategy_version)
    except ValueError:
        errors.append(f"unknown strategy {strategy_id} v{strategy_version}")
    try:
        max_conc = int(req.get("max_concurrent_positions", 3))
        if not (1 <= max_conc <= MAX_SYMBOLS):
            errors.append(f"max_concurrent_positions must be 1..{MAX_SYMBOLS}")
    except (ValueError, TypeError):
        errors.append("max_concurrent_positions must be an integer"); max_conc = 1
    costs = {**_DEFAULT_COSTS, **(req.get("costs") or {})}
    risk = {**_DEFAULT_RISK, **(req.get("risk") or {})}
    try:
        for k, v in {**costs, **risk}.items():
            if _dec(v) < 0:
                errors.append(f"{k} must be ≥ 0")
    except (InvalidOperation, TypeError):
        errors.append("costs/risk values must be numeric")
    normalized = {"symbols": symbols, "interval": interval, "start": req.get("start"), "end": req.get("end"),
                  "starting_capital": str(capital), "currency": req.get("currency", "USD"),
                  "strategy_id": strategy_id, "strategy_version": strategy_version,
                  "max_concurrent_positions": max_conc, "costs": costs, "risk": risk}
    return normalized, errors


def _checksum(decisions, trades, equity_points, metrics) -> str:
    payload = {
        "engine": ENGINE_VERSION,
        "decisions": [d["checksum"] for d in decisions],
        "trades": [[t["symbol"], t["entry_ts"], str(t["entry_price"]), str(t.get("exit_price")),
                    str(t["quantity"]), str(t.get("net_pnl")), t.get("exit_reason")] for t in trades],
        "equity": [[e["seq"], str(e["equity"])] for e in equity_points],
        "metrics": {k: v for k, v in metrics.items() if k not in ("daily_returns", "monthly_returns")},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def run_backtest(store, *, owner: str, req: dict, commit_ref: str | None = None,
                 dataset_id: str | None = None) -> str:
    """Validate + execute a bounded research run synchronously; returns the run_id. Raises
    ValidationError (→ 422) or OneActiveRunError (→ 409); data problems fail the RUN (persisted FAILED).

    `dataset_id` (R3.0A) pins an explicit, immutable, checksum-verified research dataset as the bar source.
    When None, the legacy live-`ohlc_bars` source is used (existing R3.0 behavior; pins stay NULL)."""
    normalized, errors = validate_request(req)
    if errors:
        raise ValidationError(errors)
    if store.bt_count_active(owner) >= 1:
        raise OneActiveRunError("owner already has an active (QUEUED/RUNNING) run")

    symbols, interval = normalized["symbols"], normalized["interval"]
    start_dt, end_dt = _parse_dt(normalized["start"]), _parse_dt(normalized["end"])

    # R3.0A: resolve + validate the pinned dataset BEFORE creating the run (invalid selection → 422).
    dataset_pin = None
    if dataset_id is not None:
        from .backfill import validate_selection  # noqa: PLC0415 — keep backfill import local to this path
        _ds, dataset_pin, ds_errors = validate_selection(store, dataset_id, symbols, interval,
                                                          normalized["start"], normalized["end"])
        if ds_errors:
            raise ValidationError(ds_errors)
    strategy = get_strategy(normalized["strategy_id"], normalized["strategy_version"])
    capital = _dec(normalized["starting_capital"])
    risk = normalized["risk"]
    costs = Costs(_dec(normalized["costs"]["spread_bps"]), _dec(normalized["costs"]["slippage_bps"]),
                  _dec(normalized["costs"]["commission_per_share"]), _dec(normalized["costs"]["min_commission"]))

    # Resolve the point-in-time availability policy for each symbol (fails the run if unknown), and
    # reject any date outside the versioned calendar's declared coverage (unknown holidays are unsafe).
    policy = None
    policy_error = None
    if not cal.calendar_covers(start_dt, end_dt):
        policy_error = (f"requested range {start_dt.date()}..{end_dt.date()} is outside the "
                        f"{cal.CALENDAR_VERSION} calendar coverage {cal.CALENDAR_START}..{cal.CALENDAR_END}")
    else:
        for sym in symbols:
            try:
                policy = cal.resolve_policy(sym, interval)
            except cal.PointInTimeError as e:
                policy_error = str(e); break

    run_id = new_id()
    risk_cfg_row = store.get_risk_config()
    risk_snapshot = {"canonical_risk_config": (None if risk_cfg_row is None else {
        "capital": str(risk_cfg_row.capital), "risk_per_trade_pct": str(risk_cfg_row.risk_per_trade_pct),
        "max_daily_loss_pct": str(risk_cfg_row.max_daily_loss_pct)}), "effective_risk_params": risk,
        "note": "canonical risk_config is audit-only; simulation uses effective_risk_params + starting_capital"}
    config_snapshot = {"strategy": strategy.config, "symbols": symbols, "interval": interval,
                       "start": normalized["start"], "end": normalized["end"],
                       "starting_capital": str(capital), "currency": normalized["currency"],
                       "costs": normalized["costs"], "risk": risk,
                       "max_concurrent_positions": normalized["max_concurrent_positions"],
                       "resource_limits": {"max_symbols": MAX_SYMBOLS, "max_input_bars": MAX_INPUT_BARS,
                                           "max_equity_points": MAX_EQUITY_POINTS}}
    _pol = policy or cal.AvailabilityPolicy(cal.POLICY_ID, cal.POLICY_VERSION, cal.CALENDAR_ID,
                                            cal.CALENDAR_VERSION, cal.EXCHANGE_TZ, cal.SESSION_CALENDAR,
                                            cal.ASSET_CLASS, interval)
    store.bt_create_run(
        run_id=run_id, owner=owner, strategy_id=strategy.strategy_id, strategy_version=strategy.version,
        strategy_config_json=json.dumps(strategy.config), strategy_checksum=strategy.config_checksum(),
        engine_version=ENGINE_VERSION, symbol_universe_json=json.dumps(symbols), interval=interval,
        start_ts=start_dt.isoformat(), end_ts=end_dt.isoformat(), asset_class=_pol.asset_class,
        timestamp_policy_id=_pol.policy_id, timestamp_policy_version=_pol.policy_version,
        exchange_calendar_id=_pol.calendar_id, exchange_calendar_version=_pol.calendar_version,
        exchange_tz=_pol.exchange_tz, session_calendar=_pol.session_calendar,
        data_source=(f"research_dataset:{dataset_id}" if dataset_id else "ohlc_bars"),
        config_snapshot_json=json.dumps(config_snapshot), risk_config_snapshot_json=json.dumps(risk_snapshot),
        commit_ref=commit_ref, dataset_pin=dataset_pin)
    store.bt_advance_status(run_id, "QUEUED", "RUNNING")

    if policy_error is not None:
        store.bt_write_results(run_id, expected_from="RUNNING", status="FAILED",
                               events=[{"seq": 1, "event_type": "INSUFFICIENT_POINT_IN_TIME_METADATA",
                                        "severity": "CRITICAL", "details": {"error": policy_error}}],
                               failure_code="INSUFFICIENT_POINT_IN_TIME_METADATA", failure_reason=policy_error)
        return run_id

    # Fetch historical bars per symbol (event ts in range). Point-in-time is enforced inside the replay.
    # R3.0A: a pinned dataset reads the IMMUTABLE research bars; otherwise the legacy live ohlc_bars.
    bars_by_symbol: dict[str, list[ResearchBar]] = {}
    total_bars = 0
    for sym in symbols:
        if dataset_id is not None:
            rows = store.rd_list_bars_range(dataset_id, sym, interval, start_dt.isoformat(),
                                            end_dt.isoformat(), limit=MAX_INPUT_BARS + 1)
        else:
            rows = store.list_ohlc_bars_range(sym, interval, start_dt.isoformat(), end_dt.isoformat(),
                                              limit=MAX_INPUT_BARS + 1)
        bars_by_symbol[sym] = [ResearchBar(r.ts, r.open, r.high, r.low, r.close, r.volume) for r in rows]
        total_bars += len(rows)
    if total_bars > MAX_INPUT_BARS:
        store.bt_write_results(run_id, expected_from="RUNNING", status="FAILED",
                               events=[{"seq": 1, "event_type": "INPUT_BARS_LIMIT_EXCEEDED", "severity": "CRITICAL",
                                        "details": {"total_bars": total_bars, "limit": MAX_INPUT_BARS}}],
                               failure_code="INPUT_BARS_LIMIT_EXCEEDED",
                               failure_reason=f"{total_bars} input bars exceed the {MAX_INPUT_BARS} cap")
        return run_id

    coverage = coverage_preflight(bars_by_symbol, symbols, _pol, start_dt, end_dt, strategy.warmup_bars)
    if not coverage.ok:
        store.bt_write_results(run_id, expected_from="RUNNING", status="FAILED",
                               events=[{"seq": 1, "event_type": "COVERAGE_REJECTED", "severity": "CRITICAL",
                                        "details": coverage.as_dict()}],
                               failure_code=coverage.failure_code,
                               failure_reason="insufficient historical coverage",
                               missing_data_json=json.dumps(coverage.as_dict()))
        return run_id

    risk_config = {"capital": capital, "max_daily_loss_pct": _dec(risk["max_daily_loss_pct"]),
                   "max_position_risk_pct": _dec(risk["risk_per_trade_pct"]),
                   "max_portfolio_exposure_pct": _dec(risk["max_portfolio_exposure_pct"]),
                   "max_drawdown_pct": _dec(risk["max_drawdown_pct"]),
                   "warning_threshold_pct": _dec(risk["warning_threshold_pct"]), "currency": normalized["currency"]}
    result = replay(symbols=symbols, bars_by_symbol=bars_by_symbol, policy=_pol, strategy=strategy,
                    risk_config=risk_config, costs=costs, starting_capital=capital,
                    max_concurrent=normalized["max_concurrent_positions"])

    ppy = 252.0 if interval == "1D" else 252.0 * 7
    metrics = compute_metrics(result.equity_points, result.trades, starting_capital=capital, periods_per_year=ppy)
    metrics["robustness"] = robustness_report(coverage, result.trades, result.equity_points)
    checksum = _checksum(result.decisions, result.trades, result.equity_points, metrics)
    warnings = list(result.warnings)
    if metrics["robustness"].get("min_trade_warning"):
        warnings.append(metrics["robustness"]["min_trade_warning"])

    store.bt_write_results(run_id, expected_from="RUNNING", status="COMPLETED", decisions=result.decisions,
                           trades=result.trades, equity_points=result.equity_points, events=result.events,
                           metrics_json=json.dumps(metrics), result_checksum=checksum,
                           warnings_json=json.dumps(warnings),
                           missing_data_json=json.dumps(coverage.as_dict()))
    return run_id
