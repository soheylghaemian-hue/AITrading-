import math

from atp.backtest.metrics import (
    compute_metrics,
    max_drawdown,
    returns_from_equity,
    sharpe,
    sortino,
)


def test_returns_from_equity():
    assert returns_from_equity([100, 110, 121]) == [0.1, 0.1]


def test_returns_empty_and_single():
    assert returns_from_equity([]) == []
    assert returns_from_equity([100]) == []


def test_max_drawdown_basic():
    assert math.isclose(max_drawdown([100, 120, 90, 150]), 0.25)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100, 101, 102, 103]) == 0.0


def test_sharpe_zero_variance_is_zero():
    assert sharpe([0.01, 0.01, 0.01], periods_per_year=252) == 0.0


def test_sharpe_positive_for_positive_drift():
    rets = [0.01, 0.02, -0.005, 0.015, 0.005]
    assert sharpe(rets, periods_per_year=252) > 0


def test_sortino_ignores_upside_volatility():
    # All-positive returns => no downside => sortino defined as 0 (no downside dev).
    assert sortino([0.01, 0.02, 0.03], periods_per_year=252) == 0.0


def test_compute_metrics_profit_factor_and_winrate():
    equity = [100_000, 100_100, 100_050, 100_300]
    trades = [10.0, 20.0, -10.0]  # 2 wins, 1 loss
    m = compute_metrics(equity, trades, periods_per_year=252)
    assert m.n_trades == 3
    assert math.isclose(m.win_rate, 2 / 3)
    assert math.isclose(m.profit_factor, 30.0 / 10.0)
    assert math.isclose(m.expectancy, (10 + 20 - 10) / 3)


def test_compute_metrics_flat_curve_no_crash():
    m = compute_metrics([100_000, 100_000, 100_000], [], periods_per_year=252)
    assert m.total_return == 0.0
    assert m.sharpe == 0.0
    assert m.max_drawdown == 0.0
    assert m.profit_factor == 0.0


def test_compute_metrics_total_return():
    m = compute_metrics([100_000, 110_000], [], periods_per_year=252)
    assert math.isclose(m.total_return, 0.1)
