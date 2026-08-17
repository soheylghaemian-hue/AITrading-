"""§ R3.0A — canonical dataset request identity + bounds validation.

A backfill request is identified by a canonical `request_checksum` over exactly the inputs that change the
bytes of the produced dataset: (symbol_universe, interval, range, provider, provider_contract_version,
adjustment_policy, normalization_policy, calendar_version). Two requests with the same checksum MUST
produce the same dataset, which is what makes idempotency (correction #6) and explicit dataset pinning
(correction #7) sound. Bounds (correction #2): US equities, interval 1D only, symbols ⊆ the approved
R3.0A universe (NVDA/AAPL/SPY), range within the versioned calendar coverage and no later than the last
completed session. This module is pure — it performs NO I/O and touches NO order/broker/execution path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .. import calendars as cal
from . import normalize as norm

# Correction #2 — the only symbols R3.0A is approved to backfill, and the approved start of history.
R30A_SYMBOL_UNIVERSE = ("AAPL", "NVDA", "SPY")
R30A_INTERVAL = "1D"
R30A_RANGE_START = date(2023, 1, 3)
MAX_SYMBOLS = 3


class DatasetRequestError(Exception):
    code = "DATASET_REQUEST_INVALID"


@dataclass(frozen=True, slots=True)
class DatasetRequest:
    symbols: tuple[str, ...]           # canonical: upper-cased, de-duplicated, sorted
    interval: str
    range_start: str                   # ISO date
    range_end: str                     # ISO date
    provider: str
    provider_contract_version: str
    adjustment_policy: str
    normalization_policy: str
    calendar_version: str

    def canonical_payload(self) -> dict:
        return {
            "symbols": list(self.symbols), "interval": self.interval,
            "range_start": self.range_start, "range_end": self.range_end,
            "provider": self.provider, "provider_contract_version": self.provider_contract_version,
            "adjustment_policy": self.adjustment_policy, "normalization_policy": self.normalization_policy,
            "calendar_version": self.calendar_version,
        }

    @property
    def request_checksum(self) -> str:
        blob = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def _as_date(v: str | date) -> date:
    return v if isinstance(v, date) else datetime.fromisoformat(v).date()


def _count_sessions(start: date, end: date) -> int:
    from datetime import timedelta
    n, d = 0, start
    while d <= end:
        if cal.is_session_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def build_request(symbols, interval, range_start, range_end, *, now: datetime | None = None,
                  provider: str = norm.PROVIDER,
                  provider_contract_version: str = norm.PROVIDER_CONTRACT_VERSION,
                  adjustment_policy: str = norm.ADJUSTMENT_POLICY,
                  normalization_policy: str = norm.NORMALIZATION_POLICY,
                  calendar_version: str = cal.CALENDAR_VERSION) -> DatasetRequest:
    """Validate + canonicalize a backfill request. Raises DatasetRequestError on any bounds violation."""
    syms = tuple(sorted({(s or "").strip().upper() for s in symbols}))
    if not syms:
        raise DatasetRequestError("at least one symbol is required")
    if len(syms) > MAX_SYMBOLS:
        raise DatasetRequestError(f"at most {MAX_SYMBOLS} symbols per R3.0A request, got {len(syms)}")
    unknown = [s for s in syms if s not in R30A_SYMBOL_UNIVERSE]
    if unknown:
        raise DatasetRequestError(f"symbols not in the approved R3.0A universe {R30A_SYMBOL_UNIVERSE}: {unknown}")

    iv = cal.normalize_interval(interval)
    if iv != R30A_INTERVAL:
        raise DatasetRequestError(f"R3.0A supports interval {R30A_INTERVAL} only (no intraday), got '{interval}'")

    start, end = _as_date(range_start), _as_date(range_end)
    if start > end:
        raise DatasetRequestError(f"range_start {start} is after range_end {end}")
    if start < R30A_RANGE_START:
        raise DatasetRequestError(f"range_start {start} precedes approved history start {R30A_RANGE_START}")
    if not cal.calendar_covers(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                               datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)):
        raise DatasetRequestError(f"range [{start}, {end}] is outside versioned calendar coverage "
                                  f"[{cal.CALENDAR_START}, {cal.CALENDAR_END}]")
    last_done = norm.last_completed_session(now)
    if end > last_done:
        raise DatasetRequestError(f"range_end {end} is after the last completed session {last_done} "
                                  f"(the in-progress/future session is never backfilled)")

    # A range with ZERO expected NYSE sessions (e.g. a weekend-only or holiday-only span) can never yield a
    # bar; reject it so it never becomes an empty COMPLETED dataset.
    if _count_sessions(start, end) == 0:
        raise DatasetRequestError(f"range [{start}, {end}] contains no NYSE trading sessions "
                                  f"(weekend/holiday-only ranges are rejected)")

    if provider != norm.PROVIDER:
        raise DatasetRequestError(f"unexpected provider '{provider}'")
    if adjustment_policy != norm.ADJUSTMENT_POLICY:
        raise DatasetRequestError(f"unexpected adjustment policy '{adjustment_policy}'")
    if normalization_policy != norm.NORMALIZATION_POLICY:
        raise DatasetRequestError(f"unexpected normalization policy '{normalization_policy}'")

    return DatasetRequest(symbols=syms, interval=iv, range_start=start.isoformat(), range_end=end.isoformat(),
                          provider=provider, provider_contract_version=provider_contract_version,
                          adjustment_policy=adjustment_policy, normalization_policy=normalization_policy,
                          calendar_version=calendar_version)
