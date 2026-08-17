"""Fail-closed validation for model-authored candidate patches."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .models import Goal
from .policy import AutopilotPolicy

MAX_PATCH_BYTES = 400_000
MAX_CHANGED_FILES = 80


class GuardViolation(ValueError):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=repo, capture_output=True, text=True,
                          timeout=30, shell=False, check=False)
    if proc.returncode:
        raise GuardViolation(f"git inspection failed: {(proc.stderr or proc.stdout)[-500:]}")
    return proc.stdout


def changed_files(repo: Path) -> list[str]:
    """Return modified, deleted and untracked paths without executing model content."""
    listed = _git(repo, "ls-files", "--modified", "--others", "--exclude-standard").splitlines()
    deleted = _git(repo, "diff", "--name-only", "--diff-filter=D", "HEAD").splitlines()
    return sorted(set(listed + deleted))


def load_goal(path: Path) -> Goal:
    raw = json.loads(path.read_text())
    return Goal(
        goal_id=str(raw["goal_id"]),
        objective=str(raw["objective"]),
        success_criteria=tuple(str(x) for x in raw["success_criteria"]),
        allowed_paths=tuple(str(x) for x in raw.get("allowed_paths", ())),
        max_iterations=int(raw.get("max_iterations", 2)),
    )


def validate_declared(payload: dict[str, Any], goal: Goal,
                      policy: AutopilotPolicy | None = None) -> list[str]:
    if payload.get("author") != "claude":
        raise GuardViolation("only Claude may author a candidate patch")
    patch = payload.get("patch")
    paths = payload.get("changed_files")
    if not isinstance(patch, str) or not patch.startswith("diff --git "):
        raise GuardViolation("Claude must return a non-empty unified git patch")
    if len(patch.encode()) > MAX_PATCH_BYTES:
        raise GuardViolation("candidate patch exceeds the byte limit")
    if any(token in patch for token in ("GIT binary patch", "mode 100755", "mode 160000")):
        raise GuardViolation("binary, executable and submodule patches are forbidden")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise GuardViolation("changed_files must be a non-empty string list")
    if len(paths) > MAX_CHANGED_FILES or len(set(paths)) != len(paths):
        raise GuardViolation("changed_files is oversized or contains duplicates")
    decisions = (policy or AutopilotPolicy()).authorize_files(paths, goal_paths=goal.allowed_paths)
    denied = [f"{path}: {decision.reason}" for path, decision in zip(paths, decisions) if not decision.allowed]
    if denied:
        raise GuardViolation("; ".join(denied))
    return sorted(paths)


def validate_worktree(repo: Path, goal: Goal,
                      policy: AutopilotPolicy | None = None) -> list[str]:
    paths = changed_files(repo)
    if not paths:
        raise GuardViolation("candidate produced no changed files")
    if len(paths) > MAX_CHANGED_FILES:
        raise GuardViolation("candidate changes too many files")
    decisions = (policy or AutopilotPolicy()).authorize_files(paths, goal_paths=goal.allowed_paths)
    denied = [f"{path}: {decision.reason}" for path, decision in zip(paths, decisions) if not decision.allowed]
    for path in paths:
        target = repo / path
        if target.is_symlink():
            denied.append(f"{path}: symlinks are forbidden")
    if denied:
        raise GuardViolation("; ".join(denied))
    return paths


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gigbay-autopilot-guard")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--declared-json")
    parser.add_argument("--declared-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    try:
        repo = Path(args.repo).resolve()
        goal = load_goal(Path(args.goal))
        declared = None
        if args.declared_json:
            payload = json.loads(Path(args.declared_json).read_text())
            declared = validate_declared(payload, goal)
        if args.declared_only:
            if declared is None:
                raise GuardViolation("--declared-only requires --declared-json")
            print(json.dumps({"allowed": True, "author": "claude",
                              "changed_files": declared}, sort_keys=True))
            return 0
        actual = validate_worktree(repo, goal)
        if declared is not None and declared != actual:
            raise GuardViolation(f"declared files differ from actual files: {declared!r} != {actual!r}")
        print(json.dumps({"allowed": True, "author": "claude", "changed_files": actual}, sort_keys=True))
        return 0
    except (GuardViolation, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
