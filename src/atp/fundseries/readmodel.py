"""WP7 — read-only observability read-models for the fundamentals & macro-series subsystem.

Pure functions over the durable store that return coverage / health dicts for a GET read-model. They NEVER
start a fetch, subscription or trade, and they NEVER claim full coverage when only some sources are active —
active and missing sources are reported explicitly. No HTTP surface is defined here.

SAFETY: read-only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import json


def fundamentals_source_coverage(store) -> dict:
    """Coverage by region / source type, with active vs MISSING sources called out explicitly — partial
    coverage is never presented as complete."""
    sources = store.fx_list_sources()
    active = [s for s in sources if s.available]
    missing = [s for s in sources if not s.available]

    def _bucket_json(attr_json: str) -> dict:
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
        "by_region": _bucket_json("regions_json"),
        "by_source_type": dict(sorted(by_type.items())),
        "licenses": {s.source_id: {"license_status": s.license_status, "storage_allowed": s.storage_allowed,
                                   "redistribution_allowed": s.redistribution_allowed}
                     for s in sources},
        "last_success": {s.source_id: s.last_success_at for s in sources},
        "errors": {s.source_id: s.last_error for s in sources if s.last_error},
    }


def fundamentals_health(store) -> dict:
    """A single read-model bundling source coverage, series/observation counts, value-status + series-link
    breakdowns, dedup + revision stats, and recent fundamentals import-run status. Read-only — starts nothing.
    Recent runs are filtered to the fundamentals source ids."""
    fund_source_ids = {s.source_id for s in store.fx_list_sources()}
    runs = [r for r in store.nx_list_runs(limit=200) if r.source_id in fund_source_ids][:20]
    return {
        "sources": fundamentals_source_coverage(store),
        "series": store.fx_count_series(),
        "observations": store.fx_count_observations(),
        "by_value_status": store.fx_value_status_breakdown(),
        "by_series_link_status": store.fx_series_link_breakdown(),
        "dedup": store.fx_dedup_stats(),
        "revisions": store.fx_revision_count(),
        "recent_runs": [{"run_id": r.run_id, "provider": r.provider, "source_id": r.source_id,
                         "status": r.status, "fetched": r.fetched_count, "stored": r.stored_count,
                         "duplicates": r.duplicate_count, "revisions": r.correction_count,
                         "errors": r.error_count, "cursor": r.cursor} for r in runs],
    }
