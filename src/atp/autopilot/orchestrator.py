"""Bounded builder/reviewer/verifier loop.

The first production-safe increment deliberately does not execute arbitrary model
commands or deploy anything. A builder supplies a unified patch, policy authorizes
every touched file, ``git apply`` applies it without a shell, deterministic checks
run, and an independent reviewer either accepts or returns findings for the next
bounded iteration.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import Finding, Goal, RunReport, RunStatus
from .policy import AutopilotPolicy
from .providers import ModelProvider, ProviderUnavailable
from .state import StateStore
from .verifier import Verifier

PATCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "patch", "changed_files"],
    "properties": {
        "summary": {"type": "string"},
        "patch": {"type": "string", "description": "unified git patch"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
    },
}
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["approved", "findings"],
    "properties": {
        "approved": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "detail", "file"],
                "properties": {
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                },
            },
        },
    },
}


class DevelopmentAutopilot:
    def __init__(self, *, repo: str | Path, builder: ModelProvider, reviewer: ModelProvider,
                 policy: AutopilotPolicy | None = None, verifier: Verifier | None = None,
                 state_dir: str | Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.builder = builder
        self.reviewer = reviewer
        self.policy = policy or AutopilotPolicy()
        self.verifier = verifier or Verifier(self.repo)
        self.state = StateStore(state_dir or self.repo / ".autopilot")

    def _git(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(("git", *args), cwd=self.repo, input=input_text, capture_output=True,
                              text=True, timeout=120, shell=False, check=False)

    def _event(self, report: RunReport, kind: str, payload: dict[str, Any]) -> None:
        report.events.append(self.state.append(report.run_id, kind, payload))

    def _apply_patch(self, patch: str) -> tuple[bool, str]:
        checked = self._git("apply", "--check", "--whitespace=error", "-", input_text=patch)
        if checked.returncode:
            return False, (checked.stderr or checked.stdout)[-2000:]
        applied = self._git("apply", "--whitespace=error", "-", input_text=patch)
        return applied.returncode == 0, (applied.stderr or applied.stdout)[-2000:]

    def _changed_files(self) -> list[str]:
        proc = self._git("status", "--porcelain=v1", "--untracked-files=all")
        paths: list[str] = []
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[1]
            paths.append(path.strip('"'))
        return sorted(paths)

    def run(self, goal: Goal) -> RunReport:
        base = self._git("rev-parse", "HEAD").stdout.strip()
        report = RunReport(run_id="run-" + uuid.uuid4().hex[:16], goal_id=goal.goal_id,
                           status=RunStatus.PLANNED, base_commit=base)
        dirty = self._changed_files()
        if dirty:
            report.status = RunStatus.BLOCKED_POLICY
            report.policy_reasons.append("worktree must be clean before an unattended run")
            self._event(report, "POLICY_BLOCK", {"reason": "dirty worktree", "paths": dirty})
            return self._finish(report)
        self._event(report, "RUN_STARTED", {"objective": goal.objective, "constitution":
                                            self.policy.immutable_constitution()})
        feedback: list[dict[str, Any]] = []
        for iteration in range(1, goal.max_iterations + 1):
            report.iteration = iteration
            report.status = RunStatus.BUILDING
            task = json.dumps({"objective": goal.objective, "success_criteria": goal.success_criteria,
                               "allowed_paths": goal.allowed_paths, "previous_findings": feedback,
                               "base_commit": base, "current_diff": self._git("diff", "--no-ext-diff").stdout},
                              default=str)
            try:
                proposal = self.builder.complete(role="builder", task=task, schema=PATCH_SCHEMA)
            except ProviderUnavailable as exc:
                report.status = RunStatus.BLOCKED_AUTH
                self._event(report, "PROVIDER_UNAVAILABLE", {"provider": "builder", "reason": str(exc)})
                return self._finish(report)

            declared = [str(p) for p in proposal.get("changed_files", [])]
            decisions = self.policy.authorize_files(declared, goal_paths=goal.allowed_paths)
            denied = [f"{p}: {d.reason}" for p, d in zip(declared, decisions) if not d.allowed]
            if denied:
                report.status = RunStatus.BLOCKED_POLICY
                report.policy_reasons.extend(denied)
                self._event(report, "POLICY_BLOCK", {"reasons": denied})
                return self._finish(report)
            ok, detail = self._apply_patch(str(proposal.get("patch", "")))
            if not ok:
                feedback = [{"severity": "P1", "title": "Patch rejected", "detail": detail}]
                self._event(report, "PATCH_REJECTED", {"iteration": iteration, "detail": detail})
                continue

            actual = self._changed_files()
            actual_decisions = self.policy.authorize_files(actual, goal_paths=goal.allowed_paths)
            actual_denied = [f"{p}: {d.reason}" for p, d in zip(actual, actual_decisions) if not d.allowed]
            if actual_denied:
                self._git("apply", "-R", "-", input_text=str(proposal.get("patch", "")))
                report.status = RunStatus.BLOCKED_POLICY
                report.policy_reasons.extend(actual_denied)
                self._event(report, "POLICY_BLOCK", {"reasons": actual_denied})
                return self._finish(report)

            report.changed_files = actual
            report.status = RunStatus.VERIFYING
            report.checks = self.verifier.run()
            if not all(c.passed for c in report.checks):
                feedback = [{"severity": "P1", "title": f"Check failed: {c.name}",
                             "detail": c.output_tail} for c in report.checks if not c.passed]
                self._event(report, "CHECKS_FAILED", {"iteration": iteration,
                                                      "checks": [asdict(c) for c in report.checks]})
                continue

            report.status = RunStatus.REVIEWING
            diff = self._git("diff", "--no-ext-diff", "--unified=80").stdout
            review_task = json.dumps({"goal": task, "diff": diff, "check_results":
                                      [asdict(c) for c in report.checks]}, default=str)
            try:
                review = self.reviewer.complete(role="independent_reviewer", task=review_task,
                                                schema=REVIEW_SCHEMA)
            except ProviderUnavailable as exc:
                report.status = RunStatus.BLOCKED_AUTH
                self._event(report, "PROVIDER_UNAVAILABLE", {"provider": "reviewer", "reason": str(exc)})
                return self._finish(report)
            findings = [Finding(severity=str(f.get("severity", "P2")), title=str(f.get("title", "Finding")),
                                detail=str(f.get("detail", "")), file=f.get("file"))
                        for f in review.get("findings", [])]
            report.findings = findings
            if bool(review.get("approved")) and not any(f.severity in {"P0", "P1"} for f in findings):
                report.status = RunStatus.COMPLETED
                self._event(report, "RUN_COMPLETED", {"iteration": iteration, "changed_files": actual})
                return self._finish(report)
            feedback = [asdict(f) for f in findings]
            self._event(report, "REVIEW_REJECTED", {"iteration": iteration, "findings": feedback})

        report.status = RunStatus.FAILED
        self._event(report, "ITERATION_LIMIT", {"max_iterations": goal.max_iterations})
        return self._finish(report)

    def _finish(self, report: RunReport) -> RunReport:
        report.finalize()
        self.state.snapshot(report.run_id, report.to_dict())
        return report
