"""Autonomous paper-trading control layer (§ Phase 8).

Wraps the existing AutonomousTradingDesk with an explicit, user-armed PAPER-AUTONOMOUS mode,
a data-quality gate, a decision log for observability, and a dashboard read-model — WITHOUT
ever touching IBKR execution. Paper fills are internal (PaperBroker). Default status: DISABLED.
"""

from .engine import AuditEntry, AutonomousStatus, Decision, PaperAutonomousEngine

__all__ = ["AuditEntry", "AutonomousStatus", "Decision", "PaperAutonomousEngine"]
