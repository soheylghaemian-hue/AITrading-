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
    return sha if out.returncode == 0 and _SHA_RE.match(sha) else None


def resolve_commit_sha(*, env: dict | None = None, repo_dir: str | None = None,
                       head_sha: str | None = None) -> str:
    """Return the verified 40-hex deployed commit SHA, or raise CommitVerificationError. `head_sha` (or a
    resolvable `repo_dir`) is compared against `ATP_COMMIT_REF`; a mismatch is a STALE deploy and fails
    closed. Tests may inject `env`/`head_sha`; production passes neither and reads the real environment +
    the /opt/atp/app checkout."""
    env = os.environ if env is None else env
    ref = (env.get("ATP_COMMIT_REF") or "").strip()
    if not ref:
        raise CommitVerificationError("ATP_COMMIT_REF is not set", code="COMMIT_REF_MISSING")
    if not _SHA_RE.match(ref):
        raise CommitVerificationError("ATP_COMMIT_REF is not a 40-hex commit SHA",
                                      code="COMMIT_REF_MALFORMED")
    head = head_sha if head_sha is not None else (_git_head(repo_dir) if repo_dir else None)
    if head is not None:
        if not _SHA_RE.match(head):
            raise CommitVerificationError("git HEAD is not a 40-hex SHA", code="COMMIT_HEAD_MALFORMED")
        if head != ref:
            raise CommitVerificationError("ATP_COMMIT_REF does not match the checked-out HEAD (stale deploy)",
                                          code="COMMIT_REF_STALE")
    return ref
