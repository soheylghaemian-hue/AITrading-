"""Interactive Brokers / IB Gateway adapter (§17, §24 Phase 2).

The first *real* broker behind the `Broker` interface. Everything above this file already
speaks only that interface, so wiring the live desk to IBKR is exactly this adapter — no
strategy, risk or execution code changes (§3).

Design for testability
-----------------------
This adapter is thin on purpose. The two things that actually carry risk of being wrong are
(a) mapping an `atp` `Instrument`/`Order` onto an IB contract/order, and (b) parsing IB's
account/position/fill objects back into `atp` types. Both are isolated behind an injected
seam:

* `ib` — the client. In production it's an `ib_async.IB()`; `connect()` lazy-imports it so
  importing this module never requires `ib_async`. In tests a `FakeIB` mirrors the small
  surface used here.
* `factory` — builds IB contract/order objects. Defaults to an `ib_async`-backed factory;
  tests inject a fake that returns plain namespaces.

The mapping/parsing logic is therefore fully unit-testable without a live gateway or the
`ib_async` dependency. What is *not* simulated (a real connection, market data, fills) is
honestly out of scope for the offline suite — see docs/DECISIONS.md ADR-6.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..core.enums import AssetClass, OrderStatus, OrderType, Side
from ..core.events import Bar, Instrument
from ..logging_config import get_logger
from .base import Account, Broker, Fill, Order, OrderResult, Position

log = get_logger("broker.ibkr")


@dataclass(slots=True)
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 4002            # IB Gateway paper default (4001 live, 7497/7496 TWS)
    client_id: int = 1
    account: str | None = None  # sub-account; None => the login's default
    readonly: bool = False
    order_timeout: float = 30.0
    poll_interval: float = 0.05


# --- IB status string -> atp OrderStatus -------------------------------------
_STATUS_MAP = {
    "Filled": OrderStatus.FILLED,
    "Submitted": OrderStatus.NEW,
    "PreSubmitted": OrderStatus.NEW,
    "PendingSubmit": OrderStatus.NEW,
    "PendingCancel": OrderStatus.NEW,
    "ApiPending": OrderStatus.NEW,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
}

# --- IB secType -> atp AssetClass --------------------------------------------
_SECTYPE_MAP = {
    "STK": AssetClass.EQUITY,
    "ETF": AssetClass.ETF,
    "IND": AssetClass.INDEX,
    "CASH": AssetClass.FX,
    "FUT": AssetClass.FUTURE,
    "OPT": AssetClass.OPTION,
    "CRYPTO": AssetClass.CRYPTO,
}


@dataclass(slots=True)
class OrderSpec:
    """Broker-neutral description of an order — the mapping decision, ib_async-free.

    Isolated here so the atp-Order -> IB translation (action, kind, prices) is unit-testable
    without the `ib_async` dependency. `IBFactory` just materializes this into IB objects.
    """

    action: str            # "BUY" | "SELL"
    kind: OrderType
    quantity: float
    limit_price: float | None
    stop_price: float | None
    order_ref: str


def describe_order(o: Order) -> OrderSpec:
    """Pure atp-Order -> OrderSpec mapping (single source of truth for IB translation)."""
    return OrderSpec(
        action="BUY" if o.side is Side.BUY else "SELL",
        kind=o.order_type,
        quantity=o.quantity,
        limit_price=o.limit_price,
        stop_price=o.stop_price,
        order_ref=o.client_id or "",
    )


def bar_from_ib_historical(ib_bar: Any, instrument: Instrument) -> Bar:
    """Pure IB historical BarData -> atp Bar (ib_async-free, unit-tested)."""
    ts = getattr(ib_bar, "date", None)
    if not isinstance(ts, datetime):
        # IB may return a date/str; normalize what we can, else stamp now (UTC).
        try:
            ts = datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Bar(
        instrument=instrument,
        open=float(ib_bar.open), high=float(ib_bar.high), low=float(ib_bar.low),
        close=float(ib_bar.close), volume=float(getattr(ib_bar, "volume", 0.0) or 0.0), ts=ts,
    )


def contract_spec(instrument: Instrument) -> dict:
    """Pure atp-Instrument -> IB contract fields (ib_async-free, so it's unit-testable).

    Futures need `expiry` (+ exchange); options need `expiry`/`strike`/`right`. Missing terms
    raise, rather than building a silently-wrong contract (ADR-6/ADR-15)."""
    ac = instrument.asset_class
    ccy = instrument.currency
    if ac in (AssetClass.EQUITY, AssetClass.ETF):
        return {"secType": "STK", "symbol": instrument.symbol, "exchange": "SMART", "currency": ccy}
    if ac is AssetClass.INDEX:
        return {"secType": "IND", "symbol": instrument.symbol, "exchange": "CBOE", "currency": ccy}
    if ac is AssetClass.FX:
        return {"secType": "CASH", "pair": f"{instrument.symbol}{ccy}", "currency": ccy,
                "exchange": "IDEALPRO"}
    if ac is AssetClass.CRYPTO:
        return {"secType": "CRYPTO", "symbol": instrument.symbol, "exchange": "PAXOS", "currency": ccy}
    if ac is AssetClass.FUTURE:
        if not instrument.expiry:
            raise ValueError("future contract requires `expiry` (YYYYMMDD or YYYYMM)")
        # Exchange left blank => IB qualifies by symbol+expiry; a fuller impl carries the
        # venue (CME/NYMEX/…) explicitly. Documented simplification (ADR-15).
        return {"secType": "FUT", "symbol": instrument.symbol, "exchange": "",
                "lastTradeDateOrContractMonth": instrument.expiry, "currency": ccy,
                "multiplier": str(int(instrument.multiplier)) if instrument.multiplier else ""}
    if ac is AssetClass.OPTION:
        if not (instrument.expiry and instrument.strike is not None and instrument.right):
            raise ValueError("option contract requires `expiry`, `strike` and `right`")
        if instrument.right not in ("C", "P"):
            raise ValueError("option `right` must be 'C' or 'P'")
        return {"secType": "OPT", "symbol": instrument.underlying or instrument.symbol,
                "lastTradeDateOrContractMonth": instrument.expiry, "strike": float(instrument.strike),
                "right": instrument.right, "exchange": "SMART", "currency": ccy,
                "multiplier": str(int(instrument.multiplier)) if instrument.multiplier else "100"}
    raise NotImplementedError(f"no IB contract mapping for {ac.value}")  # pragma: no cover


class IBFactory:
    """Default contract/order factory backed by ``ib_async`` (lazy-imported)."""

    def __init__(self) -> None:
        self._ibi: Any = None

    def _mod(self) -> Any:
        if self._ibi is None:
            import ib_async  # noqa: PLC0415 — lazy so the module imports without the dep

            self._ibi = ib_async
        return self._ibi

    def contract(self, instrument: Instrument) -> Any:
        ibi = self._mod()
        spec = contract_spec(instrument)   # pure mapping (validated, ib_async-free)
        sec = spec["secType"]
        if sec == "STK":
            return ibi.Stock(spec["symbol"], spec["exchange"], spec["currency"])
        if sec == "IND":
            return ibi.Index(spec["symbol"], spec["exchange"], spec["currency"])
        if sec == "CASH":
            return ibi.Forex(spec["pair"])
        if sec == "CRYPTO":
            return ibi.Crypto(spec["symbol"], spec["exchange"], spec["currency"])
        if sec == "FUT":
            return ibi.Future(spec["symbol"], spec["lastTradeDateOrContractMonth"],
                              spec["exchange"], currency=spec["currency"], multiplier=spec["multiplier"])
        if sec == "OPT":
            return ibi.Option(spec["symbol"], spec["lastTradeDateOrContractMonth"], spec["strike"],
                              spec["right"], spec["exchange"], currency=spec["currency"],
                              multiplier=spec["multiplier"])
        raise NotImplementedError(f"IBKR contract mapping for {sec} not implemented")  # pragma: no cover

    def order(self, o: Order) -> Any:
        ibi = self._mod()
        spec = describe_order(o)
        if spec.kind is OrderType.MARKET:
            order = ibi.MarketOrder(spec.action, spec.quantity)
        elif spec.kind is OrderType.LIMIT:
            order = ibi.LimitOrder(spec.action, spec.quantity, spec.limit_price)
        elif spec.kind is OrderType.STOP:
            order = ibi.StopOrder(spec.action, spec.quantity, spec.stop_price)
        elif spec.kind is OrderType.STOP_LIMIT:
            order = ibi.StopLimitOrder(spec.action, spec.quantity, spec.limit_price, spec.stop_price)
        else:  # pragma: no cover - exhaustive
            raise ValueError(f"unsupported order type {spec.kind}")
        if spec.order_ref:
            order.orderRef = spec.order_ref
        return order


class IBKRBroker(Broker):
    def __init__(
        self,
        config: IBKRConfig | None = None,
        *,
        ib: Any = None,
        factory: Any = None,
    ) -> None:
        self._cfg = config or IBKRConfig()
        self._ib = ib
        self._factory = factory or IBFactory()
        self._on_disconnect_cb: Any = None      # called with () when the socket drops
        self._on_error_cb: Any = None           # called with (reqId, code, msg, contract)
        self._last_error: tuple | None = None

    # ------------------------------------------------------------- lifecycle
    async def connect(self) -> None:
        if self._ib is None:
            import ib_async  # noqa: PLC0415 — lazy import; only needed for a real connection

            self._ib = ib_async.IB()
        await self._ib.connectAsync(
            self._cfg.host,
            self._cfg.port,
            clientId=self._cfg.client_id,
            readonly=self._cfg.readonly,
        )
        # Wire IB event hooks for health/error handling (§17): the desk/risk react to these.
        for event_name, handler in (("errorEvent", self._handle_error),
                                    ("disconnectedEvent", self._handle_disconnected)):
            evt = getattr(self._ib, event_name, None)
            if evt is not None:
                evt += handler
        log.info("connected to IBKR %s:%d clientId=%d", self._cfg.host, self._cfg.port, self._cfg.client_id)

    async def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    async def ensure_connected(self) -> bool:
        """Reconnect if the socket has dropped (§17). Returns the resulting connection state."""
        if self.is_connected():
            return True
        log.warning("IBKR reconnecting …")
        try:
            await self.connect()
        except Exception as exc:  # noqa: BLE001 — surface, let the caller decide
            log.error("IBKR reconnect failed: %r", exc)
            return False
        return self.is_connected()

    # --- health / error hooks (wire to the Risk Engine's broker-health signal, §17) ---------
    def on_disconnect(self, callback: Any) -> None:
        self._on_disconnect_cb = callback

    def on_error(self, callback: Any) -> None:
        self._on_error_cb = callback

    @property
    def last_error(self) -> tuple | None:
        return self._last_error

    def _handle_disconnected(self) -> None:  # pragma: no cover - live event
        log.warning("IBKR socket disconnected")
        if self._on_disconnect_cb is not None:
            self._on_disconnect_cb()

    def _handle_error(self, reqId, errorCode, errorString, contract=None) -> None:  # pragma: no cover
        self._last_error = (reqId, errorCode, errorString)
        # 1100/1300/2110 = connectivity lost; 502 = couldn't connect; 504 = not connected.
        if errorCode in (1100, 1300, 2110, 502, 504):
            log.error("IBKR connectivity error %s: %s", errorCode, errorString)
        if self._on_error_cb is not None:
            self._on_error_cb(reqId, errorCode, errorString, contract)

    def _require(self) -> Any:
        if self._ib is None:
            raise RuntimeError("IBKRBroker.connect() must be called before use")
        return self._ib

    # ------------------------------------------------------------- parsing
    @staticmethod
    def instrument_from_contract(contract: Any) -> Instrument:
        asset_class = _SECTYPE_MAP.get(getattr(contract, "secType", "STK"), AssetClass.EQUITY)
        symbol = getattr(contract, "localSymbol", "") or contract.symbol
        currency = getattr(contract, "currency", "USD") or "USD"
        mult_raw = getattr(contract, "multiplier", "") or "1"
        try:
            multiplier = float(mult_raw)
        except (TypeError, ValueError):
            multiplier = 1.0
        return Instrument(symbol=symbol, asset_class=asset_class, currency=currency, multiplier=multiplier)

    async def get_positions(self) -> dict[str, Position]:
        ib = self._require()
        out: dict[str, Position] = {}
        # portfolio() carries marketPrice/avgCost; positions() is the fallback source of truth.
        for item in ib.portfolio():
            inst = self.instrument_from_contract(item.contract)
            qty = float(item.position)
            if qty == 0:
                continue
            # IB avgCost is per-contract (incl. multiplier); normalize to a per-unit price.
            avg_price = float(item.averageCost) / inst.multiplier if inst.multiplier else float(item.averageCost)
            out[inst.key] = Position(
                instrument=inst,
                quantity=qty,
                avg_price=avg_price,
                market_price=float(getattr(item, "marketPrice", 0.0) or 0.0),
            )
        return out

    async def get_account(self) -> Account:
        ib = self._require()
        tags: dict[str, float] = {}
        for row in ib.accountValues(self._cfg.account) if self._cfg.account else ib.accountValues():
            if row.currency in ("", "BASE", None) or row.tag in _WANTED_TAGS:
                try:
                    tags[row.tag] = float(row.value)
                except (TypeError, ValueError):
                    continue
        positions = await self.get_positions()
        gross = sum(p.notional for p in positions.values())
        net = sum(p.quantity * p.market_price * p.instrument.multiplier for p in positions.values())
        return Account(
            cash=tags.get("TotalCashValue", 0.0),
            equity=tags.get("NetLiquidation", 0.0),
            realized_pnl=tags.get("RealizedPnL", 0.0),
            unrealized_pnl=tags.get("UnrealizedPnL", 0.0),
            gross_exposure=tags.get("GrossPositionValue", gross),
            net_exposure=net,
            positions=positions,
        )

    # ------------------------------------------------------------- market / historical data
    async def historical_bars(self, instrument: Instrument, *, duration: str = "1 D",
                              bar_size: str = "1 min", what: str = "TRADES",
                              use_rth: bool = True) -> list[Bar]:
        """Fetch historical bars for backtesting/warm-up (§3). Live-only (needs the gateway).

        `duration`/`bar_size` use IB's vocabulary ("1 D", "1 min"). Returns atp `Bar`s via the
        pure `bar_from_ib_historical` mapper (which is unit-tested)."""
        ib = self._require()
        contract = self._factory.contract(instrument)
        qualify = getattr(ib, "qualifyContractsAsync", None)
        if qualify is not None:
            await qualify(contract)
        raw = await ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
            whatToShow=what, useRTH=use_rth, formatDate=2,
        )
        return [bar_from_ib_historical(b, instrument) for b in raw]

    async def list_executions(self) -> list[Fill]:
        """Recent executions/fills for reconciliation (§17). Live-only."""
        ib = self._require()
        reqfn = getattr(ib, "reqExecutionsAsync", None)
        raw = await reqfn() if reqfn is not None else ib.fills()
        out: list[Fill] = []
        for f in raw:
            ex = getattr(f, "execution", f)
            contract = getattr(f, "contract", None)
            inst = self.instrument_from_contract(contract) if contract is not None else None
            if inst is None:
                continue
            side = Side.BUY if str(getattr(ex, "side", "BOT")).upper().startswith("B") else Side.SELL
            comm = 0.0
            report = getattr(f, "commissionReport", None)
            if report is not None and getattr(report, "commission", None) is not None:
                comm = float(report.commission)
            ts = getattr(ex, "time", None)
            out.append(Fill(inst, side, float(ex.shares), float(ex.price), comm,
                            ts if isinstance(ts, datetime) else datetime.now(timezone.utc)))
        return out

    async def open_orders(self) -> list[dict]:
        """Currently working orders at the broker, for reconciliation (§17). Live-only."""
        ib = self._require()
        reqfn = getattr(ib, "reqOpenOrdersAsync", None)
        trades = await reqfn() if reqfn is not None else ib.openTrades()
        out: list[dict] = []
        for t in trades:
            inst = self.instrument_from_contract(t.contract)
            out.append({
                "instrument_key": inst.key,
                "action": t.order.action,
                "quantity": float(t.order.totalQuantity),
                "order_type": t.order.orderType,
                "status": t.orderStatus.status,
                "filled": float(getattr(t.orderStatus, "filled", 0.0) or 0.0),
            })
        return out

    # ------------------------------------------------------------- execution
    async def place_order(self, order: Order) -> OrderResult:
        ib = self._require()
        if self._cfg.readonly:
            return OrderResult(order, OrderStatus.REJECTED, reason="readonly session")

        contract = self._factory.contract(order.instrument)
        qualify = getattr(ib, "qualifyContractsAsync", None)
        if qualify is not None:
            await qualify(contract)

        ib_order = self._factory.order(order)
        trade = ib.placeOrder(contract, ib_order)
        trade = await self._await_trade(trade)

        status = _STATUS_MAP.get(trade.orderStatus.status, OrderStatus.NEW)
        filled_qty = float(getattr(trade.orderStatus, "filled", 0.0) or 0.0)
        if status is OrderStatus.NEW and 0 < filled_qty < order.quantity:
            status = OrderStatus.PARTIALLY_FILLED

        fill = self._build_fill(order, trade) if filled_qty > 0 else None
        reason = "" if fill else trade.orderStatus.status
        return OrderResult(order, status, fill=fill, reason=reason)

    async def _await_trade(self, trade: Any) -> Any:
        """Wait until the trade is done or the timeout elapses (poll `isDone`)."""
        elapsed = 0.0
        while not trade.isDone():
            await asyncio.sleep(self._cfg.poll_interval)
            elapsed += self._cfg.poll_interval
            if elapsed >= self._cfg.order_timeout:
                log.warning("order timeout after %.1fs (status=%s)", elapsed, trade.orderStatus.status)
                break
        return trade

    def _build_fill(self, order: Order, trade: Any) -> Fill:
        filled_qty = float(trade.orderStatus.filled)
        avg_price = float(trade.orderStatus.avgFillPrice)
        commission = 0.0
        last_ts: datetime | None = None
        for f in getattr(trade, "fills", []):
            report = getattr(f, "commissionReport", None)
            if report is not None and getattr(report, "commission", None) is not None:
                commission += float(report.commission)
            t = getattr(getattr(f, "time", None), "time", None) or getattr(f, "time", None)
            if isinstance(t, datetime):
                last_ts = t
        return Fill(
            instrument=order.instrument,
            side=order.side,
            quantity=filled_qty,
            price=avg_price,
            commission=commission,
            ts=last_ts or datetime.now(timezone.utc),
        )


_WANTED_TAGS = frozenset(
    {
        "NetLiquidation",
        "TotalCashValue",
        "GrossPositionValue",
        "RealizedPnL",
        "UnrealizedPnL",
    }
)
