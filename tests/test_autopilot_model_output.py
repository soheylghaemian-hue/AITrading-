from __future__ import annotations

import ast
import hashlib
import json
import stat
import subprocess
from pathlib import Path

import pytest

from atp.autopilot import model_output
from atp.autopilot.full_file import prepare_state
from atp.autopilot.model_output import (
    CLAUDE_PHASES,
    PHASE_PATHS,
    ModelOutputViolation,
    bind_model_output,
    canonical_phase_bytes,
    main,
    validate_phase_output,
    verify_model_output,
)

PROJECT = Path(__file__).parents[1]

PLAN = {
    "objective": "Implement the selected research-only goal.",
    "instructions_for_claude": ["Keep the change deterministic."],
    "acceptance_tests": ["The focused tests pass."],
    "risks": ["Reject malformed evidence."],
}
EDIT = {
    "contract_version": "full-file-edit/v1",
    "author": "claude",
    "phase": "author",
    "base_sha": "b" * 40,
    "input_state_sha256": "c" * 64,
    "parent_patch_sha256": None,
    "edits": [
        {
            "op": "modify",
            "path": "src/atp/research/example.py",
            "before_sha256": "d" * 64,
            "content": "new\n",
        }
    ],
}
REVIEW = {
    "approved": False,
    "summary": "One blocking invariant remains.",
    "findings": [
        {
            "severity": "P1",
            "title": "Fail closed",
            "detail": "The boundary needs a deterministic rejection test.",
            "file": "src/atp/example.py",
        }
    ],
}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(mode=0o755, parents=True)
    (repo / ".autopilot").mkdir(mode=0o700)
    (repo / ".github/autopilot/goals").mkdir(mode=0o755, parents=True)
    (repo / ".github/autopilot/goals/test.json").write_text(
        json.dumps(
            {
                "goal_id": "test-goal",
                "objective": "Safe test edit",
                "success_criteria": ["tests pass"],
                "allowed_paths": ["src/atp/research/"],
                "max_iterations": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "src/atp/research").mkdir(mode=0o755, parents=True)
    (repo / "src/atp/research/example.py").write_text("old\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(repo)), check=True)
    subprocess.run(("git", "-C", str(repo), "config", "user.email", "test@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(repo), "config", "user.name", "Test"), check=True)
    subprocess.run(("git", "-C", str(repo), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repo), "commit", "-qm", "base"), check=True)
    return repo


GOAL_FILE = ".github/autopilot/goals/test.json"
CONTROL_SHA = "a" * 40


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ("git", "-C", str(repo), *args), check=True, capture_output=True
    ).stdout


def _prepare_claude_payload(repo: Path, phase: str, *, content: str = "new\n") -> dict:
    base_sha = _git(repo, "rev-parse", "HEAD").decode().strip()
    if phase == "repair":
        (repo / "src/atp/research/example.py").write_text("candidate\n", encoding="utf-8")
        (repo / ".autopilot/candidate.patch").write_bytes(
            _git(
                repo,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--full-index",
                "--binary",
                "HEAD",
            )
        )
    prepare_state(repo, GOAL_FILE, phase, base_sha, CONTROL_SHA)
    state = json.loads((repo / f".autopilot/{phase}-edit-state.json").read_text())
    files = {entry["path"]: entry for entry in state["input_state"]["files"]}
    return {
        "contract_version": "full-file-edit/v1",
        "author": "claude",
        "phase": phase,
        "base_sha": base_sha,
        "input_state_sha256": state["input_state_sha256"],
        "parent_patch_sha256": state["input_state"]["parent_patch_sha256"],
        "edits": [
            {
                "op": "modify",
                "path": "src/atp/research/example.py",
                "before_sha256": files["src/atp/research/example.py"]["sha256"],
                "content": content,
            }
        ],
    }


def _write_fixed(
    repo: Path,
    phase: str,
    payload: object | None = None,
    *,
    raw: bytes | None = None,
    mode: int = 0o644,
) -> Path:
    path = repo / PHASE_PATHS[phase]
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(raw if raw is not None else json.dumps(payload).encode("utf-8"))
    path.chmod(mode)
    return path


def _runner_temp(tmp_path: Path) -> Path:
    runner = tmp_path / "runner-temp"
    runner.mkdir(mode=0o700)
    return runner


def _write_execution(
    runner: Path,
    structured_output: object = EDIT,
    *,
    events: list[object] | None = None,
    mode: int = 0o600,
) -> Path:
    if events is None:
        events = [
            {"type": "system", "subtype": "init", "session_id": "bounded"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": structured_output,
                "total_cost_usd": 1.25,
            },
        ]
    path = runner / "claude-execution-output.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    path.chmod(mode)
    return path


def _github_output(tmp_path: Path) -> Path:
    output = tmp_path / "github-output"
    output.touch(mode=0o600)
    output.chmod(0o600)
    return output


def test_fixed_phase_paths_are_the_only_transport_destinations() -> None:
    assert PHASE_PATHS == {
        "plan": Path(".autopilot/plan.json"),
        "author": Path(".autopilot/claude.json"),
        "review": Path(".autopilot/review.json"),
        "repair": Path(".autopilot/repair.json"),
        "final_review": Path(".autopilot/final-review.json"),
    }
    assert CLAUDE_PHASES == {"author", "repair"}
    with pytest.raises(ModelOutputViolation, match="fixed model phases"):
        validate_phase_output("../../outside", PLAN)


def test_plan_bind_canonicalizes_atomically_and_verify_does_not_rewrite(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    source = _write_fixed(repo, "plan", PLAN)
    original_inode = source.stat().st_ino

    bound = bind_model_output(repo, "plan")

    expected = canonical_phase_bytes("plan", PLAN)
    assert source.read_bytes() == expected
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert source.stat().st_ino != original_inode
    assert bound.sha256 == hashlib.sha256(expected).hexdigest()
    verified_inode = source.stat().st_ino
    verified = verify_model_output(repo, "plan", bound.sha256)
    assert verified == bound
    assert source.stat().st_ino == verified_inode


@pytest.mark.parametrize(
    "phase,payload",
    [
        ("plan", PLAN),
        ("review", REVIEW),
        ("final_review", {"approved": True, "summary": "Approved.", "findings": []}),
    ],
)
def test_non_claude_phase_schemas_bind_and_verify(
    tmp_path: Path, phase: str, payload: dict
) -> None:
    repo = _repo(tmp_path)
    _write_fixed(repo, phase, payload)
    bound = bind_model_output(repo, phase)
    assert verify_model_output(repo, phase, bound.sha256) == bound
    assert bound.approved == (payload["approved"] if phase != "plan" else None)


@pytest.mark.parametrize("phase", ["author", "repair"])
def test_claude_payload_comes_only_from_one_final_success_event(
    tmp_path: Path, phase: str
) -> None:
    repo = _repo(tmp_path)
    runner = _runner_temp(tmp_path)
    payload = _prepare_claude_payload(repo, phase)
    execution = _write_execution(runner, payload)

    bound = bind_model_output(
        repo,
        phase,
        goal_file=GOAL_FILE,
        execution_file=execution,
        runner_temp=runner,
    )

    output = repo / PHASE_PATHS[phase]
    assert output.read_bytes() == canonical_phase_bytes(phase, payload)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert verify_model_output(repo, phase, bound.sha256, goal_file=GOAL_FILE) == bound
    assert bound.approved is None


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"type": "assistant"}],
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": EDIT,
            },
            {"type": "assistant"},
        ],
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": EDIT,
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": EDIT,
            },
        ],
        [
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": False,
                "structured_output": EDIT,
            }
        ],
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "structured_output": EDIT,
            }
        ],
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": None,
            }
        ],
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": json.dumps(EDIT),
            }
        ],
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": json.dumps(EDIT),
            }
        ],
        ["not-an-event"],
    ],
)
def test_malformed_or_ambiguous_claude_execution_fails_closed(
    tmp_path: Path, events: list[object]
) -> None:
    repo = _repo(tmp_path)
    runner = _runner_temp(tmp_path)
    execution = _write_execution(runner, events=events)
    with pytest.raises(ModelOutputViolation):
        bind_model_output(
            repo,
            "author",
            execution_file=execution,
            runner_temp=runner,
        )
    assert not (repo / PHASE_PATHS["author"]).exists()


