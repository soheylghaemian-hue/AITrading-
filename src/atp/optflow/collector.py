"""Options collector (§ Phase G2.3). Fetches the real option chain from the provider, aggregates it
into per-symbol flow, and upserts the top contracts (options_snapshot) + the aggregate (options_flow)
into PostgreSQL. Idempotent → restart-safe. Persists ONLY real provider data — never a fabricated IV,
volume or flow. Raises on a store failure so the service can fail closed. No execution/broker/IBKR access.
"""
from __future__ import annotations

from ..store import utcnow_iso
from .analytics import aggregate_chain

_TOP_CONTRACTS = 40   # persist the most-active contracts to options_snapshot (the chain view / future)


class OptionsCollector:
    def __init__(self, store, provider) -> None:
        self.store = store
        self.provider = provider

    def collect(self, symbol: str) -> bool:
        """Fetch + aggregate + persist options for one symbol. Returns True if real flow was persisted."""
        sym = symbol.upper()
        contracts = self.provider.get_option_chain(sym)
        if not contracts:
            return False
        flow = aggregate_chain(contracts, sym)
        if flow is None:
            return False
        now = utcnow_iso()

        for c in sorted(contracts, key=lambda x: (x.volume or 0), reverse=True)[:_TOP_CONTRACTS]:
            if not c.expiration_date or c.strike is None:
                continue
            self.store.upsert_options_snapshot(
                symbol=sym, expiration_date=c.expiration_date, strike=c.strike, option_type=c.option_type,
                timestamp=now, bid=c.bid, ask=c.ask, last=c.last, volume=c.volume,
                open_interest=c.open_interest, implied_volatility=c.implied_volatility, source="MASSIVE")

        self.store.upsert_options_flow(
            symbol=sym, timestamp=now, call_volume=flow.call_volume, put_volume=flow.put_volume,
            call_put_ratio=flow.call_put_ratio, implied_volatility=flow.implied_volatility,
            open_interest=flow.open_interest, unusual_activity_score=flow.unusual_activity_score,
            large_trade_count=flow.large_trade_count, premium_volume=flow.premium_volume,
            sentiment=flow.sentiment)
        return True
