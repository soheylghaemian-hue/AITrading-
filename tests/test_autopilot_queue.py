import json
from pathlib import Path

import pytest

from atp.autopilot.queue import QueueError, select_goal


def _queue(tmp_path: Path):
    (tmp_path / "autopilot/goals").mkdir(parents=True)
    (tmp_path / "autopilot/queue.json").write_text(json.dumps({"goals": ["sense-v1", "think-v1"]}))
    for goal in ("sense-v1", "think-v1"):
        (tmp_path / f"autopilot/goals/{goal}.json").write_text("{}")


def test_selects_first_incomplete_goal(tmp_path):
    _queue(tmp_path)
    assert select_goal(tmp_path, tmp_path / "autopilot/queue.json")[0] == "sense-v1"
    marker = tmp_path / "docs/autopilot/completed/sense-v1.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    assert select_goal(tmp_path, tmp_path / "autopilot/queue.json")[0] == "think-v1"


def test_requested_goal_must_be_trusted(tmp_path):
    _queue(tmp_path)
    with pytest.raises(QueueError):
        select_goal(tmp_path, tmp_path / "autopilot/queue.json", "../unsafe")
    with pytest.raises(QueueError):
        select_goal(tmp_path, tmp_path / "autopilot/queue.json", "not-queued")
