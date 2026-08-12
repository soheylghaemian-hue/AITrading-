"""Live/paper trading runner (§17, §24 Phase 14).

Drives the **same** `AutonomousTradingDesk` used by the backtester, but off a live market feed
instead of a historical replay — so paper and backtest behavior cannot diverge (§13). Each
bar: feed the desk, step it, book the fills. Periodically: reconcile the internal book against
the broker (§17) and run the governance monitor over the journal (§19), so a strategy that
decays in production is taken offline *mid-stream* — the learning loop, live.

Works identically over `ReplayFeed` + `PaperBroker` (offline, tested here) and
`IBKRMarketFeed` + `IBKRBroker` (live). The only broker-specific touch is forwarding quotes to
a paper broker so it can fill; a real broker fills from the market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..brokers.base import Broker
from ..brokers.reconcile import Reconciler
from ..core.enums import Side
from ..core.events import Bar, QuoteEvent
from ..desk.desk import AutonomousTradingDesk
from ..feeds.hub import FeedHub
from ..governance.decay import GovernanceMonitor
from ..journal.store import TradeJournal
from ..logging_config import get_logger
from .feed import MarketFeed

log = get_logger("live")


@dataclass(slots=True)
class RunSummary:
    bars: int = 0
    quotes: int = 0
    executed: int = 0
    blocked: int = 0
    reconciliations: int = 0
    reconciliation_breaks: int = 0
    governance_runs: int = 0
    governance_actions: int = 0
    feed_refreshes: int = 0
    feed_updates: int = 0
    suspended: list[str] = field(default_factory=list)
    internal_book: dict[str, float] = field(default_factory=dict)


class LiveRunner:
    def __init__(
        self,
        *,
        desk: AutonomousTradingDesk,
        broker: Broker,
        feed: MarketFeed,
        reconciler: Reconciler | None = None,
        monitor: GovernanceMonitor | None = None,
        journal: TradeJournal | None = None,
        feeds: FeedHub | None = None,
        reconcile_every: int = 50,
        govern_every: int = 0,           # 0 disables periodic governance
        feed_refresh_every: int = 1,     # refresh context feeds every N bars
        max_bars: int | None = None,
    ) -> None:
        self._desk = desk
        self._broker = broker
        self._feed = feed
        self._reconciler = reconciler
        self._monitor = monitor
        self._journal = journal
        self._feeds = feeds
        self._reconcile_every = reconcile_every
        self._govern_every = govern_every
        self._feed_refresh_every = feed_refresh_every
        self._max_bars = max_bars
        self._book: dict[str, float] = {}
        # PaperBroker needs quotes forwarded to it to fill; a live broker fills from the market.
        self._forward_quotes = hasattr(broker, "set_quote")

    async def run(self) -> RunSummary:
        summary = RunSummary()
        await self._feed.connect()
        try:
            async for event in self._feed.stream():
                if isinstance(event, QuoteEvent):
                    if self._forward_quotes:
                        self._broker.set_quote(event)  # type: ignore[attr-defined]
                    self._desk.on_quote(event)
                    summary.quotes += 1
                    continue

                # It's a Bar: advance the desk one step.
                bar: Bar = event
                self._desk.on_bar(bar)
                # Keep options/rates/events context current before strategies read it.
                await self._maybe_refresh_feeds(summary, bar.ts)
                report = await self._desk.step(now=bar.ts)
                self._book_fills(report.executed)
                summary.bars += 1
                summary.executed += len(report.executed)
                summary.blocked += len(report.blocked)

                await self._maybe_reconcile(summary, bar.ts)
                self._maybe_govern(summary)

                if self._max_bars is not None and summary.bars >= self._max_bars:
                    break
        finally:
            await self._feed.disconnect()

        summary.internal_book = {k: v for k, v in self._book.items() if abs(v) > 1e-9}
        log.info("run done | bars=%d executed=%d blocked=%d recon_breaks=%d gov_actions=%d",
                 summary.bars, summary.executed, summary.blocked,
                 summary.reconciliation_breaks, summary.governance_actions)
        return summary

    # ------------------------------------------------------------- internals
    def _book_fills(self, executed) -> None:
        """Maintain the desk's own intended book from fills, for reconciliation (§17)."""
        for er in executed:
            fill = er.result.fill if er.result else None
            if fill is None:
                continue
            sign = 1 if fill.side is Side.BUY else -1
            key = fill.instrument.key
            self._book[key] = self._book.get(key, 0.0) + sign * fill.quantity

    async def _maybe_refresh_feeds(self, summary: RunSummary, now: datetime) -> None:
        if self._feeds is None or self._feed_refresh_every <= 0:
            return
        # bars is incremented after step; use bars+1 so the first bar refreshes too.
        if (summary.bars + 1) % self._feed_refresh_every != 0:
            return
        results = await self._feeds.refresh_all(now)
        summary.feed_refreshes += 1
        summary.feed_updates += sum(results.values())

    async def _maybe_reconcile(self, summary: RunSummary, now: datetime) -> None:
        if self._reconciler is None or self._reconcile_every <= 0:
            return
        if summary.bars % self._reconcile_every != 0:
            return
        report = await self._reconciler.run(self._book)
        summary.reconciliations += 1
        if not report.is_consistent:
            summary.reconciliation_breaks += 1

    def _maybe_govern(self, summary: RunSummary) -> None:
        if self._monitor is None or self._journal is None or self._govern_every <= 0:
            return
        if summary.bars % self._govern_every != 0:
            return
        decisions = self._monitor.evaluate(self._journal)
        summary.governance_runs += 1
        actions = [d for d in decisions if d.action != "keep"]
        if actions:
            summary.governance_actions += len(actions)
            for d in actions:
                if d.name not in summary.suspended and d.action == "suspend":
                    summary.suspended.append(d.name)
