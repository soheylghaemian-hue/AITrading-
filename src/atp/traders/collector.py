"""Trader collector (§ Phase G2.5). Pulls traders / performance / positions from the licensed provider
and upserts them into PostgreSQL. Idempotent (PK on id / trader_id / (trader_id,symbol)) → restart-safe.
Persists ONLY real provider data — never a fabricated trader, return or position. Raises on a store
failure so the service can fail closed. No execution, no broker, no IBKR access anywhere.
"""
from __future__ import annotations

from ..store import utcnow_iso


class TraderCollector:
    def __init__(self, store, provider) -> None:
        self.store = store
        self.provider = provider

    def collect(self) -> int:
        """Persist every tracked trader + their performance + positions. Returns traders ingested."""
        ingested = 0
        for t in self.provider.get_traders():
            if not t.id or not t.name:
                continue
            self.store.upsert_trader(
                id=t.id, name=t.name, source=t.source, market_focus=t.market_focus,
                strategy_type=t.strategy_type, track_record_days=t.track_record_days)
            perf = self.provider.get_performance(t.id)
            if perf is not None:
                self.store.upsert_trader_performance(
                    trader_id=t.id, total_return=perf.total_return, annualized_return=perf.annualized_return,
                    win_rate=perf.win_rate, max_drawdown=perf.max_drawdown, sharpe_ratio=perf.sharpe_ratio,
                    sortino_ratio=perf.sortino_ratio, average_holding_period=perf.average_holding_period,
                    number_of_trades=perf.number_of_trades)
            for p in self.provider.get_positions(t.id):
                if not p.symbol or not p.direction:
                    continue
                self.store.upsert_trader_position(
                    trader_id=t.id, symbol=p.symbol, direction=p.direction, entry_price=p.entry_price,
                    position_size=p.position_size, timestamp=p.timestamp or utcnow_iso())
            ingested += 1
        return ingested
