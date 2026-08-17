"""§ R3.1A — immutable point-in-time AI/consensus intelligence collection (RESEARCH DATA ONLY).

Forward-only: one canonical snapshot per pilot symbol per completed NYSE session, captured from the exact
consensus computation with a provenance-bearing input envelope; outcomes are pinned to a COMPLETED
immutable research dataset only after maturity. Imports nothing from execution / broker / IBKR / autonomous
/ F2 / kill-switch. Safety: AUTONOMOUS=DISABLED · EXECUTION=DISABLED · IBKR ORDERS=0.
"""
from __future__ import annotations

from . import policy
from .collector import collect_session
from .commit import CommitVerificationError, resolve_commit_sha
from .envelope import canonical_json, inputs_checksum, snapshot_checksum
from .outcomes import classify, evaluate_pending
from .provenance import build_input_envelope

__all__ = [
    "policy", "collect_session", "evaluate_pending", "classify",
    "build_input_envelope", "canonical_json", "inputs_checksum", "snapshot_checksum",
    "resolve_commit_sha", "CommitVerificationError",
]
