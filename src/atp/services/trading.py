"""Trading Core + Execution Service (§ Phase C — service B).

Owns regime / agents / opportunity / portfolio / position-sizing / the authoritative Risk Engine and
the default-off durable Paper Canary owner. It consumes validated quotes from the bus and is the ONLY
place a canary intent can be processed; every decision is persisted atomically and no live broker is
ever called.

The normal loop only consumes quotes and evaluates the fail-closed input gate. Paper fills exist only
behind the literal double opt-in, explicit owner commands and human activation; there is NO live
execution and NO autonomous start. After a crash, recovery never resumes RUNNING automatically.
"""
from __future__ import annotations

import asyncio
import os

from ..runtime.gate import TradingGate
from .base import LoopbackCommandServer, Service
from .marketdata import QUOTES_CHANNEL
from .paper_canary_owner import PAPER_CANARY_OWNER_PATHS, PaperCanaryOwner
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
        self._paper_owner: PaperCanaryOwner | None = None
        self._paper_commands: LoopbackCommandServer | None = None

    def _latest_paper_quote(self, symbol: str) -> dict | None:
        """Return a same-loop snapshot; the owner performs the full quote + DB-health re-attestation."""
        quote = self._quotes.get(symbol)
        return dict(quote) if type(quote) is dict else None

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

    def can_accept_risk_reduction_input(self) -> tuple[bool, str]:
        """Gate an explicit SELL while allowing the durable daily-loss latch to stay engaged."""
        g = self.gate.can_reduce_risk()
        if not g.allowed:
            return (False, g.reason)
        if not market_data_fresh(self.store, max_age_s=self.md_max_age):
            return (False, "market data stale/unavailable -> NO RISK REDUCTION")
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
        if self._paper_owner is not None:
            raise RuntimeError("Trading Core Paper Canary owner already started")
        self._detail = f"state={self.life.status.value}"
        owner = PaperCanaryOwner(
            quote_getter=self._latest_paper_quote,
            trade_gate=self.can_accept_trade_input,
            risk_reduction_gate=self.can_accept_risk_reduction_input,
        )
        await owner.start()  # dedicated Store; recovers an active run once and never auto-activates
        self._paper_owner = owner
        token = os.environ.get("ATP_PAPER_CANARY_INTERNAL_TOKEN")
        if token:
            try:
                port = int(os.environ.get("ATP_PAPER_CANARY_OWNER_PORT", "9112"))
                commands = LoopbackCommandServer(
                    owner_loop=asyncio.get_running_loop(),
                    handler=owner.command,
                    token=token,
                    paths=PAPER_CANARY_OWNER_PATHS,
                    port=port,
                )
                commands.start()
                self._paper_commands = commands
            except Exception:
                await owner.close()
                self._paper_owner = None
                raise
        elif os.environ.get("ATP_DURABLE_PAPER_CANARY_ENABLED") == "true":
            await owner.close()
            self._paper_owner = None
            raise RuntimeError(
                "ATP_PAPER_CANARY_INTERNAL_TOKEN is required when Durable Paper Canary is enabled",
            )
        self._sub_task = asyncio.create_task(self._consume_quotes())

    async def main(self) -> None:
        while not self._stop.is_set():
            ok, reason = self.can_accept_trade_input()
            self._detail = (f"state={self.life.status.value} "
                            f"inputs={'OPEN' if ok else 'BLOCKED'} ({reason}) "
                            f"quotes={len(self._quotes)} "
                            f"paper_owner={'UP' if self._paper_owner else 'OFF'}")
            # No autonomous execution: only explicit, authenticated owner commands can enqueue a
            # default-off durable paper fill; live broker execution remains absent.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    async def on_stop(self) -> None:
        commands = self._paper_commands
        self._paper_commands = None
        if commands is not None:
            commands.close()
        if self._sub_task is not None:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):
                pass
        owner = self._paper_owner
        self._paper_owner = None
        if owner is not None:
            await owner.close()


def main() -> None:
    TradingCoreService().run()


if __name__ == "__main__":
    main()
