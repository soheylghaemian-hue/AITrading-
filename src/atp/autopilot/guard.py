"""Fail-closed validation for model-authored candidate patches."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .full_file import FullFileViolation, validate_edit_output
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
    try:
        phase = payload.get("phase") if isinstance(payload, dict) else None
        normalized = validate_edit_output(payload, phase)
    except FullFileViolation as exc:
        raise GuardViolation(str(exc)) from exc
    paths = [edit["path"] for edit in normalized["edits"]]
    decisions = (policy or AutopilotPolicy()).authorize_files(paths, goal_paths=goal.allowed_paths)
    denied = [f"{path}: {decision.reason}" for path, decision in zip(paths, decisions) if not decision.allowed]
    if denied:
        raise GuardViolation("; ".join(denied))
    return sorted(paths)


def validate_canonical_patch(path: Path) -> None:
    """Reject unsafe Git output before its bytes are hashed or published."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GuardViolation("canonical candidate patch is missing or unreadable") from exc
    if not raw.startswith(b"diff --git "):
        raise GuardViolation("canonical candidate patch must be a non-empty Git patch")
    if len(raw) > MAX_PATCH_BYTES:
        raise GuardViolation("canonical candidate patch exceeds the byte limit")
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise GuardViolation("canonical candidate patch must be UTF-8 text") from exc
    if any(line in {"GIT binary patch"} or line.startswith("Binary files ") for line in lines):
        raise GuardViolation("binary candidate patches are forbidden")
    if any(line.startswith("deleted file mode ") for line in lines):
        raise GuardViolation("file deletions are forbidden")
    if any(line.startswith(("rename from ", "rename to ", "similarity index ")) for line in lines):
        raise GuardViolation("rename candidate patches are forbidden")
    mode_headers = ("new file mode ", "old mode ", "new mode ")
    for line in lines:
        if line.startswith(mode_headers) and not line.endswith("100644"):
            raise GuardViolation("executable, symlink and submodule modes are forbidden")


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
    parser.add_argument("--canonical-patch")
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
            if args.canonical_patch:
                raise GuardViolation("--declared-only cannot validate a canonical patch")
            print(json.dumps({"allowed": True, "author": "claude",
                              "changed_files": declared}, sort_keys=True))
            return 0
        actual = validate_worktree(repo, goal)
        if declared is not None and declared != actual:
            raise GuardViolation(f"declared files differ from actual files: {declared!r} != {actual!r}")
        if args.canonical_patch:
            validate_canonical_patch(Path(args.canonical_patch))
        print(json.dumps({"allowed": True, "author": "claude", "changed_files": actual}, sort_keys=True))
        return 0
    except (GuardViolation, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
