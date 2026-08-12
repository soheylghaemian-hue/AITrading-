"""Event-driven backtesting, metrics and validation (§10/§11)."""

from .engine import Backtester, BacktestResult
from .metrics import BacktestMetrics, compute_metrics
from .validation import (
    MonteCarloResult,
    WalkForwardResult,
    monte_carlo_trade_order,
    train_test_split,
    walk_forward,
    walk_forward_windows,
)

__all__ = [
    "Backtester",
    "BacktestResult",
    "BacktestMetrics",
    "compute_metrics",
    "MonteCarloResult",
    "WalkForwardResult",
    "monte_carlo_trade_order",
    "train_test_split",
    "walk_forward",
    "walk_forward_windows",
]
