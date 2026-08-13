"""Read-only notification watchdog → iPhone (§23).

A tiny always-on monitor that pushes an alert to your phone (ntfy/Telegram, via
NotificationCenter.from_env) when something real changes. It NEVER trades, never places/cancels
orders, and by default makes NO IBKR API calls at all — it only checks whether the IB Gateway
port is reachable. Optional market-data availability alerts (opt-in) do a read-only snapshot.

Config (environment):
  NTFY_TOPIC / TELEGRAM_*        where to push (see docs/IPHONE_NOTIFICATIONS.md)
  WATCHDOG_HOST=127.0.0.1        gateway host
  WATCHDOG_PORT=4002            gateway port (paper)
  WATCHDOG_INTERVAL=120         seconds between checks
  WATCHDOG_MARKET_DATA=0        set to 1 to also alert on read-only market-data availability
  WATCHDOG_SYMBOLS=AAPL,NVDA,SPY,EUR.USD
  WATCHDOG_MD_EVERY=3           run the (opt-in) market-data check every Nth cycle

    PYTHONPATH=src NTFY_TOPIC=... python3 examples/notify_watchdog.py
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import datetime, timezone

from atp.dashboard.notifications import Kind, NotificationCenter, Severity


# --------------------------------------------------------------------------- pure core (tested)
def status_events(prev: dict, cur: dict) -> list[tuple[Kind, Severity, str]]:
    """Compare the previous and current status and return only the CHANGES worth pushing.
    No state change → no notification (so the phone never gets spammed)."""
    out: list[tuple[Kind, Severity, str]] = []
    if "gateway" in prev and prev.get("gateway") != cur.get("gateway"):
        if cur.get("gateway"):
            out.append((Kind.SYSTEM_ERROR, Severity.WARNING, "IB Gateway reachable again (port 4002)"))
        else:
            out.append((Kind.BROKER_DISCONNECT, Severity.CRITICAL, "IB Gateway NOT reachable (port 4002)"))
    prev_md, cur_md = prev.get("market", {}), cur.get("market", {})
    for sym, avail in cur_md.items():
        if sym in prev_md and prev_md[sym] != avail:
            if avail:
                out.append((Kind.DATA_FEED, Severity.WARNING, f"{sym}: market data now AVAILABLE (realtime)"))
            else:
                out.append((Kind.DATA_FEED, Severity.WARNING, f"{sym}: market data lost / unavailable"))
    return out


# --------------------------------------------------------------------------- checks
def gateway_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def market_availability(host: str, port: int, symbols: list[str]) -> dict[str, bool]:
    """Read-only per-symbol availability via a snapshot probe. No orders, no open-orders query."""
    from atp.brokers.ibkr import IBKRBroker, IBKRConfig  # noqa: PLC0415
    from atp.core.enums import AssetClass  # noqa: PLC0415
    from atp.core.events import Instrument  # noqa: PLC0415

    def _inst(sym: str) -> Instrument:
        if "." in sym:
            base, quote = sym.split(".", 1)
            return Instrument(base, AssetClass.FX, currency=quote)
        return Instrument(sym, AssetClass.EQUITY)

    def _num(v):
        try:
            f = float(v)
            return f if f == f and f > 0 else None
        except (TypeError, ValueError):
            return None

    broker = IBKRBroker(IBKRConfig(host=host, port=port, client_id=17, readonly=True))
    out: dict[str, bool] = {}
    try:
        await broker.connect()
        ib = broker._require()  # noqa: SLF001 — read-only snapshot, like the smoke test
        tickers = {}
        for sym in symbols:
            c = broker._factory.contract(_inst(sym))  # noqa: SLF001
            q = getattr(ib, "qualifyContractsAsync", None)
            if q is not None:
                await q(c)
            tickers[sym] = ib.reqMktData(c, "", True, False)  # snapshot, read-only
        for _ in range(12):
            await asyncio.sleep(0.5)
        for sym, t in tickers.items():
            has = any(_num(getattr(t, k, None)) for k in ("bid", "ask", "last"))
            out[sym] = bool(has)
    finally:
        await broker.disconnect()
    return out


# --------------------------------------------------------------------------- loop
async def main() -> int:
    host = os.environ.get("WATCHDOG_HOST", "127.0.0.1")
    port = int(os.environ.get("WATCHDOG_PORT", "4002"))
    interval = float(os.environ.get("WATCHDOG_INTERVAL", "120"))
    check_md = os.environ.get("WATCHDOG_MARKET_DATA", "0") == "1"
    md_every = max(1, int(os.environ.get("WATCHDOG_MD_EVERY", "3")))
    symbols = [s.strip() for s in os.environ.get("WATCHDOG_SYMBOLS", "AAPL,NVDA,SPY,EUR.USD").split(",") if s.strip()]

    nc = NotificationCenter.from_env()
    if not nc._sinks:  # noqa: SLF001
        print("✗ no push sink configured — set NTFY_TOPIC or TELEGRAM_* (see docs/IPHONE_NOTIFICATIONS.md)")
        return 2

    nc.push(Kind.SYSTEM_ERROR,
            f"Watchdog started — monitoring IB Gateway {host}:{port}"
            + (f", market data {symbols}" if check_md else " (gateway reachability only)"),
            severity=Severity.INFO)
    print(f"watchdog running · interval {interval:.0f}s · market-data={'on' if check_md else 'off'} · Ctrl-C to stop")

    prev: dict = {}
    cycle = 0
    while True:
        cur: dict = {"gateway": gateway_reachable(host, port)}
        if check_md and cur["gateway"] and cycle % md_every == 0:
            try:
                cur["market"] = await market_availability(host, port, symbols)
            except Exception as exc:  # noqa: BLE001 — never let a probe error kill the watchdog
                print(f"  market-data probe error: {exc!r}")
        elif "market" in prev:
            cur["market"] = prev["market"]  # carry forward between md cycles (no false changes)

        for kind, sev, msg in status_events(prev, cur):
            nc.push(kind, msg, severity=sev)
            print(f"  {datetime.now(timezone.utc):%H:%M:%S} PUSH [{sev.value}] {msg}")

        prev = cur
        cycle += 1
        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nwatchdog stopped")
