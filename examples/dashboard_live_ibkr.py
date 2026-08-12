"""Phase 2A — REAL IBKR read-only data → Command Center snapshot.

Connects to a running IB Gateway (PAPER, read-only), reads the real account, positions,
reconciliation, per-instrument market-data availability (5 states), runs a READ-ONLY AI
observation pass over whatever real data exists, and writes a real dashboard snapshot to
src/atp/dashboard/static/snapshot.json.

    PYTHONPATH=src python3 examples/dashboard_live_ibkr.py --port 4002

STRICTLY READ-ONLY. It builds NO ExecutionEngine, calls NO place_order and NO desk.step().
Orders sent = 0. No fabricated data: unavailable instruments show DATA_NOT_AVAILABLE and the
agents report NO DATA. View it with:

    python3 -m http.server 8000 --directory src/atp/dashboard/static   # open http://localhost:8000/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from atp.brokers.ibkr import IBKRBroker, IBKRConfig
from atp.brokers.reconcile import diff_positions
from atp.core.enums import AssetClass
from atp.core.events import Instrument
from atp.dashboard.observe import observe_readonly
from atp.dashboard.snapshot import build_snapshot, classify_quote
from atp.risk.engine import RiskEngine, RiskLimits, RiskState
from atp.strategy import BreakoutStrategy, MeanReversionStrategy, MomentumStrategy

OUT = Path(__file__).resolve().parents[1] / "src/atp/dashboard/static/snapshot.json"

# Fixed, small test universe. Exchange is informational for the dashboard.
UNIVERSE = [
    ("AAPL", AssetClass.EQUITY, "NASDAQ"),
    ("NVDA", AssetClass.EQUITY, "NASDAQ"),
    ("SPY", AssetClass.EQUITY, "ARCA/NYSE"),
    ("EUR.USD", AssetClass.FX, "IDEALPRO"),
]
# Specialists that can analyze from price features alone (read-only, no extra feeds).
PRICE_AGENTS = [MomentumStrategy(), MeanReversionStrategy(), BreakoutStrategy()]
# Specialists that need data/engines we do NOT source in read-only Phase 2A → reported as NO DATA.
DATA_DEPENDENT_AGENTS = ["volatility", "cross_asset", "stat_arb", "macro", "fx_carry", "event"]


def _preflight() -> bool:
    try:
        import ib_insync  # noqa: F401
        return True
    except ImportError:
        print("✗ ib_insync not installed. Run:  pip install -e \".[live]\"")
        return False


def _instrument(symbol: str, asset_class: AssetClass) -> Instrument:
    if asset_class is AssetClass.FX and "." in symbol:
        base, quote = symbol.split(".", 1)
        return Instrument(base, AssetClass.FX, currency=quote)
    return Instrument(symbol, asset_class)


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f == f else None   # drop NaN
    except (TypeError, ValueError):
        return None


async def _buying_power(ib) -> float | None:
    for row in ib.accountValues():
        if row.tag == "BuyingPower":
            return _num(row.value)
    return None


async def _probe_market_data(broker: IBKRBroker) -> list[dict]:
    """Read-only per-instrument quote availability. Captures IBKR errors by contract and never
    fabricates a price — NaN/absent → DATA_NOT_AVAILABLE."""
    ib = broker._require()  # noqa: SLF001 — read-only raw client access, like the smoke test
    errors: dict[str, tuple[int, str]] = {}

    def _on_error(reqId, code, msg, contract=None):  # ib_insync errorEvent signature
        sym = getattr(contract, "symbol", None)
        if sym is not None:
            errors[sym] = (int(code), str(msg))

    evt = getattr(ib, "errorEvent", None)
    if evt is not None:
        evt += _on_error

    now = datetime.now(timezone.utc)
    try:
        ib.reqMarketDataType(1)  # request REAL-TIME (read-only; not a permission change)
    except Exception:  # noqa: BLE001
        pass
    # Phase 1: request every snapshot up front so each gets the full settle window.
    tickers: dict[str, object] = {}
    for symbol, asset_class, _exchange in UNIVERSE:
        inst = _instrument(symbol, asset_class)
        try:
            contract = broker._factory.contract(inst)  # noqa: SLF001
            q = getattr(ib, "qualifyContractsAsync", None)
            if q is not None:
                await q(contract)
            tickers[symbol] = ib.reqMktData(contract, "", True, False)  # snapshot=True (read-only)
        except Exception as exc:  # noqa: BLE001 — surface, never fake
            errors.setdefault(symbol, (-1, repr(exc)))

    # Phase 2: one shared settle window (poll up to ~6s), then read + classify all together.
    for _ in range(12):
        await asyncio.sleep(0.5)

    mdt_names = {1: "REALTIME", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}
    out: list[dict] = []
    for symbol, asset_class, exchange in UNIVERSE:
        t = tickers.get(symbol)
        bid, ask, last = (_num(getattr(t, "bid", None)), _num(getattr(t, "ask", None)),
                          _num(getattr(t, "last", None))) if t is not None else (None, None, None)
        err_code, err_msg = errors.get(symbol, (None, ""))
        mdt_raw = getattr(t, "marketDataType", None) if t is not None else None
        delayed = mdt_raw in (3, 4)          # never present delayed data as realtime
        status, reason = classify_quote(bid=bid, ask=ask, last=last,
                                        error_code=err_code, error_msg=err_msg, delayed=delayed)
        available = status in ("DATA_AVAILABLE", "DELAYED")
        out.append({
            "symbol": symbol, "asset_class": asset_class.value, "exchange": exchange,
            "status": status, "bid": bid, "ask": ask, "last": last,
            "timestamp": now.isoformat(), "reason": reason,
            "market_data_type": mdt_names.get(mdt_raw) if available else None,
            "error_code": err_code,
        })
    if evt is not None:
        evt -= _on_error
    return out


async def _historical_bars_by_key(broker: IBKRBroker, available: list[dict]) -> dict[str, list]:
    """Read-only historical bars for instruments whose live data is available, to warm the AI
    observation. FX uses MIDPOINT. Missing/failed → simply omitted (agents report NO DATA)."""
    bars_by_key: dict[str, list] = {}
    for row in available:
        # Only instruments with valid REALTIME data may be analyzed — never delayed/unavailable.
        if row["status"] != "DATA_AVAILABLE" or row.get("market_data_type") != "REALTIME":
            continue
        inst = _instrument(row["symbol"], AssetClass(row["asset_class"]))
        what = "MIDPOINT" if inst.asset_class is AssetClass.FX else "TRADES"
        try:
            bars = await broker.historical_bars(inst, duration="2 D", bar_size="5 mins",
                                                what=what, use_rth=False)
            if bars:
                bars_by_key[inst.key] = bars
        except Exception as exc:  # noqa: BLE001
            print(f"  historical {row['symbol']}: unavailable ({exc!r})")
    return bars_by_key


def _subscriptions(market_data: list[dict]) -> list[dict]:
    """Technical market-data subscription report — real state only, nothing purchased."""
    out: list[dict] = []
    for row in market_data:
        available = row["status"] == "DATA_AVAILABLE"
        if row["asset_class"] == "fx":
            required = "IDEALPRO FX (included with account)"
            sub_required = not available
        else:
            required = ("US Securities Snapshot and Futures Value Bundle "
                        "(or NASDAQ/NYSE network real-time)")
            sub_required = not available
        out.append({
            "instrument": row["symbol"], "asset_class": row["asset_class"],
            "exchange": row["exchange"], "required_market_data": required,
            "current_status": row["status"], "ibkr_error": row.get("error_code"),
            "subscription_required": bool(sub_required),
        })
    return out


async def main(args: argparse.Namespace) -> int:
    if not _preflight():
        return 2
    OUT.parent.mkdir(parents=True, exist_ok=True)

    broker = IBKRBroker(IBKRConfig(host=args.host, port=args.port, client_id=args.client_id,
                                   account=args.account, readonly=True))
    assert broker._cfg.readonly is True, "SAFETY: session must be read-only"  # noqa: SLF001

    print(f"→ connecting to IB {args.host}:{args.port} [READ-ONLY] ...")
    try:
        await broker.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ connect failed: {exc!r}")
        return 1
    connected = broker.is_connected()
    print(f"✓ connected — is_connected() = {connected}")

    try:
        ib = broker._require()  # noqa: SLF001
        account = await broker.get_account()
        buying_power = await _buying_power(ib)
        positions = await broker.get_positions()
        recon = diff_positions({}, positions)

        market_data = await _probe_market_data(broker)
        bars_by_key = await _historical_bars_by_key(broker, market_data)
        ai_analysis = observe_readonly(bars_by_key, PRICE_AGENTS)
        for agent in DATA_DEPENDENT_AGENTS:      # complete the 9-agent picture honestly
            ai_analysis.append({"agent": agent, "instrument": "*", "status": "NO DATA",
                                "action": None, "confidence": None, "expected_return": None,
                                "reason": "requires data not sourced in read-only Phase 2A"})

        # Risk engine ACTIVE (authoritative), but execution is NOT wired in this phase.
        risk = RiskEngine(limits=RiskLimits(),
                          state=RiskState(day_start_equity=account.equity or 0.0,
                                          peak_equity=account.equity or 0.0))
        risk.set_broker_connected(connected)
        risk.mark_equity(account.equity or 0.0)

        historical_ok = bool(bars_by_key) or None
        snap = build_snapshot(
            account=account, risk=risk, mode="paper", connected=connected,
            execution_enabled=False, orders=0, buying_power=buying_power,
            market_data=market_data, subscriptions=_subscriptions(market_data),
            ai_analysis=ai_analysis, data_ok=any(r["status"] in ("DATA_AVAILABLE", "DELAYED") for r in market_data),
            historical_ok=historical_ok,
            risk_capital=account.equity,   # capital mandate reference for the TRADING RISK panel
        ).as_dict()
        OUT.write_text(json.dumps(snap, indent=2))
    finally:
        await broker.disconnect()

    # ---- console report ----
    print("\n════════ COMMAND CENTER — REAL IBKR READ-ONLY SNAPSHOT ════════")
    print(f"  mode              : PAPER   (execution: DISABLED)")
    print(f"  IBKR connection   : {'CONNECTED' if connected else 'DISCONNECTED'}")
    print(f"  equity            : {account.equity:,.2f}")
    print(f"  cash              : {account.cash:,.2f}")
    print(f"  buying power      : {buying_power:,.2f}" if buying_power is not None else "  buying power      : NO DATA")
    print(f"  realized  P&L     : {account.realized_pnl:,.2f}")
    print(f"  unrealized P&L    : {account.unrealized_pnl:,.2f}")
    print(f"  positions         : {len(positions)}")
    print(f"  reconciliation    : {recon.summary()}")
    print("  market data:")
    for r in market_data:
        detail = f"bid={r['bid']} ask={r['ask']} last={r['last']}" if r["status"] in ("DATA_AVAILABLE", "DELAYED") else r["reason"]
        print(f"    {r['symbol']:<9} {r['status']:<18} {detail}")
    sig = sum(1 for a in ai_analysis if a["status"] == "SIGNAL")
    obs = sum(1 for a in ai_analysis if a["status"] == "OBSERVATION")
    nod = sum(1 for a in ai_analysis if a["status"] == "NO DATA")
    print(f"  AI (read-only)    : {sig} SIGNAL · {obs} OBSERVATION · {nod} NO DATA")
    print(f"  orders sent       : 0")
    print(f"  cancels           : 0")
    print(f"\n  wrote {OUT}")
    return 0


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 2A: real IBKR read-only → dashboard snapshot")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002, help="4002 IB Gateway paper")
    p.add_argument("--client-id", type=int, default=8)
    p.add_argument("--account", default=None)
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_args())))
