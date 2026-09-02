"""WP9 — read-only coverage read-model for the global instrument universe.

Pure functions over the durable `instruments` table (migrations 26/27) and the fail-closed source registry
(`atp.instruments.sources`). They return a coverage dict for a GET read-model and NEVER start an import,
qualification, subscription or trade. Partial coverage is reported as partial — an unimported or blocked
source is surfaced explicitly, never presented as covered.

The read-model makes the four lifecycle stages **explicit and distinct**:
  * ``source_connected``  — a directory source is declared AND available (an entitled provider attached);
  * ``imported``          — a row exists in `instruments` (discovered from a reference source);
  * ``ibkr_verified``     — its `qualification_status` == VERIFIED (a unique IBKR contract, read-only);
  * ``tradable``          — its `tradability_status` == 'tradable' (proven tradeable — the strongest claim).

SAFETY: read-only. AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""

from __future__ import annotations

from .sources import InstrumentSourceEntry, seed_sources

# fixed, non-user column set that may be grouped (guards the f-string aggregation below)
_GROUPABLE = frozenset({
    "region", "country", "exchange", "asset_class", "source",
    "verification_status", "qualification_status", "tradability_status", "market_data_status", "sub_class",
})


def _group(store, column: str) -> dict[str, int]:
    if column not in _GROUPABLE:                       # defence-in-depth: never interpolate arbitrary text
        raise ValueError(f"not a groupable column: {column}")
    rows = store._all(f"SELECT {column}, COUNT(*) FROM instruments GROUP BY {column}")
    out: dict[str, int] = {}
    for value, count in rows:
        out[str(value) if value is not None else "UNKNOWN"] = int(count)
    return dict(sorted(out.items()))


def _scalar(store, where: str) -> int:
    return int(store._all(f"SELECT COUNT(*) FROM instruments WHERE {where}")[0][0])


def source_coverage(sources: list[InstrumentSourceEntry]) -> dict:
    """Declared directory sources with active/available vs MISSING/BLOCKED called out explicitly, plus their
    documented license and the regions / venues / asset classes each is meant to cover."""
    available = [s for s in sources if s.status == "AVAILABLE"]
    blocked = [s for s in sources if s.status == "BLOCKED"]
    missing = [s for s in sources if s.status == "MISSING"]

    def _bucket(attr: str) -> dict:
        counts: dict[str, dict] = {}
        for s in sources:
            for key in getattr(s, attr):
                b = counts.setdefault(str(key), {"declared": 0, "available": 0})
                b["declared"] += 1
                b["available"] += 1 if s.status == "AVAILABLE" else 0
        return dict(sorted(counts.items()))

    return {
        "total_declared": len(sources),
        "available_sources": [s.source_id for s in available],
        "blocked_sources": [s.source_id for s in blocked],
        "missing_sources": [s.source_id for s in missing],
        # never claim full coverage from a partial/empty active set
        "coverage_partial": bool(blocked or missing) or not available,
        "by_region": _bucket("regions"),
        "by_asset_class": _bucket("asset_classes"),
        "by_venue": _bucket("venues"),
        "licenses": {s.source_id: {"license_status": s.license_status,
                                   "storage_allowed": s.storage_allowed,
                                   "redistribution_allowed": s.redistribution_allowed,
                                   "usable": s.usable} for s in sources},
        "blocked_reasons": {s.source_id: s.blocked_reason for s in sources if s.blocked_reason},
        "sources": [s.summary() for s in sources],
    }


def instrument_coverage(store, *, sources: list[InstrumentSourceEntry] | None = None) -> dict:
    """The full instrument-universe coverage read-model: declared source coverage + imported-catalogue
    breakdown by region / country / exchange / asset class / source / qualification status, and the explicit
    four-stage funnel (source connected → imported → IBKR-verified → tradable)."""
    sources = sources if sources is not None else seed_sources()
    total = int(store._all("SELECT COUNT(*) FROM instruments")[0][0])

    src = source_coverage(sources)
    funnel = {
        "sources_connected": len(src["available_sources"]),   # 'Quelle angebunden'
        "imported": total,                                     # 'Instrument importiert'
        "ibkr_verified": _scalar(store, "qualification_status = 'VERIFIED'"),   # 'IBKR-verifiziert'
        "tradable": _scalar(store, "tradability_status = 'tradable'"),          # 'handelbar'
    }
    return {
        "generated": "read-model",   # timestamp is stamped by the caller (this fn is deterministic/pure)
        "source_coverage": src,
        "instruments": {
            "total": total,
            "by_region": _group(store, "region"),
            "by_country": _group(store, "country"),
            "by_exchange": _group(store, "exchange"),
            "by_asset_class": _group(store, "asset_class"),
            "by_source": _group(store, "source"),
            "by_verification_status": _group(store, "verification_status"),
            "by_qualification_status": _group(store, "qualification_status"),
            "by_tradability_status": _group(store, "tradability_status"),
        },
        "funnel": funnel,
        # never present a partial universe as complete
        "coverage_partial": src["coverage_partial"] or funnel["imported"] == 0
        or funnel["ibkr_verified"] < funnel["imported"],
    }
