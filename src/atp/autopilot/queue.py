"""Select the next bounded goal for the unattended workbench."""
from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

from .models import Goal

GOAL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class QueueError(ValueError):
    pass


class GoalViolation(ValueError):
    """Raised when trusted goal control data is malformed."""


MAX_GOAL_BYTES = 16_384
GOAL_KEYS = frozenset(
    {"goal_id", "objective", "success_criteria", "allowed_paths", "max_iterations"}
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoalViolation("goal JSON contains a duplicate object key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise GoalViolation("goal JSON contains a non-finite number")


def load_goal(path: Path) -> Goal:
    """Load one exact-schema goal from a regular control-plane file."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GoalViolation("goal must be a readable regular file") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise GoalViolation("goal must be a non-symlink regular file")
    if metadata.st_size > MAX_GOAL_BYTES:
        raise GoalViolation("goal exceeds the size limit")
    raw = path.read_bytes()
    if len(raw) > MAX_GOAL_BYTES or raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise GoalViolation("goal must be bounded plain UTF-8 JSON")
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoalViolation("goal is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != GOAL_KEYS:
        raise GoalViolation("goal does not use the exact trusted schema")
    goal_id = payload["goal_id"]
    objective = payload["objective"]
    criteria = payload["success_criteria"]
    allowed_paths = payload["allowed_paths"]
    iterations = payload["max_iterations"]
    if not isinstance(goal_id, str) or not GOAL_ID.fullmatch(goal_id):
        raise GoalViolation("goal_id must be a safe identifier")
    if not isinstance(objective, str) or not objective.strip():
        raise GoalViolation("goal objective must be a non-empty string")
    if not isinstance(criteria, list) or not criteria or any(
        not isinstance(item, str) or not item.strip() for item in criteria
    ):
        raise GoalViolation("success_criteria must contain non-empty strings")
    if not isinstance(allowed_paths, list) or not allowed_paths or any(
        not isinstance(item, str) or not item for item in allowed_paths
    ):
        raise GoalViolation("allowed_paths must contain non-empty strings")
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise GoalViolation("max_iterations must be an integer")
    try:
        return Goal(
            goal_id=goal_id,
            objective=objective,
            success_criteria=tuple(criteria),
            allowed_paths=tuple(allowed_paths),
            max_iterations=iterations,
        )
    except (TypeError, ValueError) as exc:
        raise GoalViolation("goal values violate the trusted contract") from exc


def select_goal(repo: Path, queue_file: Path, requested: str = "") -> tuple[str, Path] | None:
    queue = json.loads(queue_file.read_text())
    ids = queue.get("goals")
    if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and GOAL_ID.fullmatch(x) for x in ids):
        raise QueueError("queue goals must be a non-empty list of safe identifiers")
    candidates = [requested] if requested else ids
    if requested and (not GOAL_ID.fullmatch(requested) or requested not in ids):
        raise QueueError("requested goal is not in the trusted queue")
    for goal_id in candidates:
        goal_path = repo / "autopilot" / "goals" / f"{goal_id}.json"
        if not goal_path.is_file():
            raise QueueError(f"trusted goal file is missing: {goal_id}")
        if not (repo / "docs" / "autopilot" / "completed" / f"{goal_id}.json").exists():
            return goal_id, goal_path
    return None


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--queue", default="autopilot/queue.json")
    parser.add_argument("--requested", default="")
    parser.add_argument("--github-output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    try:
        repo = Path(args.repo).resolve()
        chosen = select_goal(repo, repo / args.queue, args.requested)
        values = {"has_goal": "true" if chosen else "false"}
        if chosen:
            values.update({"goal_id": chosen[0],
                           "goal_file": str(chosen[1].relative_to(repo))})
        output = "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"
        if args.github_output:
            with Path(args.github_output).open("a") as handle:
                handle.write(output)
        else:
            print(output, end="")
        return 0
    except (QueueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
