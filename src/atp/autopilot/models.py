"""Immutable contracts shared by the autonomous development loop."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RunStatus(str, Enum):
    PLANNED = "PLANNED"
    BUILDING = "BUILDING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    BLOCKED_AUTH = "BLOCKED_AUTH"
    BLOCKED_POLICY = "BLOCKED_POLICY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    objective: str
    success_criteria: tuple[str, ...]
    allowed_paths: tuple[str, ...] = ()
    max_iterations: int = 5

    def __post_init__(self) -> None:
        if not self.goal_id.strip() or not self.objective.strip():
            raise ValueError("goal_id and objective are required")
        if not self.success_criteria:
            raise ValueError("at least one success criterion is required")
        if not 1 <= self.max_iterations <= 10:
            raise ValueError("max_iterations must be between 1 and 10")


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    title: str
    detail: str
    file: str | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    command: tuple[str, ...]
    passed: bool
    exit_code: int
    output_sha256: str
    output_tail: str


@dataclass(slots=True)
class RunReport:
    run_id: str
    goal_id: str
    status: RunStatus
    base_commit: str
    iteration: int = 0
    changed_files: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    policy_reasons: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    result_checksum: str | None = None

    def finalize(self) -> RunReport:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["result_checksum"] = None
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        self.result_checksum = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        return self

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self.finalize())
        data["status"] = self.status.value
        return data
