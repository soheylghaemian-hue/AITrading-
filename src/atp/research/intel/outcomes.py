"""§ R3.1A — deterministic outcome evaluator (pins a COMPLETED immutable dataset ONLY after maturity).

For each pending (snapshot, horizon): the outcome session is N NYSE sessions after the decision session.
An outcome is written ONLY when the horizon has matured (its close is in the past) AND a COMPLETED immutable
research dataset covers both the decision and outcome sessions. Prices come exclusively from that dataset
(never live `ohlc_bars`, never fabricated). Failure semantics: not-matured / no-dataset / transient error →
stay PENDING (no row, retryable); a required bar missing inside a covering dataset (halt/delisted) → terminal
FAILED. Completed outcomes are terminal + DB-immutable. Read-only wrt trading; never enqueues a backfill.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .. import calendars as cal
from . import policy

_BULL, _BEAR, _NEUT = "BULLISH", "BEARISH", "NEUTRAL"


def _nth_session_after(d: date, n: int) -> date:
    cur, count = d, 0
    while count < n:
        cur = cur + timedelta(days=1)
        if cal.is_session_day(cur):
            count += 1
    return cur


def _as_date(v) -> date:
    return v if isinstance(v, date) else datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()


def _find_dataset(store, symbol: str, decision_d: date, outcome_d: date):
    """A COMPLETED immutable dataset covering [decision, outcome] for the symbol under the pilot's exact
    interval/adjustment/calendar. Returns the dataset row, or None (→ stay PENDING). Never live `ohlc_bars`."""
    for ds in store.rd_list_datasets(status="COMPLETED", limit=200):
        if ds.interval != policy.DATASET_INTERVAL or ds.adjustment_policy != policy.ADJUSTMENT_POLICY:
            continue
        if ds.calendar_version != policy.CALENDAR_VERSION:
            continue
        if symbol not in set(json.loads(ds.symbol_universe_json or "[]")):
            continue
        if _as_date(ds.range_start) <= decision_d and _as_date(ds.range_end) >= outcome_d:
            return ds
    return None


def classify(expected: str | None, actual: str) -> tuple[bool | None, str]:
    if expected not in (_BULL, _BEAR, _NEUT):
        return None, "ABSTAIN"
    correct = expected == actual
    return correct, ("CORRECT" if correct else "INCORRECT")


def _direction_actual(ret: Decimal, threshold: Decimal) -> str:
    if ret > threshold:
        return _BULL
    if ret < -threshold:
        return _BEAR
    return _NEUT


def evaluate_pending(store, *, now: datetime, commit_sha: str, max_outcomes: int | None = None) -> dict:
    """One bounded evaluator pass. Returns {matured, failed, pending, dataset_pending}. Idempotent +
    concurrency-safe (each outcome is written once via ON CONFLICT; terminal + immutable)."""
    now = now.astimezone(timezone.utc)
    done = store.ri_existing_outcome_keys()
    threshold = policy.NEUTRAL_THRESHOLD_PCT
    matured, failed, pending, dataset_pending = [], [], 0, 0

    for snap in store.ri_list_snapshots(universe_id=policy.UNIVERSE_ID):
        dd = _as_date(snap.decision_session_date)
        for h in policy.HORIZONS:
            if (snap.snapshot_id, h) in done:
                continue
            if max_outcomes is not None and len(matured) + len(failed) >= max_outcomes:
                return _summary(matured, failed, pending, dataset_pending)
            try:
                od = _nth_session_after(dd, h)
                if cal.session_close_utc(od) > now:
                    pending += 1
                    continue                                    # horizon not matured → stay PENDING
                ds = _find_dataset(store, snap.symbol, dd, od)
                if ds is None:
                    dataset_pending += 1
                    continue                                    # no covering dataset yet → stay PENDING
                dec_ts = f"{dd.isoformat()}T00:00:00+00:00"
                out_ts = f"{od.isoformat()}T00:00:00+00:00"
                bars = {b.ts: b for b in store.rd_list_bars_range(ds.dataset_id, snap.symbol,
                                                                  policy.DATASET_INTERVAL, dec_ts, out_ts)}
                db, ob = bars.get(dec_ts), bars.get(out_ts)
                if db is None or ob is None:                    # covering dataset but a required bar absent
                    _write(store, snap, h, ds, dec_ts, out_ts, None, None, None, None, None, None,
                           now, commit_sha, status="FAILED", failure_code="MISSING_OUTCOME_BAR")
                    failed.append((snap.snapshot_id, h))
                    continue
                dclose, oclose = Decimal(str(db.close)), Decimal(str(ob.close))
                if dclose == 0:
                    _write(store, snap, h, ds, dec_ts, out_ts, str(dclose), str(oclose), None, None, None,
                           None, now, commit_sha, status="FAILED", failure_code="ZERO_DECISION_PRICE")
                    failed.append((snap.snapshot_id, h))
                    continue
                ret = (oclose - dclose) / dclose * Decimal(100)
                actual = _direction_actual(ret, threshold)
                correct, classification = classify(snap.consensus_direction, actual)
                _write(store, snap, h, ds, dec_ts, out_ts, str(dclose), str(oclose), str(ret), actual,
                       correct, classification, now, commit_sha, status="MATURED")
                matured.append((snap.snapshot_id, h))
            except Exception:  # noqa: BLE001 — transient (DB/read) error: leave PENDING, retry next pass
                pending += 1
                continue
    return _summary(matured, failed, pending, dataset_pending)


def _write(store, snap, h, ds, dec_ts, out_ts, dclose, oclose, ret, actual, correct, classification,
           now, commit_sha, *, status, failure_code=None):
    store.ri_write_outcome({
        "snapshot_id": snap.snapshot_id, "horizon_sessions": h, "snapshot_checksum": snap.snapshot_checksum,
        "dataset_id": (ds.dataset_id if ds else None), "dataset_checksum": (ds.dataset_checksum if ds else None),
        "provider_contract_version": (ds.provider_contract_version if ds else None),
        "adjustment_policy": (ds.adjustment_policy if ds else None),
        "decision_bar_ts": dec_ts, "decision_price": dclose, "outcome_bar_ts": out_ts, "outcome_price": oclose,
        "return_pct": ret, "direction_expected": snap.consensus_direction, "direction_actual": actual,
        "direction_correct": correct, "classification": classification,
        "neutral_threshold_pct": str(policy.NEUTRAL_THRESHOLD_PCT),
        "outcome_policy_version": policy.OUTCOME_POLICY_VERSION, "status": status, "failure_code": failure_code,
        "commit_sha": commit_sha})


def _summary(matured, failed, pending, dataset_pending) -> dict:
    return {"matured": matured, "failed": failed, "pending": pending, "dataset_pending": dataset_pending,
            "matured_count": len(matured), "failed_count": len(failed)}
