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
    assert {
        finding["location"]["path"] for finding in normalized["findings"]
    } == {"src/atp/brain/prove.py", "tests/test_brain_prove.py"}


def test_schema_is_strict_and_matches_validator_bounds() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["kind"]["const"] == FEEDBACK_KIND
    assert schema["properties"]["findings"]["maxItems"] == MAX_FINDINGS
    assert schema["$defs"]["finding"]["additionalProperties"] is False
    assert schema["$defs"]["source"]["additionalProperties"] is False
    assert schema["$defs"]["source"]["properties"]["stage"]["enum"] == [
        "artifact_audit",
        "gate",
        "initial_review",
        "final_review",
    ]
    assert "does not assert" in schema["$defs"]["source"]["properties"]["stage"]["description"]
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
