"""MarketDataManager (§ Phase 10) — the provider-independent boundary between raw market data and
the autonomous pipeline.

Flow:  raw provider quotes  ->  normalize (NormalizedQuote)  ->  data-quality gate  ->  classify.

The manager knows nothing about IBKR (or any provider): it consumes plain dicts of raw fields keyed
by symbol and emits NormalizedQuote objects tagged with a QualityStatus. Only READY quotes are
handed to the AI/opportunity/risk pipeline via `ready()`. Nothing here fabricates a price, and no
quote that fails the gate can leak into trading.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .quality import QualityStatus, quality_gate
from .quote import NormalizedQuote
from .universe import GLOBAL_UNIVERSE, InstrumentSpec

# status -> the subscription-state label the dashboard shows
_SUBSCRIPTION_STATE = {
    QualityStatus.READY.value: "ACTIVE",
    QualityStatus.SUBSCRIPTION_REQUIRED.value: "REQUIRED",
    QualityStatus.DELAYED.value: "DELAYED_ONLY",
    QualityStatus.BLOCKED.value: "BLOCKED",
    QualityStatus.STALE.value: "ACTIVE",
    QualityStatus.INVALID.value: "UNKNOWN",
    QualityStatus.CLOSED_MARKET.value: "ACTIVE",
    QualityStatus.DATA_NOT_AVAILABLE.value: "UNKNOWN",
}


def _num(v):
    """Scrub raw provider numbers: NaN and the IBKR -1 'no data' sentinel (and any negative) -> None."""
    try:
        f = float(v)
        return f if (f == f and f >= 0.0) else None
    except (TypeError, ValueError):
        return None


class MarketDataManager:
    """Provider-independent normalization + quality classification over an instrument universe."""

    def __init__(self, universe: list[InstrumentSpec] | None = None, *, max_age_s: float = 30.0):
        self.universe = list(universe if universe is not None else GLOBAL_UNIVERSE)
        self.max_age_s = max_age_s
        self._by_symbol = {s.symbol: s for s in self.universe}

    # -- normalization ---------------------------------------------------------
    def normalize(self, spec: InstrumentSpec, raw: dict | None, *, now: datetime | None = None) -> NormalizedQuote:
        raw = raw or {}
        ts = raw.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = None
        q = NormalizedQuote(
            symbol=spec.symbol,
            con_id=raw.get("con_id"),
            asset_class=spec.asset_class.value,
            currency=spec.currency,
            exchange=spec.exchange,
            primary_exchange=spec.primary_exchange,
            source=raw.get("source") or None,
            bid=_num(raw.get("bid")),
            ask=_num(raw.get("ask")),
            last=_num(raw.get("last")),
            bid_size=_num(raw.get("bid_size")),
            ask_size=_num(raw.get("ask_size")),
            volume=_num(raw.get("volume")),
            timestamp=ts,
            market_data_type=raw.get("market_data_type"),
            latency_ms=_num(raw.get("latency_ms")),
            error_code=raw.get("error_code"),
            error_message=raw.get("error_message"),
        )
        status, reason = quality_gate(q, now=now or datetime.now(timezone.utc), max_age_s=self.max_age_s)
        q.status = status.value
        q.reason = reason
        return q

    def classify(self, raw_by_symbol: dict[str, dict], *,
                 specs: list[InstrumentSpec] | None = None,
                 now: datetime | None = None) -> list[NormalizedQuote]:
        now = now or datetime.now(timezone.utc)
        specs = specs if specs is not None else self.universe
        return [self.normalize(spec, raw_by_symbol.get(spec.symbol), now=now) for spec in specs]

    # -- consumers -------------------------------------------------------------
    def ready(self, quotes: list[NormalizedQuote]) -> list[NormalizedQuote]:
        """The ONLY quotes allowed into the autonomous pipeline."""
        return [q for q in quotes if q.status == QualityStatus.READY.value]

    def dashboard_rows(self, quotes: list[NormalizedQuote]) -> list[dict]:
        rows = []
        for q in quotes:
            spec = self._by_symbol.get(q.symbol)
            realtime = (q.market_data_type or "").upper() == "REALTIME"
            rows.append({
                "region": spec.region if spec else "",
                "exchange": (spec.label or q.primary_exchange) if spec else q.primary_exchange,
                "symbol": q.symbol,
                "source": q.source or (spec.label if spec else q.exchange),
                "status": q.status,
                "realtime": realtime,
                "bid": q.bid,
                "ask": q.ask,
                "last": q.last,
                "spread": q.spread,
                "bid_size": q.bid_size,
                "ask_size": q.ask_size,
                "volume": q.volume,
                "timestamp": q.timestamp.isoformat() if q.timestamp else None,
                "latency_ms": q.latency_ms,
                "error": q.error_message or (q.reason if q.status != QualityStatus.READY.value else None),
                "subscription_state": _SUBSCRIPTION_STATE.get(q.status, "UNKNOWN"),
                "currency": q.currency,
            })
        return rows
