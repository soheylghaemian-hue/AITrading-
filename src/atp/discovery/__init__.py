"""Strategy Discovery (§12): candidate rule strategies and the validation gauntlet."""

from .rules import FEATURE_ACCESSORS, FeaturePredicate, RuleStrategy
from .search import (
    DiscoveryCriteria,
    DiscoveryResult,
    SearchSpace,
    StrategyDiscovery,
    ValidationReport,
)

__all__ = [
    "FeaturePredicate",
    "RuleStrategy",
    "FEATURE_ACCESSORS",
    "SearchSpace",
    "DiscoveryCriteria",
    "DiscoveryResult",
    "StrategyDiscovery",
    "ValidationReport",
]
