"""Append-only local event journal with atomic snapshots."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"ts": datetime.now(UTC).isoformat(), "type": event_type, "payload": payload}
        path = self.root / f"{run_id}.events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":"), default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def snapshot(self, run_id: str, value: dict[str, Any]) -> Path:
        target = self.root / f"{run_id}.report.json"
        fd, name = tempfile.mkstemp(prefix=target.name + ".", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, indent=2, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return target
