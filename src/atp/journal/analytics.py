"""Trade analytics (§11).

§11: "The system analyzes why trades work or fail." This module is the first, honest slice of
that — descriptive statistics over the journal, grouped by strategy and by regime, so decay
or a strategy that only works in one regime becomes visible. It computes nothing that isn't
present in the recorded trades; no forecasting, no fabricated edge.

The learning/Strategy-Discovery layers (§11/§12) build on these groupings later.
"""

from __future__ import annotations

from dataclasses import dataclass

from .record import TradeRecord, TradeResult
from .store import TradeJournal


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


@dataclass(slots=True)
class GroupStats:
    label: str
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float          # avg realized_pnl per trade (currency)
    total_pnl: float
    avg_return: float          # avg realized_return (fraction)
    avg_expected_return: float # avg of the entry signals' expectation
    calibration: float         # avg_return - avg_expected_return (edge realized vs. promised)
    avg_holding_bars: float
    avg_mfe: float
    avg_mae: float
    avg_confidence: float

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "label": self.label, "n_trades": self.n_trades, "win_rate": self.win_rate,
            "profit_factor": self.profit_factor, "expectancy": self.expectancy,
            "total_pnl": self.total_pnl, "avg_return": self.avg_return,
            "avg_expected_return": self.avg_expected_return, "calibration": self.calibration,
            "avg_holding_bars": self.avg_holding_bars, "avg_mfe": self.avg_mfe,
            "avg_mae": self.avg_mae, "avg_confidence": self.avg_confidence,
        }


def summarize(trades: list[TradeRecord], label: str = "all") -> GroupStats:
    if not trades:
        return GroupStats(label, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    pnls = [t.realized_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )
    avg_return = _mean([t.realized_return for t in trades])
    avg_expected = _mean([t.expected_return for t in trades])

    return GroupStats(
        label=label,
        n_trades=len(trades),
        win_rate=sum(1 for t in trades if t.result is TradeResult.WIN) / len(trades),
        profit_factor=profit_factor,
        expectancy=_mean(pnls),
        total_pnl=sum(pnls),
        avg_return=avg_return,
        avg_expected_return=avg_expected,
        calibration=avg_return - avg_expected,
        avg_holding_bars=_mean([float(t.bars_held) for t in trades]),
        avg_mfe=_mean([t.mfe for t in trades]),
        avg_mae=_mean([t.mae for t in trades]),
        avg_confidence=_mean([t.confidence for t in trades]),
    )


class TradeAnalytics:
    def __init__(self, trades: list[TradeRecord]) -> None:
        self._trades = trades

    @classmethod
    def from_journal(cls, journal: TradeJournal) -> "TradeAnalytics":
        return cls(journal.all())

    def overall(self) -> GroupStats:
        return summarize(self._trades, "all")

    def by_strategy(self) -> list[GroupStats]:
        return self._grouped(lambda t: t.strategy)

    def by_regime(self) -> list[GroupStats]:
        return self._grouped(lambda t: t.regime)

    def by_strategy_regime(self) -> list[GroupStats]:
        return self._grouped(lambda t: f"{t.strategy}/{t.regime}")

    def _grouped(self, keyfn) -> list[GroupStats]:
        buckets: dict[str, list[TradeRecord]] = {}
        for t in self._trades:
            buckets.setdefault(keyfn(t), []).append(t)
        stats = [summarize(v, k) for k, v in buckets.items()]
        stats.sort(key=lambda s: s.total_pnl, reverse=True)
        return stats
