"""§ R3.0A / R3.0A.1 — backfill orchestration, split into a fast enqueue and a durable chunked worker.

The HTTP path (`enqueue_backfill`) NEVER performs provider I/O: it validates + idempotently creates or
reuses a PLANNED dataset and returns immediately. A separate, systemd-friendly one-shot worker
(`claim_and_run` / `execute_dataset`, see `worker.py`) executes the backfill OUTSIDE atp-control with its
own DB connection:

  idempotency (correction #6): an identical COMPLETED dataset is reused; an identical PLANNED/RUNNING one
    is returned as-is; a FAILED one is immutable and a retry creates a NEW dataset id linked by
    `retry_of_dataset_id`.
  lifecycle (correction #5): PLANNED → RUNNING → COMPLETED|FAILED, terminal states never mutated; a crashed
    worker's stale RUNNING row is reclaimed to FAILED before any retry.
  BOUNDED memory (R3.0A.1): fetch in bounded, session-aligned date CHUNKS; validate minute ordering +
    uniqueness within AND across chunks; insert each chunk's normalized bars incrementally while RUNNING;
    keep a STREAMING raw-pages checksum; recompute the final dataset checksum from the PERSISTED bars; write
    the terminal status + checksums + final event atomically. The full multi-year raw page set is never
    retained in memory.

This module imports NOTHING from the execution/broker/IBKR/autonomous/F2 path — it only reads market DATA
and writes the immutable research tables. It NEVER touches live `ohlc_bars`.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from ...store.base import new_id
from . import normalize as norm
from . import validate as val
from .dataset import DatasetRequest
from .provider import EntitlementError, MinuteAggregatesProvider, ProviderError
from .validate import StreamingPagesChecksum, dataset_checksum

# --- v2 (chunked) contract bounds — fixed constants so the streaming raw-pages checksum is deterministic.
CHUNK_SESSIONS = 20            # session-aligned sessions per provider request chunk
MAX_PAGES_PER_CHUNK = 12       # hard page cap per chunk fetch (else PROVIDER_PAGE_LIMIT_EXCEEDED)
MAX_RESULTS_PER_CHUNK = 200_000
STALE_RUNNING_AFTER_S = 900    # a RUNNING dataset with no heartbeat for this long is a crashed worker


class BackfillConflict(Exception):
    """An identical request is already RUNNING (correction #6 → 409, no second dataset)."""
    code = "DATASET_REQUEST_IN_PROGRESS"


class _AdjustmentMismatch(Exception):
    code = "PROVIDER_ADJUSTMENT_MISMATCH"


class _NotRunning(Exception):
    code = "DATASET_NOT_RUNNING"


def _midnight(iso_date: str) -> datetime:
    return datetime.fromisoformat(iso_date).replace(tzinfo=timezone.utc)


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _find_failed_predecessor(store, request_checksum: str) -> str | None:
    for ds in store.rd_list_datasets(status="FAILED", limit=100):
        if ds.request_checksum == request_checksum:
            return ds.dataset_id
    return None


# --------------------------------------------------------------------------- ENQUEUE (fast, no provider I/O)
def enqueue_backfill(store, request: DatasetRequest, *, owner: str,
                     supersedes_dataset_id: str | None = None) -> dict:
    """Idempotently create or reuse a PLANNED dataset. Performs NO provider network I/O and NO normalization
    — safe to call inside an HTTP request. Returns {dataset_id, status, reused, created, retry_of,
    request_checksum}."""
    checksum = request.request_checksum
    completed, running = store.rd_find_by_request_checksum(checksum)
    if completed is not None:
        return {"dataset_id": completed.dataset_id, "status": "COMPLETED", "reused": True, "created": False,
                "retry_of": completed.retry_of_dataset_id, "request_checksum": checksum}
    if running is not None:   # an identical PLANNED or RUNNING dataset already exists → return it as-is
        return {"dataset_id": running.dataset_id, "status": running.status, "reused": False, "created": False,
                "retry_of": running.retry_of_dataset_id, "request_checksum": checksum}
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
    return {"dataset_id": ds_id, "status": "PLANNED", "reused": False, "created": True,
            "retry_of": retry_of, "request_checksum": checksum}


# --------------------------------------------------------------------------- WORKER (chunked, bounded)
def _session_days(start: date, end: date, last_completed: date) -> list[date]:
    out, d = [], start
    stop = min(end, last_completed)
    from .. import calendars as cal
    while d <= stop:
        if cal.is_session_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _chunk(days: list[date], n: int):
    for i in range(0, len(days), n):
        yield days[i:i + n]


def _persisted_bars(store, dataset_id: str) -> list[dict]:
    out = []
    for r in store.rd_list_bars(dataset_id, limit=60000):
        # (dataset_id,symbol,interval,ts,session_date,open,high,low,close,volume,trade_count,source,adj)
        out.append({"symbol": r[1], "interval": r[2], "ts": r[3], "session_date": r[4],
                    "open": r[5], "high": r[6], "low": r[7], "close": r[8], "volume": r[9],
                    "trade_count": r[10], "adjustment_policy": r[12]})
    return out


def claim_dataset(store, dataset_id: str) -> bool:
    """Atomically claim a PLANNED dataset as RUNNING. Exactly one caller can win (the UPDATE is guarded on
    status='PLANNED'); a concurrent claim returns False."""
    return store.rd_advance_status(dataset_id, "PLANNED", "RUNNING")


def execute_claimed(store, dataset_id: str, provider: MinuteAggregatesProvider, *, now: datetime | None = None,
                    chunk_sessions: int = CHUNK_SESSIONS, max_pages: int = MAX_PAGES_PER_CHUNK,
                    max_results: int = MAX_RESULTS_PER_CHUNK) -> dict:
    """Execute an ALREADY-CLAIMED (RUNNING) dataset with bounded, session-aligned chunking. Never raises on a
    normal provider/validation failure (that becomes a FAILED dataset)."""
    now = now or datetime.now(timezone.utc)
    ds = store.rd_get_dataset(dataset_id)
    symbols = tuple(json.loads(ds.symbol_universe_json))   # canonical (sorted) at request build time
    start, end = date.fromisoformat(ds.range_start), date.fromisoformat(ds.range_end)
    days = _session_days(start, end, norm.last_completed_session(now))

    stream = StreamingPagesChecksum()
    last_minute_ts: dict[str, datetime] = {}
    last_bar_ts: dict[str, str] = {}
    missing_by_symbol: dict[str, list] = {}
    warnings: list[str] = []
    adjusted_seen: set[bool] = set()
    total_rows = 0
    seq = 0
    from .. import calendars as cal

    try:
        for ci, chunk in enumerate(_chunk(days, chunk_sessions)):
            cstart, cend = chunk[0], chunk[-1]
            cstart_dt, cend_dt = _midnight(cstart.isoformat()), _midnight(cend.isoformat())
            chunk_bars: list[dict] = []
            for sym in symbols:
                fetched = provider.fetch_minutes(sym, cstart.isoformat(), cend.isoformat(), adjusted=True,
                                                 max_pages=max_pages, max_results=max_results)
                adjusted_seen.add(bool(fetched.adjusted))
                if not fetched.adjusted:
                    raise _AdjustmentMismatch(f"provider returned adjusted={fetched.adjusted} for {sym}; "
                                              f"policy {ds.adjustment_policy} requires split-adjusted")
                val.validate_minutes(sym, fetched.minutes)                       # within-chunk order/uniqueness
                if fetched.minutes:                                              # cross-chunk order/uniqueness
                    if last_minute_ts.get(sym) is not None and fetched.minutes[0].ts <= last_minute_ts[sym]:
                        raise val.ValidationError(f"{sym} cross-chunk minute overlap/out-of-order at chunk {ci}")
                    last_minute_ts[sym] = fetched.minutes[-1].ts
                for pg in fetched.pages:
                    stream.update(sym, pg)                                       # streaming raw-pages checksum
                result = norm.normalize_minutes_to_daily(sym, fetched.minutes, cstart_dt, cend_dt, now=now)
                for bar in result["bars"]:
                    bts = cal.norm_ts(bar["ts"])
                    if last_bar_ts.get(sym) is not None and bts <= last_bar_ts[sym]:
                        raise val.ValidationError(f"{sym} cross-chunk bar overlap/out-of-order at chunk {ci}")
                    last_bar_ts[sym] = bts
                chunk_bars.extend(result["bars"])
                if result["missing_sessions"]:
                    missing_by_symbol.setdefault(sym, []).extend(result["missing_sessions"])
                warnings.extend(f"{sym}: {w}" for w in result["warnings"])
            val.validate_daily_bars(chunk_bars)
            seq += 1
            ev = {"seq": seq, "ts": _now_iso(now), "event_type": "CHUNK", "severity": "INFO", "symbol": None,
                  "details": {"chunk_index": ci, "sessions": [cstart.isoformat(), cend.isoformat()],
                              "bars": len(chunk_bars)}}
            if not store.rd_append_bars(dataset_id, chunk_bars, events=[ev]):
                raise _NotRunning(f"dataset {dataset_id} is no longer RUNNING (reclaimed as stale?)")
            total_rows += len(chunk_bars)

        persisted = _persisted_bars(store, dataset_id)
        data_ck = dataset_checksum(persisted)                                    # recomputed from PERSISTED bars
        raw_ck = stream.hexdigest()
        provider_flag = True if adjusted_seen == {True} else None
        seq += 1
        final_ev = {"seq": seq, "ts": _now_iso(now), "event_type": "COMPLETE", "severity": "INFO",
                    "symbol": None, "details": {"row_count": total_rows, "dataset_checksum": data_ck,
                                                "chunks": seq - 1}}
        store.rd_write_and_finalize(
            dataset_id, expected_from="RUNNING", status="COMPLETED", bars=(), events=[final_ev],
            row_count=total_rows, raw_pages_checksum=raw_ck, dataset_checksum=data_ck,
            provider_adjusted_flag=provider_flag,
            warnings_json=(json.dumps(warnings) if warnings else None),
            missing_data_json=(json.dumps(missing_by_symbol) if missing_by_symbol else None))
        return {"dataset_id": dataset_id, "status": "COMPLETED", "row_count": total_rows,
                "raw_pages_checksum": raw_ck, "dataset_checksum": data_ck, "missing_data": missing_by_symbol}

    except (_AdjustmentMismatch, val.ValidationError, ProviderError, EntitlementError, _NotRunning) as e:
        code = getattr(e, "code", e.__class__.__name__)
        seq += 1
        fev = {"seq": seq, "ts": _now_iso(now), "event_type": "FAIL", "severity": "ERROR", "symbol": None,
               "details": {"failure_code": code, "reason": str(e)}}
        # If the dataset was reclaimed to FAILED mid-run, this finalize is a no-op (not RUNNING) — that's
        # correct: the terminal record stands and is immutable.
        store.rd_write_and_finalize(dataset_id, expected_from="RUNNING", status="FAILED", bars=(),
                                    events=[fev], failure_code=code, failure_reason=str(e),
                                    missing_data_json=(json.dumps(missing_by_symbol) if missing_by_symbol else None))
        return {"dataset_id": dataset_id, "status": "FAILED", "failure_code": code, "failure_reason": str(e)}


def execute_dataset(store, dataset_id: str, provider: MinuteAggregatesProvider, **kw) -> dict | None:
    """Claim a PLANNED dataset then execute it. Returns None if it could not be claimed (already claimed /
    not PLANNED)."""
    if not claim_dataset(store, dataset_id):
        return None
    return execute_claimed(store, dataset_id, provider, **kw)


def reclaim_stale(store, *, now: datetime | None = None, stale_after_s: int = STALE_RUNNING_AFTER_S) -> list[str]:
    """Bounded stale recovery: atomically reclaim crashed RUNNING datasets to FAILED (a fresh heartbeat before
    the flip prevents reclamation). Returns only the ids actually transitioned."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=stale_after_s)).astimezone(timezone.utc).isoformat()
    return store.rd_reclaim_stale_running(
        cutoff, failure_code="STALE_RUNNING_RECLAIMED",
        failure_reason="worker heartbeat exceeded the stale threshold; reclaimed as FAILED")


def process_one(store, dataset_id: str, provider: MinuteAggregatesProvider, *, now: datetime | None = None,
                **bounds) -> dict:
    """Process EXACTLY the named dataset. Honestly rejects an unknown / already-terminal / already-RUNNING
    dataset (no side effects) rather than silently picking a different one. Returns a result dict whose
    `status` is COMPLETED / FAILED, or ERROR with an `error_code` when the dataset could not be claimed."""
    ds = store.rd_get_dataset(dataset_id)
    if ds is None:
        return {"dataset_id": dataset_id, "status": "ERROR", "error_code": "DATASET_NOT_FOUND"}
    if ds.status != "PLANNED":
        return {"dataset_id": dataset_id, "status": "ERROR", "error_code": "DATASET_NOT_PLANNED",
                "actual_status": ds.status}
    if not claim_dataset(store, dataset_id):
        return {"dataset_id": dataset_id, "status": "ERROR", "error_code": "CLAIM_CONFLICT"}
    return execute_claimed(store, dataset_id, provider, now=now, **bounds)


def claim_next_one(store, provider: MinuteAggregatesProvider, *, now: datetime | None = None, **bounds) -> dict | None:
    """Explicit `--next` mode, HARD-CAPPED to ONE dataset: pick the oldest PLANNED and process only it. Never
    drains the queue. Returns None when there is no PLANNED dataset."""
    planned = store.rd_list_datasets(status="PLANNED", limit=1)
    if not planned:
        return None
    return process_one(store, planned[0].dataset_id, provider, now=now, **bounds)


# --------------------------------------------------------------------------- convenience (tests / local)
def run_backfill(store, request: DatasetRequest, provider: MinuteAggregatesProvider, *, owner: str,
                 now: datetime | None = None, supersedes_dataset_id: str | None = None, **bounds) -> dict:
    """Enqueue + synchronously execute in one call (tests / local / demo only — NOT the HTTP path). Reuses a
    COMPLETED dataset; raises BackfillConflict if an identical dataset is already RUNNING elsewhere."""
    enq = enqueue_backfill(store, request, owner=owner, supersedes_dataset_id=supersedes_dataset_id)
    if enq["status"] == "COMPLETED":
        d = store.rd_get_dataset(enq["dataset_id"])
        return {"dataset_id": d.dataset_id, "status": "COMPLETED", "reused": True, "created": False,
                "retry_of": enq["retry_of"], "request_checksum": enq["request_checksum"],
                "dataset_checksum": d.dataset_checksum, "raw_pages_checksum": d.raw_pages_checksum,
                "row_count": d.row_count}
    if enq["status"] == "RUNNING":
        raise BackfillConflict(f"an identical request is already RUNNING (dataset {enq['dataset_id']})")
    res = execute_dataset(store, enq["dataset_id"], provider, now=now, **bounds)
    if res is None:
        raise BackfillConflict(f"could not claim dataset {enq['dataset_id']}")
    res.update({"reused": False, "created": enq["created"], "retry_of": enq["retry_of"],
                "request_checksum": enq["request_checksum"]})
    return res
