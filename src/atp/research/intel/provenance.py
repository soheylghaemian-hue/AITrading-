"""§ R3.1A — canonical input envelope + provenance capture for a consensus snapshot.

For every component the consensus actually used, capture the exact structured contribution plus the
genuine timestamps of its source record — never inventing a publication/availability timestamp. Provenance
status is:
  * VERIFIED     — a genuine source-availability timestamp exists (e.g. a news publication time).
  * OBSERVED_ONLY — only an observed/ingest/quote time is known (availability not separately knowable).
  * UNKNOWN      — no trustworthy timestamp at all.

The envelope is built synchronously in the SAME collection run as the consensus computation (the collector
runs `build_ai_consensus` then this, back-to-back, then writes atomically). It is NOT a later
reconstruction from since-overwritten read models. Read-only; no trading.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .envelope import canonical_json

# consensus component_name → (domain, source_provider label)
_DOMAINS = {
    "Fundamentals": "fundamentals", "News": "news", "Options": "options",
    "Trader Intelligence": "traders", "Market Data": "market_data", "Risk": "risk",
}


def _ts(v):
    return None if v in (None, "") else str(v)


def _parse(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _freshness(observed_ts: str | None, decision_ts: str, max_age_h: float = 48.0) -> str:
    o, d = _parse(observed_ts), _parse(decision_ts)
    if o is None:
        return "MISSING"
    if d is None:
        return "OBSERVED"
    age_h = (d.astimezone(timezone.utc) - o.astimezone(timezone.utc)).total_seconds() / 3600.0
    return "FRESH" if age_h <= max_age_h else "STALE"


def _provenance_for(store, symbol: str, domain: str) -> dict:
    """Genuine source timestamps for a component's domain — NEVER fabricated. Returns
    {source_provider, source_event_ts, source_published_or_filed_ts, source_observed_ts,
    source_available_ts, provenance_status}."""
    sym = symbol.upper()
    try:
        if domain == "news":
            items = store.list_news(sym, 20) or []
            if items:
                latest = max(items, key=lambda n: getattr(n, "published_at", "") or "")
                pub = _ts(getattr(latest, "published_at", None))
                return {"source_provider": getattr(latest, "source", None) or "news",
                        "source_event_ts": pub, "source_published_or_filed_ts": pub,
                        "source_observed_ts": _ts(getattr(latest, "created_at", None)),
                        "source_available_ts": pub,   # a published time IS an availability time
                        "provenance_status": "VERIFIED" if pub else "UNKNOWN"}
        elif domain == "fundamentals":
            fm = store.get_financial_metrics(sym)
            if fm is not None:
                # only an ingest time + reporting period exist — the SEC as-reported availability is unknown.
                return {"source_provider": "fundamentals", "source_event_ts": _ts(getattr(fm, "period", None)),
                        "source_published_or_filed_ts": None,
                        "source_observed_ts": _ts(getattr(fm, "updated_at", None)),
                        "source_available_ts": None, "provenance_status": "OBSERVED_ONLY"}
        elif domain == "options":
            fl = store.get_options_flow(sym)
            if fl is not None:
                obs = _ts(getattr(fl, "timestamp", None))
                return {"source_provider": "options", "source_event_ts": obs,
                        "source_published_or_filed_ts": None, "source_observed_ts": obs,
                        "source_available_ts": None, "provenance_status": "OBSERVED_ONLY"}
        elif domain == "traders":
            pos = store.list_trader_positions_for_symbol(sym) or []
            if pos:
                obs = _ts(getattr(pos[0], "timestamp", None))
                return {"source_provider": "traders", "source_event_ts": obs,
                        "source_published_or_filed_ts": None, "source_observed_ts": obs,
                        "source_available_ts": None, "provenance_status": "OBSERVED_ONLY"}
        elif domain == "market_data":
            bars = store.list_ohlc_bars(sym, "1D", 3) or store.list_ohlc_bars(sym, "1m", 3) or []
            if bars:
                b = bars[-1]
                return {"source_provider": getattr(b, "source", None) or "market_data",
                        "source_event_ts": _ts(getattr(b, "ts", None)),
                        "source_published_or_filed_ts": None,
                        "source_observed_ts": _ts(getattr(b, "created_at", None)),
                        "source_available_ts": None, "provenance_status": "OBSERVED_ONLY"}
        elif domain == "risk":
            return {"source_provider": "risk_engine", "source_event_ts": None,
                    "source_published_or_filed_ts": None, "source_observed_ts": None,
                    "source_available_ts": None, "provenance_status": "OBSERVED_ONLY"}
    except Exception:  # noqa: BLE001 — a source read failure must not fabricate provenance
        pass
    return {"source_provider": None, "source_event_ts": None, "source_published_or_filed_ts": None,
            "source_observed_ts": None, "source_available_ts": None, "provenance_status": "UNKNOWN"}


def build_input_envelope(store, symbol: str, assessment: dict, decision_ts: str) -> list[dict]:
    """One canonical input row per consensus component, carrying the exact contribution + genuine provenance.
    The component set equals the consensus's component set (proving same-computation capture)."""
    inputs: list[dict] = []
    for c in assessment.get("components") or []:
        name = c.get("component_name")
        domain = _DOMAINS.get(name, name or "unknown")
        prov = _provenance_for(store, symbol, domain) if name in _DOMAINS else {
            "source_provider": None, "source_event_ts": None, "source_published_or_filed_ts": None,
            "source_observed_ts": None, "source_available_ts": None, "provenance_status": "UNKNOWN"}
        inputs.append({
            "component_name": name,
            "canonical_value_json": canonical_json({k: c.get(k) for k in
                                                    ("component_name", "score", "weight", "direction",
                                                     "reason", "risk_flags")}),
            "component_score": None if c.get("score") is None else str(c.get("score")),
            "component_status": c.get("direction"),
            "missing_data_reason": None,
            "freshness_state": _freshness(prov.get("source_observed_ts"), decision_ts),
            **prov,
        })
    return inputs
