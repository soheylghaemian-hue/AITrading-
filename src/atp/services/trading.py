"""Trading Core + Execution Service (§ Phase C — service B).

Owns regime / agents / opportunity / portfolio / position-sizing / the (authoritative) Risk Engine,
plus the PaperBroker + execution lifecycle (orders/fills/idempotency/reconciliation). It consumes
validated quotes from the bus and is the ONLY place a TRADE INTENT can be produced — but it may never
submit to a broker on its own, and every decision is persisted before any execution.

Phase C acceptance constraints (ABSOLUTE SCOPE): NO autonomous start, NO paper execution, NO live.
The live loop therefore only CONSUMES quotes, tracks freshness, and evaluates the fail-closed input
gate; it never executes. Trading can only ever run after a human ARM + START via the Control API —
never automatically, and never after a crash (recover() lands in RECOVERY_REQUIRED).
"""
from __future__ import annotations

import asyncio
import os

from ..runtime.gate import TradingGate
from .base import Service
from .marketdata import QUOTES_CHANNEL
from .recovery import market_data_fresh


class TradingCoreService(Service):
    name = "trading_core"
    health_port = 9102

    def __init__(self) -> None:
        super().__init__()
        self.gate = TradingGate(self.store, self.life)
        self.md_max_age = float(os.environ.get("ATP_MD_MAX_AGE_S", "15"))
        self._quotes: dict[str, dict] = {}
        self._sub_task: asyncio.Task | None = None

    # -- fail-closed input decision -------------------------------------
    def can_accept_trade_input(self) -> tuple[bool, str]:
        """Whether a new trade input may be processed AT ALL. Fails closed on DB loss, kill switch,
        non-RUNNING state, missing risk state, daily-loss lock, or stale/absent market data."""
        g = self.gate.can_trade()
        if not g.allowed:
            return (False, g.reason)
        if not market_data_fresh(self.store, max_age_s=self.md_max_age):
            return (False, "market data stale/unavailable -> NO NEW TRADE")
        return (True, "ok")

    # -- quote consumption ----------------------------------------------
    async def _consume_quotes(self) -> None:
        # Re-subscribe across bus hiccups: a Redis outage never crashes the process and never loses
        # authoritative state; while the bus is down, quotes simply stop and the freshness gate blocks.
        while not self._stop.is_set():
            try:
                async for ev in self.bus.subscribe(QUOTES_CHANNEL):
                    sym = ev.get("symbol")
                    if sym:
                        self._quotes[sym] = ev
            except asyncio.CancelledError:
                raise
            except Exception:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def on_start(self) -> None:
        self._detail = f"state={self.life.status.value}"
        self._sub_task = asyncio.create_task(self._consume_quotes())

    async def main(self) -> None:
        while not self._stop.is_set():
            ok, reason = self.can_accept_trade_input()
            self._detail = (f"state={self.life.status.value} "
                            f"inputs={'OPEN' if ok else 'BLOCKED'} ({reason}) "
                            f"quotes={len(self._quotes)}")
            # No execution. Paper execution stays OFF; trading only ever runs after a human
            # ARM+START via Control — never automatically and never after a crash.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    async def on_stop(self) -> None:
        if self._sub_task is not None:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    TradingCoreService().run()


if __name__ == "__main__":
    main()
