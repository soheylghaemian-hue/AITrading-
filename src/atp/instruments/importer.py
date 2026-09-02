"""Idempotent, resumable, per-market-isolated instrument import (WP2).

`import_instruments` walks a set of per-market listing sources and persists each discovered instrument into
the unified `instruments` table via the store's collision-safe idempotent upsert. It is built for large,
multi-market imports where partial failure must be survivable:

  * **Idempotent** — re-running the exact same request is a no-op: instruments dedup by their stable id, and
    a finished run is detected by its request checksum and returned instead of repeated.
  * **Resumable** — progress is persisted per market on a durable import-run record; a re-run of an
    interrupted import skips the markets already completed and only processes the remainder.
  * **Per-market error isolation** — each market is imported inside its own guard; a failing market is
    recorded as failed (with an error event) and the import continues with the other markets. The run
    finishes COMPLETED (all markets ok), PARTIAL (some ok, some failed) or FAILED (all markets failed).
  * **Observable** — every market start / success / failure is written as an immutable import event, and the
    run row carries live counters (discovered / inserted / updated / unchanged / skipped / failed markets).

SAFETY: reference data only. No trading, no order/execution/broker path, no market-data subscription, no
IBKR qualification. Unknown identifiers are stored as NULL — never fabricated.
AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .listing_sources import (
    ListingCandidate,
    deduplicate_listings,
    read_nasdaq_listings,
    read_other_us_listings,
)
from .model import (
    _SUBCLASS,
    _UNIT_MULTIPLIER,
    InstrumentRecord,
    MarketDataStatus,
    SourceStatus,
    TradabilityStatus,
    VerificationStatus,
    canon_decimal_text,
    sec_type_to_asset_class,
)

# A provider yields the raw listing candidates for one market. It is called lazily (once, when the market is
# processed) so a market that is already completed on resume is never even fetched. Raising from a provider
# is how a market signals failure; the importer isolates it.
ListingProvider = Callable[[], Iterable[ListingCandidate]]


@dataclass(frozen=True, slots=True)
class MarketPlan:
    """Venue-level facts that are TRUE for every instrument in a market (used to enrich listing candidates
    that only carry symbol/exchange/currency). These are real, not fabricated: a US venue genuinely sits in
    the Americas region, the US, the New York timezone and the US-equity calendar."""

    market_id: str
    region: str
    country: str
    timezone: str
    calendar: str
    default_currency: str = "USD"
    calendar_version: str = ""


@dataclass(frozen=True, slots=True)
class MarketSource:
    plan: MarketPlan
    provider: ListingProvider


@dataclass(slots=True)
class ImportSummary:
    run_id: str
    status: str
    discovered: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    completed_markets: list = field(default_factory=list)
    failed_markets: list = field(default_factory=list)
    resumed: bool = False
    already_done: bool = False


def import_request_checksum(source_label: str, market_ids: Iterable[str]) -> str:
    """Deterministic idempotency key: the source label + the SORTED set of market ids. Market order does not
    change the request identity."""
    payload = {"source": source_label, "markets": sorted(set(market_ids)),
               "tag": "atp.instrument-import.request.v1"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_from_listing(candidate: ListingCandidate, plan: MarketPlan) -> InstrumentRecord | None:
    """Map a raw listing candidate + its market plan to a unified `InstrumentRecord`. Returns ``None`` for an
    unmappable security type (the caller counts it as skipped). Only genuinely-known values are populated;
    identifiers absent from a listing file (con_id/ISIN/FIGI/CUSIP/SEDOL, settlement currency, tick size)
    stay ``None`` — NO DATA is never invented."""
    asset_class = sec_type_to_asset_class(candidate.sec_type)
    if asset_class is None:
        return None
    multiplier = "1" if asset_class in _UNIT_MULTIPLIER else None  # definitional for cash instruments only
    currency = (candidate.currency or plan.default_currency).strip() or plan.default_currency
    return InstrumentRecord(
        symbol=candidate.symbol,
        asset_class=asset_class,
        exchange=candidate.exchange,
        trading_currency=currency,
        local_symbol=candidate.symbol,
        description=(candidate.description or "").strip() or None,
        region=plan.region,
        country=plan.country,
        primary_exchange=candidate.exchange,
        settlement_currency=None,          # not present in a listing file — NO DATA
        timezone=plan.timezone,
        trading_calendar=plan.calendar,
        calendar_version=plan.calendar_version or None,
        sub_class=_SUBCLASS.get(candidate.sec_type.strip().upper()),
        multiplier=multiplier,
        lot_size=canon_decimal_text(candidate.lot_size) if candidate.lot_size else None,
        source=(candidate.source or "").strip() or None,
        source_status=SourceStatus.DISCOVERED.value,
        verification_status=VerificationStatus.UNVERIFIED.value,   # discovered, not broker-qualified
        tradability_status=TradabilityStatus.UNKNOWN.value,
        market_data_status=MarketDataStatus.UNKNOWN.value,
    )


def _new_event_id(run_id: str, seq: int) -> str:
    return f"{run_id}-e{seq}"


def import_instruments(store, *, source_label: str, markets: list[MarketSource],
                       run_id: str | None = None) -> ImportSummary:
    """Run (or resume) an instrument import across ``markets``. Safe to call repeatedly with the same inputs:
    a finished run is returned unchanged; an interrupted run is resumed from where it stopped."""
    market_ids = [m.plan.market_id for m in markets]
    checksum = import_request_checksum(source_label, market_ids)

    completed, running = store.im_find_run_by_request_checksum(checksum)
    if completed is not None:
        return _summary_from_run(store.im_get_run(completed.run_id), already_done=True)

    resumed = running is not None
    if resumed:
        run_id = running.run_id
        done = set(json.loads(running.completed_markets_json))
    else:
        run_id = run_id or uuid.uuid4().hex
        store.im_create_import_run(run_id=run_id, request_checksum=checksum,
                                   source_label=source_label, planned_markets=market_ids)
        if not store.im_advance_run_status(run_id, "PLANNED", "RUNNING"):
            # Another worker won the PLANNED→RUNNING race; fall back to resume semantics.
            current = store.im_get_run(run_id)
            done = set(json.loads(current.completed_markets_json)) if current else set()
            resumed = True
        else:
            done = set()

    # A stable, monotonic event sequence derived from the events already persisted (so a resumed run keeps
    # appending after the existing tail rather than colliding on event ids).
    seq = len(store.im_list_run_events(run_id, limit=2000))

    for market in markets:
        mid = market.plan.market_id
        if mid in done:
            continue
        seq += 1
        store.im_append_import_event(run_id, event_id=_new_event_id(run_id, seq), event_type="MARKET_START",
                                     market=mid, seq=seq, severity="INFO")
        counts = {"discovered": 0, "inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        try:
            candidates = deduplicate_listings(list(market.provider()))
            for candidate in candidates:
                record = record_from_listing(candidate, market.plan)
                if record is None:
                    counts["skipped"] += 1
                    continue
                counts["discovered"] += 1
                outcome = store.im_upsert_instrument(record.as_record())
                counts[outcome] += 1
            seq += 1
            store.im_record_market_progress(
                run_id, market=mid, market_status="COMPLETED", counts=counts,
                event={"id": _new_event_id(run_id, seq), "seq": seq, "event_type": "MARKET_OK",
                       "severity": "INFO", "details": dict(counts)})
        except Exception as exc:  # noqa: BLE001 — per-market isolation: one market must never abort the rest
            seq += 1
            store.im_record_market_progress(
                run_id, market=mid, market_status="FAILED", counts={"skipped": counts.get("discovered", 0)},
                event={"id": _new_event_id(run_id, seq), "seq": seq, "event_type": "MARKET_ERROR",
                       "severity": "ERROR",
                       "details": {"error_type": type(exc).__name__, "error": str(exc)}})

    run = store.im_get_run(run_id)
    completed_markets = json.loads(run.completed_markets_json)
    failed_markets = json.loads(run.failed_markets_json)
    if not failed_markets:
        final_status, failure = "COMPLETED", (None, None)
    elif completed_markets:
        final_status, failure = "PARTIAL", ("PARTIAL_IMPORT", f"{len(failed_markets)} market(s) failed")
    else:
        final_status, failure = "FAILED", ("ALL_MARKETS_FAILED", "every market failed")
    store.im_finalize_run(run_id, status=final_status, failure_code=failure[0], failure_reason=failure[1])
    return _summary_from_run(store.im_get_run(run_id), resumed=resumed)


def _summary_from_run(run, *, resumed: bool = False, already_done: bool = False) -> ImportSummary:
    return ImportSummary(
        run_id=run.run_id, status=run.status, discovered=run.discovered_count,
        inserted=run.inserted_count, updated=run.updated_count, unchanged=run.unchanged_count,
        skipped=run.skipped_count, completed_markets=json.loads(run.completed_markets_json),
        failed_markets=json.loads(run.failed_markets_json), resumed=resumed, already_done=already_done,
    )


# --------------------------------------------------------------------------- ready-made US market source
US_MARKET_PLAN = MarketPlan(
    market_id="US", region="AMERICAS", country="US",
    timezone="America/New_York", calendar="us_equity", default_currency="USD",
)


def nasdaq_us_listing_provider(nasdaq_path, other_path) -> ListingProvider:
    """A US listing provider backed by already-downloaded NASDAQ Trader files (public reference data). No
    network access here — downloading is a separate concern (`catalog_sync`)."""
    def _provide() -> list[ListingCandidate]:
        return read_nasdaq_listings(nasdaq_path) + read_other_us_listings(other_path)
    return _provide


def us_market_source(nasdaq_path, other_path) -> MarketSource:
    return MarketSource(plan=US_MARKET_PLAN, provider=nasdaq_us_listing_provider(nasdaq_path, other_path))
