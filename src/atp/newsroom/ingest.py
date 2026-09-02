"""WP5 — the resumable, fault-tolerant news/filings ingest orchestrator.

Pulls read-only messages from a `NewsProvider`, maps them fail-closed to the WP2 catalogue, deduplicates and
clusters, links corrections/retractions to the immutable original, license-gates storage, and persists — all
with per-provider / per-region / per-message error isolation, cursor-based resumability, and an append-only
audit trail. Never fabricates: a missing text/timestamp stays NULL, a future publish time is flagged as a
conflict, a translation is never stored as the original, a rumor never becomes confirmed, and a secondary
source is never relabeled primary. A provider outage yields UNAVAILABLE, not a frozen 'fresh' state.

SAFETY: read-only news data only. No orders, no execution, no account, no subscription/news purchase, no new
credentials, no real network in CI, no HTTP write path. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .model import (
    LicenseStatus,
    NewsMessage,
    StorageStatus,
    message_id_for,
    utc_ts,
)
from .provider import (
    NewsProvider,
    NewsProviderRateLimitedError,
    NewsProviderUnavailableError,
)

# The only license statuses under which the licensed BODY may be persisted; any other status (UNKNOWN,
# METADATA_ONLY, NO_LICENSE) forces metadata-only storage regardless of a provider's storage_allowed flag.
# Stored as value-strings so the gate is robust whether the provider reports an enum or a bare string.
_STORAGE_LICENSES = frozenset({LicenseStatus.LICENSED_STORE_REDISTRIBUTE.value,
                               LicenseStatus.LICENSED_STORE_ONLY.value})


def _license_value(status) -> str:
    """Normalize a license status (enum or string) to its canonical value-string; fail-closed to '' on None."""
    if status is None:
        return ""
    return status.value if isinstance(status, LicenseStatus) else str(status)


@dataclass(frozen=True, slots=True)
class IngestConfig:
    page_limit: int = 100
    max_pages: int = 50
    start_cursor: str | None = None


@dataclass(slots=True)
class IngestSummary:
    run_id: str
    status: str
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    ambiguous: int = 0
    corrections: int = 0
    retractions: int = 0
    unmapped: int = 0
    error: int = 0
    completed_regions: list = field(default_factory=list)
    failed_regions: list = field(default_factory=list)


def ingest_request_checksum(run_label: str, provider: str, source_id: str) -> str:
    payload = {"label": run_label, "provider": provider, "source_id": source_id,
               "tag": "atp.news-ingest.request.v1"}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _region_of(item) -> str:
    if item.regions:
        return str(item.regions[0])
    if item.countries:
        return str(item.countries[0])
    return "GLOBAL"


def ingest_news(store, provider: NewsProvider, *, run_label: str, config: IngestConfig | None = None,
                run_id: str | None = None, now: str | None = None) -> IngestSummary:
    """Run (or start fresh) a news-import pass for one provider. Each call is its own run; a crashed run's
    source cursor lets a later run resume, and stale RUNNING runs are reclaimed separately."""
    config = config or IngestConfig()
    now = utc_ts(now) or utc_ts(datetime.now(UTC))
    checksum = ingest_request_checksum(run_label, provider.name, provider.source_id)
    run_id = run_id or uuid.uuid4().hex
    store.nx_create_run(run_id=run_id, request_checksum=checksum, run_label=run_label,
                        provider=provider.name, source_id=provider.source_id)
    if not store.nx_advance_run_status(run_id, "PLANNED", "RUNNING"):
        # the run could not be moved PLANNED→RUNNING (another actor owns it, or it is already terminal) — never
        # proceed with every guarded write silently no-opping, and never stomp a run this call does not own.
        # Fail closed: bail and report the run's current state as-is.
        return _summary(store.nx_get_run(run_id))
    lic = provider.license_metadata()

    def fail(code: str, reason: str, *, available: bool) -> IngestSummary:
        store.nx_append_run_event(run_id, event={"id": f"{run_id}-e1", "seq": 1, "provider": provider.name,
                                                 "event_type": code, "severity": "ERROR", "reason": reason})
        store.nx_mark_source_result(provider.source_id, available=available, success=False, last_error=reason)
        store.nx_finalize_run(run_id, status="FAILED", failure_code=code, failure_reason=reason)
        return _summary(store.nx_get_run(run_id))

    if not provider.configured:
        return fail("PROVIDER_NOT_CONFIGURED", "provider is not configured", available=False)
    status = provider.provider_status()
    if not status.available:
        return fail("PROVIDER_UNAVAILABLE", status.reason or "provider unavailable", available=False)

    seq = store.nx_max_event_seq(run_id)
    cursor = config.start_cursor
    connection_lost = False
    pages = 0
    while pages < config.max_pages:
        page_cursor = cursor          # the cursor that fetched THIS page (for resume-without-loss)
        try:
            page = provider.fetch_new(cursor=cursor, limit=config.page_limit)
        except NewsProviderRateLimitedError as exc:
            seq += 1
            store.nx_append_run_event(run_id, event={"id": f"{run_id}-e{seq}", "seq": seq,
                                                     "provider": provider.name, "event_type": "RATE_LIMITED",
                                                     "severity": "WARN", "reason": str(exc)})
            break                                            # stop politely; the cursor lets a later run resume
        except NewsProviderUnavailableError as exc:
            seq += 1
            store.nx_append_run_event(run_id, event={"id": f"{run_id}-e{seq}", "seq": seq,
                                                     "provider": provider.name, "event_type": "PROVIDER_UNAVAILABLE",
                                                     "severity": "ERROR", "reason": str(exc)})
            connection_lost = True
            break

        by_region: dict[str, list] = {}
        for item in page.items:
            by_region.setdefault(_region_of(item), []).append(item)
        page_failed = False
        for region in sorted(by_region):
            try:
                region_ok = True
                for item in by_region[region]:
                    seq, item_ok = _process_item(store, provider, lic, item, region, run_id, seq, now)
                    region_ok = region_ok and item_ok
                seq += 1
                if region_ok:
                    store.nx_record_region(run_id, region=region, region_status="COMPLETED",
                                           event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                                  "region": region, "event_type": "REGION_OK", "severity": "INFO"})
                else:
                    # a message in this region errored (soft, isolated) → the region is not fully done; hold the
                    # page so resume retries it. Its stored siblings dedup; the failed message is re-fetched.
                    page_failed = True
                    store.nx_record_region(run_id, region=region, region_status="FAILED",
                                           event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                                  "region": region, "event_type": "REGION_INCOMPLETE",
                                                  "severity": "WARN", "reason": "one or more messages errored"})
            except Exception as exc:  # noqa: BLE001 — per-region isolation: one region never aborts the rest
                page_failed = True
                seq += 1
                store.nx_record_region(run_id, region=region, region_status="FAILED",
                                       event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                              "region": region, "event_type": "REGION_ERROR", "severity": "ERROR",
                                              "reason": f"{type(exc).__name__}: {exc}"})
        if page_failed:
            # a region OR a message on this page failed → keep the resume cursor at THIS page so a later run
            # re-fetches it (successful items dedup by message_id, the failed item retries) — never advance
            # past it. A deterministically-failing item will re-stop here: that is the fail-closed choice
            # (observable, no data loss), preferred over silently dropping the item and advancing.
            store.nx_set_cursor(run_id, page_cursor)
            break
        cursor = page.next_cursor
        store.nx_set_cursor(run_id, cursor)
        pages += 1
        if cursor is None:
            break

    if not connection_lost:
        store.nx_mark_source_result(provider.source_id, available=True, success=True)
    run = store.nx_get_run(run_id)
    completed = json.loads(run.completed_regions_json)
    failed = json.loads(run.failed_regions_json)
    if connection_lost:
        final, failure = "FAILED", ("PROVIDER_UNAVAILABLE", "provider became unavailable")
    elif failed and completed:
        final, failure = "PARTIAL", ("PARTIAL_INGEST", f"{len(failed)} region(s) failed")
    elif failed:
        final, failure = "FAILED", ("ALL_REGIONS_FAILED", "every region failed")
    elif run.error_count > 0:
        final, failure = "PARTIAL", ("MESSAGE_ERRORS", f"{run.error_count} message(s) errored")
    else:
        final, failure = "COMPLETED", (None, None)
    store.nx_finalize_run(run_id, status=final, failure_code=failure[0], failure_reason=failure[1])
    return _summary(store.nx_get_run(run_id))


def _process_item(store, provider, lic, item, region, run_id, seq, now) -> tuple[int, bool]:
    """Map + dedup + license-gate + persist ONE message, fully isolated (never raises). Returns
    ``(new_seq, ok)`` — ``ok=False`` signals a per-message error so the caller can hold the resume cursor on
    this page rather than silently dropping the item (fail-closed: no data loss)."""
    try:
        # license-gated storage: the licensed BODY is stored only when storage is BOTH flagged allowed AND the
        # license status is one that actually grants storage — a missing/unknown/no-license status forces
        # metadata-only (title + link + provenance) even if a buggy adapter set storage_allowed=True. Fail closed.
        if lic.storage_allowed and _license_value(lic.license_status) in _STORAGE_LICENSES:
            body = item.body
            storage = StorageStatus.STORED_FULL if (body or "").strip() else StorageStatus.STORED_METADATA_ONLY
        else:
            body = None
            storage = StorageStatus.STORED_METADATA_ONLY
        correction_of = (message_id_for(provider.name, item.correction_of_provider_id)
                         if item.correction_of_provider_id else None)
        retraction_of = (message_id_for(provider.name, item.retraction_of_provider_id)
                         if item.retraction_of_provider_id else None)
        msg = NewsMessage(
            provider=provider.name, provider_id=item.provider_id, source_id=provider.source_id,
            source_type=_provider_source_type(provider),
            primacy=item.primacy.value, original_title=item.title, original_body=body,
            # fetched_body is the ungated as-fetched content, used only for the dedup checksum (never persisted),
            # so duplicate identity is independent of the license/storage gate above.
            fetched_body=item.body,
            original_language=item.language, url=item.url, published_at=item.published_at, received_at=now,
            correction_at=(now if (correction_of or retraction_of) else None),
            event_category=item.event_category.value,
            license_status=_license_value(lic.license_status),
            storage_status=storage.value,
            correction_of_id=correction_of, retraction_of_id=retraction_of,
            affected_countries=tuple(item.countries), affected_regions=tuple(item.regions),
            affected_industries=tuple(item.industries), affected_companies=tuple(item.companies),
            affected_exchanges=tuple(item.exchanges),
            provenance={"provider": provider.name, "source_id": provider.source_id,
                        "source_name": item.source_name, "fetched_at": now})

        # exact-duplicate detection (provider-neutral content checksum)
        dup_of = store.nx_find_message_id_by_checksum(msg.content_checksum)
        dup_of = dup_of if (dup_of and dup_of != msg.message_id) else None

        # fail-closed instrument mapping (catalogue only) — a bare symbol can never be VERIFIED
        mapping: dict[str, str] = {}
        for h in item.mapping_hints:
            for iid, mstatus in store.nx_resolve_instruments(symbol=h.symbol, exchange=h.exchange,
                                                             isin=h.isin, con_id=h.con_id):
                if mapping.get(iid) != "VERIFIED":
                    mapping[iid] = mstatus
        mapping_rows = [(iid, mstatus, None, "catalogue") for iid, mstatus in sorted(mapping.items())]
        has_hints = bool(item.mapping_hints)

        seq += 1
        store.nx_insert_message(
            msg.as_record(duplicate_of_id=dup_of), mappings=mapping_rows, run_id=run_id,
            duplicate=bool(dup_of), ambiguous=any(s == "AMBIGUOUS" for s in mapping.values()),
            unmapped=(has_hints and not mapping), is_correction=bool(correction_of),
            is_retraction=bool(retraction_of),
            run_event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "region": region,
                       "message_id": msg.message_id, "event_type": "MESSAGE_STORED", "severity": "INFO",
                       "reason": msg.content_checksum})
        # The append-only CORRECTION/RETRACTION audit links on the immutable original are recorded
        # ATOMICALLY inside nx_insert_message (forward + backfill, order-independent), so there is nothing
        # here that can fail after the counters commit and cause a double-count.
        return seq, True
    except Exception as exc:  # noqa: BLE001 — per-message isolation: one message never aborts the region
        seq += 1
        # Record the FAILED message identifiably (derived message_id + provider_id) so it is a recoverable
        # dead-letter, not a silent drop; ``ok=False`` makes the caller hold the cursor on this page so the
        # item is re-fetched on resume (stored siblings dedup by message_id) rather than lost.
        store.nx_bump(run_id, {"fetched_count": 1, "error_count": 1},
                      event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "region": region,
                             "message_id": message_id_for(provider.name, item.provider_id),
                             "event_type": "MESSAGE_ERROR", "severity": "ERROR",
                             "reason": f"provider_id={item.provider_id}: {type(exc).__name__}: {exc}"})
        return seq, False


def _provider_source_type(provider) -> str:
    st = getattr(provider, "source_type", None)
    if st is None:
        return "OTHER"
    return st.value if hasattr(st, "value") else str(st)


def _summary(run) -> IngestSummary:
    return IngestSummary(
        run_id=run.run_id, status=run.status, fetched=run.fetched_count, stored=run.stored_count,
        duplicates=run.duplicate_count, ambiguous=run.ambiguous_count, corrections=run.correction_count,
        retractions=run.retraction_count, unmapped=run.unmapped_count, error=run.error_count,
        completed_regions=json.loads(run.completed_regions_json),
        failed_regions=json.loads(run.failed_regions_json))
