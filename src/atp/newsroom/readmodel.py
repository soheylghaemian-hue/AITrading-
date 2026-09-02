"""WP5 — read-only observability read-models for the news/filings subsystem.

Pure functions over the durable store that return coverage / health dicts for a GET read-model. They NEVER
start a fetch, translation, subscription or trade, and they NEVER claim full coverage when only some sources
are active — active and missing sources are reported explicitly. No HTTP surface is defined here.

SAFETY: read-only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import json


def _flatten(items) -> list:
    out: list = []
    for x in items:
        out.extend(x)
    return out


def news_source_coverage(store) -> dict:
    """Coverage by region / language / source type, with active vs MISSING sources called out explicitly —
    partial coverage is never presented as complete."""
    sources = store.nx_list_sources()
    active = [s for s in sources if s.available]
    missing = [s for s in sources if not s.available]

    def _bucket(attr_json: str) -> dict:
        counts: dict[str, dict] = {}
        for s in sources:
            for key in json.loads(getattr(s, attr_json)):
                b = counts.setdefault(str(key), {"sources": 0, "active": 0})
                b["sources"] += 1
                b["active"] += 1 if s.available else 0
        return dict(sorted(counts.items()))

    by_type: dict[str, dict] = {}
    for s in sources:
        b = by_type.setdefault(s.source_type, {"sources": 0, "active": 0})
        b["sources"] += 1
        b["active"] += 1 if s.available else 0

    return {
        "total_sources": len(sources),
        "active_sources": [s.source_id for s in active],
        "missing_sources": [s.source_id for s in missing],
        "coverage_partial": bool(missing) or not sources,   # never claim full coverage from a partial set
        "by_region": _bucket("regions_json"),
        "by_language": _bucket("languages_json"),
        "by_source_type": dict(sorted(by_type.items())),
        "primary_sources": [s.source_id for s in sources if s.primacy == "PRIMARY"],
        "secondary_sources": [s.source_id for s in sources if s.primacy == "SECONDARY"],
        "licenses": {s.source_id: {"license_status": s.license_status, "storage_allowed": s.storage_allowed,
                                   "redistribution_allowed": s.redistribution_allowed}
                     for s in sources},
        "last_success": {s.source_id: s.last_success_at for s in sources},
        "errors": {s.source_id: s.last_error for s in sources if s.last_error},
    }


def news_health(store) -> dict:
    """A single read-model bundling source coverage, dedup rate, ambiguous mappings, corrections/retractions,
    and recent import-run status. Read-only — starts nothing."""
    runs = store.nx_list_runs(limit=20)
    return {
        "sources": news_source_coverage(store),
        "messages": store.nx_count_messages(),
        "dedup": store.nx_dedup_stats(),
        "ambiguous_mappings": store.nx_count_ambiguous_mappings(),
        "corrections_retractions": store.nx_corrections_retractions(),
        "recent_runs": [{"run_id": r.run_id, "provider": r.provider, "source_id": r.source_id,
                         "status": r.status, "fetched": r.fetched_count, "stored": r.stored_count,
                         "duplicates": r.duplicate_count, "ambiguous": r.ambiguous_count,
                         "corrections": r.correction_count, "retractions": r.retraction_count,
                         "errors": r.error_count, "cursor": r.cursor} for r in runs],
    }
