"""Performance & risk metrics for backtests (§10/§11).

Pure functions over an equity curve and a list of closed-trade P&Ls. No numpy dependency
so these run in the offline suite and are trivially auditable. Every number the platform
reports about a strategy is computed here — nothing is fabricated (§25).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(slots=True)
class BacktestMetrics:
    n_periods: int
    n_trades: int
    total_return: float          # fraction over the whole curve
    cagr: float                  # annualized, given periods_per_year
    volatility: float            # annualized stdev of period returns
    sharpe: float
    sortino: float
    max_drawdown: float          # fraction, positive number (0.12 == -12%)
    calmar: float
    win_rate: float              # fraction of closed trades with pnl > 0
    profit_factor: float         # gross profit / gross loss
    expectancy: float            # average pnl per closed trade
    avg_win: float
    avg_loss: float

    def as_dict(self) -> dict[str, float]:
        return {
            "n_periods": float(self.n_periods),
            "n_trades": float(self.n_trades),
            "total_return": self.total_return,
            "cagr": self.cagr,
            "volatility": self.volatility,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown": self.max_drawdown,
            "calmar": self.calmar,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
        }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def returns_from_equity(equity_curve: list[float]) -> list[float]:
    """Period simple returns from an equity curve. No lookahead by construction."""
    out: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev != 0:
            out.append((equity_curve[i] - prev) / prev)
    return out


def max_drawdown(equity_curve: list[float]) -> float:
    """Maximum peak-to-trough drawdown as a positive fraction."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def sharpe(returns: list[float], periods_per_year: float, rf: float = 0.0) -> float:
    if not returns:
        return 0.0
    excess = [r - rf / periods_per_year for r in returns]
    sd = _stdev(excess)
    if sd == 0:
        return 0.0
    return (_mean(excess) / sd) * math.sqrt(periods_per_year)


def sortino(returns: list[float], periods_per_year: float, rf: float = 0.0) -> float:
    if not returns:
        return 0.0
    excess = [r - rf / periods_per_year for r in returns]
    downside = [min(0.0, e) for e in excess]
    dd = math.sqrt(sum(d * d for d in downside) / len(downside)) if downside else 0.0
    if dd == 0:
        return 0.0
    return (_mean(excess) / dd) * math.sqrt(periods_per_year)


def compute_metrics(
    equity_curve: list[float],
    trade_pnls: list[float],
    periods_per_year: float = 252.0,
) -> BacktestMetrics:
    rets = returns_from_equity(equity_curve)
    n_periods = len(rets)

    if len(equity_curve) >= 2 and equity_curve[0] != 0:
        total_return = (equity_curve[-1] - equity_curve[0]) / equity_curve[0]
    else:
        total_return = 0.0

    if n_periods > 0 and (1 + total_return) > 0:
        years = n_periods / periods_per_year
        cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0.0
    else:
        cagr = 0.0

    vol = _stdev(rets) * math.sqrt(periods_per_year)
    mdd = max_drawdown(equity_curve)
    calmar = (cagr / mdd) if mdd > 0 else 0.0

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )

    return BacktestMetrics(
        n_periods=n_periods,
        n_trades=len(trade_pnls),
        total_return=total_return,
        cagr=cagr,
        volatility=vol,
        sharpe=sharpe(rets, periods_per_year),
        sortino=sortino(rets, periods_per_year),
        max_drawdown=mdd,
        calmar=calmar,
        win_rate=(len(wins) / len(trade_pnls)) if trade_pnls else 0.0,
        profit_factor=profit_factor,
        expectancy=_mean(trade_pnls),
        avg_win=_mean(wins),
        avg_loss=_mean(losses),
    )
