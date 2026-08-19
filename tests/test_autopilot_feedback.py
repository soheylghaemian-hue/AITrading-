from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from atp.autopilot.feedback import (
    FEEDBACK_KIND,
    MAX_FEEDBACK_BYTES,
    MAX_FINDINGS,
    MAX_SOURCES_PER_FINDING,
    OUTPUT_RELATIVE,
    FeedbackViolation,
    canonical_feedback_bytes,
    load_feedback,
    main,
    materialize_feedback,
    validate_feedback,
)
from atp.autopilot.policy import AutopilotPolicy
from atp.autopilot.queue import load_goal

PROJECT = Path(__file__).parents[1]
GOAL_PATH = PROJECT / "autopilot/goals/trader-brain-think-v1.json"
FEEDBACK_PATH = (
    PROJECT
    / ".github/autopilot/feedback/trader-brain-think-v1.json"
)
PROVE_GOAL_PATH = PROJECT / "autopilot/goals/trader-brain-prove-v1.json"
PROVE_FEEDBACK_PATH = (
    PROJECT
    / ".github/autopilot/feedback/trader-brain-prove-v1.json"
)
SCHEMA_PATH = PROJECT / ".github/autopilot/schemas/review-feedback.schema.json"
GOAL_RELATIVE = "autopilot/goals/trader-brain-think-v1.json"
GOAL_ID = "trader-brain-think-v1"
BASE_SHA = "0c44df3e00e452cab1d72e2796f90895f3b89734"


def _goal():
    return load_goal(GOAL_PATH)


def _payload() -> dict:
    return json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))


def _finding(**changes) -> dict:
    finding = {
        "id": "one-finding",
        "severity": "P1",
        "title": "One blocking finding",
        "detail": "This is bounded, passive review evidence.",
        "location": {"path": "src/atp/brain/think.py", "line": 1},
        "sources": [
            {
                "run_id": 123,
                "job_id": 456,
                "stage": "final_review",
                "base_sha": BASE_SHA,
            }
        ],
    }
    finding.update(changes)
    return finding


def _minimal_payload(**changes) -> dict:
    payload = {
        "schema_version": 1,
        "kind": FEEDBACK_KIND,
        "goal_id": GOAL_ID,
        "findings": [_finding()],
    }
    payload.update(changes)
    return payload


def _copy_control(tmp_path: Path, *, include_feedback: bool = True) -> Path:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    shutil.copytree(PROJECT / "autopilot", control / "autopilot")
    if include_feedback:
        destination = control / ".github/autopilot/feedback"
        destination.mkdir(parents=True)
        shutil.copy2(FEEDBACK_PATH, destination / FEEDBACK_PATH.name)
    return control


def _copy_candidate(tmp_path: Path) -> Path:
    repo = tmp_path / "candidate"
    destination = repo / GOAL_RELATIVE
    destination.parent.mkdir(parents=True)
    shutil.copy2(GOAL_PATH, destination)
    return repo


def _write_feedback(path: Path, payload: dict | None = None, *, raw: bytes | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if raw is not None else json.dumps(payload or _minimal_payload()).encode())
    return path


def test_static_feedback_captures_all_unique_prior_p1_findings() -> None:
    normalized = load_feedback(FEEDBACK_PATH, _goal())
    assert normalized["schema_version"] == 1
    assert normalized["kind"] == FEEDBACK_KIND
    assert normalized["goal_id"] == GOAL_ID
    assert {finding["id"] for finding in normalized["findings"]} == {
        "future-stale-sense-admission",
        "malformed-invalidation-identifiers",
        "mutable-calibration-weights",
        "timezone-fold-future-leakage",
    }
    assert {finding["severity"] for finding in normalized["findings"]} == {"P1"}


