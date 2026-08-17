"""Select the next bounded goal for the unattended workbench."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GOAL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class QueueError(ValueError):
    pass


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
