"""Options Intelligence Service (§ Phase G2.3 — READ-ONLY).

A 9th independently-supervised process (isolated from Trading Core / Risk / Broker / OHLC / News /
Traders / Fundamentals). It polls the configured options provider (Massive/Polygon by default, via the
licensed MASSIVE_API_KEY), aggregates the chain and persists per-symbol flow to PostgreSQL; the Control
API serves a deterministic options intelligence score + signals/risks. It never trades, never places
orders, never touches IBKR, and holds no broker credentials.

Options entitlement is separate from stocks — no key / no entitlement / a fetch failure → nothing
persisted → NO DATA (never a fabricated IV or flow). PostgreSQL unavailable → FAIL CLOSED (DEGRADED).
"""
from __future__ import annotations

import asyncio
import os

from ..optflow.collector import OptionsCollector
from ..optflow.provider import resolve_provider
from .base import Service

DEFAULT_SYMBOLS = ["AAPL", "NVDA", "SPY"]


def options_symbols() -> list[str]:
    raw = os.environ.get("ATP_OPTIONS_SYMBOLS")
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SYMBOLS)


class OptionsIntelligenceService(Service):
    name = "options_intelligence"
    health_port = 9108

    def __init__(self) -> None:
        super().__init__()
        self.provider = resolve_provider()
        self.collector = OptionsCollector(self.store, self.provider)
        self.symbols = options_symbols()
        self.poll_interval = float(os.environ.get("ATP_OPTIONS_POLL_S", "1800"))  # 30 min
        self._loaded = 0
        self._degraded = False

    async def main(self) -> None:
        while not self._stop.is_set():
            if not self.provider.configured:
                self._detail = (f"no options provider configured (provider={self.provider.name}) "
                                "-> NO DATA, never fabricated")
                await self._sleep(self.poll_interval)
                continue
            loaded = 0
            errors = 0
            for sym in self.symbols:
                try:
                    if await asyncio.to_thread(self.collector.collect, sym):
                        loaded += 1
                except Exception:
                    errors += 1                            # DB/provider error -> fail-closed (persist nothing)
            self._loaded = loaded
            self._degraded = errors > 0
            self._detail = (f"DEGRADED: {errors}/{len(self.symbols)} symbols failed (fail-closed); "
                            f"symbols={self._loaded}" if self._degraded
                            else f"provider={self.provider.name}; symbols_loaded={self._loaded}")
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    OptionsIntelligenceService().run()


if __name__ == "__main__":
    main()