def test_repeated_calibration_reports_remain_one_finding_with_distinct_sources() -> None:
    findings = {finding["id"]: finding for finding in load_feedback(FEEDBACK_PATH, _goal())["findings"]}
    mutable = findings["mutable-calibration-weights"]
    assert [(source["run_id"], source["job_id"]) for source in mutable["sources"]] == [
        (32111724638, 95641192568),
        (32120821519, 95667903995),
        (32128048410, 95821078096),
    ]
    assert sum(
        source["job_id"] == 95667903995
        for finding in findings.values()
        for source in finding["sources"]
    ) == 1
    assert sum(
        source["job_id"] == 95821078096
        for finding in findings.values()
        for source in finding["sources"]
    ) == 1


def test_prove_feedback_records_the_exact_failed_gates_without_scope_expansion() -> None:
    goal = load_goal(PROVE_GOAL_PATH)
    normalized = load_feedback(PROVE_FEEDBACK_PATH, goal)
    assert normalized["goal_id"] == "trader-brain-prove-v1"
    assert {finding["id"] for finding in normalized["findings"]} == {
        "abutting-window-fixture-reference",
        "malformed-none-helper-substitution",
        "malformed-window-constructor-fixture",
        "randomness-lexical-false-positive",
    }
    assert {finding["severity"] for finding in normalized["findings"]} == {"P1"}
    assert {
        (
            source["run_id"],
            source["job_id"],
            source["stage"],
            source["base_sha"],
        )
        for finding in normalized["findings"]
        for source in finding["sources"]
    } == {
        (
            32229557214,
            96002679156,
            "gate",
            "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        ),
        (
            32274484539,
            96144478054,
            "gate",
            "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        ),
    }
    assert {
        finding["location"]["path"] for finding in normalized["findings"]
    } == {"tests/test_brain_prove.py"}


def test_schema_is_strict_and_matches_validator_bounds() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["kind"]["const"] == FEEDBACK_KIND
    assert schema["properties"]["findings"]["maxItems"] == MAX_FINDINGS
    assert schema["$defs"]["finding"]["additionalProperties"] is False
    assert schema["$defs"]["source"]["additionalProperties"] is False
    assert schema["$defs"]["source"]["properties"]["stage"]["enum"] == [
        "gate",
        "initial_review",
        "final_review",
    ]
    assert schema["$defs"]["finding"]["properties"]["sources"]["maxItems"] == MAX_SOURCES_PER_FINDING


@pytest.mark.parametrize("severity", ["P2", "P3", "", 1, None])
def test_only_p0_and_p1_are_accepted(severity) -> None:
    with pytest.raises(FeedbackViolation, match="P0/P1"):
        validate_feedback(_minimal_payload(findings=[_finding(severity=severity)]), _goal())


@pytest.mark.parametrize("key", ["allowed_paths", "tools", "instructions", "patch", "content"])
def test_top_level_scope_or_instruction_fields_are_rejected(key: str) -> None:
    payload = _minimal_payload()
    payload[key] = ["src/"]
    with pytest.raises(FeedbackViolation, match="top-level schema"):
        validate_feedback(payload, _goal())


@pytest.mark.parametrize("key", ["allowed_paths", "tools", "instructions", "patch", "content"])
def test_finding_scope_or_instruction_fields_are_rejected(key: str) -> None:
    finding = _finding()
    finding[key] = "forbidden"
    with pytest.raises(FeedbackViolation, match="finding must use"):
        validate_feedback(_minimal_payload(findings=[finding]), _goal())


@pytest.mark.parametrize(
    "path",
    [
        "/src/atp/brain/think.py",
        "../src/atp/brain/think.py",
        "src/atp/brain/../execution/live.py",
        "src/atp/brain//think.py",
        "src\\atp\\brain\\think.py",
        "src/atp/execution/live.py",
        "infra/systemd/atp.service",
        ".github/workflows/autopilot.yml",
    ],
)
def test_location_is_canonical_goal_bound_and_policy_allowed(path: str) -> None:
    with pytest.raises(FeedbackViolation):
        validate_feedback(
            _minimal_payload(findings=[_finding(location={"path": path})]),
            _goal(),
            AutopilotPolicy(),
        )


def test_wrong_goal_and_unknown_kind_fail_closed() -> None:
    with pytest.raises(FeedbackViolation, match="goal_id"):
        validate_feedback(_minimal_payload(goal_id="another-goal"), _goal())
    with pytest.raises(FeedbackViolation, match="kind"):
        validate_feedback(_minimal_payload(kind="model_instructions"), _goal())


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_schema_version_is_exact_integer_one(version) -> None:
    with pytest.raises(FeedbackViolation, match="schema_version"):
        validate_feedback(_minimal_payload(schema_version=version), _goal())


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update({"run_id": True}),
        lambda value: value.update({"run_id": 0}),
        lambda value: value.update({"job_id": -1}),
        lambda value: value.update({"stage": "review"}),
        lambda value: value.update({"base_sha": "A" * 40}),
        lambda value: value.update({"base_sha": "0" * 39}),
        lambda value: value.update({"extra": "no"}),
    ],
)
def test_source_contract_is_exact(mutator) -> None:
    source = deepcopy(_finding()["sources"][0])
    mutator(source)
    with pytest.raises(FeedbackViolation):
        validate_feedback(_minimal_payload(findings=[_finding(sources=[source])]), _goal())


