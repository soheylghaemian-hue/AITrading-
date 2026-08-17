"""§ R3.0A.1 — durable one-shot research backfill worker (systemd-friendly). NOT atp-control.

Runs OUTSIDE the Control/Observability API in its own process with its own DB connection. One invocation:
reclaims crashed/stale RUNNING datasets to FAILED, then claims and executes every currently-PLANNED dataset
with bounded, session-aligned chunking, writing COMPLETED/FAILED deterministically and leaving terminal
datasets immutable. It uses NO in-process background thread and NO FastAPI BackgroundTasks — the control
API never blocks on provider I/O.

Invocation (systemd one-shot / timer or cron):
    ATP_STORE_URL=postgres://…  ATP_BACKFILL_ENABLED=1  MASSIVE_API_KEY=…  \
        python -m atp.research.backfill.worker

The real provider is DOUBLE-gated: without ATP_BACKFILL_ENABLED=1 AND MASSIVE_API_KEY the worker is a no-op
(PLANNED datasets are left untouched for a later, approved run). The API key is read from the environment
only and is NEVER printed to stdout/logs. This module imports nothing from the execution/broker/IBKR/
autonomous/F2 path and never touches live `ohlc_bars`.
"""
from __future__ import annotations

import json
import os
import sys

from ...store import open_store
from .provider import PolygonAggregatesProvider
from .runner import claim_and_run


def build_provider_from_env():
    """Return (provider, None) when the real backfill is explicitly enabled + credentialed, else (None,
    reason). The key is read from the environment only and never returned in the reason string."""
    if os.environ.get("ATP_BACKFILL_ENABLED") != "1":
        return None, "ATP_BACKFILL_ENABLED != 1 (real backfill disabled)"
    if not os.environ.get("MASSIVE_API_KEY"):
        return None, "MASSIVE_API_KEY not set"
    return PolygonAggregatesProvider(os.environ["MASSIVE_API_KEY"]), None


def main(argv=None) -> int:
    store_url = os.environ.get("ATP_STORE_URL") or os.environ.get("DATABASE_URL")
    if not store_url:
        print(json.dumps({"ok": False, "reason": "no store url (set ATP_STORE_URL / DATABASE_URL)"}))
        return 2
    provider, reason = build_provider_from_env()
    if provider is None:
        print(json.dumps({"ok": True, "skipped": True, "reason": reason}))   # no-op, leaves PLANNED intact
        return 0
    store = open_store(store_url, migrate=False)   # never runs migrations from the worker
    out = claim_and_run(store, provider)
    print(json.dumps({"ok": True, "reclaimed": out["reclaimed"], "processed": out["processed"],
                      "results": [{"dataset_id": r["dataset_id"], "status": r["status"],
                                   "failure_code": r.get("failure_code")} for r in out["results"]]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
