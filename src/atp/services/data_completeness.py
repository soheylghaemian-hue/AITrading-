"""Data Completeness Service (§ Phase C1 — READ-ONLY, reliability layer).

A 14th independently-supervised process — the Completeness Analyzer. Each cycle it measures, for every
watched symbol, how complete GIGBAY's information is across the 7 intelligence domains and records an
immutable snapshot (one per symbol per hour; ON CONFLICT DO NOTHING → history is never rewritten).

It only MEASURES information quality. It performs NO trading, NO order/broker/IBKR interaction, NO
strategy activation, holds no credentials, and never touches the Risk Engine or Execution. Nothing is
fabricated — a missing source scores 0. PostgreSQL down → FAIL CLOSED (records nothing; systemd restarts).
"""
from __future__ import annotations

import asyncio
import os

from ..completeness.engine import compute_completeness, record_completeness
from .base import Service

DEFAULT_SYMBOLS = ["AAPL", "NVDA", "SPY"]


def completeness_symbols() -> list[str]:
    raw = os.environ.get("ATP_COMPLETENESS_SYMBOLS") or os.environ.get("ATP_EVALUATION_SYMBOLS")
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SYMBOLS)


class DataCompletenessService(Service):
    name = "data_completeness"
    health_port = 9113

    def __init__(self) -> None:
        super().__init__()
        self.symbols = completeness_symbols()
        self.poll_interval = float(os.environ.get("ATP_COMPLETENESS_POLL_S", "1800"))  # 30 min
        self._recorded = 0
        self._degraded = False

    async def main(self) -> None:
        await self._cycle()                                 # measure at startup, then every interval
        while not self._stop.is_set():
            await self._sleep(self.poll_interval)
            if not self._stop.is_set():
                await self._cycle()

    async def _cycle(self) -> None:
        try:
            self._recorded = await asyncio.to_thread(record_completeness, self.store, self.symbols)
            states = {}
            for sym in self.symbols:
                c = await asyncio.to_thread(compute_completeness, self.store, sym)
                states[sym] = f"{sym}={c['score']}/{c['state']}"
            self._degraded = False
            self._detail = f"recorded={self._recorded} " + " ".join(states.values())
        except Exception:
            self._degraded = True                           # DB error → fail-closed (record nothing)
            self._detail = "DEGRADED: completeness measurement failed (fail-closed)"

    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    DataCompletenessService().run()


if __name__ == "__main__":
    main()
