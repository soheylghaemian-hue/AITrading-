"""Broker abstraction (§3 modularity, §17).

The strategy layer must never depend on a single broker (§3). Everything above this module
speaks only to the `Broker` interface; the first concrete adapter is IBKR/IB Gateway (§17),
and `PaperBroker` is the deterministic in-memory adapter used for backtests and paper
trading (§14/§24). Swapping brokers is an adapter change, not a strategy change.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime

from ..core.enums import OrderStatus, OrderType, Side
from ..core.events import Instrument


@dataclass(slots=True)
class Order:
    """An order request (§16). Quantity is always positive; direction is `side`."""

    instrument: Instrument
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    client_id: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError(f"{self.order_type} order requires a limit_price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError(f"{self.order_type} order requires a stop_price")

    @property
    def signed_quantity(self) -> float:
        return self.side.sign * self.quantity


@dataclass(slots=True)
class Fill:
    """The realized result of an order (§20 cost accounting)."""

    instrument: Instrument
    side: Side
    quantity: float
    price: float
    commission: float
    ts: datetime


@dataclass(slots=True)
class OrderResult:
    """Outcome of submitting an order to the broker."""

    order: Order
    status: OrderStatus
    fill: Fill | None = None
    reason: str = ""

    @property
    def filled(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)


@dataclass(slots=True)
class Position:
    """A held position. `quantity` is signed (long > 0, short < 0)."""

    instrument: Instrument
    quantity: float
    avg_price: float
    market_price: float = 0.0

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.market_price * self.instrument.multiplier

    @property
    def unrealized_pnl(self) -> float:
        return (self.market_price - self.avg_price) * self.quantity * self.instrument.multiplier


@dataclass(slots=True)
class Account:
    """Point-in-time account snapshot (§20 accounting)."""

    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    gross_exposure: float
    net_exposure: float
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def gross_leverage(self) -> float:
        return self.gross_exposure / self.equity if self.equity > 0 else 0.0


class Broker(abc.ABC):
    """Minimal async broker interface the whole platform depends on (§3)."""

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def get_account(self) -> Account: ...

    @abc.abstractmethod
    async def get_positions(self) -> dict[str, Position]: ...

    @abc.abstractmethod
    async def place_order(self, order: Order) -> OrderResult: ...
