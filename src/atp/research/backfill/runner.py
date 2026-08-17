"""§ R3.0A — backfill orchestration (idempotent, immutable, mock-driven in tests).

`run_backfill` turns a validated `DatasetRequest` into exactly one immutable dataset:

  idempotency (correction #6): an identical COMPLETED dataset is reused; an identical RUNNING/PLANNED one
    raises `BackfillConflict` (409); a FAILED one is immutable and a retry creates a NEW dataset id linked
    by `retry_of_dataset_id`.
  lifecycle (correction #5): PLANNED → RUNNING → COMPLETED|FAILED, terminal states never mutated.
  provider (corrections #3/#4): fetch split-adjusted 1-minute aggregates, REJECT a returned adjusted flag
    that is not the requested one (`PROVIDER_ADJUSTMENT_MISMATCH`), normalize RTH minutes → daily bars,
    validate minutes + daily bars, persist bars + events + finalize in ONE transaction.

This module imports NOTHING from the execution/broker/IBKR/autonomous/F2 path — it only reads market DATA
and writes the immutable research tables. It NEVER touches live `ohlc_bars`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ...store.base import new_id
from . import normalize as norm
from . import validate as val
from .dataset import DatasetRequest
from .provider import EntitlementError, MinuteAggregatesProvider, ProviderError


class BackfillConflict(Exception):
    """An identical request is already RUNNING/PLANNED (correction #6 → 409, no second dataset)."""
    code = "DATASET_REQUEST_IN_PROGRESS"


class _AdjustmentMismatch(Exception):
    code = "PROVIDER_ADJUSTMENT_MISMATCH"


def _midnight(iso_date: str) -> datetime:
    return datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)


def _find_failed_predecessor(store, request_checksum: str) -> str | None:
    for ds in store.rd_list_datasets(status="FAILED", limit=100):
        if ds.request_checksum == request_checksum:
            return ds.dataset_id
    return None


def run_backfill(store, request: DatasetRequest, provider: MinuteAggregatesProvider, *, owner: str,
                 now: datetime | None = None, allow_reuse: bool = True,
                 supersedes_dataset_id: str | None = None) -> dict:
    """Execute (or reuse) a backfill. Returns a result dict; never raises on a normal provider/validation
    failure (that becomes a FAILED dataset), only on a genuine idempotency conflict."""
    now = now or datetime.now(timezone.utc)
    checksum = request.request_checksum

    completed, running = store.rd_find_by_request_checksum(checksum)
    if allow_reuse and completed is not None:
        return {"dataset_id": completed.dataset_id, "status": "COMPLETED", "reused": True,
                "request_checksum": checksum, "dataset_checksum": completed.dataset_checksum,
                "row_count": completed.row_count}
    if running is not None:
        raise BackfillConflict(f"an identical request is already {running.status} (dataset {running.dataset_id})")

    retry_of = _find_failed_predecessor(store, checksum)
    ds_id = new_id()
    store.rd_create_dataset(
        dataset_id=ds_id, owner=owner, request_checksum=checksum,
        symbol_universe_json=json.dumps(list(request.symbols)), interval=request.interval,
        provider=request.provider, provider_contract_version=request.provider_contract_version,
        adjustment_policy=request.adjustment_policy, normalization_policy=request.normalization_policy,
        calendar_version=request.calendar_version, range_start=request.range_start,
        range_end=request.range_end, missing_minute_threshold=str(norm.MISSING_MINUTE_THRESHOLD),
        supersedes_dataset_id=supersedes_dataset_id, retry_of_dataset_id=retry_of)

    if not store.rd_advance_status(ds_id, "PLANNED", "RUNNING"):
        raise BackfillConflict(f"dataset {ds_id} could not enter RUNNING")

    start_dt, end_dt = _midnight(request.range_start), _midnight(request.range_end)
    events: list[dict] = []
    seq = 0

    def ev(event_type, *, severity="INFO", symbol=None, **details):
        nonlocal seq
        seq += 1
        events.append({"seq": seq, "ts": norm_now_iso(now), "event_type": event_type,
                       "severity": severity, "symbol": symbol, "details": details})

    try:
        all_bars: list[dict] = []
        pages_by_symbol: dict[str, list[dict]] = {}
        missing_by_symbol: dict[str, list] = {}
        warnings: list[str] = []
        provider_adjusted_seen: set[bool] = set()

        for sym in request.symbols:
            fetched = provider.fetch_minutes(sym, request.range_start, request.range_end, adjusted=True)
            provider_adjusted_seen.add(bool(fetched.adjusted))
            if not fetched.adjusted:
                raise _AdjustmentMismatch(f"provider returned adjusted={fetched.adjusted} for {sym}, "
                                          f"policy {request.adjustment_policy} requires split-adjusted")
            pages_by_symbol[sym] = fetched.pages
            ev("FETCH", symbol=sym, minutes=len(fetched.minutes), pages=len(fetched.pages),
               adjusted=fetched.adjusted)
            val.validate_minutes(sym, fetched.minutes)
            result = norm.normalize_minutes_to_daily(sym, fetched.minutes, start_dt, end_dt, now=now)
            all_bars.extend(result["bars"])
            if result["missing_sessions"]:
                missing_by_symbol[sym] = result["missing_sessions"]
            warnings.extend(f"{sym}: {w}" for w in result["warnings"])
            ev("NORMALIZE", symbol=sym, daily_bars=len(result["bars"]),
               missing_sessions=len(result["missing_sessions"]),
               out_of_session_minutes=result["out_of_session_minutes"])

        val.validate_daily_bars(all_bars)
        raw_ck = val.raw_pages_checksum(pages_by_symbol)
        data_ck = val.dataset_checksum(all_bars)
        provider_flag = all(provider_adjusted_seen) and len(provider_adjusted_seen) == 1
        ev("COMPLETE", severity="INFO", row_count=len(all_bars), dataset_checksum=data_ck)

        store.rd_write_and_finalize(
            ds_id, expected_from="RUNNING", status="COMPLETED", bars=all_bars, events=events,
            row_count=len(all_bars), raw_pages_checksum=raw_ck, dataset_checksum=data_ck,
            provider_adjusted_flag=provider_flag,
            warnings_json=(json.dumps(warnings) if warnings else None),
            missing_data_json=(json.dumps(missing_by_symbol) if missing_by_symbol else None))
        return {"dataset_id": ds_id, "status": "COMPLETED", "reused": False, "retry_of": retry_of,
                "request_checksum": checksum, "raw_pages_checksum": raw_ck, "dataset_checksum": data_ck,
                "row_count": len(all_bars), "missing_data": missing_by_symbol}

    except (_AdjustmentMismatch, val.ValidationError, ProviderError, EntitlementError) as e:
        code = getattr(e, "code", e.__class__.__name__)
        ev("FAIL", severity="ERROR", failure_code=code, reason=str(e))
        store.rd_write_and_finalize(ds_id, expected_from="RUNNING", status="FAILED", events=events,
                                    failure_code=code, failure_reason=str(e))
        return {"dataset_id": ds_id, "status": "FAILED", "reused": False, "retry_of": retry_of,
                "request_checksum": checksum, "failure_code": code, "failure_reason": str(e)}


def norm_now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()
