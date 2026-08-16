"""Macro Intelligence Service (§ Phase R1.2 — READ-ONLY, intelligence input).

A 15th independently-supervised process — the Macro Collector. Each cycle it pulls the global macro
environment (rates / inflation / employment / volatility / currency / commodities) from the configured
provider and appends an immutable snapshot (one per hour; ON CONFLICT DO NOTHING → history never
rewritten). Macro data moves slowly, so it polls a few times a day.

It only MEASURES the environment. It performs NO trading, NO order/broker/IBKR interaction, NO strategy
activation, holds no credentials beyond the read-only macro data key, and never touches the Risk Engine
or Execution. No provider / no key → NO DATA (never fabricated). PostgreSQL down → FAIL CLOSED.
"""
from __future__ import annotations

import asyncio
import os

from ..macrodata.collector import MacroCollector
from ..macrodata.provider import resolve_provider
from ..macrodata.readmodel import build_macro
from .base import Service


class MacroIntelligenceService(Service):
    name = "macro_intelligence"
    health_port = 9114

    def __init__(self) -> None:
        super().__init__()
        self.provider = resolve_provider()
        self.collector = MacroCollector(self.store, self.provider)
        self.poll_interval = float(os.environ.get("ATP_MACRO_POLL_S", "21600"))  # 6h; macro is slow-moving
        self._degraded = False

    async def main(self) -> None:
        while not self._stop.is_set():
            if not self.provider.configured:
                self._detail = (f"no macro provider configured (provider={self.provider.name}) "
                                "-> NO DATA, never fabricated")
                await self._sleep(self.poll_interval)
                continue
            try:
                persisted = await asyncio.to_thread(self.collector.collect)
                macro = await asyncio.to_thread(build_macro, self.store)
                self._degraded = False
                self._detail = (f"provider={self.provider.name} persisted={persisted} "
                                f"regime={macro.get('regime')} score={macro.get('score')}")
            except Exception:
                self._degraded = True                      # provider/DB error -> fail-closed (persist nothing)
                self._detail = "DEGRADED: macro collection failed (fail-closed)"
            await self._sleep(self.poll_interval)

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    MacroIntelligenceService().run()


if __name__ == "__main__":
    main()
