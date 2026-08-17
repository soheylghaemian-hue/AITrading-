"""Risk Control Center (§ Phase R2.0) — READ-ONLY capital-protection observability + config gate.

Makes capital-protection limits visible, persistent, deterministic, configurable, auditable and
available to AI Governance. It is a GATE and OBSERVABILITY layer only: it never trades, never generates
or submits an order, never touches the Trading-Core RiskEngine / broker / IBKR / execution / autonomous
paths, and never mutates the kill switch (that stays authoritative in KillSwitchRow via the existing
control endpoints). Canonical capital / risk_per_trade_pct / max_daily_loss_pct stay in risk_config;
this layer adds only the risk_control_policy companion + immutable risk_events. Missing data → NO DATA,
never zero, never READY. Changing limits here does NOT enable trading.
"""

from .config import ACCEPTED_CURRENCIES, EXAMPLE_CONFIG, validate_config
from .evaluate import BLOCKED, NO_DATA, READY, WARNING, evaluate_risk_state
from .readmodel import build_risk_config_view, build_risk_events, build_risk_status, combined_config

__all__ = [
    "validate_config", "EXAMPLE_CONFIG", "ACCEPTED_CURRENCIES",
    "evaluate_risk_state", "READY", "WARNING", "BLOCKED", "NO_DATA",
    "build_risk_status", "build_risk_config_view", "build_risk_events", "combined_config",
]
