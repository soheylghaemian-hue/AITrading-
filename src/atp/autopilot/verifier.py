"""Deterministic verification and evidence capture."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import CheckResult


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 900


DEFAULT_CHECKS = (
    VerificationCommand("backend", ("python3", "-m", "pytest")),
)


class Verifier:
    def __init__(self, repo: str | Path, commands: tuple[VerificationCommand, ...] = DEFAULT_CHECKS) -> None:
        self.repo = Path(repo).resolve()
        self.commands = commands

    def run(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        for check in self.commands:
            try:
                proc = subprocess.run(check.argv, cwd=self.repo, capture_output=True, text=True,
                                      timeout=check.timeout_seconds, shell=False, check=False)
                output = (proc.stdout or "") + (proc.stderr or "")
                code = proc.returncode
            except subprocess.TimeoutExpired as exc:
                output = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + "\nTIMEOUT"
                code = 124
            results.append(CheckResult(
                name=check.name, command=check.argv, passed=code == 0, exit_code=code,
                output_sha256=hashlib.sha256(output.encode()).hexdigest(), output_tail=output[-4000:],
            ))
        return results
