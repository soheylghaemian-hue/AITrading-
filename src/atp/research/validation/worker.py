"""§ R3.1A — one-shot validation runner worker (systemd-friendly, research only).

    python -m atp.research.validation.worker run

Freezes the current matured-outcome set and writes an immutable validation run (COMPLETED only if the
preregistered gate passes, else INSUFFICIENT — never a fabricated result). Verifies the deployed commit
fail-closed. Runs OUTSIDE atp-control with its own DB connection; never trades / touches live `ohlc_bars`.
Exit codes: 0 success; 1 fail-closed provenance error or unexpected error; 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from ...store import open_store
from ..intel.commit import CommitVerificationError, resolve_commit_sha
from .runner import run_validation


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="atp.research.validation.worker",
                                description="Deterministic AI-validation run (research only).")
    p.add_argument("command", choices=["run"], help="freeze the matured set and write an immutable run")
    return p.parse_args(argv)


def main(argv=None, *, _now: datetime | None = None, _commit_sha: str | None = None) -> int:
    _parse_args(argv if argv is not None else sys.argv[1:])
    store_url = os.environ.get("ATP_STORE_URL") or os.environ.get("DATABASE_URL")
    if not store_url:
        print(json.dumps({"ok": False, "reason": "no store url (set ATP_STORE_URL / DATABASE_URL)"}))
        return 2
    try:
        commit_sha = _commit_sha or resolve_commit_sha(repo_dir=os.environ.get("ATP_REPO_DIR"))
    except CommitVerificationError as e:
        print(json.dumps({"ok": False, "error": "COMMIT_UNVERIFIED", "code": e.code}))
        return 1
    store = open_store(store_url, migrate=False)
    try:
        r = run_validation(store, commit_sha=commit_sha, now=_now or datetime.now(timezone.utc))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": type(e).__name__}))
        return 1
    print(json.dumps({"ok": True, "run_id": r["run_id"], "status": r["status"],
                      "gate_passed": r["gate_passed"], "result_checksum": r["result_checksum"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
