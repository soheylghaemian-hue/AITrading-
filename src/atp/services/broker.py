"""Broker Connector Service (§ Phase F1 — IBKR, READ-ONLY).

A 4th independently-supervised process, isolated from Market Data / Trading Core / Control. It opens a
READ-ONLY IBKR API session to the local IB Gateway (paper), reads account / positions / open orders /
executions, reconciles them against PostgreSQL, writes a reconciliation result + heartbeat, and
publishes a read-model for the Control API. It never requests US-equity market data from IBKR (that
stays on Massive) and it NEVER submits, cancels or modifies an order.

Two independent execution barriers:
  1. IB Gateway "Read-Only API" is enabled (order methods rejected at the Gateway), and the client
     connects with ``readonly=True``.
  2. ``BROKER_EXECUTION_ENABLED=false`` (default) — ``submit_order`` raises ExecutionDisabled BEFORE
     any IBKR order method could be reached. There is no code path that calls placeOrder/cancelOrder.

Reconciliation is fail-closed: an unexplained mismatch is reported as a break and drives the durable
lifecycle to RECOVERY_REQUIRED (never auto-repaired, never auto-resumed to RUNNING).
"""
from __future__ import annotations

import os
import signal
import time
from datetime import datetime, timezone

from ..runtime.lifecycle import LifecycleManager, RuntimeStatus
from ..store import D, open_store
from ..store.money import to_decimal
from .base import build_dsn, redis_url
from .recovery import build_recovery_checks

SERVICE = "broker"
SNAPSHOT_KEY = "broker:snapshot"          # Control read-model (cache; never authoritative, no secrets)
HEARTBEAT_INTERVAL = 5.0
RECONCILE_INTERVAL = float(os.environ.get("ATP_BROKER_RECONCILE_S", "15"))
POS_TOL = D("0.00000001")


def execution_enabled() -> bool:
    return os.environ.get("BROKER_EXECUTION_ENABLED", "false").strip().lower() in ("1", "true", "yes")


