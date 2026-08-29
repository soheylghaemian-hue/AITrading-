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
    LEARN_GOAL_ID,
    LEARN_MAX_FEEDBACK_BYTES,
    LEARN_MAX_FINDINGS,
    LEARN_MAX_SOURCES_PER_FINDING,
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
LEARN_GOAL_PATH = PROJECT / "autopilot/goals/trader-brain-learn-v1.json"
LEARN_FEEDBACK_PATH = (
    PROJECT
    / ".github/autopilot/feedback/trader-brain-learn-v1.json"
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


def test_prove_feedback_records_exact_unresolved_evidence_without_scope_expansion() -> None:
    goal = load_goal(PROVE_GOAL_PATH)
    normalized = load_feedback(PROVE_FEEDBACK_PATH, goal)
    assert normalized["goal_id"] == "trader-brain-prove-v1"
    assert {finding["id"] for finding in normalized["findings"]} == {
        "abutting-window-fixture-reference",
        "exact-start-proposal-lookahead",
        "forged-result-aggregate-inconsistency",
        "future-import-allowlist-fixture",
        "governance-enum-identity-contamination",
        "governance-enum-value-case-fixture",
        "incomplete-outcome-manifest-proven",
        "input-binding-exceptions-escape-fail-closed",
        "input-checksum-semantic-collisions",
        "iterator-input-repeatability-drift",
        "malformed-none-helper-substitution",
        "malformed-window-constructor-fixture",
        "observation-order-proof-checksum-drift",
        "proposal-subclass-checksum-instability",
        "randomness-lexical-false-positive",
        "rolling-fold-schedule-rejected",
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
        (
            32304385086,
            96238666327,
            "gate",
            "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        ),
        (
            32338959044,
            96340857621,
            "artifact_audit",
            "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        ),
        (
            32384013016,
            96482675509,
            "gate",
            "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        ),
        (
            32391041286,
            96508989387,
            "final_review",
            "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        ),
        (
            32406043752,
            96552681529,
            "gate",
            "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        ),
    }
    assert len(normalized["findings"]) == MAX_FINDINGS
    run_32 = {
        finding["id"]: finding
        for finding in normalized["findings"]
        if any(source["run_id"] == 32384013016 for source in finding["sources"])
    }
    assert {
        finding_id: (finding["location"]["path"], finding["location"]["line"])
        for finding_id, finding in run_32.items()
    } == {
        "future-import-allowlist-fixture": ("tests/test_brain_prove.py", 567),
        "governance-enum-value-case-fixture": ("tests/test_brain_prove.py", 581),
        "observation-order-proof-checksum-drift": ("src/atp/brain/prove.py", 235),
    }
    assert all(
        finding["sources"]
        == [
            {
                "run_id": 32384013016,
                "job_id": 96482675509,
                "stage": "gate",
                "base_sha": "c4aba55569b505d118709ecb85be9cd1286b2b0d",
            }
        ]
        for finding in run_32.values()
    )
    run_33 = {
        finding["id"]: finding
        for finding in normalized["findings"]
        if any(source["run_id"] == 32391041286 for source in finding["sources"])
    }
    assert {
        finding_id: (finding["location"]["path"], finding["location"]["line"])
        for finding_id, finding in run_33.items()
    } == {
        "forged-result-aggregate-inconsistency": ("src/atp/brain/prove.py", 290),
        "incomplete-outcome-manifest-proven": ("src/atp/brain/prove.py", 710),
        "input-checksum-semantic-collisions": ("src/atp/brain/prove.py", 431),
        "proposal-subclass-checksum-instability": ("src/atp/brain/prove.py", 595),
    }
    assert all(
        finding["sources"]
        == [
            {
                "run_id": 32338959044,
                "job_id": 96340857621,
                "stage": "artifact_audit",
                "base_sha": "c4aba55569b505d118709ecb85be9cd1286b2b0d",
            },
            {
                "run_id": 32391041286,
                "job_id": 96508989387,
                "stage": "final_review",
                "base_sha": "c4aba55569b505d118709ecb85be9cd1286b2b0d",
            },
        ]
        for finding in run_33.values()
    )
    assert "arbitrary WindowMetrics counts" in run_33[
        "forged-result-aggregate-inconsistency"
    ]["detail"]
    assert "neither timestamped nor bound to the pre-evaluation proposal" in run_33[
        "incomplete-outcome-manifest-proven"
    ]["detail"]
    assert "beyond depth 16 to the same deep token" in run_33[
        "input-checksum-semantic-collisions"
    ]["detail"]
    assert "exact-type rejection occurs only after getattr and __getattribute__" in run_33[
        "proposal-subclass-checksum-instability"
    ]["detail"]
    run_34 = {
        finding["id"]: finding
        for finding in normalized["findings"]
        if any(source["run_id"] == 32406043752 for source in finding["sources"])
    }
    assert {
        finding_id: (finding["location"]["path"], finding["location"]["line"])
        for finding_id, finding in run_34.items()
    } == {
        "randomness-lexical-false-positive": ("tests/test_brain_prove.py", 532),
    }
    assert run_34["randomness-lexical-false-positive"]["sources"] == [
        {
            "run_id": 32229557214,
            "job_id": 96002679156,
            "stage": "gate",
            "base_sha": "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        },
        {
            "run_id": 32406043752,
            "job_id": 96552681529,
            "stage": "gate",
            "base_sha": "c4aba55569b505d118709ecb85be9cd1286b2b0d",
        },
    ]
    assert run_34["randomness-lexical-false-positive"]["title"] == (
        "Research-only source fixture treats documentation prose as safety evidence"
    )
    assert run_34["randomness-lexical-false-positive"]["detail"] == (
        "Run 25 rejects the harmless docstring word randomness by scanning the complete "
        "source text for the substring random although its executable imports and calls "
        "are allowed. Run 34 replaces that scan with AST checks, which pass, but then "
        "requires the prose words randomness, execution, broker, clock and trading to "
        "exist; the candidate source lacks broker, so the gate fails. Documentation "
        "wording is not executable safety evidence and must neither cause rejection nor "
        "be required for acceptance."
    )
    assert {
        finding["location"]["path"] for finding in normalized["findings"]
    } == {"src/atp/brain/prove.py", "tests/test_brain_prove.py"}


def test_learn_feedback_records_exact_run_evidence_through_110() -> None:
    goal = load_goal(LEARN_GOAL_PATH)
    assert MAX_FEEDBACK_BYTES < LEARN_FEEDBACK_PATH.stat().st_size <= LEARN_MAX_FEEDBACK_BYTES
    normalized = load_feedback(LEARN_FEEDBACK_PATH, goal)
    assert normalized["goal_id"] == "trader-brain-learn-v1"
    assert {finding["id"] for finding in normalized["findings"]} == {
        "authority-field-lexical-false-positive",
        "closure-fixture-rejects-benign-class-cell",
        "comparison-invalid-proof-shadowed-by-model-validation",
        "comparison-fixture-confounds-proof-mismatch",
        "coordinated-drift-tampering-bypasses-revalidation",
        "empty-evidence-manufactures-retirement-grounds",
        "evidence-failure-precedence-permutation-dependent",
        "equivalent-permutations-produce-unequal-results",
        "evaluator-closure-exposes-commitment-sealer",
        "future-registered-model-produces-actionable-drift",
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
        "local-proposal-tamper-invalidates-proof-first",
        "nested-result-failure-reason-leaks-across-transition-boundary",
        "reinstate-chain-allows-challenger-promotion",
        "tamper-fixture-leaks-champion-role",
        "timezone-spelling-fixture-assumes-evidence-inequality",
        "timezone-utc-fixture-not-in-immutable-allowlist",
        "transition-fixture-confounds-ordering",
    }
    assert {finding["severity"] for finding in normalized["findings"]} == {"P1"}
    assert {
        finding["id"]: (finding["location"]["path"], finding["location"]["line"])
        for finding in normalized["findings"]
    } == {
        "authority-field-lexical-false-positive": ("tests/test_brain_learn.py", 538),
        "closure-fixture-rejects-benign-class-cell": (
            "tests/test_brain_learn.py",
            463,
        ),
        "comparison-invalid-proof-shadowed-by-model-validation": (
            "src/atp/brain/learn.py",
            814,
        ),
        "comparison-fixture-confounds-proof-mismatch": (
            "tests/test_brain_learn.py",
            286,
        ),
        "coordinated-drift-tampering-bypasses-revalidation": (
            "src/atp/brain/learn.py",
            648,
        ),
        "empty-evidence-manufactures-retirement-grounds": (
            "src/atp/brain/learn.py",
            1212,
        ),
        "evidence-failure-precedence-permutation-dependent": (
            "src/atp/brain/learn.py",
            734,
        ),
        "equivalent-permutations-produce-unequal-results": (
            "src/atp/brain/learn.py",
            969,
        ),
        "evaluator-closure-exposes-commitment-sealer": (
            "src/atp/brain/learn.py",
            326,
        ),
        "future-registered-model-produces-actionable-drift": (
            "src/atp/brain/learn.py",
            1085,
        ),
        "invalid-policy-fixture-raises-before-result-refusal": (
            "tests/test_brain_learn.py",
            91,
        ),
        "learn-documentation-omits-model-role": (
            "docs/TRADER_BRAIN.md",
            379,
        ),
        "local-proposal-tamper-invalidates-proof-first": (
            "tests/test_brain_learn.py",
            381,
        ),
        "nested-result-failure-reason-leaks-across-transition-boundary": (
            "src/atp/brain/learn.py",
            665,
        ),
        "reinstate-chain-allows-challenger-promotion": (
            "src/atp/brain/learn.py",
            1063,
        ),
        "tamper-fixture-leaks-champion-role": ("tests/test_brain_learn.py", 593),
        "timezone-spelling-fixture-assumes-evidence-inequality": (
            "tests/test_brain_learn.py",
            208,
        ),
        "timezone-utc-fixture-not-in-immutable-allowlist": (
            "tests/test_brain_learn.py",
            437,
        ),
        "transition-fixture-confounds-ordering": (
            "src/atp/brain/learn.py",
            808,
        ),
    }
    run_36_source = {
        "run_id": 32438421194,
        "job_id": 96648106153,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_38_source = {
        "run_id": 32452907320,
        "job_id": 96689547045,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_40_source = {
        "run_id": 32458581800,
        "job_id": 96710258496,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_42_source = {
        "run_id": 32481392244,
        "job_id": 96778775008,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_43_source = {
        "run_id": 32491533881,
        "job_id": 96806873114,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_44_source = {
        "run_id": 32528444226,
        "job_id": 96921311025,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_45_source = {
        "run_id": 32542812818,
        "job_id": 96958966128,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_47_source = {
        "run_id": 32554139745,
        "job_id": 96991065888,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_48_source = {
        "run_id": 32558489491,
        "job_id": 97000836840,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_49_gate_source = {
        "run_id": 32561133319,
        "job_id": 97005629148,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_49_artifact_audit_source = {
        "run_id": 32561133319,
        "job_id": 97003088227,
        "stage": "artifact_audit",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_50_gate_source = {
        "run_id": 32563676193,
        "job_id": 97011712519,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_51_gate_source = {
        "run_id": 32566023747,
        "job_id": 97016998982,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_51_artifact_audit_source = {
        "run_id": 32566023747,
        "job_id": 97014947094,
        "stage": "artifact_audit",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_52_gate_source = {
        "run_id": 32567915055,
        "job_id": 97021257732,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_54_gate_source = {
        "run_id": 32571575828,
        "job_id": 97030214389,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_55_gate_source = {
        "run_id": 32573632245,
        "job_id": 97035359522,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_56_gate_source = {
        "run_id": 32575669191,
        "job_id": 97040696467,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_58_gate_source = {
        "run_id": 32580176027,
        "job_id": 97051417091,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_59_gate_source = {
        "run_id": 32582631576,
        "job_id": 97056894378,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_60_gate_source = {
        "run_id": 32584681403,
        "job_id": 97062107703,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_61_gate_source = {
        "run_id": 32586981689,
        "job_id": 97067468408,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_62_gate_source = {
        "run_id": 32589317435,
        "job_id": 97073687658,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_63_gate_source = {
        "run_id": 32591600980,
        "job_id": 97079081660,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_64_gate_source = {
        "run_id": 32594040692,
        "job_id": 97086336473,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_65_gate_source = {
        "run_id": 32597293666,
        "job_id": 97092753578,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_66_gate_source = {
        "run_id": 32599754306,
        "job_id": 97099076876,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_67_gate_source = {
        "run_id": 32602620926,
        "job_id": 97106277328,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_68_gate_source = {
        "run_id": 32612737124,
        "job_id": 97130419906,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_70_final_review_source = {
        "run_id": 32622788013,
        "job_id": 97158669218,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_71_gate_source = {
        "run_id": 32633659803,
        "job_id": 97182598097,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_72_gate_source = {
        "run_id": 32645720123,
        "job_id": 97212330684,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_73_final_review_source = {
        "run_id": 32647948386,
        "job_id": 97221994851,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_74_gate_source = {
        "run_id": 32652854008,
        "job_id": 97233419145,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_75_gate_source = {
        "run_id": 32659559724,
        "job_id": 97246482381,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_77_gate_source = {
        "run_id": 32703179396,
        "job_id": 97364220767,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_78_gate_source = {
        "run_id": 32706739347,
        "job_id": 97375145112,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_79_final_review_source = {
        "run_id": 32710835689,
        "job_id": 97397112789,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_81_gate_source = {
        "run_id": 32723366295,
        "job_id": 97424477755,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_83_gate_source = {
        "run_id": 32775112610,
        "job_id": 97594147727,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_84_gate_source = {
        "run_id": 32780584056,
        "job_id": 97607411133,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_85_gate_source = {
        "run_id": 32783772777,
        "job_id": 97616893006,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_86_final_review_source = {
        "run_id": 32787386882,
        "job_id": 97632504247,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_87_gate_source = {
        "run_id": 32792874063,
        "job_id": 97643139981,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_88_gate_source = {
        "run_id": 32796627477,
        "job_id": 97654873093,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_89_gate_source = {
        "run_id": 32800959884,
        "job_id": 97665592734,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_90_gate_source = {
        "run_id": 32803661401,
        "job_id": 97674010884,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_91_gate_source = {
        "run_id": 32806323964,
        "job_id": 97681832518,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_92_gate_source = {
        "run_id": 32807336891,
        "job_id": 97685853506,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_93_gate_source = {
        "run_id": 32809181955,
        "job_id": 97689719271,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_94_gate_source = {
        "run_id": 32818046185,
        "job_id": 97717028235,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_95_gate_source = {
        "run_id": 32827723868,
        "job_id": 97745862652,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_96_final_review_source = {
        "run_id": 32866240334,
        "job_id": 97879171688,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_97_gate_source = {
        "run_id": 32893857889,
        "job_id": 97959003873,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_98_final_review_source = {
        "run_id": 32900924868,
        "job_id": 97988300665,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_99_final_review_source = {
        "run_id": 32928828112,
        "job_id": 98068464635,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_100_final_review_source = {
        "run_id": 32946095269,
        "job_id": 98123742802,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_102_final_review_source = {
        "run_id": 33012259990,
        "job_id": 98335174166,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_103_final_review_source = {
        "run_id": 33017684407,
        "job_id": 98351339516,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_104_gate_source = {
        "run_id": 33023684128,
        "job_id": 98368496484,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_105_gate_source = {
        "run_id": 33056437991,
        "job_id": 98470434882,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_106_gate_source = {
        "run_id": 33081133985,
        "job_id": 98556516262,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_107_final_review_source = {
        "run_id": 33176015354,
        "job_id": 98880221483,
        "stage": "final_review",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_108_gate_source = {
        "run_id": 33185013277,
        "job_id": 98902048639,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_109_gate_source = {
        "run_id": 33247076204,
        "job_id": 99088517810,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    run_110_gate_source = {
        "run_id": 33267734642,
        "job_id": 99143394411,
        "stage": "gate",
        "base_sha": "8b45683d7cee8e1c5e794d83a15e4d4e973596be",
    }
    findings = {finding["id"]: finding for finding in normalized["findings"]}
    assert {
        finding_id: finding["sources"]
        for finding_id, finding in findings.items()
    } == {
        "authority-field-lexical-false-positive": [
            run_36_source,
            run_51_gate_source,
            run_60_gate_source,
            run_64_gate_source,
            run_70_final_review_source,
            run_81_gate_source,
            run_110_gate_source,
        ],
        "closure-fixture-rejects-benign-class-cell": [
            run_49_gate_source,
            run_52_gate_source,
            run_60_gate_source,
        ],
        "comparison-invalid-proof-shadowed-by-model-validation": [
            run_45_source,
            run_47_source,
            run_86_final_review_source,
            run_99_final_review_source,
            run_103_final_review_source,
            run_104_gate_source,
            run_107_final_review_source,
        ],
        "comparison-fixture-confounds-proof-mismatch": [
            run_38_source,
            run_50_gate_source,
            run_88_gate_source,
        ],
        "coordinated-drift-tampering-bypasses-revalidation": [
            run_40_source,
            run_47_source,
            run_48_source,
            run_49_artifact_audit_source,
            run_51_artifact_audit_source,
            run_54_gate_source,
            run_56_gate_source,
            run_63_gate_source,
            run_70_final_review_source,
            run_73_final_review_source,
            run_77_gate_source,
            run_79_final_review_source,
            run_81_gate_source,
            run_86_final_review_source,
            run_96_final_review_source,
            run_97_gate_source,
            run_98_final_review_source,
            run_99_final_review_source,
            run_100_final_review_source,
            run_102_final_review_source,
            run_103_final_review_source,
            run_105_gate_source,
            run_107_final_review_source,
            run_108_gate_source,
            run_109_gate_source,
        ],
        "empty-evidence-manufactures-retirement-grounds": [run_96_final_review_source],
        "evidence-failure-precedence-permutation-dependent": [run_40_source],
        "equivalent-permutations-produce-unequal-results": [
            run_42_source,
            run_48_source,
            run_63_gate_source,
            run_86_final_review_source,
            run_99_final_review_source,
        ],
        "evaluator-closure-exposes-commitment-sealer": [
            run_42_source,
            run_102_final_review_source,
        ],
        "future-registered-model-produces-actionable-drift": [
            run_96_final_review_source,
        ],
        "invalid-policy-fixture-raises-before-result-refusal": [
            run_44_source,
            run_45_source,
            run_55_gate_source,
            run_56_gate_source,
            run_58_gate_source,
            run_59_gate_source,
            run_61_gate_source,
            run_62_gate_source,
            run_66_gate_source,
            run_67_gate_source,
            run_68_gate_source,
            run_75_gate_source,
            run_83_gate_source,
            run_85_gate_source,
            run_89_gate_source,
            run_91_gate_source,
            run_92_gate_source,
            run_93_gate_source,
            run_94_gate_source,
            run_95_gate_source,
            run_104_gate_source,
            run_106_gate_source,
            run_108_gate_source,
            run_110_gate_source,
        ],
        "learn-documentation-omits-model-role": [
            run_43_source,
            run_61_gate_source,
            run_65_gate_source,
            run_66_gate_source,
            run_71_gate_source,
            run_72_gate_source,
            run_73_final_review_source,
            run_74_gate_source,
            run_75_gate_source,
            run_78_gate_source,
            run_84_gate_source,
            run_85_gate_source,
            run_87_gate_source,
            run_88_gate_source,
            run_90_gate_source,
            run_93_gate_source,
            run_95_gate_source,
            run_104_gate_source,
        ],
        "local-proposal-tamper-invalidates-proof-first": [run_43_source],
        "nested-result-failure-reason-leaks-across-transition-boundary": [
            run_75_gate_source,
        ],
        "reinstate-chain-allows-challenger-promotion": [
            run_42_source,
            run_70_final_review_source,
        ],
        "tamper-fixture-leaks-champion-role": [run_36_source, run_88_gate_source],
        "timezone-spelling-fixture-assumes-evidence-inequality": [
            run_43_source,
            run_49_gate_source,
            run_56_gate_source,
            run_59_gate_source,
            run_61_gate_source,
            run_87_gate_source,
            run_95_gate_source,
            run_106_gate_source,
        ],
        "timezone-utc-fixture-not-in-immutable-allowlist": [
            run_44_source,
            run_45_source,
            run_49_gate_source,
            run_56_gate_source,
            run_61_gate_source,
            run_62_gate_source,
        ],
        "transition-fixture-confounds-ordering": [
            run_38_source,
            run_50_gate_source,
            run_81_gate_source,
        ],
    }
    assert findings["authority-field-lexical-false-positive"]["title"] == (
        "Authority API mismatch"
    )
    assert findings["authority-field-lexical-false-positive"]["detail"] == (
        "36/51/60/64/70 mix lexical/import/call checks with dotted/bare or collapsed "
        "relative names. Run 70 allowlists every relative import as \".\" and does not "
        "inspect loaded transitive modules, so broker/runtime/service/live-trading "
        "siblings pass. Run 81 substring-scans dir(result); forbidden \"size\" matches "
        "inherited __sizeof__, so a safe result fails. Inspect declared "
        "public/dataclass names exactly; match exact forms/module paths and actual "
        "import graph; keep no-order/no-execution. 110 vars()+callable flags imported "
        "dataclass as authority although closure checks pass; test issuance capability, "
        "not callability."
    )
    assert findings["tamper-fixture-leaks-champion-role"]["title"] == (
        "Test fixtures leak shared state across tests"
    )
    assert findings["tamper-fixture-leaks-champion-role"]["detail"] == (
        "Run 36 mutates aliased CHAMPION without restoring it, causing "
        "ROLE_MISMATCH/order dependence. Build fresh models; keep fail-closed rejection. "
        "Run 88's import-graph fixture deletes every loaded atp/atp.* module from "
        "sys.modules without restoring it; already-collected governance tests retain "
        "the old ModelStatus class while later imports create a second enum class, so "
        "identity checks and ModelRegistry.by_status become order-dependent. Run the "
        "import-graph check in a subprocess or restore the exact module cache; retain "
        "real transitive-import rejection."
    )
    assert findings["comparison-fixture-confounds-proof-mismatch"]["title"] == (
        "Comparison fixtures still collapse into self-comparison"
    )
    assert findings["comparison-fixture-confounds-proof-mismatch"]["detail"] == (
        "38/50 self-compare: 38 reuses challenger proposal; 50's champion shares "
        "challenger ID/proposal and correct role. SELF_COMPARISON is right; use distinct "
        "IDs and wrong role; pin both. Run 88 labels a CHALLENGER twin with the "
        "champion's exact model_id/proposal_id as a genuine self-comparison but expects "
        "ROLE_MISMATCH; both roles are correct, so SELF_COMPARISON is the pinned result. "
        "Expect SELF_COMPARISON, or give the wrong-role case distinct identities; retain "
        "role-before-identity precedence."
    )
    assert findings["transition-fixture-confounds-ordering"]["title"] == (
        "Transition order"
    )
    assert findings["transition-fixture-confounds-ordering"]["detail"] == (
        "38/50 mix reversal order with unknowable evidence/CHALLENGER start, correctly "
        "refusing. Build coherent evidence/time fixtures for the intended order. Run "
        "81 exposes an unreachable declared reason: accepted comparison evidence "
        "requires CHAMPION/CHALLENGER records, exact ModelRecord equality includes "
        "role, and _bind_retirement checks evidence equality before RETIRED, so every "
        "RETIRED model yields EVIDENCE_MODEL_MISMATCH before MODEL_ALREADY_RETIRED. "
        "Check MODEL_ALREADY_RETIRED before exact-role evidence membership (or remove "
        "the dead reason), then pin every declared transition reason and deterministic "
        "precedence; qualify docs; keep fail-closed evidence revalidation."
    )
    assert findings["coordinated-drift-tampering-bypasses-revalidation"]["title"] == (
        "Accepted-state minting persists"
    )
    assert (
        findings["coordinated-drift-tampering-bypasses-revalidation"]["detail"]
        == "D/C/R/I=Drift/Comparison/Retirement/Reinstatement+Result;CS=checksum. 40/47-49/51/54/56/"
        "63 public ctors/_Warrant/__setstate__: mint/restore/rebind acceptance. 70 hand-built acc"
        "epted-input D stays consumer-valid after inputs.model_id/prior_confidence edits. 73/86 c"
        "omparison+98 drift FunctionTypes use learn.__dict__ (73/86+copied globals); 96/100/103 e"
        "xact-global clones mint acceptance. 73 frame-globals/co_name-only _issued_by admits forg"
        "ed ModelRecords/fake proof CS/arbitrary returns; retirement/reinstatement fail. 77 accep"
        "ted.inputs.model.model_id edit rederives input_identity; CS passes. 79 caller-provenance"
        "/recomputed-field ctors rebuild CS-passing D; public _bind_* rebinds. 81 exposed inputs/"
        "identity/model_id/restored_role+recompute-only __post_init__ rebuild unissued accepted I"
        ". 86 code-only _Seal/public fields allow ComparisonInputs substitution/owner-bound accep"
        "tance. 96 _Seal binds type/fingerprint not owner; object.__new__ copy passes CS/retireme"
        "nt. 97 frozen+slots __setstate__ shadows _Issued; {} succeeds; crafted state mutates fie"
        "lds. 98 _Provenance.verify trusts writable owner/fingerprint+visible _digest/_result_pay"
        "load; object.__new__ forgery passes CS/retirement. 99 tuple.__new__(_Seal,(...,weakref.r"
        "ef(forged))) bypasses __new__; owner-bound object.__new__ copy passes; base ctor unteste"
        "d. 100 object.__new__/__setattr__+visible fingerprint forges provenance; arbitrary-Proof"
        "Summary C passes CS/retirement. 98/100 tests only unclaimed/underived provenance+copied "
        "globals. 103 writable _Provenance.owner/state/_mint rebuilds D passing retirement; retir"
        "ement_id edit+recomputed state passes reinstatement; extra attrs ignored. 105 accepted S"
        "enseResult.as_of rebind+forged-time drift rebuilds acceptance; bind original SENSE issua"
        "nce, not self-consistency. 107 \"Accepted LEARN results remain forgeable and mutable\": de"
        "rivation-only _require_accepted admits coherent direct C through CS/evaluate_retirement;"
        " identity edits rederive CS; tests bless equivalent ctor-state/evaluator clones/identity"
        " rebinding. \"Mutated SENSE issuance can be recommitted\": _canonical_window replays as_of"
        "/dynamic CS; valid-partition as_of-rebind/coordinated nested-evidence edits violate orig"
        "inal issuance. 108 CS accepts D.prior_confidence 0.9->0.1, R.retirement_id r-1->r-forged"
        ", I.reversal_id rev-7->rev-forged: original evaluator issuance unbound. 109 gate: 5 vali"
        "d tests(3D/1C/1I): genuine/equal; CS raises \"may only be issued by this module's evaluat"
        "ors\" post-return as _check_shell calls stack-only _minting before ledger _require_issued"
        "; R too. Block non-evaluator ctor/copy/restore/rebind/mutation/extra-attrs/FunctionType/"
        "state in CS+consumers; immutable issuance binds exact exported evaluator-code/module-glo"
        "bals/owner/original-input-IDs/full fingerprint; reject clones/unknown attrs; all 4 rejec"
        "t restore. _issue gates construction; then _ISSUED verifies exact identity+fingerprint. "
        "Keep usable deterministic CS/consumer revalidation; pin every forgery+genuine-result reg"
        "ression."
    )
    assert findings["empty-evidence-manufactures-retirement-grounds"]["title"] == (
        "Empty evidence can manufacture retirement grounds"
    )
    assert findings["empty-evidence-manufactures-retirement-grounds"]["detail"] == (
        "Run 96: With empty canonical evidence and prior_confidence <= 0.25, "
        "drift_score is 0.0 but abstain is true; evaluate_retirement then accepts "
        "DRIFT_ABSTENTION. This contradicts the required no-manufactured-evidence "
        "invariant and the documentation."
    )
    assert findings["evidence-failure-precedence-permutation-dependent"]["title"] == (
        "Evidence failure precedence depends on tuple order"
    )
    assert (
        findings["evidence-failure-precedence-permutation-dependent"]["detail"]
        == "Duplicate+out-of-order permutations differ in reason/checksum: _admit "
        "interleaves evidence and ID checks. Phase checks deterministically."
    )
    assert findings["evaluator-closure-exposes-commitment-sealer"]["title"] == (
        "Internal commitment can be forged through the evaluator closure"
    )
    assert findings["evaluator-closure-exposes-commitment-sealer"]["detail"] == (
        "evaluate_drift.__closure__ exposes a sealer: recommit mutated evidence/derived "
        "fields, and checksum/comparison trust them. Hide it; test bypass. Run 102 final "
        "review, \"Closure-held issuance key permits fabricated retirement evidence\": "
        "The keyed `_prime` is reachable through "
        "`ComparisonResult.__mro__[1].__init__.__closure__`. A caller can create a result "
        "with `object.__new__`, populate invented but internally consistent proof summaries, "
        "obtain `_prime`, recompute `_stamp`, and pass both `checksum()` and "
        "`evaluate_retirement`; this was reproduced with a fabricated superior-challenger "
        "comparison. This violates the required closure secrecy and consumer rejection of "
        "fabricated provenance."
    )
    assert findings["future-registered-model-produces-actionable-drift"]["title"] == (
        "Future-registered models produce actionable drift"
    )
    assert findings["future-registered-model-produces-actionable-drift"]["detail"] == (
        "Run 96: evaluate_drift accepts a model whose registered_at is after as_of. "
        "That pre-registration drift can later retire the model once registration time "
        "passes, allowing point-in-time leakage into a transition."
    )
    assert findings["reinstate-chain-allows-challenger-promotion"]["title"] == (
        "Challenger promotion"
    )
    assert findings["reinstate-chain-allows-challenger-promotion"]["detail"] == (
        "42/70 permit CHALLENGER→RETIRED→CHAMPION: 42 lacks a reversal ID and allows "
        "any active next_role; 70 re-derives previous_role from mutable "
        "retirement.inputs.model.role. Bind the exact reversal and original "
        "role/provenance; reject tampering; forbid self-promotion."
    )
    assert findings["equivalent-permutations-produce-unequal-results"]["title"] == (
        "Equivalent inputs produce unequal results"
    )
    assert findings["equivalent-permutations-produce-unequal-results"]["detail"] == (
        "42/48/63 store noncanonical tuples/datetimes despite canonical checksums, so "
        "permutations/fold spellings yield unequal results. Sort tuples, normalize UTC, "
        "pin equality. Run 86 stores caller-spelled ProveResults in ComparisonInputs: "
        "proven results with permuted-but-equivalent windows have identical proof "
        "checksums, while their comparisons are unequal despite sharing a comparison "
        "checksum. Store one fully canonical proof snapshot per side, including every "
        "proof field comparison later consumes; derive equality and checksum from that "
        "same canonical state without ignoring proof fields; retain strict proof "
        "revalidation. Run 99 final review, \"Canonical equality and checksums "
        "diverge\": Drift stores canonical SENSE output containing raw "
        "`Evidence.assertions`. Reversing an otherwise equivalent assertion tuple "
        "produces unequal DriftResults but identical checksums because serialization "
        "sorts assertions. Conversely, prior confidences `0.0` and `-0.0` produce equal "
        "results with different checksums. Nested evidence timestamps also retain "
        "caller timezone spellings instead of being stored in UTC."
    )
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_48_source in finding["sources"]
    } == {
        "coordinated-drift-tampering-bypasses-revalidation",
        "equivalent-permutations-produce-unequal-results",
    }
    assert findings["timezone-spelling-fixture-assumes-evidence-inequality"][
        "title"
    ] == "Timezone fixture errors"
    assert findings["timezone-spelling-fixture-assumes-evidence-inequality"][
        "detail"
    ] == (
        "43/49/56/59/61 use raw fold equality or stale evidence. Compare UTC instants, "
        "then canonical state. Run 87's fixture expects aware datetimes expressed as "
        "UTC and +05:30 to compare unequal, but Python datetime equality compares the "
        "represented instants and they are equal. Assert that the UTC offsets or tzinfo "
        "spellings differ, then retain UTC-normalized input, result and checksum equality. "
        "Run 95 checks offset inequality only after _drift has passed both timestamp "
        "spellings through SENSE, whose accepted SenseResult is UTC-normalized; both "
        "stored offsets are therefore zero. Assert differing raw fixture offsets before "
        "evaluation, then retain result and checksum equality on canonical outputs. Run "
        "106: the timezone fixture rebuilds 09:00 wall-clock under +05:30/-04:00, changing "
        "UTC instants to 03:30Z/13:00Z; equality failure is correct. Preserve original "
        "instants when only spelling zones, then retain canonical result/checksum equality."
    )
    assert findings["local-proposal-tamper-invalidates-proof-first"]["title"] == (
        "Proof-tamper"
    )
    assert findings["local-proposal-tamper-invalidates-proof-first"]["detail"] == (
        "43 tampers proof-bound proposal; PROOF_NOT_PROVEN rightly precedes "
        "PROOF_MODEL_MISMATCH. Use clean data; keep revalidation/order strict."
    )
    assert findings["learn-documentation-omits-model-role"]["title"] == (
        "LEARN public API/docs drift"
    )
    assert findings["learn-documentation-omits-model-role"]["detail"] == (
        "43/61 omit ModelRole or exact research-only wording. 65/66/74/84 use raw "
        "scans that reject line wrapping or Markdown; 71 misorders __all__ by placing "
        "RejectedEvidence before ReinstatementInputs. Exact-sentence fixtures disagree "
        "with docs: 72 uses a colon instead of the required period after "
        "comparison/PROVE model binding; 85 joins the stateless-replay sentence with a "
        "semicolon; 87 writes \"identical checksums, because\" instead of ending "
        "\"identical checksums.\"; 88 joins challenger non-promotion to its explanation "
        "with a colon instead of ending \"promotion authority.\"; 90 continues after a "
        "colon with lowercase \"every\" instead of the separate \"Every proof must grade "
        "exactly the proposal_id its ModelRecord names.\". Run 73 promises exactly-once "
        "reversal although stateless evaluate_reinstatement records no consumption and "
        "reaccepts the same retirement, identity, reversal ID, role and timestamp. Run "
        "75 strips underscores so INVALID_INPUT cannot match; 78 strips > from >= so "
        "the abstention boundary cannot match, and expects \"a proof...\" while docs say "
        "\"the proof...\". Run 93 exports ComparisonPreference (CHAMPION, CHALLENGER, "
        "INCONCLUSIVE; research evidence only) but docs omit it, so the strict "
        "public-API inventory correctly fails. Document every non-evaluator export, "
        "including ModelRole and ComparisonPreference, plus exact research-only, "
        "binding, equality, reason, replay and non-promotion phrases. Put each pinned "
        "sentence, case and punctuation boundary exactly; move explanations to "
        "separate sentences; use markup-aware normalization; keep sorted unique "
        "exports and strict docs/binding guards. Remove the exactly-once promise or "
        "represent replay consumption. Run 95 joins `Equivalent inputs produce equal "
        "results and identical checksums` to its explanation with an em dash instead of "
        "ending the pinned sentence with a period. End that exact sentence, then put the "
        "explanation in a separate sentence; keep markup-aware normalization and strict "
        "documentation guards. Run 104 gate: docs continue the pinned reinstatement-role "
        "sentence with \", so\"; its required period is absent. End it, then explain "
        "challenger restoration separately."
    )
    assert findings["invalid-policy-fixture-raises-before-result-refusal"]["title"] == (
        "Evaluator fixture errors"
    )
    assert findings["invalid-policy-fixture-raises-before-result-refusal"]["detail"] == (
        "44-45/55-56/58-59/61-62/66-68/75/83 misuse ctors/defaults/thresholds/__setstate__ shapes"
        "/helpers. 66 replaces explicit None; 67 passes accepted output to _reason and reads a ra"
        "ising property outside pytest.raises; 68 exactly compares 0.05-0.005 with 0.045; 75 call"
        "s undefined _forge. 83 makes all Mar 1-2 items stale at Mar 10 under six days; reversing"
        " empty usable/clearing empty contradictions are no-ops; zero-day freshness stays canonic"
        "al all-rejected; it compares INVALID_INPUT/INVALID_MODEL checksums. 85: empty canonical "
        "SenseResult with prior 1.0/drift 0.0 is accepted without abstention, so retirement yield"
        "s INSUFFICIENT_EVIDENCE, not INVALID_DRIFT. 89: accepted drift binds default CHAMPION wh"
        "ile retirement targets CHALLENGER; EVIDENCE_MODEL_MISMATCH precedes drift "
        "refusal. 91/110: _comparison(None) defaults sides; accepted output cannot "
        "test INVALID_MODEL/INVALID_COMPARISON; proof wins. 92: expects "
        "EVIDENCE_MODEL_MISMATCH although superior comparison f"
        "avors target challenger, correctly insufficient; its table captures the original accepte"
        "d graph, resets accepted, then mutates stale targets while checking fresh output. 93: e-"
        "dup/e-other are distinct, so forward is canonical and only reverse invalid, not duplicat"
        "e permutations; local def evaluate_drift shadows the import and raises UnboundLocalError"
        " before the closure probe. Use sentinels; distinct/adverse/correctly bound inputs; true "
        "duplicate IDs; fresh per-case mutation targets; module-qualified genuine evaluators; def"
        "ined builders, explicit raises, pytest.approx; truly refused/tampered nested results and"
        " like-reason comparisons. Preserve canonicalization, binding, precedence, guards and los"
        "sless float/reason/checksum semantics. 94: mutating previous_role makes checksum() raise"
        ", then the test wrongly expects evaluate_reinstatement to raise; the public evaluator re"
        "turns INVALID_RETIREMENT for nested failure. Assert stable refusal; retain nested revali"
        "dation/fail-closed translation. 95: helper .astimezone(tz) converts naive as_of in runne"
        "r local zone to valid aware input before evaluate_comparison; pass it directly/use an in"
        "validity-preserving helper; assert stable refusal and strict precedence. 104: _compariso"
        "n reads champion/challenger.proposal_id for default proofs before the evaluator; invalid"
        " shells raise AttributeError, not stable refusals. Pass explicit proofs/preserve invalid"
        " shells. 106: Feb-20 comparison keeps default Mar-1 proofs; UNKNOWABLE_EVIDENCE is corre"
        "ct. Align proof/as_of. 108 gate: shared direct-SenseResult test rejects four malformed f"
        "uture/stale/duplicate/mispartitioned cases in THINK+LEARN; its fifth, SenseResult(_stamp"
        "(5), LIMIT, (), (), ()), is canonical/self-consistent. THINK needs no evaluator issuance"
        "; LEARN does. Split: THINK admits it; LEARN returns INVALID_SENSE_RESULT. _accepted_resu"
        "lts() reinstates an accepted CHAMPION retirement, so restored_role is already CHAMPION; "
        "setting CHAMPION is a no-op and checksum correctly does not raise. Mutate a different ro"
        "le."
    )
    assert findings[
        "nested-result-failure-reason-leaks-across-transition-boundary"
    ]["title"] == "Nested failures break transition refusals"
    assert findings[
        "nested-result-failure-reason-leaks-across-transition-boundary"
    ]["detail"] == (
        "Run 75 _require_issued rethrows a prior result's _LearnError rather than "
        "translating it to its caller-supplied transition reason. Retirement then "
        "places ComparisonFailure/DriftFailure inside a TransitionFailure tuple and "
        "raises while constructing the refusal; reinstatement exposes "
        "INCONSISTENT_RESULT instead of INVALID_RETIREMENT. Map every failed nested "
        "result/checksum/reissue to INVALID_COMPARISON, INVALID_DRIFT or "
        "INVALID_RETIREMENT, return one typed authority-free refusal, and keep exact "
        "enum, checksum and consumer-revalidation guards."
    )
    assert findings["timezone-utc-fixture-not-in-immutable-allowlist"]["title"] == (
        "Fixture immutability conflict"
    )
    assert findings["timezone-utc-fixture-not-in-immutable-allowlist"]["detail"] == (
        "44-45/49/56/61-62 misclassify registry classes, timezone.utc or future "
        "annotations. Exempt exact immutable metadata; reject mutable values."
    )
    assert findings["closure-fixture-rejects-benign-class-cell"]["title"] == (
        "Closure scan flags inert cells"
    )
    assert findings["closure-fixture-rejects-benign-class-cell"]["detail"] == (
        "49/52/60 flag inert class/dataclass repr cells as minters. Detect real "
        "accepted-state factories; keep constructor/reflection/mutation probes and no "
        "sealer."
    )
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_49_gate_source in finding["sources"]
    } == {
        "closure-fixture-rejects-benign-class-cell",
        "timezone-spelling-fixture-assumes-evidence-inequality",
        "timezone-utc-fixture-not-in-immutable-allowlist",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_49_artifact_audit_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_50_gate_source in finding["sources"]
    } == {
        "comparison-fixture-confounds-proof-mismatch",
        "transition-fixture-confounds-ordering",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_51_gate_source in finding["sources"]
    } == {"authority-field-lexical-false-positive"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_51_artifact_audit_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_52_gate_source in finding["sources"]
    } == {"closure-fixture-rejects-benign-class-cell"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_54_gate_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_55_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_56_gate_source in finding["sources"]
    } == {
        "coordinated-drift-tampering-bypasses-revalidation",
        "invalid-policy-fixture-raises-before-result-refusal",
        "timezone-spelling-fixture-assumes-evidence-inequality",
        "timezone-utc-fixture-not-in-immutable-allowlist",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_58_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_59_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "timezone-spelling-fixture-assumes-evidence-inequality",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_60_gate_source in finding["sources"]
    } == {
        "authority-field-lexical-false-positive",
        "closure-fixture-rejects-benign-class-cell",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_61_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
        "timezone-spelling-fixture-assumes-evidence-inequality",
        "timezone-utc-fixture-not-in-immutable-allowlist",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_62_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "timezone-utc-fixture-not-in-immutable-allowlist",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_63_gate_source in finding["sources"]
    } == {
        "coordinated-drift-tampering-bypasses-revalidation",
        "equivalent-permutations-produce-unequal-results",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_64_gate_source in finding["sources"]
    } == {"authority-field-lexical-false-positive"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_65_gate_source in finding["sources"]
    } == {"learn-documentation-omits-model-role"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_66_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_86_final_review_source in finding["sources"]
    } == {
        "comparison-invalid-proof-shadowed-by-model-validation",
        "coordinated-drift-tampering-bypasses-revalidation",
        "equivalent-permutations-produce-unequal-results",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_87_gate_source in finding["sources"]
    } == {
        "learn-documentation-omits-model-role",
        "timezone-spelling-fixture-assumes-evidence-inequality",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_88_gate_source in finding["sources"]
    } == {
        "comparison-fixture-confounds-proof-mismatch",
        "learn-documentation-omits-model-role",
        "tamper-fixture-leaks-champion-role",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_89_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_90_gate_source in finding["sources"]
    } == {"learn-documentation-omits-model-role"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_91_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_92_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_93_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_94_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_95_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
        "timezone-spelling-fixture-assumes-evidence-inequality",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_96_final_review_source in finding["sources"]
    } == {
        "coordinated-drift-tampering-bypasses-revalidation",
        "empty-evidence-manufactures-retirement-grounds",
        "future-registered-model-produces-actionable-drift",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_97_gate_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_98_final_review_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_99_final_review_source in finding["sources"]
    } == {
        "comparison-invalid-proof-shadowed-by-model-validation",
        "coordinated-drift-tampering-bypasses-revalidation",
        "equivalent-permutations-produce-unequal-results",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_100_final_review_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_102_final_review_source in finding["sources"]
    } == {
        "coordinated-drift-tampering-bypasses-revalidation",
        "evaluator-closure-exposes-commitment-sealer",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_103_final_review_source in finding["sources"]
    } == {
        "comparison-invalid-proof-shadowed-by-model-validation",
        "coordinated-drift-tampering-bypasses-revalidation",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_104_gate_source in finding["sources"]
    } == {
        "comparison-invalid-proof-shadowed-by-model-validation",
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_105_gate_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_106_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "timezone-spelling-fixture-assumes-evidence-inequality",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_107_final_review_source in finding["sources"]
    } == {
        "comparison-invalid-proof-shadowed-by-model-validation",
        "coordinated-drift-tampering-bypasses-revalidation",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_108_gate_source in finding["sources"]
    } == {
        "coordinated-drift-tampering-bypasses-revalidation",
        "invalid-policy-fixture-raises-before-result-refusal",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_109_gate_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_110_gate_source in finding["sources"]
    } == {
        "authority-field-lexical-false-positive",
        "invalid-policy-fixture-raises-before-result-refusal",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_67_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_68_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_70_final_review_source in finding["sources"]
    } == {
        "authority-field-lexical-false-positive",
        "coordinated-drift-tampering-bypasses-revalidation",
        "reinstate-chain-allows-challenger-promotion",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_71_gate_source in finding["sources"]
    } == {"learn-documentation-omits-model-role"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_72_gate_source in finding["sources"]
    } == {"learn-documentation-omits-model-role"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_73_final_review_source in finding["sources"]
    } == {
        "coordinated-drift-tampering-bypasses-revalidation",
        "learn-documentation-omits-model-role",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_74_gate_source in finding["sources"]
    } == {"learn-documentation-omits-model-role"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_75_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
        "nested-result-failure-reason-leaks-across-transition-boundary",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_77_gate_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_78_gate_source in finding["sources"]
    } == {"learn-documentation-omits-model-role"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_79_final_review_source in finding["sources"]
    } == {"coordinated-drift-tampering-bypasses-revalidation"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_81_gate_source in finding["sources"]
    } == {
        "authority-field-lexical-false-positive",
        "coordinated-drift-tampering-bypasses-revalidation",
        "transition-fixture-confounds-ordering",
    }
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_83_gate_source in finding["sources"]
    } == {"invalid-policy-fixture-raises-before-result-refusal"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_84_gate_source in finding["sources"]
    } == {"learn-documentation-omits-model-role"}
    assert {
        finding_id
        for finding_id, finding in findings.items()
        if run_85_gate_source in finding["sources"]
    } == {
        "invalid-policy-fixture-raises-before-result-refusal",
        "learn-documentation-omits-model-role",
    }
    assert findings["comparison-invalid-proof-shadowed-by-model-validation"]["title"] == (
        "Proof order"
    )
    assert findings["comparison-invalid-proof-shadowed-by-model-validation"]["detail"] == (
        "45/47 bind proofs; champion-first checks make role swaps change wrong/unproven "
        "reasons. Validate shells then proofs: INVALID_PROOF, PROOF_NOT_PROVEN, "
        "PROOF_MODEL_MISMATCH, knowability. Keep construction/checksum "
        "binding/fail-closed order. Run 86 checks model knowability before "
        "proof-to-model binding, so a future-registered model with a wrong-proposal "
        "proof returns INVALID_MODEL instead of the pinned PROOF_MODEL_MISMATCH. Keep "
        "exact ModelRecord shape validation before field access; then bind each proof "
        "to its model before model and proof knowability, side-symmetrically; retain "
        "earlier proof-checksum and proven-status phases. Run 99 final review, "
        "\"Required failure and precedence coverage is incomplete\": The tests never "
        "exercise `ComparisonFailure.INVALID_MODEL`, champion-side proof failures, or "
        "overlapping proof-binding/model-knowability defects. Thus not every declared "
        "public reason, side-symmetric phase, and documented precedence rule has "
        "deterministic regression coverage as required. Run 103 final review, "
        "\"Comparison tests omit required knowability symmetry\": "
        "`UNKNOWABLE_EVIDENCE` is tested only with a future champion model. There is no "
        "challenger-model case or champion/challenger proof-knowability coverage, so "
        "both sides of the complete validation phase are not exercised as explicitly "
        "required. Run 104 gate: _canonical_model accesses fields before _validate_model; "
        "non-ModelRecord raises AttributeError, so drift returns INVALID_INPUT, not "
        "INVALID_MODEL. Type-check first; retain reason precedence. Run 107 final review, "
        "\"Invalid exact model shapes bypass the declared reason phase\": comparison checks only "
        "type(record) is ModelRecord; an exact reflective shell with missing slots then raises "
        "in _canonical_model, and the generic handler returns INVALID_INPUT, not required "
        "INVALID_MODEL. The invalid-exact-shape regression is absent."
    )
    assert len(normalized["findings"]) == LEARN_MAX_FINDINGS
    assert {
        finding["location"]["path"] for finding in normalized["findings"]
    } == {
        "docs/TRADER_BRAIN.md",
        "src/atp/brain/learn.py",
        "tests/test_brain_learn.py",
    }


def test_schema_is_strict_and_matches_validator_bounds() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert MAX_FEEDBACK_BYTES == 16_384
    assert MAX_FINDINGS == 16
    assert MAX_SOURCES_PER_FINDING == 8
    assert LEARN_MAX_FEEDBACK_BYTES == 34_816
    assert LEARN_MAX_FINDINGS == 19
    assert LEARN_MAX_SOURCES_PER_FINDING == 25
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["kind"]["const"] == FEEDBACK_KIND
    assert schema["properties"]["findings"]["maxItems"] == LEARN_MAX_FINDINGS
    assert schema["$defs"]["finding"]["additionalProperties"] is False
    assert schema["$defs"]["source"]["additionalProperties"] is False
    assert schema["$defs"]["source"]["properties"]["stage"]["enum"] == [
        "artifact_audit",
        "gate",
        "initial_review",
        "final_review",
    ]
    assert "does not assert" in schema["$defs"]["source"]["properties"]["stage"]["description"]
    assert (
        schema["$defs"]["finding"]["properties"]["sources"]["maxItems"]
        == LEARN_MAX_SOURCES_PER_FINDING
    )
    goal_limit = schema["allOf"]
    assert goal_limit == [
        {
            "if": {"properties": {"goal_id": {"const": LEARN_GOAL_ID}}},
            "then": {},
            "else": {
                "properties": {
                    "findings": {
                        "maxItems": MAX_FINDINGS,
                        "items": {
                            "properties": {
                                "sources": {"maxItems": MAX_SOURCES_PER_FINDING}
                            }
                        }
                    }
                }
            },
        }
    ]


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


def test_artifact_audit_is_independent_evidence_not_review_attribution() -> None:
    source = deepcopy(_finding()["sources"][0])
    source["stage"] = "artifact_audit"
    normalized = validate_feedback(
        _minimal_payload(findings=[_finding(sources=[source])]),
        _goal(),
    )
    assert normalized["findings"][0]["sources"][0]["stage"] == "artifact_audit"


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
    normalized = validate_feedback(
        _minimal_payload(findings=too_many_findings[:-1]),
        _goal(),
    )
    assert len(normalized["findings"]) == MAX_FINDINGS
    with pytest.raises(FeedbackViolation, match="findings count"):
        validate_feedback(_minimal_payload(findings=too_many_findings), _goal())
    sources = []
    for index in range(MAX_SOURCES_PER_FINDING + 1):
        source = deepcopy(_finding()["sources"][0])
        source["job_id"] += index
        sources.append(source)
    normalized = validate_feedback(
        _minimal_payload(findings=[_finding(sources=sources[:-1])]),
        _goal(),
    )
    assert len(normalized["findings"][0]["sources"]) == MAX_SOURCES_PER_FINDING
    with pytest.raises(FeedbackViolation, match="sources count"):
        validate_feedback(_minimal_payload(findings=[_finding(sources=sources)]), _goal())


def test_learn_finding_count_has_a_separate_authorized_bound() -> None:
    goal = load_goal(LEARN_GOAL_PATH)
    findings = [
        _finding(
            id=f"finding-{index}",
            location={"path": "tests/test_brain_learn.py", "line": 1},
        )
        for index in range(LEARN_MAX_FINDINGS + 1)
    ]
    normalized = validate_feedback(
        _minimal_payload(goal_id=LEARN_GOAL_ID, findings=findings[:-1]),
        goal,
    )
    assert len(normalized["findings"]) == LEARN_MAX_FINDINGS
    with pytest.raises(FeedbackViolation, match="findings count"):
        validate_feedback(
            _minimal_payload(goal_id=LEARN_GOAL_ID, findings=findings),
            goal,
        )


def test_prove_keeps_the_default_finding_bound() -> None:
    goal = load_goal(PROVE_GOAL_PATH)
    findings = [
        _finding(
            id=f"finding-{index}",
            location={"path": "tests/test_brain_prove.py", "line": 1},
        )
        for index in range(MAX_FINDINGS + 1)
    ]
    normalized = validate_feedback(
        _minimal_payload(goal_id=goal.goal_id, findings=findings[:-1]),
        goal,
    )
    assert len(normalized["findings"]) == MAX_FINDINGS
    with pytest.raises(FeedbackViolation, match="findings count"):
        validate_feedback(
            _minimal_payload(goal_id=goal.goal_id, findings=findings),
            goal,
        )


def test_learn_source_count_has_a_separate_authorized_bound() -> None:
    sources = []
    for index in range(LEARN_MAX_SOURCES_PER_FINDING + 1):
        source = deepcopy(_finding()["sources"][0])
        source["job_id"] += index
        sources.append(source)
    finding = _finding(
        location={"path": "tests/test_brain_learn.py", "line": 1},
        sources=sources[:-1],
    )
    payload = _minimal_payload(goal_id=LEARN_GOAL_ID, findings=[finding])
    normalized = validate_feedback(payload, load_goal(LEARN_GOAL_PATH))
    assert len(normalized["findings"][0]["sources"]) == LEARN_MAX_SOURCES_PER_FINDING
    finding["sources"] = sources
    with pytest.raises(FeedbackViolation, match="sources count"):
        validate_feedback(payload, load_goal(LEARN_GOAL_PATH))


def test_prove_keeps_the_default_source_bound() -> None:
    sources = []
    for index in range(MAX_SOURCES_PER_FINDING + 1):
        source = deepcopy(_finding()["sources"][0])
        source["job_id"] += index
        sources.append(source)
    goal = load_goal(PROVE_GOAL_PATH)
    finding = _finding(
        location={"path": "tests/test_brain_prove.py", "line": 1},
        sources=sources[:-1],
    )
    payload = _minimal_payload(goal_id=goal.goal_id, findings=[finding])
    normalized = validate_feedback(payload, goal)
    assert len(normalized["findings"][0]["sources"]) == MAX_SOURCES_PER_FINDING
    finding["sources"] = sources
    with pytest.raises(FeedbackViolation, match="sources count"):
        validate_feedback(payload, goal)


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


@pytest.mark.parametrize("goal_path", [GOAL_PATH, PROVE_GOAL_PATH])
def test_non_learn_size_is_checked_before_json_decoding(
    tmp_path: Path,
    goal_path: Path,
) -> None:
    path = tmp_path / "bounded.json"
    path.write_bytes(b"{" + b"x" * (MAX_FEEDBACK_BYTES - 1))
    with pytest.raises(FeedbackViolation, match="not valid UTF-8 JSON"):
        load_feedback(path, load_goal(goal_path))
    path.write_bytes(b"{" + b"x" * MAX_FEEDBACK_BYTES)
    with pytest.raises(FeedbackViolation, match="size limit"):
        load_feedback(path, load_goal(goal_path))


def test_learn_size_has_a_separate_authorized_bound(tmp_path: Path) -> None:
    path = tmp_path / "learn-bounded.json"
    goal = load_goal(LEARN_GOAL_PATH)
    sources = []
    for index in range(MAX_SOURCES_PER_FINDING):
        source = deepcopy(_finding()["sources"][0])
        source["job_id"] += index
        source["stage"] = "gate"
        sources.append(source)
    findings = [
        _finding(
            id=f"finding-{index}",
            title="T",
            detail="D",
            location={"path": "tests/test_brain_learn.py", "line": 1},
            sources=deepcopy(sources),
        )
        for index in range(LEARN_MAX_FINDINGS)
    ]
    payload = _minimal_payload(goal_id=LEARN_GOAL_ID, findings=findings)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    remaining = LEARN_MAX_FEEDBACK_BYTES - len(raw)
    for finding in findings:
        extension = min(remaining, 3000 - len(finding["detail"]))
        finding["detail"] += "x" * extension
        remaining -= extension
    assert remaining == 0
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert len(raw) == LEARN_MAX_FEEDBACK_BYTES
    path.write_bytes(raw)
    assert load_feedback(path, goal)["goal_id"] == LEARN_GOAL_ID
    path.write_bytes(raw + b" ")
    with pytest.raises(FeedbackViolation, match="size limit"):
        load_feedback(path, goal)


def test_learn_canonical_size_cannot_exceed_the_authorized_bound(tmp_path: Path) -> None:
    path = tmp_path / "learn-canonical-bounded.json"
    goal = load_goal(LEARN_GOAL_PATH)
    findings = [
        _finding(
            id=f"finding-{index}",
            title="T",
            detail="D",
            location={"path": "tests/test_brain_learn.py", "line": 1},
        )
        for index in range(LEARN_MAX_FINDINGS)
    ]
    payload = _minimal_payload(goal_id=LEARN_GOAL_ID, findings=findings)
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    remaining = LEARN_MAX_FEEDBACK_BYTES - len(raw)
    for finding in findings:
        extension = min(remaining, 3000 - len(finding["detail"]))
        finding["detail"] += "x" * extension
        remaining -= extension
    assert remaining == 0
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(raw) == LEARN_MAX_FEEDBACK_BYTES
    path.write_bytes(raw)
    with pytest.raises(FeedbackViolation, match="canonical feedback exceeds"):
        load_feedback(path, goal)


def test_bom_nul_and_invalid_utf8_are_rejected(tmp_path: Path) -> None:
    for index, raw in enumerate((b"\xef\xbb\xbf{}", b'{"x":"\x00"}', b"\xff")):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(raw)
        with pytest.raises(FeedbackViolation):
            load_feedback(path, _goal())


def test_canonical_bytes_and_hash_ignore_input_order() -> None:
    first_payload = _payload()
    second_payload = deepcopy(first_payload)
    second_payload["findings"].reverse()
    for finding in second_payload["findings"]:
        finding["sources"].reverse()
    first = canonical_feedback_bytes(validate_feedback(first_payload, _goal()))
    second = canonical_feedback_bytes(validate_feedback(second_payload, _goal()))
    assert first == second
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_materializer_writes_only_fixed_complete_mode_0600_output(tmp_path: Path) -> None:
    control = _copy_control(tmp_path)
    repo = _copy_candidate(tmp_path)
    result = materialize_feedback(repo, control, GOAL_RELATIVE)
    output = repo / OUTPUT_RELATIVE
    assert result.found is True
    assert result.finding_count == 4
    assert result.output == OUTPUT_RELATIVE.as_posix()
    assert output.read_bytes() == canonical_feedback_bytes(load_feedback(FEEDBACK_PATH, _goal()))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert result.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert not list(output.parent.glob("*.tmp"))
    with pytest.raises(FeedbackViolation, match="already exists|stale"):
        materialize_feedback(repo, control, GOAL_RELATIVE)


def test_missing_feedback_produces_no_context_and_rejects_stale_context(tmp_path: Path) -> None:
    control = _copy_control(tmp_path, include_feedback=False)
    repo = _copy_candidate(tmp_path)
    result = materialize_feedback(repo, control, GOAL_RELATIVE)
    assert result.found is False
    assert result.sha256 == hashlib.sha256(b"").hexdigest()
    assert not (repo / OUTPUT_RELATIVE).exists()
    (repo / OUTPUT_RELATIVE.parent).mkdir()
    (repo / OUTPUT_RELATIVE).write_text("{}", encoding="utf-8")
    with pytest.raises(FeedbackViolation, match="stale"):
        materialize_feedback(repo, control, GOAL_RELATIVE)


def test_feedback_leaf_and_control_components_must_not_be_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    shutil.copy2(FEEDBACK_PATH, target)
    link = tmp_path / "feedback.json"
    link.symlink_to(target)
    with pytest.raises(FeedbackViolation, match="non-symlink"):
        load_feedback(link, _goal())

    control = _copy_control(tmp_path / "nested", include_feedback=False)
    external = tmp_path / "external"
    (external / "autopilot/feedback").mkdir(parents=True)
    shutil.copy2(FEEDBACK_PATH, external / "autopilot/feedback" / FEEDBACK_PATH.name)
    (control / ".github").symlink_to(external, target_is_directory=True)
    repo = _copy_candidate(tmp_path)
    with pytest.raises(FeedbackViolation, match="symlink"):
        materialize_feedback(repo, control, GOAL_RELATIVE)


def test_output_parent_must_not_be_a_symlink(tmp_path: Path) -> None:
    control = _copy_control(tmp_path)
    repo = _copy_candidate(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / OUTPUT_RELATIVE.parent).symlink_to(outside, target_is_directory=True)
    with pytest.raises(FeedbackViolation, match="unsafe"):
        materialize_feedback(repo, control, GOAL_RELATIVE)
    assert not (outside / OUTPUT_RELATIVE.name).exists()


def test_repo_root_path_must_not_traverse_a_symlinked_component(tmp_path: Path) -> None:
    control = _copy_control(tmp_path)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    _copy_candidate(real_parent)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(FeedbackViolation, match="symlinked components"):
        materialize_feedback(linked_parent / "candidate", control, GOAL_RELATIVE)


def test_non_regular_feedback_is_rejected_without_opening_it(tmp_path: Path) -> None:
    directory = tmp_path / "feedback.json"
    directory.mkdir()
    with pytest.raises(FeedbackViolation, match="regular file"):
        load_feedback(directory, _goal())
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "feedback.fifo"
        os.mkfifo(fifo)
        with pytest.raises(FeedbackViolation, match="regular file"):
            load_feedback(fifo, _goal())


def test_cli_emits_only_bounded_summary_and_safe_github_outputs(tmp_path: Path, capsys) -> None:
    control = _copy_control(tmp_path)
    repo = _copy_candidate(tmp_path)
    github_output = tmp_path / "github-output"
    github_output.touch(mode=0o600)
    assert main(
        [
            "--repo",
            str(repo),
            "--control-root",
            str(control),
            "--goal",
            GOAL_RELATIVE,
            "--github-output",
            str(github_output),
        ]
    ) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    expected_sha256 = hashlib.sha256(
        canonical_feedback_bytes(load_feedback(FEEDBACK_PATH, _goal()))
    ).hexdigest()
    assert summary == {
        "found": True,
        "goal_id": GOAL_ID,
        "finding_count": 4,
        "sha256": expected_sha256,
        "output": None,
    }
    assert not (repo / OUTPUT_RELATIVE).exists()
    assert "Future" not in captured.out
    assert "detail" not in captured.out
    values = dict(line.split("=", 1) for line in github_output.read_text().splitlines())
    assert values["has_feedback"] == "true"
    assert values["feedback_count"] == "4"
    assert values["feedback_path"] == ""
    assert values["feedback_sha256"] == summary["sha256"]

    assert main(
        [
            "--repo",
            str(repo),
            "--control-root",
            str(control),
            "--goal",
            GOAL_RELATIVE,
            "--expected-sha256",
            expected_sha256,
            "--materialize",
        ]
    ) == 0
    materialized = json.loads(capsys.readouterr().out)
    assert materialized["output"] == OUTPUT_RELATIVE.as_posix()
    assert hashlib.sha256((repo / OUTPUT_RELATIVE).read_bytes()).hexdigest() == expected_sha256


def test_prepare_digest_is_required_and_must_match_before_materialization(tmp_path: Path) -> None:
    control = _copy_control(tmp_path)
    repo = _copy_candidate(tmp_path)
    with pytest.raises(FeedbackViolation, match="changed after prepare"):
        materialize_feedback(repo, control, GOAL_RELATIVE, "0" * 64)
    assert not (repo / OUTPUT_RELATIVE).exists()


def test_feedback_is_bound_to_the_candidate_goal_not_a_control_copy(tmp_path: Path) -> None:
    control = _copy_control(tmp_path)
    repo = _copy_candidate(tmp_path)
    control_goal = control / GOAL_RELATIVE
    divergent = json.loads(control_goal.read_text(encoding="utf-8"))
    divergent["allowed_paths"] = ["docs/"]
    control_goal.write_text(json.dumps(divergent), encoding="utf-8")

    result = materialize_feedback(repo, control, GOAL_RELATIVE, write_output=False)
    assert result.found is True
    (repo / GOAL_RELATIVE).write_text("{}", encoding="utf-8")
    with pytest.raises(FeedbackViolation, match="selected candidate goal is invalid"):
        materialize_feedback(repo, control, GOAL_RELATIVE, write_output=False)


def test_cli_requires_the_hash_and_materialize_flags_as_a_pair(tmp_path: Path, capsys) -> None:
    control = _copy_control(tmp_path)
    repo = _copy_candidate(tmp_path)
    common = [
        "--repo",
        str(repo),
        "--control-root",
        str(control),
        "--goal",
        GOAL_RELATIVE,
    ]
    assert main(common + ["--materialize"]) == 2
    assert main(common + ["--expected-sha256", "0" * 64]) == 2
    assert not (repo / OUTPUT_RELATIVE).exists()
    assert capsys.readouterr().err.count("must be supplied together") == 2


def test_validator_has_no_network_subprocess_or_dynamic_execution_imports() -> None:
    source = (PROJECT / "src/atp/autopilot/feedback.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"socket", "subprocess", "requests", "urllib", "httpx", "shlex"}
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert imported.isdisjoint(forbidden)
    assert called.isdisjoint({"eval", "exec", "compile", "__import__"})


def test_canonical_feedback_contains_no_scope_or_action_fields() -> None:
    normalized = load_feedback(FEEDBACK_PATH, _goal())
    serialized = canonical_feedback_bytes(normalized).decode("utf-8")
    for forbidden in ("allowed_paths", '"tools"', '"instructions"', '"patch"', '"content"'):
        assert forbidden not in serialized
    allowed = _goal().allowed_paths
    policy = AutopilotPolicy()
    assert all(
        policy.classify_path(finding["location"]["path"], goal_paths=allowed).allowed
        for finding in normalized["findings"]
    )
