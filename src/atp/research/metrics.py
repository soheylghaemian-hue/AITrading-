"""§ R3.0 — performance / risk / robustness metrics (RESEARCH ONLY, pure, numpy-free).

The pure equity-curve math (returns, drawdown, Sharpe, Sortino) is re-implemented here on purpose: it
must NOT import `atp.backtest.metrics`, because `atp.backtest` transitively imports the execution-coupled
legacy engine (broker/execution). This keeps the R3.0 import graph free of prohibited modules.

Every metric returns NO DATA / NOT APPLICABLE honestly when the sample is insufficient — a ratio is
never fabricated to look complete. Sharpe/Sortino assumptions are documented in the output.
"""
from __future__ import annotations

import math
from decimal import Decimal

NO_DATA = "NO DATA"
NOT_APPLICABLE = "NOT APPLICABLE"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def returns_from_equity(equity: list[float]) -> list[float]:
    out = []
    for i in range(1, len(equity)):
        p = equity[i - 1]
        if p != 0:
            out.append((equity[i] - p) / p)
    return out


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def _streaks(pnls: list[float]) -> tuple[int, int]:
    max_w = max_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w += 1; cur_l = 0; max_w = max(max_w, cur_w)
        elif p < 0:
            cur_l += 1; cur_w = 0; max_l = max(max_l, cur_l)
        else:
            cur_w = cur_l = 0
    return max_w, max_l


