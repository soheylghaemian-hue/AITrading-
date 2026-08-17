"""§ R3.1A — exact deployed-commit provenance (fail-closed).

Production is a Git checkout under /opt/atp/app managed by systemd (not a container image). Every snapshot,
outcome and validation run must record an EXACT 40-hex commit SHA. `resolve_commit_sha` requires
`ATP_COMMIT_REF` to be a 40-hex string and, when the Git checkout is reachable, requires it to equal the
checkout's actual HEAD. A missing / malformed / stale-mismatched value raises `CommitVerificationError`
(fail closed) — it NEVER silently returns null, a short SHA, or an unverified value. Read-only; no trading.
"""
from __future__ import annotations

import os
import re
import subprocess

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_REPO_DIR = "/opt/atp/app"        # the documented production Git/systemd checkout


class CommitVerificationError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _git_head(repo_dir: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (out.stdout or "").strip()
    return sha if out.returncode == 0 and sha else None


def resolve_commit_sha(*, env: dict | None = None, repo_dir: str | None = None,
                       head_sha: str | None = None) -> str:
    """Return the verified 40-hex deployed commit SHA, or raise CommitVerificationError (fail closed). The Git
    HEAD comparison is NEVER silently skipped: unless a test injects `head_sha`, HEAD is read from
    `repo_dir` → `ATP_REPO_DIR` → the documented `/opt/atp/app` checkout, and an unresolvable/unreadable repo,
    an invalid HEAD, a missing ref or a stale mismatch ALL fail closed before any row is written."""
    env = os.environ if env is None else env
    ref = (env.get("ATP_COMMIT_REF") or "").strip()
    if not ref:
        raise CommitVerificationError("ATP_COMMIT_REF is not set", code="COMMIT_REF_MISSING")
    if not _SHA_RE.match(ref):
        raise CommitVerificationError("ATP_COMMIT_REF is not a 40-hex commit SHA", code="COMMIT_REF_MALFORMED")
    if head_sha is None:                                 # production: MUST resolve + read HEAD (no skip)
        rd = repo_dir or env.get("ATP_REPO_DIR") or DEFAULT_REPO_DIR
        head_sha = _git_head(rd)
        if head_sha is None:
            raise CommitVerificationError(f"could not read git HEAD from repo dir '{rd}'",
                                          code="COMMIT_HEAD_UNREADABLE")
    if not _SHA_RE.match(head_sha):
        raise CommitVerificationError("git HEAD is not a 40-hex SHA", code="COMMIT_HEAD_MALFORMED")
    if head_sha != ref:
        raise CommitVerificationError("ATP_COMMIT_REF does not match the checked-out HEAD (stale deploy)",
                                      code="COMMIT_REF_STALE")
    return ref