def test_gate_is_an_explicit_allowed_evidence_stage() -> None:
    source = deepcopy(_finding()["sources"][0])
    source["stage"] = "gate"
    normalized = validate_feedback(
        _minimal_payload(findings=[_finding(sources=[source])]),
        _goal(),
    )
    assert normalized["findings"][0]["sources"][0]["stage"] == "gate"


def test_duplicate_finding_ids_and_sources_are_rejected() -> None:
    with pytest.raises(FeedbackViolation, match="finding.id"):
        validate_feedback(
            _minimal_payload(findings=[_finding(), _finding(title="Another title")]),
            _goal(),
        )
    source = _finding()["sources"][0]
    with pytest.raises(FeedbackViolation, match="sources"):
        validate_feedback(
            _minimal_payload(findings=[_finding(sources=[source, deepcopy(source)])]),
            _goal(),
        )


def test_finding_and_source_counts_are_bounded() -> None:
    too_many_findings = [
        _finding(id=f"finding-{index}") for index in range(MAX_FINDINGS + 1)
    ]
    with pytest.raises(FeedbackViolation, match="findings count"):
        validate_feedback(_minimal_payload(findings=too_many_findings), _goal())
    sources = []
    for index in range(MAX_SOURCES_PER_FINDING + 1):
        source = deepcopy(_finding()["sources"][0])
        source["job_id"] += index
        sources.append(source)
    with pytest.raises(FeedbackViolation, match="sources count"):
        validate_feedback(_minimal_payload(findings=[_finding(sources=sources)]), _goal())


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", " leading"),
        ("title", "line\nbreak"),
        ("title", "tab\tvalue"),
        ("detail", "trailing "),
        ("detail", "bad\x00value"),
        ("title", "e\u0301"),
        ("title", "x" * 201),
        ("detail", "x" * 3001),
    ],
)
def test_text_is_bounded_single_line_stripped_and_normalized(field: str, value: str) -> None:
    with pytest.raises(FeedbackViolation):
        validate_feedback(_minimal_payload(findings=[_finding(**{field: value})]), _goal())


def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1,"kind":"unresolved_review_findings",'
        '"goal_id":"trader-brain-think-v1","findings":[]}',
        encoding="utf-8",
    )
    with pytest.raises(FeedbackViolation, match="duplicate"):
        load_feedback(duplicate, _goal())
    nonfinite = tmp_path / "nan.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(FeedbackViolation, match="non-finite"):
        load_feedback(nonfinite, _goal())


def test_size_is_checked_before_json_decoding(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b"{" + b"x" * MAX_FEEDBACK_BYTES)
    with pytest.raises(FeedbackViolation, match="size limit"):
        load_feedback(path, _goal())


def test_bom_nul_and_invalid_utf8_are_rejected(tmp_path: Path) -> None:
    for index, raw in enumerate((b"\xef\xbb\xbf{}", b'{"x":"\x00"}', b"\xff")):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(raw)
        with pytest.raises(FeedbackViolation):
            load_feedback(path, _goal())
