"""Machine-enforced autonomy boundaries for the Development Autopilot.

Green actions may proceed unattended. Yellow actions may only proceed when an
explicit policy flag enables them. Red actions are never performed by this package.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath


class RiskTier(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    tier: RiskTier
    reason: str


@dataclass(frozen=True, slots=True)
class AutopilotPolicy:
    """Fail-closed policy. Production, secrets and trading authority stay red."""

    allow_yellow: bool = False

    RED_PREFIXES = (
        "infra/systemd/",
        "src/atp/brokers/",
        "src/atp/execution/",
        "src/atp/live/",
        "src/atp/risk/",
        "src/atp/riskcontrol/",
        "src/atp/runtime/",
        "src/atp/services/",
    )
    RED_NAMES = (
        ".env", "atp.env", "secrets", "credentials", "id_rsa", "id_ed25519",
    )
    YELLOW_PREFIXES = ("infra/", ".github/workflows/")
    GREEN_PREFIXES = (
        "src/atp/autopilot/", "src/atp/brain/", "src/atp/research/",
        "tests/", "docs/", "frontend/", ".github/ISSUE_TEMPLATE/",
    )

    def classify_path(self, path: str, *, goal_paths: tuple[str, ...] = ()) -> Decision:
        normalized = str(PurePosixPath(path.strip().lstrip("/")))
        parts = PurePosixPath(normalized).parts
        if not normalized or ".." in parts:
            return Decision(False, RiskTier.RED, "invalid or escaping path")
        lower = normalized.lower()
        if any(name in lower for name in self.RED_NAMES):
            return Decision(False, RiskTier.RED, "secret-bearing path is immutable")
        if any(normalized.startswith(p) for p in self.RED_PREFIXES):
            return Decision(False, RiskTier.RED, "trading/production authority is outside autopilot scope")
        if goal_paths and not any(normalized == p.rstrip("/") or normalized.startswith(p) for p in goal_paths):
            return Decision(False, RiskTier.RED, "path is outside the goal's declared scope")
        if any(normalized.startswith(p) for p in self.YELLOW_PREFIXES):
            return Decision(self.allow_yellow, RiskTier.YELLOW,
                            "yellow action enabled" if self.allow_yellow else "yellow action requires policy enablement")
        if any(normalized.startswith(p) for p in self.GREEN_PREFIXES):
            return Decision(True, RiskTier.GREEN, "bounded research/development path")
        return Decision(False, RiskTier.RED, "path is not allowlisted")

    def authorize_files(self, paths: list[str], *, goal_paths: tuple[str, ...] = ()) -> list[Decision]:
        return [self.classify_path(p, goal_paths=goal_paths) for p in paths]

    @staticmethod
    def immutable_constitution() -> dict[str, object]:
        return {
            "production_deploy": False,
            "production_migration": False,
            "secret_read_or_write": False,
            "broker_access": False,
            "execution_enable": False,
            "real_money": False,
            "leverage_enable_or_increase": False,
            "risk_limit_relaxation": False,
            "autonomous_trading": False,
        }
