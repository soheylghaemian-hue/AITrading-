"""Read-only IBKR market-data probe for the live dashboard (§10/§22).

Uses the EXISTING IBKR adapter read-only: qualify + snapshot reqMktData, classify into the five
states, and NEVER present a sentinel (-1) or NaN as a real price. No orders, no order-management.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..core.enums import AssetClass
from ..core.events import Instrument
from ..dashboard.snapshot import classify_quote

# (symbol, asset class, informational exchange) — the fixed watchlist.
DEFAULT_UNIVERSE: list[tuple[str, AssetClass, str]] = [
    ("EUR.USD", AssetClass.FX, "IDEALPRO"),
    ("AAPL", AssetClass.EQUITY, "NASDAQ"),
    ("NVDA", AssetClass.EQUITY, "NASDAQ"),
    ("SPY", AssetClass.EQUITY, "ARCA/NYSE"),
]
_MDT = {1: "REALTIME", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}


def _instrument(symbol: str, asset_class: AssetClass) -> Instrument:
    if asset_class is AssetClass.FX and "." in symbol:
        base, quote = symbol.split(".", 1)
        return Instrument(base, AssetClass.FX, currency=quote)
    return Instrument(symbol, asset_class)


def _price(v) -> float | None:
    """A real price only if finite and > 0 — IBKR uses -1 (and NaN) as 'no data' sentinels."""
    try:
        f = float(v)
        return f if (f == f and f > 0.0) else None
    except (TypeError, ValueError):
        return None


def _size(v) -> float | None:
    try:
        f = float(v)
        return f if (f == f and f >= 0.0) else None
    except (TypeError, ValueError):
        return None


async def probe_market_data(broker, universe=DEFAULT_UNIVERSE, *, settle: float = 6.0) -> list[dict]:
    """Return per-instrument availability (5 states) with real bid/ask/last/sizes or None. The
    broker must be a connected read-only IBKR broker. No orders are ever sent."""
    ib = broker._require()  # noqa: SLF001 — read-only client access, same as the smoke test
    errors: dict[str, tuple[int, str]] = {}

    def _on_error(reqId, code, msg, contract=None):
        sym = getattr(contract, "symbol", None)
        if sym is not None:
            errors[sym] = (int(code), str(msg).split(",")[0])

    evt = getattr(ib, "errorEvent", None)
    if evt is not None:
        evt += _on_error
    try:
        ib.reqMarketDataType(1)  # request REAL-TIME (read-only)
    except Exception:  # noqa: BLE001
        pass

    tickers: dict[str, object] = {}
    for symbol, asset_class, _exch in universe:
        try:
            contract = broker._factory.contract(_instrument(symbol, asset_class))  # noqa: SLF001
            q = getattr(ib, "qualifyContractsAsync", None)
            if q is not None:
                await q(contract)
            tickers[symbol] = ib.reqMktData(contract, "", True, False)  # snapshot, read-only
        except Exception as exc:  # noqa: BLE001
            errors.setdefault(symbol, (-1, repr(exc)))

    for _ in range(int(settle * 2)):
        await asyncio.sleep(0.5)

    now = datetime.now(timezone.utc).isoformat()
    out: list[dict] = []
    for symbol, asset_class, exchange in universe:
        t = tickers.get(symbol)
        bid, ask, last = (_price(getattr(t, "bid", None)), _price(getattr(t, "ask", None)),
                          _price(getattr(t, "last", None))) if t is not None else (None, None, None)
        bsz = _size(getattr(t, "bidSize", None)) if t is not None else None
        asz = _size(getattr(t, "askSize", None)) if t is not None else None
        code, msg = errors.get(symbol, (None, ""))
        mdt_raw = getattr(t, "marketDataType", None) if t is not None else None
        delayed = mdt_raw in (3, 4)
        status, reason = classify_quote(bid=bid, ask=ask, last=last, error_code=code,
                                        error_msg=msg, delayed=delayed)
        available = status in ("DATA_AVAILABLE", "DELAYED")
        out.append({
            "symbol": symbol, "asset_class": asset_class.value, "exchange": exchange,
            "status": status, "market_data_type": _MDT.get(mdt_raw) if available else None,
            "bid": bid, "ask": ask, "last": last, "bid_size": bsz, "ask_size": asz,
            "timestamp": now, "error_code": code, "error_message": (msg or None), "reason": reason,
        })
    if evt is not None:
        evt -= _on_error
    return out


def subscription_report(market_data: list[dict]) -> list[dict]:
    """Technical subscription status derived from the probe — real state only, nothing purchased."""
    out: list[dict] = []
    for row in market_data:
        available = row["status"] == "DATA_AVAILABLE"
        if row["asset_class"] == "fx":
            required, sub_required = "IDEALPRO FX (included with account)", not available
        else:
            required = ("US Securities Snapshot and Futures Value Bundle "
                        "(or NASDAQ/NYSE network real-time)")
            sub_required = not available
        out.append({
            "instrument": row["symbol"], "asset_class": row["asset_class"], "exchange": row["exchange"],
            "required_market_data": required, "current_status": row["status"],
            "ibkr_error": row.get("error_code"), "subscription_required": bool(sub_required),
        })
    return out
