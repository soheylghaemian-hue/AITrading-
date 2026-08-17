"""§ R3.0A — dataset read-models (JSON for the API / frontend).

Pure projections over the immutable dataset tables. "superseded_by" is DERIVED here (correction #5) from
`rd_superseded_by` — there is no SUPERSEDED status and nothing is mutated to compute it. No I/O beyond the
read-only store methods; no order/broker/execution path.
"""
from __future__ import annotations

import json

from ...store.base import ResearchDatasetRow


def dataset_summary(store, ds: ResearchDatasetRow) -> dict:
    return {
        "dataset_id": ds.dataset_id, "owner": ds.owner, "status": ds.status,
        "symbols": _load(ds.symbol_universe_json, []), "interval": ds.interval,
        "provider": ds.provider, "provider_contract_version": ds.provider_contract_version,
        "adjustment_policy": ds.adjustment_policy, "normalization_policy": ds.normalization_policy,
        "calendar_version": ds.calendar_version, "range_start": ds.range_start, "range_end": ds.range_end,
        "row_count": ds.row_count, "dataset_checksum": ds.dataset_checksum,
        "raw_pages_checksum": ds.raw_pages_checksum, "request_checksum": ds.request_checksum,
        "provider_adjusted_flag": ds.provider_adjusted_flag,
        "supersedes_dataset_id": ds.supersedes_dataset_id, "retry_of_dataset_id": ds.retry_of_dataset_id,
        "superseded_by": store.rd_superseded_by(ds.dataset_id),
        "failure_code": ds.failure_code, "failure_reason": ds.failure_reason,
        "created_at": ds.created_at, "started_at": ds.started_at, "ended_at": ds.ended_at,
    }


def dataset_detail(store, ds: ResearchDatasetRow) -> dict:
    out = dataset_summary(store, ds)
    out["missing_minute_threshold"] = ds.missing_minute_threshold
    out["warnings"] = _load(ds.warnings_json, [])
    out["missing_data"] = _load(ds.missing_data_json, {})
    out["events"] = [{"seq": e.seq, "ts": e.ts, "event_type": e.event_type, "severity": e.severity,
                      "symbol": e.symbol, "details": _load(e.details_json, {})}
                     for e in store.rd_list_events(ds.dataset_id, limit=2000)]
    return out


def dataset_coverage(store, ds: ResearchDatasetRow) -> dict:
    """Per-symbol bar count + first/last session, plus the recorded missing sessions."""
    symbols = _load(ds.symbol_universe_json, [])
    per_symbol = []
    for sym in symbols:
        rows = store.rd_list_bars(ds.dataset_id, symbol=sym)
        first = rows[0][3] if rows else None    # ts is column index 3
        last = rows[-1][3] if rows else None
        per_symbol.append({"symbol": sym, "bar_count": len(rows), "first_ts": first, "last_ts": last})
    return {"dataset_id": ds.dataset_id, "status": ds.status, "interval": ds.interval,
            "range_start": ds.range_start, "range_end": ds.range_end,
            "adjustment_policy": ds.adjustment_policy, "dataset_checksum": ds.dataset_checksum,
            "per_symbol": per_symbol, "missing_data": _load(ds.missing_data_json, {})}


def datasets_list(store, rows: list[ResearchDatasetRow]) -> dict:
    return {"datasets": [dataset_summary(store, r) for r in rows], "count": len(rows)}


def _load(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default
