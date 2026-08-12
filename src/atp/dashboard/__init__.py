"""Dashboard (§21/§22): read-model snapshot + FastAPI backend + bundled static page.

`build_snapshot` and `DashboardSnapshot` are dependency-free and tested. `create_app` /
`DashboardContext` are imported lazily by callers that have FastAPI installed.
"""

from .notifications import Kind, Notification, NotificationCenter, Severity
from .snapshot import DashboardSnapshot, RiskView, build_snapshot

__all__ = [
    "DashboardSnapshot", "RiskView", "build_snapshot",
    "NotificationCenter", "Notification", "Severity", "Kind",
]
