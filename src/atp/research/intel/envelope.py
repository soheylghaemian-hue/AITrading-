"""§ R3.1A — deterministic canonical serialization + checksums for immutable intel snapshots.

`inputs_checksum` fingerprints the exact canonical input envelope (component-ordered, provenance included);
`snapshot_checksum` fingerprints the decision core + inputs_checksum. Both are pure and stable, so a
re-derivation from the persisted rows reproduces them exactly. No trading, no I/O.
"""
from __future__ import annotations

import hashlib
import json

# The provenance-bearing fields of one input row, in a fixed order, that define its canonical identity.
_INPUT_FIELDS = ("component_name", "canonical_value_json", "component_score", "component_status",
                 "source_provider", "source_event_ts", "source_published_or_filed_ts", "source_observed_ts",
                 "source_available_ts", "provenance_status", "missing_data_reason", "freshness_state")


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def inputs_checksum(inputs: list[dict]) -> str:
    h = hashlib.sha256()
    for c in sorted(inputs, key=lambda x: x.get("component_name") or ""):
        h.update(canonical_json({k: c.get(k) for k in _INPUT_FIELDS}).encode())
    return "sha256:" + h.hexdigest()


def snapshot_checksum(core: dict, inputs_ck: str) -> str:
    """`core` = the decision-defining snapshot fields (symbol, session, policies, consensus, governance,
    decision price provenance, expected contract). The checksum binds the decision to its exact inputs."""
    payload = canonical_json({"core": core, "inputs_checksum": inputs_ck})
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
