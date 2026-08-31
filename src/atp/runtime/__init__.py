"""Durable runtime: lifecycle recovery, idempotent orders, positions, fail-closed gate (§ Phase B)."""

from .gate import GateResult, TradingGate, enforce_daily_loss, remaining_daily_budget, today_utc
from .lifecycle import (
    CONFIRM_PHRASE,
    RECOVERY_STEPS,
    LifecycleError,
    LifecycleManager,
    RuntimeStatus,
)
from .orders import OrderManager
from .paper_canary import (
    DurablePaperCanary,
    PaperCanaryConfig,
    PaperCanaryConfigurationError,
    PaperCanaryError,
    PaperCanaryOrderIds,
    PaperCanaryRecovery,
    PaperCanaryRequestError,
    PaperCanarySafetyError,
    PaperCanarySnapshot,
    PaperCanaryStateError,
    PaperCanarySubmission,
    paper_canary_order_ids,
)
from .positions import ReconResult, apply_fill_to_position, reconcile, reconstruct_positions

__all__ = [
    "CONFIRM_PHRASE",
    "RECOVERY_STEPS",
    "DurablePaperCanary",
    "GateResult",
    "LifecycleError",
    "LifecycleManager",
    "OrderManager",
    "PaperCanaryConfig",
    "PaperCanaryConfigurationError",
    "PaperCanaryError",
    "PaperCanaryOrderIds",
    "PaperCanaryRecovery",
    "PaperCanaryRequestError",
    "PaperCanarySafetyError",
    "PaperCanarySnapshot",
    "PaperCanaryStateError",
    "PaperCanarySubmission",
    "ReconResult",
    "RuntimeStatus",
    "TradingGate",
    "apply_fill_to_position",
    "enforce_daily_loss",
    "paper_canary_order_ids",
    "reconcile",
    "reconstruct_positions",
    "remaining_daily_budget",
    "today_utc",
]
