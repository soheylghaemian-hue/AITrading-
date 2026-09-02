"""WP6 — the resumable, fault-tolerant macro / geopolitical / regulatory event ingest orchestrator.

Pulls read-only events from a `MacroEventProvider`, builds a WP5 newsroom record PLUS a macro overlay for
each, maps them fail-closed to the WP2 catalogue (reusing the WP5 resolver), deduplicates, links
corrections/retractions to the immutable original, license-gates storage, and persists — all with
per-provider / per-region / per-message error isolation, cursor-based resumability, and an append-only audit
trail, REUSING the WP5 `news_import_runs` lifecycle. Never fabricates: a missing text/timestamp stays NULL, a
future publish time is flagged (inherited from the newsroom record), a broad macro event that names no
instrument is linked NONE (not a fabricated match), severity is research metadata. A provider outage yields a
FAILED run + source UNAVAILABLE, not a frozen 'fresh' state.

SAFETY: read-only macro event data only. No orders, no execution, no account, no subscription/news purchase,
no new credentials, no real network in CI, no HTTP write path. AUTONOMOUS=DISABLED · EXECUTION=DISABLED ·
IBKR ORDERS=0.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from atp.newsroom.model import (
    EventCategory,
    LicenseStatus,
    NewsMessage,
    SourceType,
    StorageStatus,
    message_id_for,
    utc_ts,
)
from atp.newsroom.provider import NewsProviderRateLimitedError, NewsProviderUnavailableError

from .model import MacroEvent, MacroEventType, link_status_from_mappings
from .provider import MacroEventProvider

# storage of the licensed BODY requires BOTH a flag and a storable license status (fail-closed, as in WP5)
_STORAGE_LICENSES = frozenset({LicenseStatus.LICENSED_STORE_REDISTRIBUTE.value,
                               LicenseStatus.LICENSED_STORE_ONLY.value})

# macro_type → newsroom event category (fail-closed: anything unmapped → UNCLASSIFIED, never fabricated)
_CENTRAL_BANK_TYPES = frozenset({
    MacroEventType.MONETARY_POLICY_DECISION.value, MacroEventType.RATE_GUIDANCE.value,
    MacroEventType.POLICY_STATEMENT.value, MacroEventType.INFLATION_REPORT.value,
    MacroEventType.FX_INTERVENTION.value, MacroEventType.LIQUIDITY_OPERATION.value,
    MacroEventType.SYSTEMIC_RISK_WARNING.value, MacroEventType.SUPRANATIONAL_OUTLOOK.value})
_GEOPOLITICAL_TYPES = frozenset({
    MacroEventType.SANCTION.value, MacroEventType.EMBARGO.value, MacroEventType.EXPORT_CONTROL.value,
    MacroEventType.TARIFF_MEASURE.value, MacroEventType.TRADE_AGREEMENT.value,
    MacroEventType.ARMED_CONFLICT.value, MacroEventType.CIVIL_UNREST.value,
    MacroEventType.ENERGY_SUPPLY_WARNING.value, MacroEventType.TRANSPORT_DISRUPTION.value})

# source_class → newsroom source type
_CENTRAL_BANK_CLASSES = frozenset({"CENTRAL_BANK"})
_REGULATOR_CLASSES = frozenset({"NATIONAL_REGULATOR", "SANCTIONS_AUTHORITY", "TRADE_AUTHORITY"})


@dataclass(frozen=True, slots=True)
class MacroIngestConfig:
    page_limit: int = 100
    max_pages: int = 50
    start_cursor: str | None = None


@dataclass(slots=True)
class MacroIngestSummary:
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


def macro_ingest_request_checksum(run_label: str, provider: str, source_id: str) -> str:
    payload = {"label": run_label, "provider": provider, "source_id": source_id,
               "tag": "atp.macro-ingest.request.v1"}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _v(x) -> str:
    # extract an enum's .value; None → "" (never the fabricated literal "None"). Item enum fields carry
    # fail-closed enum defaults, so None here is out-of-contract input — degrade to empty, don't invent.
    if x is None:
        return ""
    return x.value if hasattr(x, "value") else str(x)


def _license_value(status) -> str:
    if status is None:
        return ""
    return status.value if isinstance(status, LicenseStatus) else str(status)


def _event_category_for(macro_type: str) -> str:
    if macro_type in _CENTRAL_BANK_TYPES:
        return EventCategory.MACRO_CENTRAL_BANK.value
    if macro_type in _GEOPOLITICAL_TYPES:
        return EventCategory.GEOPOLITICS_REF.value
    if macro_type == MacroEventType.REGULATORY_ACTION.value:
        return EventCategory.REGULATION.value
    if macro_type == MacroEventType.OTHER.value:
        return EventCategory.OTHER.value
    return EventCategory.UNCLASSIFIED.value       # fail-closed — never fabricate a category


def _source_type_for(source_class: str) -> str:
    if source_class in _CENTRAL_BANK_CLASSES:
        return SourceType.CENTRAL_BANK.value
    if source_class in _REGULATOR_CLASSES:
        return SourceType.REGULATOR.value
    return SourceType.OTHER.value


def _region_of(item) -> str:
    if item.regions:
        return str(item.regions[0])
    if item.countries:
        return str(item.countries[0])
    return "GLOBAL"


def ingest_macro_events(store, provider: MacroEventProvider, *, run_label: str,
                        config: MacroIngestConfig | None = None, run_id: str | None = None,
                        now: str | None = None) -> MacroIngestSummary:
    """Run (or start fresh) a macro-event import pass for one provider. Each call is its own run; a crashed
    run's source cursor lets a later run resume, and stale RUNNING runs are reclaimed separately. Reuses the
    WP5 `news_import_runs` lifecycle (a macro event is a newsroom record + a macro overlay)."""
    config = config or MacroIngestConfig()
    now = utc_ts(now) or utc_ts(datetime.now(UTC))
    checksum = macro_ingest_request_checksum(run_label, provider.name, provider.source_id)
    run_id = run_id or uuid.uuid4().hex
    store.nx_create_run(run_id=run_id, request_checksum=checksum, run_label=run_label,
                        provider=provider.name, source_id=provider.source_id)
    if not store.nx_advance_run_status(run_id, "PLANNED", "RUNNING"):
        # the run could not be moved PLANNED→RUNNING (another actor owns it, or it is already terminal) — never
        # proceed with every guarded write silently no-opping. Fail closed: report the current state as-is.
        return _summary(store.nx_get_run(run_id))
    lic = provider.license_metadata()

    def fail(code: str, reason: str, *, available: bool) -> MacroIngestSummary:
        store.nx_append_run_event(run_id, event={"id": f"{run_id}-e1", "seq": 1, "provider": provider.name,
                                                 "event_type": code, "severity": "ERROR", "reason": reason})
        store.mx_mark_macro_source_result(provider.source_id, available=available, success=False,
                                          last_error=reason)
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
                    page_failed = True
                    store.nx_record_region(run_id, region=region, region_status="FAILED",
                                           event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                                  "region": region, "event_type": "REGION_INCOMPLETE",
                                                  "severity": "WARN", "reason": "one or more events errored"})
            except Exception as exc:  # noqa: BLE001 — per-region isolation: one region never aborts the rest
                page_failed = True
                seq += 1
                store.nx_record_region(run_id, region=region, region_status="FAILED",
                                       event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name,
                                              "region": region, "event_type": "REGION_ERROR", "severity": "ERROR",
                                              "reason": f"{type(exc).__name__}: {exc}"})
        if page_failed:
            # a region OR an event on this page failed → hold the resume cursor at THIS page so a later run
            # re-fetches it (stored siblings dedup by message_id, the failed item retries) — never advance past
            # it and silently drop the item. A deterministic poison item re-stops here: fail-closed, no loss.
            store.nx_set_cursor(run_id, page_cursor)
            break
        cursor = page.next_cursor
        store.nx_set_cursor(run_id, cursor)
        pages += 1
        if cursor is None:
            break

    if not connection_lost:
        store.mx_mark_macro_source_result(provider.source_id, available=True, success=True)
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
        final, failure = "PARTIAL", ("EVENT_ERRORS", f"{run.error_count} event(s) errored")
    else:
        final, failure = "COMPLETED", (None, None)
    store.nx_finalize_run(run_id, status=final, failure_code=failure[0], failure_reason=failure[1])
    return _summary(store.nx_get_run(run_id))


def _process_item(store, provider, lic, item, region, run_id, seq, now) -> tuple[int, bool]:
    """Map + dedup + license-gate + persist ONE macro event (newsroom record + overlay), fully isolated (never
    raises). Returns ``(new_seq, ok)`` — ``ok=False`` holds the resume cursor on this page (no data loss)."""
    try:
        # license-gated storage: the licensed BODY is stored only when BOTH storage is flagged allowed AND the
        # license status actually grants storage — otherwise metadata-only, regardless of the flag (fail closed).
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
        macro_type = _v(item.macro_type)
        source_class = _v(item.source_class)

        # fail-closed instrument mapping (catalogue only, reusing the WP5 resolver) — a bare symbol is never
        # VERIFIED; most macro events name no instrument at all (link NONE).
        mapping: dict[str, str] = {}
        for h in item.mapping_hints:
            for iid, mstatus in store.nx_resolve_instruments(symbol=h.symbol, exchange=h.exchange,
                                                             isin=h.isin, con_id=h.con_id):
                if mapping.get(iid) != "VERIFIED":
                    mapping[iid] = mstatus
        mapping_rows = [(iid, mstatus, None, "catalogue") for iid, mstatus in sorted(mapping.items())]
        # a hint counts only if it carries a USABLE identifier (con_id / isin / symbol) — a vacuous MappingHint()
        # is not an attempted mapping, so it stays link NONE (not instrument-specific), never UNMAPPED.
        had_hints = any((h.con_id is not None) or bool(h.isin) or bool(h.symbol) for h in item.mapping_hints)
        link_status = link_status_from_mappings(mapping.values(), had_hints=had_hints)

        msg = NewsMessage(
            provider=provider.name, provider_id=item.provider_id, source_id=provider.source_id,
            source_type=_source_type_for(source_class), primacy=item.primacy.value,
            original_title=item.title, original_body=body, fetched_body=item.body,
            original_language=item.language, url=item.url, published_at=item.published_at, received_at=now,
            correction_at=(now if (correction_of or retraction_of) else None),
            event_category=_event_category_for(macro_type),
            license_status=_license_value(lic.license_status), storage_status=storage.value,
            correction_of_id=correction_of, retraction_of_id=retraction_of,
            affected_countries=tuple(item.countries), affected_regions=tuple(item.regions),
            provenance={"provider": provider.name, "source_id": provider.source_id,
                        "source_name": item.source_name, "fetched_at": now, "domain": "macro"})

        dup_of = store.nx_find_message_id_by_checksum(msg.content_checksum)
        dup_of = dup_of if (dup_of and dup_of != msg.message_id) else None

        macro = MacroEvent(
            message_id=msg.message_id, macro_type=macro_type, source_class=source_class,
            geo_scope=_v(item.geo_scope), severity=_v(item.severity), policy_area=item.policy_area,
            affected_regions=tuple(item.regions), affected_countries=tuple(item.countries),
            affected_blocs=tuple(item.blocs), affected_asset_classes=tuple(item.asset_classes),
            link_status=link_status.value, correction_of_id=correction_of, retraction_of_id=retraction_of,
            published_at=item.published_at,
            provenance={"provider": provider.name, "source_id": provider.source_id, "fetched_at": now})

        seq += 1
        store.mx_insert_macro_event(
            msg.as_record(duplicate_of_id=dup_of), macro.as_record(), mappings=mapping_rows, run_id=run_id,
            duplicate=bool(dup_of), ambiguous=any(s == "AMBIGUOUS" for s in mapping.values()),
            unmapped=(had_hints and not mapping), is_correction=bool(correction_of),
            is_retraction=bool(retraction_of),
            run_event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "region": region,
                       "message_id": msg.message_id, "event_type": "MACRO_EVENT_STORED", "severity": "INFO",
                       "reason": macro_type})
        return seq, True
    except Exception as exc:  # noqa: BLE001 — per-message isolation: one event never aborts the region
        seq += 1
        # record the FAILED event identifiably (derived message_id + provider_id) as a recoverable dead-letter;
        # ok=False holds the cursor on this page so it is re-fetched on resume rather than lost.
        store.nx_bump(run_id, {"fetched_count": 1, "error_count": 1},
                      event={"id": f"{run_id}-e{seq}", "seq": seq, "provider": provider.name, "region": region,
                             "message_id": message_id_for(provider.name, item.provider_id),
                             "event_type": "MACRO_EVENT_ERROR", "severity": "ERROR",
                             "reason": f"provider_id={item.provider_id}: {type(exc).__name__}: {exc}"})
        return seq, False


def _summary(run) -> MacroIngestSummary:
    return MacroIngestSummary(
        run_id=run.run_id, status=run.status, fetched=run.fetched_count, stored=run.stored_count,
        duplicates=run.duplicate_count, ambiguous=run.ambiguous_count, corrections=run.correction_count,
        retractions=run.retraction_count, unmapped=run.unmapped_count, error=run.error_count,
        completed_regions=json.loads(run.completed_regions_json),
        failed_regions=json.loads(run.failed_regions_json))
