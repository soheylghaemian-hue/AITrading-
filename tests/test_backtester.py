"""Backtester runs the real desk pipeline over synthetic history — no lookahead,
deterministic, and no fabricated P&L (equity is a pure function of the fed bars)."""

import math
from datetime import datetime, timedelta, timezone

from atp.backtest.engine import Backtester
from atp.backtest.validation import (
    monte_carlo_trade_order,
    train_test_split,
    walk_forward,
    walk_forward_windows,
)
from atp.core.enums import AssetClass
from atp.core.events import Bar, Instrument
from atp.policy import TradingPolicy
from atp.regime.classifier import RegimeClassifier
from atp.strategy.momentum import MomentumStrategy

INST = Instrument(symbol="X", asset_class=AssetClass.EQUITY)
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _oscillating_bars(n: int = 200, base: float = 100.0, amp: float = 4.0) -> list[Bar]:
    """A mean-reverting, oscillating series so the regime-aware strategy round-trips."""
    bars = []
    for i in range(n):
        price = base + amp * math.sin(i / 6.0) + 0.05 * i  # gentle drift + cycles
        bars.append(
            Bar(
                instrument=INST,
                open=price,
                high=price * 1.002,
                low=price * 0.998,
                close=price,
                volume=1000 + i,
                ts=START + timedelta(minutes=i),
            )
        )
    return bars


def _backtester() -> Backtester:
    policy = TradingPolicy(capital=100_000.0)
    return Backtester(
        policy=policy,
        strategies=[MomentumStrategy()],
        regime=RegimeClassifier(trend_threshold=0.25, low_vol_percentile=0.4),
    )


async def test_backtest_produces_equity_curve_of_right_length():
    bars = _oscillating_bars(150)
    res = await _backtester().run(bars, periods_per_year=252 * 390)
    assert len(res.equity_curve) == len(bars)
    assert len(res.timestamps) == len(bars)
    assert res.starting_equity == 100_000.0
    assert math.isfinite(res.ending_equity)


async def test_backtest_is_deterministic():
    bars = _oscillating_bars(120)
    r1 = await _backtester().run(bars)
    r2 = await _backtester().run(bars)
    assert r1.equity_curve == r2.equity_curve
    assert r1.trade_pnls == r2.trade_pnls


async def test_backtest_metrics_computable():
    bars = _oscillating_bars(200)
    res = await _backtester().run(bars, periods_per_year=252 * 390)
    m = res.metrics(periods_per_year=252 * 390)
    assert m.n_periods == len(bars) - 1
    # Whatever the sign of returns, drawdown is a valid fraction in [0, 1].
    assert 0.0 <= m.max_drawdown <= 1.0


async def test_backtest_executes_some_trades():
    bars = _oscillating_bars(200)
    res = await _backtester().run(bars)
    # The oscillating series should trigger at least one round trip through the desk.
    assert res.n_executed > 0


def test_train_test_split_is_chronological_no_leakage():
    bars = _oscillating_bars(100)
    train, test = train_test_split(bars, 0.7)
    assert len(train) == 70
    assert len(test) == 30
    assert train[-1].ts < test[0].ts  # test strictly after train


def test_walk_forward_windows_non_overlapping():
    bars = _oscillating_bars(100)
    windows = walk_forward_windows(bars, train_size=40, test_size=20)
    assert len(windows) == 3  # starts at 0, 20, 40
    for train, test in windows:
        assert len(train) == 40
        assert len(test) == 20
        assert train[-1].ts < test[0].ts


async def test_walk_forward_runs_out_of_sample():
    bars = _oscillating_bars(160)
    windows = walk_forward_windows(bars, train_size=60, test_size=30)
    wf = await walk_forward(_backtester(), windows)
    assert wf.n_windows == len(windows)
    assert wf.combined_metrics is not None


def test_monte_carlo_percentiles_ordered():
    trades = [50.0, -30.0, 80.0, -60.0, 20.0, -10.0, 40.0]
    mc = monte_carlo_trade_order(trades, starting_equity=100_000, n_runs=500, seed=1)
    assert mc.final_equity_p05 <= mc.final_equity_p50 <= mc.final_equity_p95
    assert 0.0 <= mc.prob_loss <= 1.0
    assert mc.max_drawdown_p50 <= mc.max_drawdown_p95


def test_monte_carlo_empty_trades_is_safe():
    mc = monte_carlo_trade_order([], starting_equity=100_000, n_runs=100)
    assert mc.final_equity_p50 == 100_000
    assert mc.max_drawdown_p95 == 0.0