def test_action_output_or_other_file_cannot_substitute_for_execution_file(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _write_fixed(repo, "author", EDIT)
    with pytest.raises(ModelOutputViolation, match="execution_file"):
        bind_model_output(repo, "author")


def test_execution_file_must_be_absolute_confined_regular_and_safe_mode(
    tmp_path: Path,
) -> None:
    runner = _runner_temp(tmp_path)

    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    outside.chmod(0o600)
    with pytest.raises(ModelOutputViolation, match="confined"):
        bind_model_output(
            _repo(tmp_path / "outside-case"),
            "author",
            execution_file=outside,
            runner_temp=runner,
        )

    execution = _write_execution(runner)
    with pytest.raises(ModelOutputViolation, match="absolute"):
        bind_model_output(
            _repo(tmp_path / "relative-case"),
            "author",
            execution_file=Path(execution.name),
            runner_temp=runner,
        )

    execution.chmod(0o666)
    with pytest.raises(ModelOutputViolation, match="permissions"):
        bind_model_output(
            _repo(tmp_path / "mode-case"),
            "author",
            execution_file=execution,
            runner_temp=runner,
        )


def test_execution_file_and_components_must_not_be_symlinks(tmp_path: Path) -> None:
    runner = _runner_temp(tmp_path)
    real = _write_execution(runner)
    link = runner / "linked-execution.json"
    link.symlink_to(real)
    with pytest.raises(ModelOutputViolation, match="symlink"):
        bind_model_output(
            _repo(tmp_path / "leaf-link"),
            "author",
            execution_file=link,
            runner_temp=runner,
        )

    actual_dir = runner / "actual"
    actual_dir.mkdir(mode=0o700)
    nested = _write_execution(actual_dir)
    linked_dir = runner / "linked"
    linked_dir.symlink_to(actual_dir, target_is_directory=True)
    with pytest.raises(ModelOutputViolation, match="symlink"):
        bind_model_output(
            _repo(tmp_path / "component-link"),
            "author",
            execution_file=linked_dir / nested.name,
            runner_temp=runner,
        )


def test_execution_file_size_is_bounded_before_json_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    runner = _runner_temp(tmp_path)
    execution = runner / "large.json"
    execution.write_bytes(b"[" + b"x" * 32)
    execution.chmod(0o600)
    monkeypatch.setattr(model_output, "MAX_EXECUTION_FILE_BYTES", 16)
    with pytest.raises(ModelOutputViolation, match="size limit"):
        bind_model_output(
            repo,
            "author",
            execution_file=execution,
            runner_temp=runner,
        )


@pytest.mark.parametrize(
    "raw,match",
    [
        (
            (
                b'{"objective":"a","objective":"b","instructions_for_claude":["x"],'
                b'"acceptance_tests":["x"],"risks":[]}'
            ),
            "duplicate",
        ),
        (
            (
                b'{"objective":NaN,"instructions_for_claude":["x"],'
                b'"acceptance_tests":["x"],"risks":[]}'
            ),
            "non-finite",
        ),
        (
            (
                b'{"objective":1e999,"instructions_for_claude":["x"],'
                b'"acceptance_tests":["x"],"risks":[]}'
            ),
            "non-finite",
        ),
    ],
)
def test_strict_json_rejects_duplicate_keys_and_all_nonfinite_numbers(
    tmp_path: Path, raw: bytes, match: str
) -> None:
    repo = _repo(tmp_path)
    _write_fixed(repo, "plan", raw=raw)
    with pytest.raises(ModelOutputViolation, match=match):
        bind_model_output(repo, "plan")


def test_claude_execution_json_is_also_strict(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    runner = _runner_temp(tmp_path)
    execution = runner / "claude-execution-output.json"
    execution.write_bytes(
        b'[{"type":"result","subtype":"success","is_error":false,'
        b'"structured_output":{"author":"claude","author":"claude",'
        b'"patch":"diff --git x","changed_files":["x"]}}]'
    )
    execution.chmod(0o600)
    with pytest.raises(ModelOutputViolation, match="duplicate"):
        bind_model_output(
            repo,
            "author",
            execution_file=execution,
            runner_temp=runner,
        )


@pytest.mark.parametrize(
    "phase,payload",
    [
        ("plan", {**PLAN, "extra": "forbidden"}),
        ("plan", {**PLAN, "instructions_for_claude": []}),
        ("author", {**EDIT, "author": "codex"}),
        ("author", {**EDIT, "contract_version": "unknown"}),
        ("author", {**EDIT, "extra": "forbidden"}),
        (
            "author",
            {
                **EDIT,
                "edits": [{**EDIT["edits"][0], "path": "../outside.py"}],
            },
        ),
        ("review", {**REVIEW, "approved": "false"}),
        ("review", {**REVIEW, "approved": True}),
        (
            "final_review",
            {
                "approved": False,
                "summary": "No.",
                "findings": [{**REVIEW["findings"][0], "severity": "P4"}],
            },
        ),
    ],
)
def test_every_phase_schema_fails_closed_on_invalid_contract(
    phase: str, payload: dict
) -> None:
    with pytest.raises(ModelOutputViolation):
        canonical_phase_bytes(phase, payload)


def test_existing_or_unsafe_fixed_output_is_never_overwritten_for_claude(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    payload = _prepare_claude_payload(repo, "author")
    stale = _write_fixed(repo, "author", EDIT, raw=b"stale", mode=0o600)
    runner = _runner_temp(tmp_path)
    execution = _write_execution(runner, payload)
    with pytest.raises(ModelOutputViolation, match="stale"):
        bind_model_output(
            repo,
            "author",
            goal_file=GOAL_FILE,
            execution_file=execution,
            runner_temp=runner,
        )
    assert stale.read_bytes() == b"stale"

    second_repo = _repo(tmp_path / "parent-link")
    (second_repo / ".autopilot").rmdir()
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    (second_repo / ".autopilot").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ModelOutputViolation, match="symlink|unsafe"):
        bind_model_output(
            second_repo,
            "author",
            goal_file=GOAL_FILE,
            execution_file=execution,
            runner_temp=runner,
        )
    assert not (outside / "claude.json").exists()


def test_non_claude_source_must_be_regular_non_symlink_and_safe_mode(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    external = tmp_path / "plan.json"
    external.write_text(json.dumps(PLAN), encoding="utf-8")
    target = repo / PHASE_PATHS["plan"]
    target.symlink_to(external)
    with pytest.raises(ModelOutputViolation, match="symlink"):
        bind_model_output(repo, "plan")
    target.unlink()
    _write_fixed(repo, "plan", PLAN, mode=0o755)
    with pytest.raises(ModelOutputViolation, match="permissions"):
        bind_model_output(repo, "plan")


def test_repo_root_must_not_traverse_a_symlinked_component(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    repo = _repo(real_parent)
    _write_fixed(repo, "plan", PLAN)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ModelOutputViolation, match="symlinked components"):
        bind_model_output(linked_parent / "repo", "plan")


def test_verify_accepts_read_only_artifact_mode_but_rejects_tampering_and_bad_digest(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    path = _write_fixed(repo, "plan", PLAN)
    bound = bind_model_output(repo, "plan")

    with pytest.raises(ModelOutputViolation, match="digest"):
        verify_model_output(repo, "plan", "0" * 64)
    with pytest.raises(ModelOutputViolation, match="lowercase hex"):
        verify_model_output(repo, "plan", "A" * 64)

    # upload/download-artifact does not preserve producer mode 0600 and normally
    # recreates downloaded files as safe, non-writable-by-others mode 0644.
    path.chmod(0o644)
    assert verify_model_output(repo, "plan", bound.sha256) == bound
    path.chmod(0o600)
    path.write_text(json.dumps(PLAN, indent=2), encoding="utf-8")
    with pytest.raises(ModelOutputViolation, match="canonical byte form"):
        verify_model_output(repo, "plan", hashlib.sha256(path.read_bytes()).hexdigest())

    canonical = canonical_phase_bytes("plan", PLAN)
    path.write_bytes(canonical.replace(b"research-only", b"research only"))
    path.chmod(0o600)
    with pytest.raises(ModelOutputViolation, match="bound SHA-256"):
        verify_model_output(repo, "plan", bound.sha256)


def test_cli_emits_only_digest_and_review_decision_never_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "PRIVATE-MODEL-PAYLOAD-MARKER"
    repo = _repo(tmp_path)
    edit = _prepare_claude_payload(repo, "author", content=f"{marker}\n")
    runner = _runner_temp(tmp_path)
    execution = _write_execution(runner, edit)
    github_output = _github_output(tmp_path)

    assert main(
        [
            "--repo",
            str(repo),
            "--phase",
            "author",
            "--bind",
            "--goal",
            GOAL_FILE,
            "--execution-file",
            str(execution),
            "--runner-temp",
            str(runner),
            "--github-output",
            str(github_output),
        ]
    ) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert set(summary) == {"mode", "phase", "sha256"}
    assert marker not in captured.out
    assert marker not in captured.err
    github_values = dict(
        line.split("=", 1) for line in github_output.read_text(encoding="utf-8").splitlines()
    )
    assert github_values == {"model_sha256": summary["sha256"]}
    assert marker not in github_output.read_text(encoding="utf-8")

    review_repo = _repo(tmp_path / "review-case")
    _write_fixed(review_repo, "review", REVIEW)
    review_output = _github_output(tmp_path / "review-case")
    assert main(
        [
            "--repo",
            str(review_repo),
            "--phase",
            "review",
            "--bind",
            "--github-output",
            str(review_output),
        ]
    ) == 0
    review_values = dict(
        line.split("=", 1) for line in review_output.read_text().splitlines()
    )
    assert set(review_values) == {"model_sha256", "approved"}
    assert review_values["approved"] == "false"


def test_cli_errors_never_echo_rejected_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "DO-NOT-ECHO-THIS-PAYLOAD"
    repo = _repo(tmp_path)
    _write_fixed(repo, "plan", raw=(marker + " not json").encode("utf-8"))
    github_output = _github_output(tmp_path)
    assert main(
        [
            "--repo",
            str(repo),
            "--phase",
            "plan",
            "--bind",
            "--github-output",
            str(github_output),
        ]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert marker not in captured.err
    assert "model_output_validation_failed" in captured.err
    assert github_output.read_bytes() == b""


def test_cli_requires_boundaries_for_each_mode(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    _write_fixed(repo, "plan", PLAN)
    assert main(["--repo", str(repo), "--phase", "plan", "--bind"]) == 2
    assert main(["--repo", str(repo), "--phase", "plan", "--verify"]) == 2
    github_output = _github_output(tmp_path)
    assert main(
        [
            "--repo",
            str(repo),
            "--phase",
            "plan",
            "--verify",
            "--expected-sha256",
            "0" * 64,
            "--github-output",
            str(github_output),
        ]
    ) == 2
    assert capsys.readouterr().err.count("model_output_validation_failed") == 3


def test_github_output_must_be_regular_private_from_group_writes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write_fixed(repo, "plan", PLAN)
    github_output = _github_output(tmp_path)
    github_output.chmod(0o666)
    with pytest.raises(ModelOutputViolation, match="permissions"):
        result = bind_model_output(repo, "plan")
        model_output._append_github_output(github_output, result)


def test_transport_uses_only_stdlib_and_no_dynamic_or_process_execution() -> None:
    source = (PROJECT / "src/atp/autopilot/model_output.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_imports = {
        "requests",
        "urllib",
        "httpx",
        "socket",
        "subprocess",
        "shlex",
    }
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert imported.isdisjoint(forbidden_imports)
    assert called.isdisjoint({"eval", "exec", "compile", "__import__"})


def test_no_payload_named_cli_argument_or_environment_fallback_exists() -> None:
    source = (PROJECT / "src/atp/autopilot/model_output.py").read_text(encoding="utf-8")
    assert "PATCH_JSON" not in source
    assert "PLAN_JSON" not in source
    assert "REVIEW_JSON" not in source
    assert "structured-output" not in source
    assert "--payload" not in source
    assert "os.environ" not in source
