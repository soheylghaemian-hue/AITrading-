"""§ R3.1A / R3.1A.1 — deterministic outcome evaluator (verified dataset pin, only after maturity).

For each pending (snapshot, horizon) the outcome session is N NYSE sessions after the decision session. An
outcome is written ONLY when the horizon matured AND a FULLY VALID immutable dataset supplies both bars.
Dataset selection is a frozen deterministic policy: exact match on status/symbol/interval/calendar/provider-
contract/adjustment/normalization + range coverage, checksum RE-VERIFIED (cached within the invocation),
deterministic ordering by dataset_id. If no valid dataset yields both bars → stay PENDING (never terminally
fail just because one covering dataset lacks a bar). Prices come only from the verified dataset (never live
`ohlc_bars`, never fabricated). The observed decision price is reconciled with the dataset decision bar (never
silently replaced). Every terminal outcome carries a reproducibility checksum. Concurrency-safe/idempotent:
only a row this worker actually inserted is reported as newly matured/failed. Never enqueues a backfill.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ...store.money import money_str
from ..backfill.validate import dataset_checksum
from .. import calendars as cal
from . import policy
from .envelope import canonical_json

_BULL, _BEAR, _NEUT = "BULLISH", "BEARISH", "NEUTRAL"
# Frozen deterministic selection rule (bound into OUTCOME_POLICY_VERSION):
DATASET_SELECTION_RULE = "exact-policy-match + checksum-verified + range-cover; order by dataset_id asc"
_PRICE_TOL = Decimal("0.00000001")


def _nth_session_after(d: date, n: int) -> date:
    cur, count = d, 0
    while count < n:
        cur = cur + timedelta(days=1)
        if cal.is_session_day(cur):
            count += 1
    return cur


def _as_date(v) -> date:
    return v if isinstance(v, date) else datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()


def _verify_dataset(store, ds, cache: dict) -> bool:
    """Strongly re-verify the persisted dataset checksum from its bars. Cached within the worker invocation."""
    if ds.dataset_id in cache:
        return cache[ds.dataset_id]
    ok = False
    try:
        bars = [{"symbol": r[1], "interval": r[2], "ts": r[3], "session_date": r[4], "open": r[5], "high": r[6],
                 "low": r[7], "close": r[8], "volume": r[9], "trade_count": r[10], "adjustment_policy": r[12]}
                for r in store.rd_list_bars(ds.dataset_id, limit=60000)]
        ok = bool(ds.dataset_checksum) and dataset_checksum(bars) == ds.dataset_checksum
    except Exception:  # noqa: BLE001 — a read failure is transient; treat as unverified (stay pending)
        ok = False
    cache[ds.dataset_id] = ok
    return ok


def _valid_datasets(store, symbol: str, decision_d: date, outcome_d: date, cache: dict) -> list:
    """All FULLY VALID datasets for (symbol, [decision,outcome]) under the frozen selection rule, ordered
    deterministically by dataset_id. Exact policy match + checksum re-verified + range coverage."""
    out = []
    for ds in store.rd_list_datasets(status="COMPLETED", limit=1000):
        if (ds.interval != policy.DATASET_INTERVAL or ds.adjustment_policy != policy.ADJUSTMENT_POLICY
                or ds.calendar_version != policy.CALENDAR_VERSION
                or ds.provider_contract_version != policy.PROVIDER_CONTRACT_VERSION
                or ds.normalization_policy != policy.NORMALIZATION_POLICY):
            continue
        if symbol not in set(json.loads(ds.symbol_universe_json or "[]")):
            continue
        if not (_as_date(ds.range_start) <= decision_d and _as_date(ds.range_end) >= outcome_d):
            continue
        if not _verify_dataset(store, ds, cache):
            continue
        out.append(ds)
    return sorted(out, key=lambda d: d.dataset_id)


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


def _outcome_checksum(rec: dict) -> str:
    fields = ("snapshot_id", "snapshot_checksum", "horizon_sessions", "dataset_id", "dataset_checksum",
              "provider_contract_version", "adjustment_policy", "decision_bar_ts", "decision_price",
              "outcome_bar_ts", "outcome_price", "return_pct", "direction_expected", "direction_actual",
              "classification", "neutral_threshold_pct", "outcome_policy_version", "commit_sha")
    return "sha256:" + hashlib.sha256(canonical_json({k: rec.get(k) for k in fields}).encode()).hexdigest()


def evaluate_pending(store, *, now: datetime, commit_sha: str, max_outcomes: int | None = None) -> dict:
    """One bounded evaluator pass. Reports only outcomes THIS worker inserted (concurrency-accurate)."""
    now = now.astimezone(timezone.utc)
    done = store.ri_existing_outcome_keys()
    threshold = policy.NEUTRAL_THRESHOLD_PCT
    verify_cache: dict = {}
    matured, failed, already, pending, dataset_pending = [], [], [], 0, 0

    for snap in store.ri_list_snapshots(universe_id=policy.UNIVERSE_ID):
        dd = _as_date(snap.decision_session_date)
        for h in policy.HORIZONS:
            if (snap.snapshot_id, h) in done:
                continue
            if max_outcomes is not None and len(matured) + len(failed) >= max_outcomes:
                return _summary(matured, failed, already, pending, dataset_pending)
            try:
                od = _nth_session_after(dd, h)
                if cal.session_close_utc(od) > now:
                    pending += 1
                    continue                                    # horizon not matured → stay PENDING
                valid = _valid_datasets(store, snap.symbol, dd, od, verify_cache)
                if not valid:
                    dataset_pending += 1
                    continue                                    # no fully-valid dataset yet → stay PENDING
                dec_ts, out_ts = f"{dd.isoformat()}T00:00:00+00:00", f"{od.isoformat()}T00:00:00+00:00"
                chosen = db = ob = None
                for ds in valid:                                # deterministic order; first with BOTH bars wins
                    bars = {b.ts: b for b in store.rd_list_bars_range(ds.dataset_id, snap.symbol,
                                                                      policy.DATASET_INTERVAL, dec_ts, out_ts)}
                    if dec_ts in bars and out_ts in bars:
                        chosen, db, ob = ds, bars[dec_ts], bars[out_ts]
                        break
                if chosen is None:
                    pending += 1                                # covering-but-bar-absent → PENDING (not FAILED)
                    continue
                dclose, oclose = Decimal(str(db.close)), Decimal(str(ob.close))
                if dclose == 0:                                 # a valid dataset with a structurally bad price
                    _write(store, snap, h, chosen, dec_ts, out_ts, money_str(dclose), money_str(oclose),
                           None, None, None, None, None, now, commit_sha, status="FAILED",
                           failure_code="ZERO_DECISION_PRICE", results=(matured, failed, already))
                    continue
                ret = (oclose - dclose) / dclose * Decimal(100)
                actual = _direction_actual(ret, threshold)
                correct, classification = classify(snap.consensus_direction, actual)
                recon = _reconcile_decision_price(snap.decision_price, dclose)
                _write(store, snap, h, chosen, dec_ts, out_ts, money_str(dclose), money_str(oclose), str(ret),
                       actual, correct, classification, recon, now, commit_sha, status="MATURED",
                       results=(matured, failed, already))
            except Exception:  # noqa: BLE001 — transient (DB/read) error: leave PENDING, retry next pass
                pending += 1
                continue
    return _summary(matured, failed, already, pending, dataset_pending)


def _reconcile_decision_price(observed, dataset_close: Decimal) -> str:
    """Reconcile the snapshot's OBSERVED decision price with the authoritative dataset decision bar. The
    dataset value is always used for the return; this only records the relationship (never a silent swap)."""
    if observed in (None, ""):
        return "OBSERVED_ABSENT"
    try:
        return "MATCH" if abs(Decimal(str(observed)) - dataset_close) <= _PRICE_TOL else "MISMATCH_OBSERVED_VS_DATASET"
    except Exception:  # noqa: BLE001
        return "OBSERVED_UNPARSEABLE"


def _write(store, snap, h, ds, dec_ts, out_ts, dclose, oclose, ret, actual, correct, classification, recon,
           now, commit_sha, *, status, failure_code=None, results):
    matured, failed, already = results
    rec = {
        "snapshot_id": snap.snapshot_id, "horizon_sessions": h, "snapshot_checksum": snap.snapshot_checksum,
        "dataset_id": (ds.dataset_id if ds else None), "dataset_checksum": (ds.dataset_checksum if ds else None),
        "provider_contract_version": (ds.provider_contract_version if ds else None),
        "adjustment_policy": (ds.adjustment_policy if ds else None),
        "decision_bar_ts": dec_ts, "decision_price": dclose, "outcome_bar_ts": out_ts, "outcome_price": oclose,
        "return_pct": ret, "direction_expected": snap.consensus_direction, "direction_actual": actual,
        "direction_correct": correct, "classification": classification,
        "neutral_threshold_pct": str(policy.NEUTRAL_THRESHOLD_PCT),
        "outcome_policy_version": policy.OUTCOME_POLICY_VERSION, "status": status, "failure_code": failure_code,
        "decision_price_bar_ts": snap.decision_price_bar_ts, "decision_price_reconciliation": recon,
        "commit_sha": commit_sha}
    rec["outcome_checksum"] = _outcome_checksum(rec)
    inserted = store.ri_write_outcome(rec)                       # bool: did THIS worker insert it?
    if not inserted:
        already.append((snap.snapshot_id, h))                   # concurrent idempotent conflict, not a maturation
    elif status == "MATURED":
        matured.append((snap.snapshot_id, h))
    else:
        failed.append((snap.snapshot_id, h))


def _summary(matured, failed, already, pending, dataset_pending) -> dict:
    return {"matured": matured, "failed": failed, "already_existing": already, "pending": pending,
            "dataset_pending": dataset_pending, "matured_count": len(matured), "failed_count": len(failed),
            "already_existing_count": len(already)}
