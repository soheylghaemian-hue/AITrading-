"""READ-ONLY Massive realtime validation (§ Phase 10.2).

Connects to the Massive realtime stocks WebSocket, subscribes to AAPL/NVDA/SPY, streams for a short
window, normalizes every tick through the EXISTING MarketDataManager + quality_gate, and prints one
honest line per symbol. NEVER prints the API key. NO orders, NO IBKR, NO delayed/mock fallback.

If auth or realtime entitlement fails, it prints the exact Massive server message and STOPS.
"""

import argparse
import asyncio
from datetime import datetime, timezone

from atp.marketdata import QualityStatus
from atp.marketdata.massive_provider import (
    MASSIVE_SYMBOLS,
    MassiveAuthError,
    MassiveEntitlementError,
    MassiveProvider,
)


def _s(v):
    return "—" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


async def main(seconds: float) -> int:
    provider = MassiveProvider(MASSIVE_SYMBOLS)
    if not provider.has_key:
        print("MASSIVE_API_KEY = MISSING — stopping (no fake key, no IBKR fallback).")
        return 2
    print("MASSIVE_API_KEY = CONFIGURED (value never printed)")
    try:
        await provider.connect()
    except MassiveEntitlementError as e:
        print(f"MASSIVE REALTIME ENTITLEMENT = NO\nexact server message: {e}")
        print("STOP — not switching to delayed / IBKR / mock.")
        return 3
    except MassiveAuthError as e:
        print(f"MASSIVE AUTH = FAILED\nexact server message: {e}")
        print("STOP.")
        return 3
    print(f"authenticated + subscribed; streaming {seconds:.0f}s (real live ticks) ...")
    await provider.drain(seconds)

    now = datetime.now(timezone.utc)
    quotes = provider.quotes(now=now)
    raw = provider.raw_by_symbol()
    await provider.close()

    ready = 0
    print("\n================= PER-INSTRUMENT (READ-ONLY) =================")
    for q in quotes:
        r = raw.get(q.symbol, {})
        gate = "DATA_AVAILABLE" if q.status == QualityStatus.READY.value else q.status
        if q.status == QualityStatus.READY.value:
            ready += 1
        recv = r.get("receive_timestamp")
        print(f"\n{q.symbol}")
        print(f"  source              = {q.source}")
        print(f"  market_data_type    = {q.market_data_type or '—'}")
        print(f"  status              = {gate}")
        print(f"  bid / ask / last    = {_s(q.bid)} / {_s(q.ask)} / {_s(q.last)}")
        print(f"  bid_size / ask_size = {_s(q.bid_size)} / {_s(q.ask_size)}")
        print(f"  volume (accum.)     = {_s(q.volume)}")
        print(f"  exchange            = {q.primary_exchange} (venue bid/ask id {_s(r.get('bid_exch'))}/{_s(r.get('ask_exch'))})")
        print(f"  provider timestamp  = {q.timestamp.isoformat() if q.timestamp else '—'}")
        print(f"  local receive ts    = {recv.isoformat() if recv else '—'}")
        print(f"  latency             = {('%d ms' % round(q.latency_ms)) if q.latency_ms is not None else '—'}")
        print(f"  live ticks received = {r.get('events', 0)}")
        if q.status != QualityStatus.READY.value:
            print(f"  reason              = {q.reason}")

    print("\n================= SUMMARY TABLE =================")
    hdr = f"{'SYMBOL':<7}{'SOURCE':<8}{'DTYPE':<10}{'STATUS':<18}{'BID':>10}{'ASK':>10}{'LAST':>10}{'LAT(ms)':>9}"
    print(hdr); print("-" * len(hdr))
    for q in quotes:
        gate = "DATA_AVAILABLE" if q.status == QualityStatus.READY.value else q.status
        lat = None if q.latency_ms is None else round(q.latency_ms)
        print(f"{q.symbol:<7}{(q.source or '—'):<8}{(q.market_data_type or '—'):<10}{gate:<18}"
              f"{_s(q.bid):>10}{_s(q.ask):>10}{_s(q.last):>10}{_s(lat):>9}")

    print(f"\nREALTIME (READY via Massive): {ready}/{len(quotes)}")
    print("orders=0 cancels=0 placeOrder=0  IBKR readonly untouched  execution_enabled=false  autonomous=DISABLED")
    return 0 if ready == len(quotes) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.seconds)))
