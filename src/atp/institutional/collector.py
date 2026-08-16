"""Institutional collector (§ Phase R1.3). Persists 13F quarter-over-quarter position changes (from the
SEC 13F provider's holdings history) and SEC Form 4 insider transactions into PostgreSQL. Idempotent
(immutable ids, ON CONFLICT DO NOTHING) → restart-safe. Persists ONLY real provider data — never a
fabricated change or transaction. No execution, no broker, no IBKR, no copy-trading anywhere.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .changes import analyze_changes
from .clusters import WINDOWS, detect_cluster


class InstitutionalCollector:
    def __init__(self, store, holdings_provider, insider_provider, *, ciks=None, symbols=None) -> None:
        self.store = store
        self.holdings = holdings_provider              # Sec13FTraderProvider (get_holdings_history)
        self.insiders = insider_provider               # SecForm4Provider (get_insider_transactions)
        self.ciks = list(ciks) if ciks is not None else list(getattr(holdings_provider, "_ciks", []))
        self.symbols = list(symbols) if symbols is not None else sorted(
            getattr(insider_provider, "_map", {}).keys())

    def collect_changes(self) -> int:
        """13F QoQ position changes per institution. Returns rows recorded."""
        recorded = 0
        for cik in self.ciks:
            hist = self.holdings.get_holdings_history(cik)
            current = hist.get("current")
            if not current:
                continue
            name = hist.get("name") or cik
            prev_holdings = (hist.get("previous") or {}).get("holdings")
            period = current.get("period")
            for c in analyze_changes(name, current.get("holdings", []), prev_holdings, period):
                cid = f"{str(cik).zfill(10)}:{c['symbol']}:{c['filing_period']}"
                self.store.insert_institutional_change(
                    id=cid, institution=c["institution"], symbol=c["symbol"],
                    previous_shares=c["previous_shares"], current_shares=c["current_shares"],
                    share_change=c["share_change"], percentage_change=c["percentage_change"],
                    direction=c["direction"], filing_period=c["filing_period"])
                recorded += 1
        return recorded

    def collect_insiders(self) -> int:
        """Recent Form 4 insider BUY/SELL per watched symbol. Returns rows recorded."""
        recorded = 0
        for sym in self.symbols:
            for i, tx in enumerate(self.insiders.get_insider_transactions(sym)):
                tid = f"{tx.get('accession', 'na')}:{sym.upper()}:{i}"
                self.store.insert_insider_transaction(
                    id=tid, symbol=sym.upper(), insider_name=tx.get("insider_name"),
                    title=tx.get("title"), transaction_type=tx.get("transaction_type"),
                    shares=tx.get("shares"), price=tx.get("price"),
                    transaction_date=tx.get("transaction_date"))
                recorded += 1
        return recorded

    def collect_clusters(self, now: datetime | None = None) -> int:
        """Snapshot the current insider cluster per (symbol, window) from persisted Form 4 data (§ R1.4).
        Immutable per day. Returns snapshots recorded."""
        now = now or datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        recorded = 0
        for sym in self.symbols:
            txns = self.store.list_insider_transactions(sym.upper(), 500)
            if not txns:
                continue
            for w in WINDOWS:
                c = detect_cluster(txns, w, now)
                if c["cluster_type"] is None:              # no in-window activity → nothing to record
                    continue
                cid = f"{sym.upper()}:{c['time_window']}:{day}"
                self.store.insert_insider_cluster(
                    id=cid, symbol=sym.upper(), time_window=c["time_window"], cluster_type=c["cluster_type"],
                    insider_count=c["insider_count"], weighted_score=c["score"],
                    total_shares=c["total_shares"], total_value=c["total_value"])
                recorded += 1
        return recorded

    def collect(self) -> dict:
        changes = self.collect_changes()
        insiders = self.collect_insiders()
        clusters = self.collect_clusters()                 # derived from the just-persisted Form 4 data
        return {"changes": changes, "insiders": insiders, "clusters": clusters}
