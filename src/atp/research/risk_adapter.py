"""§ R3.0 — Governance/Risk Validation adapter (RESEARCH ONLY).

Reuses the PURE R2.0 evaluator `atp.riskcontrol.evaluate.evaluate_risk_state` (imports `decimal` only —
proven clean) fed EXCLUSIVELY simulated historical portfolio inputs. It does NOT read today's
/risk/status and does NOT call the live order-veto RiskEngine. The kill switch is synthetic ARMED
(never STOPPED — a backtest has no live kill switch). `evaluate_governance` is NOT used in R3.0 (no AI
assessment inputs are read — that path is deferred to R3.1).
"""
from __future__ import annotations

from decimal import Decimal

from ..riskcontrol.evaluate import evaluate_risk_state


class _Pnl:
    __slots__ = ("realized_pnl", "unrealized_pnl", "updated_at")

    def __init__(self, realized: Decimal, unrealized: Decimal, ts: str) -> None:
        self.realized_pnl, self.unrealized_pnl, self.updated_at = realized, unrealized, ts


class _RiskState:
    __slots__ = ("peak_equity", "day_start_equity", "killed")

    def __init__(self, peak: Decimal) -> None:
        self.peak_equity, self.day_start_equity, self.killed = peak, peak, False


class _KillSwitch:
    engaged = False           # synthetic ARMED — a backtest never engages a live kill switch


def evaluate_sim_risk(config: dict, *, realized: Decimal, unrealized: Decimal, ts: str, peak_equity: Decimal,
                      equity: Decimal, gross_pct, net_pct, position_risk_pct=None) -> dict:
    """Deterministic risk state over the SIMULATED portfolio, via the pure R2.0 evaluator."""
    exposure = {"gross_pct": gross_pct, "net_pct": net_pct}
    if position_risk_pct is not None:
        exposure["position_risk_pct"] = position_risk_pct
    return evaluate_risk_state(
        config=config, daily_pnl=_Pnl(realized, unrealized, ts), risk_state=_RiskState(peak_equity),
        kill_switch=_KillSwitch(), exposure=exposure, equity=equity)
