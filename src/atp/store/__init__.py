"""Durable source-of-truth persistence (§ Phase B).

PostgreSQL is authoritative in production; SQLite is the local/test backend behind the same Store
interface. `open_store` selects a backend from a URL:
    postgres://…  / postgresql://…  -> PostgresStore
    sqlite:///path  or a bare path   -> SqliteStore
"""

from .base import (
    AiAssessmentComponentRow,
    AiAssessmentRow,
    AiPredictionOutcomeRow,
    AiPredictionRow,
    AnalystEstimatesRow,
    AuditEventRow,
    CompanyRow,
    DailyLossLockRow,
    DailyPnlRow,
    DecisionRow,
    FillRow,
    FinancialMetricsRow,
    KillSwitchRow,
    NewsItemRow,
    OptionsFlowRow,
    OptionsSnapshotRow,
    OrderRow,
    PositionRow,
    RiskConfigRow,
    RiskStateRow,
    RuntimeStateRow,
    SqlStore,
    Store,
    ValuationRow,
    TraderPerformanceRow,
    TraderPositionRow,
    TraderRow,
    new_id,
    utcnow_iso,
)
from .money import D, money_str, to_decimal
from .schema import Migrator
from .sqlite_store import SqliteStore, open_sqlite

__all__ = [
    "Store", "SqlStore", "SqliteStore", "open_sqlite", "open_store", "Migrator",
    "D", "money_str", "to_decimal", "new_id", "utcnow_iso",
    "RuntimeStateRow", "KillSwitchRow", "DailyPnlRow", "DailyLossLockRow",
    "RiskConfigRow", "RiskStateRow", "OrderRow", "FillRow", "PositionRow", "AuditEventRow",
    "DecisionRow", "NewsItemRow", "TraderRow", "TraderPerformanceRow", "TraderPositionRow",
    "CompanyRow", "FinancialMetricsRow", "ValuationRow", "AnalystEstimatesRow",
    "OptionsSnapshotRow", "OptionsFlowRow", "AiAssessmentRow", "AiAssessmentComponentRow",
    "AiPredictionRow", "AiPredictionOutcomeRow",
]


def open_store(url: str, *, migrate: bool = True):
    """Open the durable store for a URL. Postgres in production, SQLite locally/in tests."""
    if url.startswith(("postgres://", "postgresql://")):
        from .postgres_store import open_postgres  # noqa: PLC0415 — lazy (psycopg optional)
        return open_postgres(url, migrate=migrate)
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///"):]
    return open_sqlite(url, migrate=migrate)
