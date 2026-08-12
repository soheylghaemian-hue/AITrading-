"""The trade experience record (§11 Lernendes System).

§11 is explicit about what must be stored for *every* trade so the system can later analyze
why trades work or fail: instrument, strategy, regime, features, signal, confidence, expected
vs. actual return, slippage, fees, holding period, MFE, MAE, result and model version. This
dataclass is exactly that record — one completed position episode (entry → flat).

It is a plain, serializable value object (no behavior) so it maps cleanly onto any store
(in-memory, SQLite today; Postgres later, §21) without changing callers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum


class TradeResult(str, Enum):
    WIN = "win"
    LOSS = "loss"
    SCRATCH = "scratch"

    @staticmethod
    def of(pnl: float, tol: float = 1e-9) -> "TradeResult":
        if pnl > tol:
            return TradeResult.WIN
        if pnl < -tol:
            return TradeResult.LOSS
        return TradeResult.SCRATCH


@dataclass(slots=True)
class TradeRecord:
    # --- identity / attribution (§11) --------------------------------------
    trade_id: str
    instrument_key: str
    asset_class: str
    direction: str                 # "long" | "short"
    strategy: str
    regime: str
    model_version: str

    # --- entry / exit ------------------------------------------------------
    entry_ts: datetime
    exit_ts: datetime
    quantity: float
    entry_price: float
    exit_price: float

    # --- expectation vs. reality (§11) -------------------------------------
    confidence: float
    expected_return: float         # fraction, from the entry signal
    realized_return: float         # fraction, on entry notional
    gross_pnl: float               # currency, before costs
    commission: float              # currency
    realized_pnl: float            # currency, net of commission
    slippage: float                # fraction, entry fill vs. decision price (adverse +)

    # --- path statistics (§11) ---------------------------------------------
    mfe: float                     # max favorable excursion, fraction of entry
    mae: float                     # max adverse excursion, fraction of entry
    bars_held: int
    holding_seconds: float

    result: TradeResult
    rationale: str = ""
    features: dict = field(default_factory=dict)

    # --- full learning attribution (§1) — defaulted so older callers still construct ------
    underlying: str = ""           # underlying symbol (family the instrument belongs to)
    agent: str = ""                # the trader/agent that opened it (usually == strategy)
    signal_action: str = ""        # BUY / SELL / … the entry signal expressed
    signal_strength: float = 0.0   # raw signal magnitude (distinct reporting from confidence)
    expected_risk: float = 0.0     # planned risk as a fraction of entry price (stop distance)
    stop_price: float = 0.0        # planned stop level
    target_price: float = 0.0      # planned target level
    financing_cost: float = 0.0    # currency; borrow/financing (0 in paper, real via broker)
    strategy_version: str = "v0"   # strategy code/params version (distinct from model_version)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["result"] = self.result.value
        d["entry_ts"] = self.entry_ts.isoformat()
        d["exit_ts"] = self.exit_ts.isoformat()
        return d
