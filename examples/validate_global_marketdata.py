"""READ-ONLY GLOBAL market-data validation (§ Phase 10).

Qualifies every instrument in the global universe against IBKR, requests a real-time snapshot,
normalizes it through the provider-independent MarketDataManager + data-quality gate, and prints
one line per instrument: STATUS, DATA_TYPE, BID, ASK, LAST, BID_SIZE, ASK_SIZE, VOLUME, TIMESTAMP,
SOURCE, PRIMARY_EXCHANGE, ERROR_CODE, ERROR_MESSAGE.

NO orders, NO cancels, NO placeOrder. readonly=True. This never changes any account/subscription
state — it only observes what IBKR delivers and classifies it. Instruments without an active
real-time subscription honestly report SUBSCRIPTION_REQUIRED / BLOCKED — never a fabricated price.
"""

import argparse
import asyncio
from datetime import datetime, timezone

from atp.brokers.ibkr import IBKRBroker, IBKRConfig
from atp.core.enums import AssetClass
from atp.marketdata import GLOBAL_UNIVERSE, MarketDataManager, QualityStatus

MDT = {1: "REALTIME", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


async def main(client_id: int, settle: float) -> None:
    broker = IBKRBroker(IBKRConfig(host="127.0.0.1", port=4002, client_id=client_id, readonly=True))
    await broker.connect()
    ib = broker._require()
    from ib_async import Forex, Stock  # noqa: PLC0415

    errors: dict[str, tuple[int, str]] = {}

    def on_err(reqId, code, msg, contract=None):
        sym = getattr(contract, "symbol", None)
        if sym is not None:
            errors[sym] = (int(code), " ".join(str(msg).split())[:160])
    ib.errorEvent += on_err
    try:
        ib.reqMarketDataType(1)  # REAL-TIME only
    except Exception:
        pass

    # Build + qualify one contract per spec.
    contracts = {}
    for spec in GLOBAL_UNIVERSE:
        try:
            if spec.asset_class is AssetClass.FX:
                c = Forex(spec.symbol.replace(".", ""))
            else:
                c = Stock(spec.symbol, spec.exchange, spec.currency, primaryExchange=spec.primary_exchange)
            await ib.qualifyContractsAsync(c)
            contracts[spec.symbol] = c
        except Exception as exc:
            errors[spec.symbol] = (-1, f"qualify failed: {exc!r}"[:160])

    # Fire snapshot requests (read-only).
    tickers = {}
    for spec in GLOBAL_UNIVERSE:
        c = contracts.get(spec.symbol)
        if c is None:
            continue
        try:
            tickers[spec.symbol] = ib.reqMktData(c, "", True, False)
        except Exception as exc:
            errors[spec.symbol] = (-1, repr(exc)[:160])
    for _ in range(int(settle / 0.5)):
        await asyncio.sleep(0.5)

    now = datetime.now(timezone.utc)
    raw = {}
    for spec in GLOBAL_UNIVERSE:
        t = tickers.get(spec.symbol)
        c = contracts.get(spec.symbol)
        code, msg = errors.get(spec.symbol, (None, None))
        raw[spec.symbol] = {
            "con_id": getattr(c, "conId", None) if c else None,
            "source": getattr(t, "lastExchange", None) if t else None,
            "bid": _num(getattr(t, "bid", None)) if t else None,
            "ask": _num(getattr(t, "ask", None)) if t else None,
            "last": _num(getattr(t, "last", None)) if t else None,
            "bid_size": _num(getattr(t, "bidSize", None)) if t else None,
            "ask_size": _num(getattr(t, "askSize", None)) if t else None,
            "volume": _num(getattr(t, "volume", None)) if t else None,
            "market_data_type": MDT.get(getattr(t, "marketDataType", None)) if t else None,
            "timestamp": now,
            "error_code": code,
            "error_message": msg,
        }

    ib.errorEvent -= on_err
    await broker.disconnect()

    mgr = MarketDataManager()
    quotes = mgr.classify(raw, now=now)

    hdr = (f"{'REGION':<12}{'SYMBOL':<8}{'STATUS':<22}{'DTYPE':<9}"
           f"{'BID':>10}{'ASK':>10}{'LAST':>10}{'BIDSZ':>8}{'ASKSZ':>8}{'VOL':>10}  "
           f"{'SOURCE':<8}{'PRIM':<8}{'ERR':>6}  MESSAGE")
    print("\n" + hdr)
    print("-" * len(hdr))
    by_status: dict[str, int] = {}
    for q in quotes:
        spec = _spec(q.symbol)
        by_status[q.status] = by_status.get(q.status, 0) + 1
        print(f"{spec.region:<12}{q.symbol:<8}{q.status:<22}{(q.market_data_type or '—'):<9}"
              f"{_s(q.bid):>10}{_s(q.ask):>10}{_s(q.last):>10}{_s(q.bid_size):>8}{_s(q.ask_size):>8}{_s(q.volume):>10}  "
              f"{(q.source or '—'):<8}{q.primary_exchange:<8}{_s(q.error_code):>6}  {q.error_message or q.reason}")

    ready = [q for q in quotes if q.status == QualityStatus.READY.value]
    print("\n=== SUMMARY ===")
    print(f"instruments total : {len(quotes)}")
    print(f"REALTIME (READY)  : {len(ready)}  {[q.symbol for q in ready]}")
    for st, n in sorted(by_status.items()):
        if st != QualityStatus.READY.value:
            print(f"{st:<20}: {n}")
    print("\norders=0 cancels=0 placeOrder=0 readonly=True")


def _spec(sym):
    for s in GLOBAL_UNIVERSE:
        if s.symbol == sym:
            return s
    raise KeyError(sym)


def _s(v):
    return "—" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", type=int, default=25)
    ap.add_argument("--settle", type=float, default=8.0)
    args = ap.parse_args()
    asyncio.run(main(args.client_id, args.settle))
