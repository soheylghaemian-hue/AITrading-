"""Durable source-of-truth persistence (§ Phase B).

PostgreSQL is authoritative in production; SQLite is the local/test backend behind the same Store
interface. `open_store` selects a backend from a URL:
    postgres://…  / postgresql://…  -> PostgresStore
    sqlite:///path  or a bare path   -> SqliteStore
"""

from .base import (
    AiAssessmentComponentRow,
    AiAssessmentRow,
    AiGovernanceResultRow,
    AiPredictionOutcomeRow,
    AiPredictionRow,
    AnalystEstimatesRow,
    AuditEventRow,
    BacktestDecisionRow,
    BacktestEquityPointRow,
    BacktestEventRow,
    BacktestMetricsRow,
    BacktestRunRow,
    BacktestTradeRow,
    CompanyRow,
    DailyLossLockRow,
    DailyPnlRow,
    DataCompletenessRow,
    DecisionRow,
    FillRow,
    FinancialMetricsRow,
    InsiderClusterRow,
    InsiderTransactionRow,
    InstitutionalChangeRow,
    InstrumentImportEventRow,
    InstrumentImportRunRow,
    InstrumentQualificationEventRow,
    InstrumentQualificationRunRow,
    InstrumentRow,
    KillSwitchRow,
    MacroSnapshotRow,
    NewsItemRow,
    OptionsFlowRow,
    OptionsSnapshotRow,
    OrderRow,
    PaperCanaryAccountRow,
    PaperCanaryConflict,
    PaperCanaryError,
    PaperCanaryFillCommitResult,
    PaperCanaryFillRow,
    PaperCanaryOrderEventRow,
    PaperCanaryOrderRow,
    PaperCanaryPositionRow,
    PaperCanaryReconciliationRow,
    PaperCanaryRunRow,
    PaperCanarySafetyError,
    PaperCanaryStateError,
    PositionRow,
    ResearchDatasetEventRow,
    ResearchDatasetRow,
    ResearchIntelInputRow,
    ResearchIntelOutcomeRow,
    ResearchIntelSnapshotRow,
    ResearchValidationRunRow,
    RiskConfigRow,
    RiskControlPolicyRow,
    RiskEventRow,
    RiskStateRow,
    RuntimeStateRow,
    SqlStore,
    Store,
    TraderPerformanceRow,
    TraderPositionRow,
    TraderRow,
    ValuationRow,
    new_id,
    paper_canary_config_checksum,
    paper_canary_request_checksum,
    risk_config_token,
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
    "AiPredictionRow", "AiPredictionOutcomeRow", "AiGovernanceResultRow", "DataCompletenessRow",
    "MacroSnapshotRow", "InstitutionalChangeRow", "InsiderTransactionRow", "InsiderClusterRow",
    "RiskControlPolicyRow", "RiskEventRow", "risk_config_token",
    "PaperCanaryError", "PaperCanaryConflict", "PaperCanaryStateError",
    "PaperCanarySafetyError", "PaperCanaryRunRow", "PaperCanaryAccountRow",
    "PaperCanaryOrderRow", "PaperCanaryFillRow", "PaperCanaryPositionRow",
    "PaperCanaryOrderEventRow", "PaperCanaryReconciliationRow",
    "PaperCanaryFillCommitResult", "paper_canary_config_checksum",
    "paper_canary_request_checksum",
    "BacktestRunRow", "BacktestDecisionRow", "BacktestTradeRow", "BacktestEquityPointRow",
    "BacktestEventRow", "BacktestMetricsRow", "ResearchDatasetRow", "ResearchDatasetEventRow",
    "ResearchIntelSnapshotRow", "ResearchIntelInputRow", "ResearchIntelOutcomeRow",
    "ResearchValidationRunRow",
    "InstrumentRow", "InstrumentImportRunRow", "InstrumentImportEventRow",
    "InstrumentQualificationRunRow", "InstrumentQualificationEventRow",
]


def open_store(url: str, *, migrate: bool = True):
    """Open the durable store for a URL. Postgres in production, SQLite locally/in tests."""
    if url.startswith(("postgres://", "postgresql://")):
        from .postgres_store import open_postgres  # noqa: PLC0415 — lazy (psycopg optional)
        return open_postgres(url, migrate=migrate)
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///"):]
    return open_sqlite(url, migrate=migrate)
