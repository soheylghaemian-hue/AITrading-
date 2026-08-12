"""Notification center (§23).

A severity-ranked, bounded log of system events the owner should see — trades, risk warnings,
halts, broker/data problems, reconciliation errors, model/strategy changes, emergency stops.
The trading engine pushes notifications; the dashboard reads them. It never fabricates events —
an empty center means nothing has happened.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# Canonical event kinds (§23). Free-form messages are allowed; the kind drives filtering/UI.
class Kind(str, Enum):
    TRADE_OPENED = "trade_opened"
    TRADE_CLOSED = "trade_closed"
    RISK_WARNING = "risk_warning"
    RISK_HALT = "risk_halt"
    BROKER_DISCONNECT = "broker_disconnect"
    DATA_FEED = "data_feed"
    RECONCILIATION_ERROR = "reconciliation_error"
    MODEL_DECAY = "model_decay"
    STRATEGY_SUSPENDED = "strategy_suspended"
    STRATEGY_DISCOVERED = "strategy_discovered"
    MODEL_APPROVED = "model_approved"
    SYSTEM_ERROR = "system_error"
    EMERGENCY_STOP = "emergency_stop"


@dataclass(slots=True)
class Notification:
    ts: datetime
    severity: Severity
    kind: Kind
    message: str

    def as_dict(self) -> dict:
        return {"ts": self.ts.isoformat(), "severity": self.severity.value,
                "kind": self.kind.value, "message": self.message}


class NotificationCenter:
    def __init__(self, *, capacity: int = 500) -> None:
        self._items: deque[Notification] = deque(maxlen=capacity)

    def push(self, kind: Kind, message: str, *, severity: Severity = Severity.INFO,
             ts: datetime | None = None) -> Notification:
        n = Notification(ts or datetime.now(timezone.utc), severity, kind, message)
        self._items.append(n)
        return n

    def recent(self, limit: int = 50) -> list[Notification]:
        return list(self._items)[-limit:][::-1]     # newest first

    def by_severity(self, severity: Severity, limit: int = 50) -> list[Notification]:
        return [n for n in reversed(self._items) if n.severity is severity][:limit]

    def unresolved_critical(self) -> int:
        return sum(1 for n in self._items if n.severity is Severity.CRITICAL)

    def __len__(self) -> int:
        return len(self._items)