class ExecutionDisabled(RuntimeError):
    """Raised whenever any order submission is attempted while execution is disabled (F1 hard guard)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile_state(db_pos: dict, bk_pos: dict, db_open_count: int, bk_open_count: int,
                    *, tol=POS_TOL) -> tuple[bool, list[str]]:
    """Pure reconciliation: broker truth vs durable PostgreSQL state. Returns (consistent, breaks).
    A position diff, an unknown broker position, a DB position absent at the broker, or an open-order
    count mismatch are all breaks. Never repairs anything."""
    breaks: list[str] = []
    dbp = {k: v for k, v in db_pos.items() if v != 0}
    bkp = {k: v for k, v in bk_pos.items() if v != 0}
    for sym in sorted(set(dbp) | set(bkp)):
        dq = dbp.get(sym, D(0))
        bq = bkp.get(sym, D(0))
        if abs(dq - bq) > tol:
            breaks.append(f"position {sym}: db={dq} broker={bq}")
    if db_open_count != bk_open_count:
        breaks.append(f"open orders: db={db_open_count} broker={bk_open_count}")
    return (len(breaks) == 0, breaks)


class BrokerConnector:
    def __init__(self) -> None:
        from ib_async import IB                     # lazy: importing this module never requires ib_async
        self.store = open_store(build_dsn(), migrate=False)
        self.life = LifecycleManager(self.store)
        try:
            from ..persistence.state import RedisStateStore
            self._snap = RedisStateStore(redis_url()) if redis_url() else None
        except Exception:
            self._snap = None
        self.ib = IB()
        self.host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
        self.port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
        self.client_id = int(os.environ.get("ATP_BROKER_CLIENT_ID", "7"))
        self._stop = False
        self._connected = False
        self._account: str | None = None
        self._reconciled = False
        self._last_reconcile: str | None = None
        self._last_break: str | None = None

    # -- HARD execution guard (barrier #2) -------------------------------
    @staticmethod
    def submit_order(*_a, **_k):
        """The ONLY place an order could ever be created. It refuses BEFORE any IBKR call unless
        BROKER_EXECUTION_ENABLED is explicitly true — placeOrder/cancelOrder/modifyOrder are never
        reached. In F1 execution is disabled, so this always raises."""
        if not execution_enabled():
            raise ExecutionDisabled(
                "BROKER_EXECUTION_ENABLED=false — order submission rejected before IBKR (F1 read-only)")
        raise ExecutionDisabled("execution path not implemented in F1 (read-only only)")

    # -- connection ------------------------------------------------------
    def connect(self) -> bool:
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=True, timeout=25)
        except Exception as e:
            self._connected = False
            self._detail_hb("DEGRADED", f"connect failed: {type(e).__name__}")
            return False
        self._connected = self.ib.isConnected()
        if self._connected:
            accts = self.ib.managedAccounts()
            self._account = accts[0] if accts else None
            if self._account:
                # Do NOT call reqAccountUpdates() here: on this paper account it blocks
                # indefinitely waiting for accountDownloadEnd, which never arrives and stalls
                # the whole connect() (service hangs in ep_poll, connect() never returns).
                # It is unnecessary — accountSummary() below delivers equity/cash/buying_power,
                # and positions()/reqAllOpenOrders()/fills() are independent of it.
                try:
                    self.ib.reqAccountSummary()
                except Exception:
                    pass
            self.ib.sleep(4)                          # let account summary / position / order feeds populate
        return self._connected

    # -- read-only broker state ------------------------------------------
    def _broker_state(self) -> dict:
        acct = self._account or ""
        want = ("NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds")
        vals: dict = {}
        ccy = None
        # accountSummary reports consolidated base-currency values; accept whatever currency the
        # account is denominated in (EUR/USD/…), never hard-filter it out.
        for v in self.ib.accountSummary(acct):
            if v.tag in want and v.value not in (None, ""):
                vals[v.tag] = v.value
                ccy = v.currency or ccy
        if not vals:                                  # fallback to the streaming account values
            base, anyv = {}, {}
            for v in self.ib.accountValues(acct):
                if v.tag in want and v.value not in (None, ""):
                    (base if v.currency == "BASE" else anyv).setdefault(v.tag, (v.value, v.currency))
            for t in want:
                src = base.get(t) or anyv.get(t)
                if src:
                    vals[t] = src[0]
                    ccy = ccy or src[1]
        positions = {}
        for p in self.ib.positions(acct):
            sym = p.contract.symbol
            positions[sym] = positions.get(sym, D(0)) + to_decimal(str(p.position))
        open_orders = self.ib.reqAllOpenOrders()
        fills = self.ib.fills()
        return {
            "account": acct,
            "paper": acct[:2] in ("DU", "DF", "DI"),
            "equity": vals.get("NetLiquidation"),
            "cash": vals.get("TotalCashValue"),
            "buying_power": vals.get("BuyingPower"),
            "available_funds": vals.get("AvailableFunds"),
            "currency": ccy,
            "positions": positions,
            "open_order_count": len(open_orders),
            "fill_count": len(fills),
        }

    # -- reconciliation (broker truth vs PostgreSQL) ---------------------
    def reconcile(self, bstate: dict) -> tuple[bool, list[str]]:
        """Compare broker truth against durable PostgreSQL state via reconcile_state(). Never repairs."""
        from ..runtime.positions import reconstruct_positions
        db_pos = {k: v.quantity for k, v in reconstruct_positions(self.store).items()}
        db_open = self.store._one(
            "SELECT COUNT(*) FROM orders WHERE state IN ('AUTHORIZED','SUBMITTED','PENDING')")[0]
        return reconcile_state(db_pos, bstate["positions"], db_open, bstate["open_order_count"])

    def _persist_reconcile(self, ok: bool, breaks: list[str]) -> None:
        self._reconciled = ok
        self._last_reconcile = now_iso()
        self._last_break = None if ok else "; ".join(breaks)[:400]
        try:
            self.store.audit(actor="broker", action="RECONCILE",
                             previous_state=self.life.status.value, new_state=self.life.status.value,
                             reason=("reconciled" if ok else f"MISMATCH: {self._last_break}"))
        except Exception:
            pass
        # fail-closed on mismatch: an active trading runtime must HALT; never auto-repair / auto-run.
        if not ok:
            try:
                if self.life.status in (RuntimeStatus.RUNNING, RuntimeStatus.ARMED):
                    self.life.halt(reason=f"broker reconciliation mismatch: {self._last_break}", actor="broker")
            except Exception:
                pass

    # -- observability ---------------------------------------------------
    def _detail_hb(self, status: str, detail: str) -> None:
        try:
            self.store.upsert_heartbeat(service=SERVICE, status=status, detail=detail[:200] or None)
        except Exception:
            pass

    def _write_readmodel(self, bstate: dict | None) -> None:
        rs = None
        try:
            r = self.store.get_runtime_state(); rs = r.status if r else None
        except Exception:
            pass
        snap = {
            "broker": "IBKR",
            "mode": "PAPER" if (bstate and bstate.get("paper")) else ("PAPER" if self._account and self._account[:2] in ("DU","DF","DI") else "UNKNOWN"),
            "connection": "CONNECTED" if self._connected else "DISCONNECTED",
            "account": (bstate or {}).get("account") or self._account,
            "reconciliation": (("RECONCILED" if self._reconciled else "MISMATCH")
                               if self._connected else "UNAVAILABLE"),
            "last_reconcile": self._last_reconcile,
            "last_break": self._last_break,
            "equity": (bstate or {}).get("equity"),
            "cash": (bstate or {}).get("cash"),
            "buying_power": (bstate or {}).get("buying_power"),
            "currency": (bstate or {}).get("currency"),
            "position_count": len((bstate or {}).get("positions", {})),
            "open_order_count": (bstate or {}).get("open_order_count"),
            "execution_enabled": execution_enabled(),
            "runtime_state": rs,
            "ts": now_iso(),
        }
        if self._snap is not None:
            try:
                self._snap.set(SNAPSHOT_KEY, snap)
            except Exception:
                pass

    # -- lifecycle -------------------------------------------------------
    def _cycle(self) -> None:
        if not self.ib.isConnected():
            self._connected = False
            self._detail_hb("DEGRADED", "broker DISCONNECTED -> fail closed")
            self._write_readmodel(None)
            return
        self._connected = True
        bstate = self._broker_state()
        ok, breaks = self.reconcile(bstate)
        self._persist_reconcile(ok, breaks)
        self._detail_hb("UP" if ok else "DEGRADED",
                        f"CONNECTED {bstate['account']} paper={bstate['paper']} "
                        f"recon={'OK' if ok else 'MISMATCH'} pos={len(bstate['positions'])} oo={bstate['open_order_count']}")
        self._write_readmodel(bstate)

    def run(self) -> None:
        # recover() at startup: an unexpected restart NEVER auto-resumes RUNNING.
        self.life.recover()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "_stop", True))
        last_recon = 0.0
        backoff = 2.0
        while not self._stop:
            if not self.ib.isConnected():
                self._detail_hb("DEGRADED", "connecting to IB Gateway...")
                if not self.connect():
                    self._write_readmodel(None)
                    # cap the reconnect sleep at 10s so the PG heartbeat stays fresh while disconnected:
                    # an alive-but-disconnected broker then reports DISCONNECTED (not falsely STALE); only
                    # a dead/hung broker (no heartbeat) crosses the control STALE threshold.
                    self._sleep(min(backoff, 10.0)); backoff = min(10.0, backoff * 2); continue
                backoff = 2.0
                self._cycle(); last_recon = time.monotonic()      # startup reconciliation
            else:
                if time.monotonic() - last_recon >= RECONCILE_INTERVAL:
                    self._cycle(); last_recon = time.monotonic()
                else:
                    self._detail_hb("UP" if self._reconciled else "DEGRADED", "connected; idle")
            self._sleep(HEARTBEAT_INTERVAL)
        try:
            self.ib.disconnect()
        except Exception:
            pass
        self.store.close()

    def _sleep(self, secs: float) -> None:
        # ib.sleep runs the ib_async event loop (processing IB messages) while waiting.
        try:
            self.ib.sleep(secs)
        except Exception:
            time.sleep(secs)


def main() -> None:
    BrokerConnector().run()


if __name__ == "__main__":
    main()