def compute_metrics(equity_points: list[dict], trades: list[dict], *, starting_capital: Decimal,
                    periods_per_year: float = 252.0) -> dict:
    equities = [float(p["equity"]) for p in equity_points]
    rets = returns_from_equity(equities)
    n_periods, n_trades = len(rets), len(trades)
    start = float(starting_capital)
    end = equities[-1] if equities else start
    total_return = ((end - start) / start) if start else 0.0

    pnls = [float(t["net_pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)

    def ratio_ok(n: int) -> bool:
        return n >= 2

    vol = _stdev(rets) * math.sqrt(periods_per_year) if ratio_ok(len(rets)) else None
    sd = _stdev(rets)
    sharpe = ((_mean(rets) / sd) * math.sqrt(periods_per_year)) if (ratio_ok(len(rets)) and sd > 0) else None
    downside = [min(0.0, r) for r in rets]
    dd_std = math.sqrt(sum(d * d for d in downside) / len(downside)) if downside else 0.0
    sortino = ((_mean(rets) / dd_std) * math.sqrt(periods_per_year)) if (ratio_ok(len(rets)) and dd_std > 0) else None
    mdd = max_drawdown(equities)

    years = n_periods / periods_per_year if periods_per_year else 0.0
    cagr = ((1 + total_return) ** (1 / years) - 1) if (years > 0 and (1 + total_return) > 0) else None

    total_comm = sum((Decimal(str(t["commission"])) for t in trades), Decimal(0))
    total_slip = sum((Decimal(str(t["slippage"])) for t in trades), Decimal(0))
    bars_in_pos = sum(int(t.get("bars_held") or 0) for t in trades)
    exposure_time = (bars_in_pos / len(equity_points)) if equity_points else None
    traded_notional = sum((Decimal(str(t["entry_price"])) + Decimal(str(t.get("exit_price") or 0)))
                          * Decimal(str(t["quantity"])) for t in trades)
    turnover = float(traded_notional / starting_capital) if starting_capital else None
    max_w, max_l = _streaks(pnls)

    def f(x):
        return None if x is None else round(float(x), 6)

    m = {
        "starting_capital": round(start, 2),
        "ending_capital": round(end, 2),
        "total_return": round(total_return, 6),
        "annualized_return": (NOT_APPLICABLE if cagr is None else round(cagr, 6)),
        "max_drawdown": round(mdd, 6),
        "sharpe": (NOT_APPLICABLE if sharpe is None else round(sharpe, 4)),
        "sortino": (NOT_APPLICABLE if sortino is None else round(sortino, 4)),
        "volatility": (NOT_APPLICABLE if vol is None else round(vol, 6)),
        "profit_factor": (round(gross_profit / gross_loss, 4) if gross_loss > 0
                          else (NOT_APPLICABLE if not n_trades else ("inf" if gross_profit > 0 else 0.0))),
        "win_rate": (round(len(wins) / n_trades, 4) if n_trades else NO_DATA),
        "loss_rate": (round(len(losses) / n_trades, 4) if n_trades else NO_DATA),
        "expectancy": (round(_mean(pnls), 4) if n_trades else NO_DATA),
        "avg_win": (round(_mean(wins), 4) if wins else NO_DATA),
        "avg_loss": (round(_mean(losses), 4) if losses else NO_DATA),
        "payoff_ratio": (round(_mean(wins) / abs(_mean(losses)), 4) if (wins and losses) else NOT_APPLICABLE),
        "num_trades": n_trades,
        # R3.1A: this metric is Σ bars_held (all symbols) ÷ portfolio-timeline bars = average concurrent
        # open positions (legitimately >1 multi-asset). Corrected label is `avg_concurrent_positions`;
        # `exposure_time` is retained UNCHANGED (same value) for backward-compatible API/old runs.
        "avg_concurrent_positions": (NO_DATA if exposure_time is None else round(exposure_time, 4)),
        "exposure_time": (NO_DATA if exposure_time is None else round(exposure_time, 4)),
        "turnover": (NO_DATA if turnover is None else round(turnover, 4)),
        "total_commissions": round(float(total_comm), 2),
        "total_slippage": round(float(total_slip), 2),
        "best_trade": (round(max(pnls), 2) if pnls else NO_DATA),
        "worst_trade": (round(min(pnls), 2) if pnls else NO_DATA),
        "consecutive_wins": max_w,
        "consecutive_losses": max_l,
        "n_periods": n_periods,
        "sharpe_assumptions": {"risk_free_rate": 0.0, "periods_per_year": periods_per_year,
                               "return_basis": "per-equity-point simple returns"},
        "daily_returns": _period_returns(equity_points, "day"),
        "monthly_returns": _period_returns(equity_points, "month"),
    }
    return m


def _period_returns(equity_points: list[dict], grain: str) -> list[dict]:
    """Last-equity-of-period → period simple returns. Honest empty list when insufficient."""
    if len(equity_points) < 2:
        return []
    buckets: dict[str, float] = {}
    order: list[str] = []
    for p in equity_points:
        ts = p["ts"]
        key = ts[:10] if grain == "day" else ts[:7]
        if key not in buckets:
            order.append(key)
        buckets[key] = float(p["equity"])
    out = []
    prev = None
    for k in order:
        v = buckets[k]
        if prev is not None and prev != 0:
            out.append({"period": k, "return": round((v - prev) / prev, 6)})
        prev = v
    return out


def robustness_report(coverage, trades: list[dict], equity_points: list[dict], *, min_trades: int = 20) -> dict:
    """In-sample/out-of-sample separation, coverage, concentration and cost sensitivity — no optimizer."""
    n_trades = len(trades)
    by_symbol: dict[str, float] = {}
    net = 0.0
    for t in trades:
        p = float(t["net_pnl"])
        net += p
        by_symbol[t["symbol"]] = by_symbol.get(t["symbol"], 0.0) + p
    by_month: dict[str, float] = {}
    for t in trades:
        mth = (t.get("exit_ts") or t["entry_ts"])[:7]
        by_month[mth] = by_month.get(mth, 0.0) + float(t["net_pnl"])
    total_costs = sum((Decimal(str(t["commission"])) + Decimal(str(t["slippage"])) for t in trades), Decimal(0))
    gross = net + float(total_costs)
    cov = coverage.as_dict() if hasattr(coverage, "as_dict") else coverage
    missing_ratios = [s["missing_ratio"] for s in cov.get("symbols", [])]
    # simple in-sample / out-of-sample split of the equity curve (walk-forward-ready architecture)
    half = len(equity_points) * 7 // 10
    ins = [float(p["equity"]) for p in equity_points[:half]]
    oos = [float(p["equity"]) for p in equity_points[half:]]

    def seg_return(e):
        return round((e[-1] - e[0]) / e[0], 6) if len(e) >= 2 and e[0] else NO_DATA

    def conc(d: dict) -> float | str:
        vals = [abs(v) for v in d.values()]
        tot = sum(vals)
        return round(max(vals) / tot, 4) if tot > 0 else NO_DATA

    return {
        "min_trade_warning": (f"only {n_trades} trades (< {min_trades}); results are not statistically "
                              f"reliable" if n_trades < min_trades else None),
        "missing_data_ratio": round(sum(missing_ratios) / len(missing_ratios), 4) if missing_ratios else NO_DATA,
        "data_coverage_by_symbol": {s["symbol"]: {"available": s["available_bars"], "expected": s["expected_bars"],
                                                  "missing_ratio": s["missing_ratio"]} for s in cov.get("symbols", [])},
        "concentration_by_symbol": conc(by_symbol),
        "concentration_by_period": conc(by_month),
        "in_sample_return": seg_return(ins),
        "out_of_sample_return": seg_return(oos),
        "cost_sensitivity": {"total_costs": round(float(total_costs), 2), "gross_pnl": round(gross, 2),
                             "net_pnl": round(net, 2),
                             "cost_to_gross_ratio": (round(float(total_costs) / abs(gross), 4) if gross else NO_DATA)},
        "disclaimer": ("Backtest performance is hypothetical, reflects a single historical path over a "
                       "narrow strategy, and is NOT a guarantee or prediction of future results. Research "
                       "only — never live trading."),
    }
