"""IBKR gateway READ-ONLY smoke test (§12/§17) — RUN THIS AGAINST YOUR OWN IB GATEWAY.

Strictly read-only. It **cannot** place, modify or cancel orders — that capability is not in
this script. The default path is a *provably read-only* connectivity + market-data test and
issues **no order-management request at all**. It only:

  1. checks `ib_async` is installed,
  2. connects to a running IB Gateway / TWS,
  3. confirms the account and reads the account summary + cash,
  4. reads current positions,
  5. checks the broker/connection status,
  6. requests a small, fixed set of market-data snapshots (never fabricated — real or none).

Reading open orders is an order-management-category request (`reqOpenOrders`) that IB Gateway's
Read-Only API may flag; it is therefore **not** in the default path. It runs only when you pass
`--check-open-orders` explicitly.

Prerequisites (see docs/IBKR_SETUP.md):
  * IB Gateway or TWS running and logged in to a **paper** account.
  * API enabled (Configure → Settings → API), 127.0.0.1 trusted, socket port noted.
  * `pip install -e ".[live]"`.
  * Default paper ports: IB Gateway 4002, TWS 7497.  (Live: 4001 / 7496.)

Usage:
    PYTHONPATH=src python3 examples/ibkr_smoke.py --port 4002
    PYTHONPATH=src python3 examples/ibkr_smoke.py --port 4002 --symbols AAPL,EUR.USD

SAFETY: read-only. No orders. Use a PAPER account. CI never runs this; Claude will not run it
for you — it touches a real broker connection. Credentials live only in the Gateway.
"""

from __future__ import annotations

import argparse
import asyncio

from atp.brokers.ibkr import IBKRBroker, IBKRConfig
from atp.brokers.reconcile import diff_positions
from atp.core.enums import AssetClass
from atp.core.events import Instrument


def _preflight() -> bool:
    try:
        import ib_async  # noqa: F401
        return True
    except ImportError:
        print("✗ ib_async not installed. Run:  pip install -e \".[live]\"")
        return False


def _parse_symbol(token: str) -> Instrument:
    """AAPL -> equity; EUR.USD -> FX pair (base.quote)."""
    token = token.strip().upper()
    if "." in token:
        base, quote = token.split(".", 1)
        return Instrument(base, AssetClass.FX, currency=quote)
    return Instrument(token, AssetClass.EQUITY)


async def _market_data_snapshots(broker: IBKRBroker, symbols: list[str]) -> None:
    """Request a small, fixed set of read-only market-data snapshots. Errors are surfaced, not
    hidden, and never replaced with fabricated data."""
    print(f"\n── market data ({len(symbols)}) ─────────────")
    ib = broker._require()  # noqa: SLF001 — smoke test intentionally reaches the raw client
    for sym in symbols:
        inst = _parse_symbol(sym)
        try:
            contract = broker._factory.contract(inst)  # noqa: SLF001
            q = getattr(ib, "qualifyContractsAsync", None)
            if q is not None:
                await q(contract)
            ticker = ib.reqMktData(contract, "", True, False)   # snapshot=True (read-only)
            # Yield to the running loop so ib_async can process incoming ticks. Must be
            # asyncio.sleep, NOT ib.sleep() — the latter drives the loop itself and raises
            # "event loop is already running" when we're already inside asyncio.run(main()).
            await asyncio.sleep(2.0)
            # A value counts as present only if it's not None and not NaN. IBKR returns NaN
            # (not None) when there's no subscription — NaN is not a price and must never be
            # shown as a quote, only as DATA_NOT_AVAILABLE.
            def _present(v: object) -> bool:
                return v is not None and v == v  # NaN != NaN
            bid = getattr(ticker, "bid", None)
            ask = getattr(ticker, "ask", None)
            last = getattr(ticker, "last", None)
            if not (_present(bid) or _present(ask) or _present(last)):
                print(f"  {sym:<10} DATA_NOT_AVAILABLE (no subscription / market closed)")
            else:
                print(f"  {sym:<10} bid={bid} ask={ask} last={last}")
        except Exception as exc:  # noqa: BLE001 — market-data/subscription errors must be visible
            print(f"  {sym:<10} error: {exc!r}")


async def main(args: argparse.Namespace) -> int:
    if not _preflight():
        return 2

    broker = IBKRBroker(IBKRConfig(
        host=args.host, port=args.port, client_id=args.client_id,
        account=args.account, readonly=True,          # hard read-only session
    ))

    print(f"→ connecting to IB {args.host}:{args.port} (clientId={args.client_id}) [READ-ONLY] ...")
    try:
        await broker.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ connect failed: {exc!r}")
        print("  Is the gateway running, logged in, and is the API enabled on this port?")
        return 1
    print(f"✓ connected — broker.is_connected() = {broker.is_connected()}")

    try:
        account = await broker.get_account()
        print("\n── account summary ─────────────────────────")
        print(f"  equity        : {account.equity:,.2f}")
        print(f"  cash          : {account.cash:,.2f}")
        print(f"  realized  P&L : {account.realized_pnl:,.2f}")
        print(f"  unrealized P&L: {account.unrealized_pnl:,.2f}")
        print(f"  gross exposure: {account.gross_exposure:,.2f}  ({account.gross_leverage:.2f}x)")

        positions = await broker.get_positions()
        print(f"\n── positions ({len(positions)}) ─────────────────────")
        for key, p in positions.items():
            print(f"  {key:<18} qty={p.quantity:>10.2f}  avg={p.avg_price:>10.4f}  mark={p.market_price:>10.4f}")
        if not positions:
            print("  (flat)")

        # Reading open orders (reqOpenOrders) is order-management-category — off by default so
        # the standard smoke test stays provably read-only. Opt in only with --check-open-orders.
        if getattr(args, "check_open_orders", False):
            try:
                open_orders = await broker.open_orders()
                print(f"\n── open orders ({len(open_orders)}) ───────────────────")
                for o in open_orders:
                    print(f"  {o['instrument_key']:<18} {o['action']} {o['quantity']} {o['order_type']} "
                          f"status={o['status']} filled={o['filled']}")
                if not open_orders:
                    print("  (none)")
            except Exception as exc:  # noqa: BLE001
                print(f"  open orders unavailable: {exc!r}")
        else:
            print("\n── open orders ────────────────────────────")
            print("  skipped (order-management request; pass --check-open-orders to include)")

        report = diff_positions({}, positions)
        print(f"\n── reconciliation vs empty internal book ──")
        print(f"  {report.summary()}")

        await _market_data_snapshots(broker, [s for s in args.symbols.split(",") if s.strip()])
    finally:
        await broker.disconnect()
        print("\n✓ disconnected")
    return 0


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IBKR gateway READ-ONLY smoke test (no orders)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002, help="4002 IB Gateway paper, 7497 TWS paper")
    p.add_argument("--client-id", type=int, default=7)
    p.add_argument("--account", default=None)
    p.add_argument("--symbols", default="AAPL", help="comma-separated, e.g. AAPL,EUR.USD")
    p.add_argument("--check-open-orders", dest="check_open_orders", action="store_true",
                   help="opt-in: also read open orders (reqOpenOrders — order-management "
                        "category, off by default to keep the smoke test provably read-only)")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_args())))
