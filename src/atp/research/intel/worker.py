"""§ R3.1A — one-shot forward-only intelligence collection/evaluation worker (systemd-friendly).

    python -m atp.research.intel.worker collect     # snapshot the eligible just-completed session
    python -m atp.research.intel.worker evaluate     # mature pending outcomes against COMPLETED datasets

Runs OUTSIDE atp-control in its own process/DB connection. It derives the session itself from the verified
clock (NO CLI flag can target an arbitrary historical date → production backdating is impossible), verifies
the deployed commit fail-closed, does bounded idempotent work, and never trades / enqueues a backfill /
touches live `ohlc_bars`. Exit codes: 0 success/no-op; 1 fail-closed provenance error or unexpected error;
2 usage error. Secrets (store URL) are never printed. The `_now`/`_commit_sha` kwargs are a TEST seam only —
argparse never sets them, so the CLI cannot backdate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from ...store import open_store
from .collector import collect_session
from .commit import CommitVerificationError, resolve_commit_sha
from .outcomes import evaluate_pending


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="atp.research.intel.worker",
                                description="Forward-only immutable AI-validation collection (research only).")
    p.add_argument("command", choices=["collect", "evaluate"], help="collect a session, or evaluate outcomes")
    return p.parse_args(argv)


def main(argv=None, *, _now: datetime | None = None, _commit_sha: str | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    store_url = os.environ.get("ATP_STORE_URL") or os.environ.get("DATABASE_URL")
    if not store_url:
        print(json.dumps({"ok": False, "reason": "no store url (set ATP_STORE_URL / DATABASE_URL)"}))
        return 2

    now = _now or datetime.now(timezone.utc)
    try:
        commit_sha = _commit_sha or resolve_commit_sha(repo_dir=os.environ.get("ATP_REPO_DIR"))
    except CommitVerificationError as e:
        print(json.dumps({"ok": False, "error": "COMMIT_UNVERIFIED", "code": e.code}))
        return 1

    store = open_store(store_url, migrate=False)   # the worker never runs migrations
    try:
        if args.command == "collect":
            r = collect_session(store, now=now, commit_sha=commit_sha)
            print(json.dumps({"ok": True, "command": "collect", "eligible": r["eligible"],
                              "session_date": r.get("session_date"), "written": r["written"],
                              "already_collected": r.get("already_collected", []), "skipped": r["skipped"],
                              "reason": r.get("reason")}))
            return 0
        r = evaluate_pending(store, now=now, commit_sha=commit_sha)
        print(json.dumps({"ok": True, "command": "evaluate", "matured": r["matured_count"],
                          "failed": r["failed_count"], "pending": r["pending"],
                          "dataset_pending": r["dataset_pending"]}))
        return 0
    except Exception as e:  # noqa: BLE001 — surface as non-zero without leaking secrets
        print(json.dumps({"ok": False, "command": args.command, "error": type(e).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
