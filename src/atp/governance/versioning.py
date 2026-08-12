"""Model versioning & validated promotion (§19).

§19: every model has a version with its params and performance stored; a new model is adopted
*only after a validated improvement*, and on decay the system can roll back to an earlier
version. This module is that ledger: candidates are proposed with their out-of-sample metrics
and promoted only if they beat the incumbent by a margin — otherwise the incumbent stands.

No live model is ever swapped on an unvalidated promise (§25). Promotion is a pure comparison
of recorded metrics; `rollback()` restores the previous promoted version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..logging_config import get_logger

log = get_logger("governance")


class ModelStatus(str, Enum):
    """A model's lifecycle stage (§9). Deployment only ever moves forward through validation."""

    RESEARCH = "research"
    TESTING = "testing"
    PAPER = "paper"
    APPROVED = "approved"
    LIVE = "live"
    SUSPENDED = "suspended"
    RETIRED = "retired"


# Allowed lifecycle transitions (§9). A model cannot jump straight to LIVE without validating.
_ALLOWED: dict[ModelStatus, set[ModelStatus]] = {
    ModelStatus.RESEARCH: {ModelStatus.TESTING, ModelStatus.RETIRED},
    ModelStatus.TESTING: {ModelStatus.PAPER, ModelStatus.RESEARCH, ModelStatus.RETIRED},
    ModelStatus.PAPER: {ModelStatus.APPROVED, ModelStatus.TESTING, ModelStatus.RETIRED},
    ModelStatus.APPROVED: {ModelStatus.LIVE, ModelStatus.PAPER, ModelStatus.RETIRED},
    ModelStatus.LIVE: {ModelStatus.SUSPENDED, ModelStatus.RETIRED},
    ModelStatus.SUSPENDED: {ModelStatus.LIVE, ModelStatus.RETIRED},
    ModelStatus.RETIRED: set(),
}


@dataclass(slots=True)
class ModelVersion:
    strategy: str
    version: str
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)   # validated (OOS) metrics
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # --- full model-governance metadata (§9) ------------------------------
    model_id: str = ""
    status: ModelStatus = ModelStatus.RESEARCH
    training_dataset: str = ""
    feature_set: list[str] = field(default_factory=list)
    training_date: datetime | None = None
    deployment_date: datetime | None = None
    validation_results: dict = field(default_factory=dict)
    oos_results: dict = field(default_factory=dict)
    paper_results: dict = field(default_factory=dict)
    production_results: dict = field(default_factory=dict)

    def can_transition(self, new_status: ModelStatus) -> bool:
        return new_status in _ALLOWED[self.status]


@dataclass(slots=True)
class PromotionPolicy:
    primary_metric: str = "sharpe"     # the metric that must improve
    min_improvement: float = 0.0       # candidate must exceed incumbent by at least this
    require_positive: bool = True      # candidate's primary metric must be > 0


@dataclass(slots=True)
class PromotionResult:
    promoted: bool
    reason: str
    active: ModelVersion


class ModelRegistry:
    """Per-strategy lineage of promoted model versions, with validated promotion & rollback."""

    def __init__(self, policy: PromotionPolicy | None = None) -> None:
        self._policy = policy or PromotionPolicy()
        self._current: dict[str, ModelVersion] = {}
        self._lineage: dict[str, list[ModelVersion]] = {}

    def set_baseline(self, version: ModelVersion) -> ModelVersion:
        """Adopt a first version unconditionally (nothing to compare against yet)."""
        self._current[version.strategy] = version
        self._lineage.setdefault(version.strategy, []).append(version)
        log.info("baseline model %s=%s", version.strategy, version.version)
        return version

    def current(self, strategy: str) -> ModelVersion | None:
        return self._current.get(strategy)

    def lineage(self, strategy: str) -> list[ModelVersion]:
        return list(self._lineage.get(strategy, []))

    # ------------------------------------------------------------- lifecycle (§9)
    def transition(self, version: ModelVersion, new_status: ModelStatus) -> ModelVersion:
        """Advance a model through its lifecycle, enforcing the allowed transitions (§9).

        Raises ValueError on an illegal jump (e.g. RESEARCH → LIVE). Records the deployment
        date when a model goes LIVE."""
        if not version.can_transition(new_status):
            raise ValueError(
                f"illegal model transition {version.status.value} → {new_status.value} "
                f"for {version.strategy}:{version.version}"
            )
        old = version.status
        version.status = new_status
        if new_status is ModelStatus.LIVE:
            version.deployment_date = datetime.now(timezone.utc)
        log.info("model %s:%s %s → %s", version.strategy, version.version, old.value, new_status.value)
        return version

    def attach_results(self, version: ModelVersion, phase: str, results: dict) -> None:
        """Store validation/oos/paper/production results as a model moves through stages (§9)."""
        target = {
            "validation": "validation_results", "oos": "oos_results",
            "paper": "paper_results", "production": "production_results",
        }.get(phase)
        if target is None:
            raise ValueError(f"unknown results phase '{phase}'")
        setattr(version, target, dict(results))

    def by_status(self, status: ModelStatus) -> list[ModelVersion]:
        out: list[ModelVersion] = []
        for versions in self._lineage.values():
            out.extend(v for v in versions if v.status is status)
        return out

    def _metric(self, v: ModelVersion) -> float:
        return float(v.metrics.get(self._policy.primary_metric, float("-inf")))

    def promote(self, candidate: ModelVersion) -> PromotionResult:
        """Promote `candidate` iff it validates as an improvement; else keep the incumbent."""
        p = self._policy
        incumbent = self._current.get(candidate.strategy)
        cand_metric = self._metric(candidate)

        if p.require_positive and cand_metric <= 0:
            reason = f"rejected: {p.primary_metric}={cand_metric:.3f} not positive"
            return PromotionResult(False, reason, incumbent or candidate)

        if incumbent is None:
            self.set_baseline(candidate)
            return PromotionResult(True, "adopted as baseline", candidate)

        inc_metric = self._metric(incumbent)
        if cand_metric >= inc_metric + p.min_improvement and cand_metric > inc_metric:
            self._current[candidate.strategy] = candidate
            self._lineage.setdefault(candidate.strategy, []).append(candidate)
            reason = (f"promoted {incumbent.version}->{candidate.version}: "
                      f"{p.primary_metric} {inc_metric:.3f}->{cand_metric:.3f}")
            log.info(reason)
            return PromotionResult(True, reason, candidate)

        reason = (f"rejected {candidate.version}: {p.primary_metric} {cand_metric:.3f} "
                  f"does not beat incumbent {inc_metric:.3f} by {p.min_improvement:.3f}")
        return PromotionResult(False, reason, incumbent)

    def rollback(self, strategy: str) -> ModelVersion | None:
        """Revert to the previous promoted version (§19 decay rollback). None if no prior."""
        history = self._lineage.get(strategy, [])
        if len(history) < 2:
            log.warning("cannot roll back %s: no prior version", strategy)
            return None
        history.pop()                      # drop the current
        previous = history[-1]
        self._current[strategy] = previous
        log.warning("ROLLBACK %s -> %s", strategy, previous.version)
        return previous
