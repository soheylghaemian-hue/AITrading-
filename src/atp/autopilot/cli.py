"""Offline CLI for replaying and verifying an approved response bundle."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import Goal, RunStatus
from .orchestrator import DevelopmentAutopilot
from .policy import AutopilotPolicy
from .providers import ScriptedProvider


def _args(argv):
    parser = argparse.ArgumentParser(prog="gigbay-autopilot")
    parser.add_argument("goal", help="JSON goal file")
    parser.add_argument("responses", help="approved offline builder/reviewer response bundle")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--allow-yellow", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    raw = json.loads(Path(args.goal).read_text())
    bundle = json.loads(Path(args.responses).read_text())
    goal = Goal(goal_id=raw["goal_id"], objective=raw["objective"],
                success_criteria=tuple(raw["success_criteria"]),
                allowed_paths=tuple(raw.get("allowed_paths", ())),
                max_iterations=int(raw.get("max_iterations", 5)))
    runner = DevelopmentAutopilot(
        repo=args.repo,
        builder=ScriptedProvider(list(bundle.get("builder", ()))),
        reviewer=ScriptedProvider(list(bundle.get("reviewer", ()))),
        policy=AutopilotPolicy(allow_yellow=args.allow_yellow),
    )
    report = runner.run(goal)
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.status is RunStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
