"""Broker adapters (§3/§17). Strategy code depends only on the `Broker` interface.

`ibkr` is safe to import without `ib_insync` installed — the dependency is lazy-loaded only
when a real connection is opened.
"""

from .base import Account, Broker, Fill, Order, OrderResult, Position
from .ibkr import IBFactory, IBKRBroker, IBKRConfig
from .paper import PaperBroker
from .reconcile import (
    FullReconciliationReport,
    GenericBreak,
    InternalState,
    ReconciliationBreak,
    ReconciliationReport,
    Reconciler,
    diff_positions,
    reconcile_full,
)

__all__ = [
    "Account",
    "Broker",
    "Fill",
    "Order",
    "OrderResult",
    "Position",
    "PaperBroker",
    "IBKRBroker",
    "IBKRConfig",
    "IBFactory",
    "Reconciler",
    "ReconciliationReport",
    "ReconciliationBreak",
    "diff_positions",
    "InternalState",
    "FullReconciliationReport",
    "GenericBreak",
    "reconcile_full",
]
