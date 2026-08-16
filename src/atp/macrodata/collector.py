"""Macro collector (§ Phase R1.2). Pulls the global macro environment from the provider and appends an
immutable snapshot (one per hour) to PostgreSQL. Idempotent → restart-safe. Persists ONLY real provider
data — never a fabricated rate, CPI or VIX. Raises on a store failure so the service can fail closed.
No execution, no broker, no IBKR access anywhere.
"""
from __future__ import annotations

from datetime import datetime, timezone


class MacroCollector:
    def __init__(self, store, provider) -> None:
        self.store = store
        self.provider = provider

    def collect(self, now: datetime | None = None) -> bool:
        """Fetch + persist one macro snapshot. Returns True if any real metric was persisted."""
        now = now or datetime.now(timezone.utc)
        m = self.provider.snapshot()
        if not m.any_present():
            return False                                   # NO DATA → nothing persisted (never fabricated)
        sid = f"macro:{now.strftime('%Y-%m-%dT%H')}"
        self.store.insert_macro_snapshot(
            id=sid, timestamp=now.isoformat(), fed_rate=m.fed_rate, treasury_10y=m.treasury_10y,
            treasury_2y=m.treasury_2y, cpi=m.cpi, unemployment=m.unemployment, vix=m.vix, dxy=m.dxy,
            oil=m.oil, gold=m.gold, source=self.provider.name)
        return True
