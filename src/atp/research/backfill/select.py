"""§ R3.0A — explicit dataset selection for a backtest (correction #7).

A backtest MUST pin an explicit COMPLETED dataset — there is no implicit "latest". `validate_selection`
checks the pinned dataset is COMPLETED, contains every requested symbol, matches the interval, covers the
range, matches the calendar + adjustment + normalization policies, and passes a byte-level checksum
re-verification of its persisted bars. It returns (dataset_row, pin_dict, errors); a non-empty `errors`
list means the selection is invalid and the caller must reject the backtest. Read-only; no order/broker
path.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from .. import calendars as cal
from . import normalize as norm
from .validate import dataset_checksum


def _as_date(v) -> date:
    if isinstance(v, date):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()


def _bars_for_checksum(store, dataset_id: str) -> list[dict]:
    out = []
    for r in store.rd_list_bars(dataset_id, limit=60000):
        # (dataset_id,symbol,interval,ts,session_date,open,high,low,close,volume,trade_count,source,adjustment_policy)
        out.append({"symbol": r[1], "interval": r[2], "ts": r[3], "session_date": r[4],
                    "open": r[5], "high": r[6], "low": r[7], "close": r[8], "volume": r[9],
                    "trade_count": r[10], "adjustment_policy": r[12]})
    return out


def validate_selection(store, dataset_id: str, symbols, interval: str, start, end):
    """Returns (dataset_row_or_None, pin_dict_or_None, errors)."""
    errors: list[str] = []
    ds = store.rd_get_dataset(dataset_id) if dataset_id else None
    if ds is None:
        return None, None, [f"dataset '{dataset_id}' not found"]
    if ds.status != "COMPLETED":
        errors.append(f"dataset {dataset_id} is {ds.status}, not COMPLETED")

    ds_symbols = set(json.loads(ds.symbol_universe_json or "[]"))
    missing = [s for s in symbols if s not in ds_symbols]
    if missing:
        errors.append(f"dataset does not contain requested symbols: {missing}")

    if cal.normalize_interval(interval) != ds.interval:
        errors.append(f"interval mismatch: request {interval} vs dataset {ds.interval}")

    try:
        req_start, req_end = _as_date(start), _as_date(end)
        if req_start < _as_date(ds.range_start) or req_end > _as_date(ds.range_end):
            errors.append(f"dataset range [{ds.range_start}, {ds.range_end}] does not cover "
                          f"requested [{req_start}, {req_end}]")
    except (ValueError, TypeError):
        errors.append("request start/end are not valid dates")

    if ds.calendar_version != cal.CALENDAR_VERSION:
        errors.append(f"calendar mismatch: dataset {ds.calendar_version} vs {cal.CALENDAR_VERSION}")
    if ds.adjustment_policy != norm.ADJUSTMENT_POLICY:
        errors.append(f"adjustment policy mismatch: dataset {ds.adjustment_policy}")
    if ds.normalization_policy != norm.NORMALIZATION_POLICY:
        errors.append(f"normalization policy mismatch: dataset {ds.normalization_policy}")

    # Byte-level checksum re-verification of the persisted bars.
    if ds.status == "COMPLETED":
        recomputed = dataset_checksum(_bars_for_checksum(store, dataset_id))
        if recomputed != ds.dataset_checksum:
            errors.append(f"dataset checksum verification FAILED (stored {ds.dataset_checksum}, "
                          f"recomputed {recomputed})")

    pin = {"dataset_id": ds.dataset_id, "provider": ds.provider,
           "provider_contract_version": ds.provider_contract_version,
           "adjustment_policy": ds.adjustment_policy, "normalization_policy": ds.normalization_policy,
           "calendar_version": ds.calendar_version, "checksum": ds.dataset_checksum}
    return ds, (pin if not errors else None), errors
