"""§ R3.0A.1 / R3.0A.2 — durable one-shot research backfill worker (systemd-friendly). NOT atp-control.

Runs OUTSIDE the Control/Observability API in its own process with its own DB connection. ONE invocation
reclaims crashed/stale RUNNING datasets (bounded, atomic) and then processes EXACTLY ONE explicitly-selected
PLANNED dataset with bounded, session-aligned chunking, writing COMPLETED/FAILED deterministically and
leaving terminal datasets immutable. It NEVER drains the PLANNED queue — every real backfill stays a
separate, explicit approval. No in-process background thread, no FastAPI BackgroundTasks.

Invocation (systemd one-shot / timer or cron) — the dataset is chosen explicitly:
    ATP_STORE_URL=postgres://…  ATP_BACKFILL_ENABLED=1  MASSIVE_API_KEY=…  \
        python -m atp.research.backfill.worker --dataset-id <dataset_id>
    # or, opt-in, hard-capped to the single oldest PLANNED dataset:
        python -m atp.research.backfill.worker --next

Exit-code contract:
    0  → dataset COMPLETED, OR a disabled/missing-key no-op (skipped=true), OR --next with nothing PLANNED.
    1  → claim conflict / unknown / not-PLANNED dataset, a FAILED result, or an unexpected worker error.
    2  → usage error (no store URL, or neither --dataset-id nor --next).

The real provider is DOUBLE-gated: without ATP_BACKFILL_ENABLED=1 AND MASSIVE_API_KEY the worker is a no-op
(PLANNED datasets are left untouched). The API key is read from the environment only and is NEVER printed;
the store URL (which may embed a DB password) is never printed either. Output is bounded operational
metadata only. This module imports nothing from the execution/broker/IBKR/autonomous/F2 path and never
touches live `ohlc_bars`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ...store import open_store
from .provider import PolygonAggregatesProvider
from .runner import claim_next_one, process_one, reclaim_stale


def build_provider_from_env():
    """Return (provider, None) when the real backfill is explicitly enabled + credentialed, else (None,
    reason). The key is read from the environment only and never returned in the reason string."""
    if os.environ.get("ATP_BACKFILL_ENABLED") != "1":
        return None, "ATP_BACKFILL_ENABLED != 1 (real backfill disabled)"
    if not os.environ.get("MASSIVE_API_KEY"):
        return None, "MASSIVE_API_KEY not set"
    return PolygonAggregatesProvider(os.environ["MASSIVE_API_KEY"]), None


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="atp.research.backfill.worker",
                                description="Process exactly one PLANNED research dataset (never a backlog).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dataset-id", dest="dataset_id", default=None,
                   help="claim and process ONLY this PLANNED dataset")
    g.add_argument("--next", dest="next", action="store_true",
                   help="process the single oldest PLANNED dataset (opt-in; hard-capped to one)")
    return p.parse_args(argv)


def main(argv=None, *, provider=None) -> int:
    """`provider` is a test-only injection seam (a mock aggregates provider); production always resolves the
    real, env-gated provider so no live/paid request can occur without ATP_BACKFILL_ENABLED + MASSIVE_API_KEY."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    store_url = os.environ.get("ATP_STORE_URL") or os.environ.get("DATABASE_URL")
    if not store_url:
        print(json.dumps({"ok": False, "reason": "no store url (set ATP_STORE_URL / DATABASE_URL)"}))
        return 2
    if not args.dataset_id and not args.next:
        print(json.dumps({"ok": False, "reason": "specify --dataset-id <id> or --next (no implicit backlog)"}))
        return 2

    if provider is None:
        provider, reason = build_provider_from_env()
        if provider is None:
            print(json.dumps({"ok": True, "skipped": True, "reason": reason}))   # no-op, leaves PLANNED intact
            return 0

    store = open_store(store_url, migrate=False)   # never runs migrations from the worker
    reclaimed = reclaim_stale(store)               # bounded, atomic stale recovery

    try:
        res = process_one(store, args.dataset_id, provider) if args.dataset_id else claim_next_one(store, provider)
    except Exception as e:  # noqa: BLE001 — surface as a non-zero exit with a bounded, credential-free message
        print(json.dumps({"ok": False, "reclaimed": reclaimed, "error": type(e).__name__}))
        return 1

    if res is None:   # --next with an empty PLANNED queue
        print(json.dumps({"ok": True, "reclaimed": reclaimed, "processed": None, "reason": "no PLANNED dataset"}))
        return 0

    status = res.get("status")
    out = {"ok": status == "COMPLETED", "reclaimed": reclaimed, "dataset_id": res.get("dataset_id"),
           "status": status, "failure_code": res.get("failure_code"), "error_code": res.get("error_code")}
    print(json.dumps(out))
    return 0 if status == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
