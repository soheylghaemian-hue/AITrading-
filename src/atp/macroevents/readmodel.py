"""WP6 — read-only observability read-models for the macro / geopolitical event subsystem.

Pure functions over the durable store that return coverage / health dicts for a GET read-model. They NEVER
start a fetch, subscription or trade, and they NEVER claim full coverage when only some macro sources are
active — active and missing sources are reported explicitly. They read the WP6 `macro_events`/`macro_sources`
overlay and the reused WP5 aggregates (dedup / corrections / retractions come from the newsroom record a
macro event is keyed to). No HTTP surface is defined here.

SAFETY: read-only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

import json


def macro_source_coverage(store) -> dict:
    """Macro source coverage by region / mandate / source class, with active vs MISSING sources called out
    explicitly — partial coverage is never presented as complete."""
    sources = store.mx_list_macro_sources()
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

    by_class: dict[str, dict] = {}
    by_mandate: dict[str, dict] = {}
    for s in sources:
        for bucket, key in ((by_class, s.source_class), (by_mandate, s.mandate)):
            b = bucket.setdefault(str(key), {"sources": 0, "active": 0})
            b["sources"] += 1
            b["active"] += 1 if s.available else 0

    return {
        "total_sources": len(sources),
        "active_sources": [s.source_id for s in active],
        "missing_sources": [s.source_id for s in missing],
        "coverage_partial": bool(missing) or not sources,   # never claim full coverage from a partial set
        "by_region": _bucket_json("regions_json"),
        "by_source_class": dict(sorted(by_class.items())),
        "by_mandate": dict(sorted(by_mandate.items())),
        "licenses": {s.source_id: {"license_status": s.license_status, "storage_allowed": s.storage_allowed,
                                   "redistribution_allowed": s.redistribution_allowed}
                     for s in sources},
        "last_success": {s.source_id: s.last_success_at for s in sources},
        "errors": {s.source_id: s.last_error for s in sources if s.last_error},
    }


def macro_health(store) -> dict:
    """A single read-model bundling macro source coverage, event counts, type / link breakdowns, cluster
    count, dedup + corrections/retractions (inherited from the newsroom record), and recent macro import-run
    status. Read-only — starts nothing. Recent runs are filtered to the macro source ids."""
    macro_source_ids = {s.source_id for s in store.mx_list_macro_sources()}
    # macro runs share news_import_runs with WP5 news runs — pull a wide recent window before filtering to
    # macro sources so a burst of news runs cannot crowd macro runs out of this recent view.
    runs = [r for r in store.nx_list_runs(limit=200) if r.source_id in macro_source_ids][:20]
    return {
        "sources": macro_source_coverage(store),
        "events": store.mx_count_macro_events(),
        "by_type": store.mx_macro_type_breakdown(),
        "by_link_status": store.mx_link_status_breakdown(),
        "clusters": store.mx_macro_cluster_count(),
        "dedup": store.nx_dedup_stats(),                       # dedup is a property of the newsroom record
        "corrections_retractions": store.nx_corrections_retractions(),
        "recent_runs": [{"run_id": r.run_id, "provider": r.provider, "source_id": r.source_id,
                         "status": r.status, "fetched": r.fetched_count, "stored": r.stored_count,
                         "duplicates": r.duplicate_count, "ambiguous": r.ambiguous_count,
                         "corrections": r.correction_count, "retractions": r.retraction_count,
                         "errors": r.error_count, "cursor": r.cursor} for r in runs],
    }
