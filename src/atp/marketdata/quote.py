"""NormalizedQuote — one provider-independent quote shape the whole system speaks (§ Phase 10)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(slots=True)
class NormalizedQuote:
    symbol: str
    con_id: int | None
    asset_class: str
    currency: str
    exchange: str
    primary_exchange: str
    source: str | None
    bid: float | None
    ask: float | None
    last: float | None
    bid_size: float | None
    ask_size: float | None
    volume: float | None
    timestamp: datetime | None
    market_data_type: str | None
    status: str = ""            # data-quality status (set by the gate)
    reason: str = ""            # human reason for the status
    latency_ms: float | None = None   # feed one-way latency (recv - source ts), when known
    error_code: int | None = None
    error_message: str | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return self.ask - self.bid
        return None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat() if self.timestamp else None
        d["spread"] = self.spread
        return d
