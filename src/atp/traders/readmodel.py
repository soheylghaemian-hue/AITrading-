"""Trader read-model assembly (§ Phase G2.5). PURE composition from the store — reused by the Control
API and unit-tested directly. Quality is computed deterministically from real performance; consensus
is quality-weighted. Missing data → None/empty (NO DATA), never fabricated. No secrets.
"""
from __future__ import annotations

from .consensus import compute_consensus
from .quality import quality_score


def build_symbol_consensus(store, symbol: str) -> dict:
    """Quality-weighted trader consensus for a symbol + the ranked contributors."""
    sym = symbol.upper()
    positions = store.list_trader_positions_for_symbol(sym)
    entries: list[tuple[str, float | None]] = []
    contributors: list[dict] = []
    for p in positions:
        trader = store.get_trader(p.trader_id)
        perf = store.get_trader_performance(p.trader_id)
        q = quality_score(perf, trader.track_record_days if trader else None)
        entries.append((p.direction, q))
        contributors.append({
            "id": p.trader_id,
            "name": trader.name if trader else p.trader_id,
            "quality": q,
            "strategy": trader.strategy_type if trader else None,
            "market_focus": trader.market_focus if trader else None,
            "direction": (p.direction or "").upper(),
        })
    res = compute_consensus(entries)
    contributors.sort(key=lambda c: (c["quality"] if c["quality"] is not None else -1.0), reverse=True)
    return {
        "symbol": sym,
        "consensus": res.consensus,
        "long_percent": res.long_percent,
        "short_percent": res.short_percent,
        "neutral_percent": res.neutral_percent,
        "weighted_score": res.weighted_score,
        "contributor_count": res.contributor_count,
        "contributors": contributors,
    }


def build_trader_profile(store, trader_id: str) -> dict | None:
    """A single trader's identity + performance + risk + strategy + positions. None when unknown."""
    trader = store.get_trader(trader_id)
    if trader is None:
        return None
    perf = store.get_trader_performance(trader_id)
    q = quality_score(perf, trader.track_record_days)
    positions = store.list_trader_positions_for_trader(trader_id)
    return {
        "id": trader.id, "name": trader.name, "source": trader.source,
        "market_focus": trader.market_focus, "strategy_type": trader.strategy_type,
        "track_record_days": trader.track_record_days, "quality": q,
        "performance": ({
            "total_return": perf.total_return, "annualized_return": perf.annualized_return,
            "win_rate": perf.win_rate, "number_of_trades": perf.number_of_trades,
            "average_holding_period": perf.average_holding_period,
        } if perf else None),
        "risk": ({
            "max_drawdown": perf.max_drawdown, "sharpe_ratio": perf.sharpe_ratio,
            "sortino_ratio": perf.sortino_ratio,
        } if perf else None),
        "strategy": {"strategy_type": trader.strategy_type, "market_focus": trader.market_focus},
        "positions": [{
            "symbol": p.symbol, "direction": p.direction, "entry_price": p.entry_price,
            "position_size": p.position_size, "timestamp": p.timestamp,
        } for p in positions],
    }
