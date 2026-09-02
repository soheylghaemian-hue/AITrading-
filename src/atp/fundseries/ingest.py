"""WP7 — the resumable, fault-tolerant fundamentals & macro-series ingest orchestrator.

Pulls read-only observations from a `FundamentalProvider`, upserts each one's SERIES (mapping it fail-closed
to the WP2 catalogue via the reused resolver), deduplicates, links REVISIONS to the immutable prior data
point, license-gates the value, and persists immutable observations — all with per-provider / per-region /
per-message error isolation, cursor-based resumability, and an append-only audit trail, REUSING the WP5
`news_import_runs` lifecycle. Never fabricates: a missing value stays NULL (never zero, never interpolated), a
missing publish time is never replaced by the receive time, a future publish time is flagged, and dedup
identity is a property of the as-fetched value, independent of the license gate. A provider outage yields a
FAILED run + source UNAVAILABLE, not a frozen 'fresh' state.

SAFETY: read-only fundamentals/macro data only. No orders, no execution, no account, no subscription/data
purchase, no new credentials, no real network in CI, no HTTP write path. AUTONOMOUS=DISABLED ·
EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from atp.newsroom.model import LicenseStatus, StorageStatus, utc_ts

from .model import (
    FundamentalObservation,
    FundamentalSeries,
    normalize_value,
    observation_id_for,
    series_id_for,
)
from .provider import FundamentalProvider, NewsProviderRateLimitedError, NewsProviderUnavailableError

# storage of the value requires BOTH a flag and a storable license status (fail-closed, as in WP5/WP6)
_STORAGE_LICENSES = frozenset({LicenseStatus.LICENSED_STORE_REDISTRIBUTE.value,
                               LicenseStatus.LICENSED_STORE_ONLY.value})


@dataclass(frozen=True, slots=True)
class FundamentalIngestConfig:
    page_limit: int = 100
    max_pages: int = 50
    start_cursor: str | None = None


@dataclass(slots=True)
class FundamentalIngestSummary:
    run_id: str
    status: str
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    revisions: int = 0
    error: int = 0
    completed_regions: list = field(default_factory=list)
    failed_regions: list = field(default_factory=list)


def fundamentals_ingest_request_checksum(run_label: str, provider: str, source_id: str) -> str:
    payload = {"label": run_label, "provider": provider, "source_id": source_id,
               "tag": "atp.fundseries-ingest.request.v1"}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _v(x) -> str:
    if x is None:
        return ""
    return x.value if hasattr(x, "value") else str(x)


def _license_value(status) -> str:
    if status is None:
        return ""
    return status.value if isinstance(status, LicenseStatus) else str(status)


def _region_of(item) -> str:
    # enum-safe: a (str, Enum) member's str() is its dotted repr, so extract .value first (the region field is
    # typed str, but this keeps the run's region-bucket label correct even for an off-contract enum input).
    if item.region:
        return str(getattr(item.region, "value", item.region)).upper()
    if item.country:
        return str(getattr(item.country, "value", item.country)).upper()
    return "GLOBAL"


def ingest_fundamentals(store, provider: FundamentalProvider, *, run_label: str,
                        config: FundamentalIngestConfig | None = None, run_id: str | None = None,
                        now: str | None = None) -> FundamentalIngestSummary:
    """Run (or start fresh) a fundamentals import pass for one provider. Each call is its own run; a crashed
    run's source cursor lets a later run resume, and stale RUNNING runs are reclaimed separately. Reuses the
    WP5 `news_import_runs` lifecycle."""
    config = config or FundamentalIngestConfig()
    now = utc_ts(now) or utc_ts(datetime.now(UTC))
    checksum = fundamentals_ingest_request_checksum(run_label, provider.name, provider.source_id)
    run_id = run_id or uuid.uuid4().hex
    store.nx_create_run(run_id=run_id, request_checksum=checksum, run_label=run_label,
                        provider=provider.name, source_id=provider.source_id)
    if not store.nx_advance_run_status(run_id, "PLANNED", "RUNNING"):
        # the run could not be started (another actor owns it, or it is already terminal) — never proceed with
        # every guarded write silently no-opping. Fail closed: report the current state as-is.
        return _summary(store.nx_get_run(run_id))
    lic = provider.license_metadata()

    def fail(code: str, reason: str, *, available: bool) -> FundamentalIngestSummary:
        store.nx_append_run_event(run_id, event={"id": f"{run_id}-e1", "seq": 1, "provider": provider.name,
                                                 "event_type": code, "severity": "ERROR", "reason": reason})
        store.fx_mark_source_result(provider.source_id, available=available, success=False, last_error=reason)
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
    rate_limited = False
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
            rate_limited = True
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
                    page_failed = True
                    store.nx_record_region(run_id, region=region, region_status="FAILED",
                                           event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                                  "region": region, "event_type": "REGION_INCOMPLETE",
                                                  "severity": "WARN", "reason": "one or more observations errored"})
            except Exception as exc:  # noqa: BLE001 — per-region isolation: one region never aborts the rest
                page_failed = True
                seq += 1
                store.nx_record_region(run_id, region=region, region_status="FAILED",
                                       event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                              "region": region, "event_type": "REGION_ERROR", "severity": "ERROR",
                                              "reason": f"{type(exc).__name__}: {exc}"})
        if page_failed:
            # a region OR an observation on this page failed → hold the resume cursor at THIS page so a later
            # run re-fetches it (stored siblings dedup by observation_id, the failed item retries) — never
            # advance past it and silently drop the item. Fail-closed: no data loss.
            store.nx_set_cursor(run_id, page_cursor)
            break
        cursor = page.next_cursor
        store.nx_set_cursor(run_id, cursor)
        pages += 1
        if cursor is None:
            break

    # source health, honestly: a mid-run outage marks the source DOWN; a rate-limit is a soft throttle (the
    # source is up, but this pass is NOT a fresh success — do not bump last_success_at, never a frozen 'fresh'
    # state); only a clean pass records a success.
    if connection_lost:
        store.fx_mark_source_result(provider.source_id, available=False, success=False,
                                    last_error="provider became unavailable")
    elif rate_limited:
        store.fx_mark_source_result(provider.source_id, available=True, success=False,
                                    last_error="provider rate-limited")
    else:
        store.fx_mark_source_result(provider.source_id, available=True, success=True)
    run = store.nx_get_run(run_id)
    completed = json.loads(run.completed_regions_json)
    failed = json.loads(run.failed_regions_json)
    if connection_lost:
        final, failure = "FAILED", ("PROVIDER_UNAVAILABLE", "provider became unavailable")
    elif failed and completed:
        final, failure = "PARTIAL", ("PARTIAL_INGEST", f"{len(failed)} region(s) failed")
    elif failed:
        final, failure = "FAILED", ("ALL_REGIONS_FAILED", "every region failed")
    elif rate_limited:
        # a throttled pass is incomplete, not a clean success — PARTIAL, with the cursor held for resume
        final, failure = "PARTIAL", ("RATE_LIMITED", "provider rate-limited; pass incomplete, resume pending")
    elif run.error_count > 0:
        final, failure = "PARTIAL", ("OBSERVATION_ERRORS", f"{run.error_count} observation(s) errored")
    else:
        final, failure = "COMPLETED", (None, None)
    store.nx_finalize_run(run_id, status=final, failure_code=failure[0], failure_reason=failure[1])
    return _summary(store.nx_get_run(run_id))


def _process_item(store, provider, lic, item, region, run_id, seq, now) -> tuple[int, bool]:
    """Upsert series + map fail-closed + dedup + license-gate + persist ONE observation, fully isolated (never
    raises). Returns ``(new_seq, ok)`` — ``ok=False`` holds the resume cursor on this page (no data loss)."""
    try:
        sid = series_id_for(provider.source_id, item.series_key)

        # fail-closed SERIES → instrument mapping (catalogue only, reusing the WP5 resolver): a bare symbol is
        # never VERIFIED; a macro series with no usable hint is link NONE (not instrument-specific).
        mapping: dict[str, str] = {}
        for h in item.mapping_hints:
            for iid, mstatus in store.nx_resolve_instruments(symbol=h.symbol, exchange=h.exchange,
                                                             isin=h.isin, con_id=h.con_id):
                if mapping.get(iid) != "VERIFIED":
                    mapping[iid] = mstatus
        mapping_rows = [(iid, mstatus, None, "catalogue") for iid, mstatus in sorted(mapping.items())]
        had_hints = any((h.con_id is not None) or bool(h.isin) or bool(h.symbol) for h in item.mapping_hints)

        series = FundamentalSeries(
            source_id=provider.source_id, series_key=item.series_key, category=_v(item.category),
            metric=item.metric, unit=_v(item.unit), frequency=_v(item.frequency), region=item.region,
            country=item.country, currency=item.currency, description=item.description,
            provenance={"provider": provider.name, "source_id": provider.source_id, "fetched_at": now})
        # link_status is DERIVED inside fx_upsert_series from the immutable stored mapping rows (not the
        # per-observation recompute), so it can never be an order-dependent fabricated VERIFIED.
        store.fx_upsert_series(series.as_record(), mappings=mapping_rows, had_hints=had_hints)

        # license-gated value: the value/value_text are stored only when storage is BOTH flagged allowed AND
        # the license status grants storage — otherwise metadata-only, regardless of the flag (fail closed).
        raw_value = normalize_value(item.value)
        storable = lic.storage_allowed and _license_value(lic.license_status) in _STORAGE_LICENSES
        if storable:
            stored_value, stored_text = raw_value, item.value_text
            storage = (StorageStatus.STORED_FULL
                       if (raw_value is not None or (item.value_text or "").strip())
                       else StorageStatus.STORED_METADATA_ONLY)
        else:
            stored_value, stored_text = None, None
            storage = StorageStatus.STORED_METADATA_ONLY

        revision_of = (observation_id_for(provider.name, item.revision_of_provider_id)
                       if item.revision_of_provider_id else None)

        obs = FundamentalObservation(
            series_id=sid, provider=provider.name, provider_id=item.provider_id, source_id=provider.source_id,
            period=item.period, period_start=item.period_start, period_end=item.period_end,
            value=stored_value, fetched_value=raw_value, value_text=stored_text,
            revision_seq=int(item.revision_seq or 0), revision_of_id=revision_of,
            is_preliminary=bool(item.is_preliminary), published_at=item.published_at, received_at=now,
            license_status=_license_value(lic.license_status), storage_status=storage.value,
            provenance={"provider": provider.name, "source_id": provider.source_id,
                        "source_name": item.source_name, "fetched_at": now})

        # exact-duplicate detection over the as-fetched content (independent of the license gate)
        dup_of = store.fx_find_observation_by_checksum(obs.content_checksum)
        dup_of = dup_of if (dup_of and dup_of != obs.observation_id) else None

        seq += 1
        store.fx_insert_observation(
            obs.as_record(duplicate_of_id=dup_of), run_id=run_id, duplicate=bool(dup_of),
            revised=bool(revision_of),
            run_event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "region": region,
                       "message_id": obs.observation_id, "event_type": "OBSERVATION_STORED", "severity": "INFO",
                       "reason": item.series_key})
        return seq, True
    except Exception as exc:  # noqa: BLE001 — per-message isolation: one observation never aborts the region
        seq += 1
        # record the FAILED observation identifiably (derived id + provider_id) as a recoverable dead-letter;
        # ok=False holds the cursor on this page so it is re-fetched on resume rather than lost.
        store.nx_bump(run_id, {"fetched_count": 1, "error_count": 1},
                      event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "region": region,
                             "message_id": observation_id_for(provider.name, item.provider_id),
                             "event_type": "OBSERVATION_ERROR", "severity": "ERROR",
                             "reason": f"provider_id={item.provider_id}: {type(exc).__name__}: {exc}"})
        return seq, False


def _summary(run) -> FundamentalIngestSummary:
    return FundamentalIngestSummary(
        run_id=run.run_id, status=run.status, fetched=run.fetched_count, stored=run.stored_count,
        duplicates=run.duplicate_count, revisions=run.correction_count, error=run.error_count,
        completed_regions=json.loads(run.completed_regions_json),
        failed_regions=json.loads(run.failed_regions_json))
