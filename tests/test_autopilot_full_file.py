from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from atp.autopilot import full_file
from atp.autopilot.full_file import (
    CONTRACT_VERSION,
    FullFileViolation,
    materialize,
    prepare_state,
    validate_edit_output,
    validate_output_against_state,
)

GOAL_FILE = ".github/autopilot/goals/test.json"
CONTROL_SHA = "a" * 40


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ("git", "-C", str(repo), *args), check=True, capture_output=True
    ).stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / ".github/autopilot/goals").mkdir(parents=True)
    (repo / ".autopilot").mkdir(mode=0o700)
    (repo / "src/atp/research").mkdir(parents=True)
    (repo / ".gitignore").write_text(".autopilot/\n", encoding="utf-8")
    (repo / ".github/autopilot/goals/test.json").write_text(
        json.dumps(
            {
                "goal_id": "safe-test",
                "objective": "Modify research-only files",
                "success_criteria": ["tests pass"],
                "allowed_paths": ["src/atp/research/"],
                "max_iterations": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "src/atp/research/base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "src/atp/research/obsolete.py").write_text("OLD = True\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(repo)), check=True)
    subprocess.run(("git", "-C", str(repo), "config", "user.email", "test@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(repo), "config", "user.name", "Test"), check=True)
    subprocess.run(("git", "-C", str(repo), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repo), "commit", "-qm", "base"), check=True)
    return repo, _git(repo, "rev-parse", "HEAD").decode().strip()


def _state(repo: Path, phase: str) -> dict:
    return json.loads((repo / f".autopilot/{phase}-edit-state.json").read_text())


def _edit(
    repo: Path,
    phase: str,
    edits: list[dict],
) -> dict:
    state = _state(repo, phase)
    return {
        "contract_version": CONTRACT_VERSION,
        "author": "claude",
        "phase": phase,
        "base_sha": state["input_state"]["base_sha"],
        "input_state_sha256": state["input_state_sha256"],
        "parent_patch_sha256": state["input_state"]["parent_patch_sha256"],
        "edits": edits,
    }


def _before(repo: Path, phase: str, path: str) -> str:
    entries = {entry["path"]: entry for entry in _state(repo, phase)["input_state"]["files"]}
    return entries[path]["sha256"]


def _write_model(repo: Path, phase: str, payload: dict) -> str:
    path = repo / (".autopilot/claude.json" if phase == "author" else ".autopilot/repair.json")
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def test_prepare_state_is_canonical_goal_bound_and_scope_limited(tmp_path: Path) -> None:
    repo, base_sha = _repo(tmp_path)
    digest = prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    raw = (repo / ".autopilot/author-edit-state.json").read_bytes()
    state = json.loads(raw)

    assert raw == (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert digest == state["input_state_sha256"]
    assert state["input_state"]["base_sha"] == base_sha
    assert state["input_state"]["control_sha"] == CONTROL_SHA
    assert state["input_state"]["parent_patch_sha256"] is None
    assert [entry["path"] for entry in state["input_state"]["files"]] == [
        "src/atp/research/base.py",
        "src/atp/research/obsolete.py",
    ]
    assert state["input_state"]["directories"] == [
        "src",
        "src/atp",
        "src/atp/research",
    ]
    assert stat.S_IMODE((repo / ".autopilot/author-edit-state.json").stat().st_mode) == 0o600


def test_materializer_preflights_then_modifies_and_creates(tmp_path: Path) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    payload = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 2\n",
            },
            {
                "op": "create",
                "path": "src/atp/research/new.py",
                "before_sha256": None,
                "content": "NEW = True\n",
            },
        ],
    )
    digest = _write_model(repo, "author", payload)

    assert materialize(repo, GOAL_FILE, "author", digest) == [
        "src/atp/research/base.py",
        "src/atp/research/new.py",
    ]
    assert (repo / "src/atp/research/base.py").read_text() == "VALUE = 2\n"
    assert (repo / "src/atp/research/new.py").read_text() == "NEW = True\n"
    assert (repo / "src/atp/research/obsolete.py").read_text() == "OLD = True\n"
    assert stat.S_IMODE((repo / "src/atp/research/new.py").stat().st_mode) == 0o644
    _git(repo, "add", "--intent-to-add", "--all")
    patch = _git(
        repo,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--full-index",
        "--binary",
        "HEAD",
    )
    assert patch.count(b"diff --git ") == 2


def test_live_state_change_fails_before_any_edit_is_written(tmp_path: Path) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    payload = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 2\n",
            },
            {
                "op": "create",
                "path": "src/atp/research/new.py",
                "before_sha256": None,
                "content": "NEW = True\n",
            },
        ],
    )
    digest = _write_model(repo, "author", payload)
    (repo / "src/atp/research/base.py").write_text("TAMPERED = True\n")

    with pytest.raises(FullFileViolation, match="state no longer matches"):
        materialize(repo, GOAL_FILE, "author", digest)
    assert not (repo / "src/atp/research/new.py").exists()


def test_late_preflight_failure_leaves_earlier_targets_unchanged(tmp_path: Path) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    payload = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 2\n",
            },
            {
                "op": "create",
                "path": "src/atp/research/missing/new.py",
                "before_sha256": None,
                "content": "NEW = True\n",
            },
        ],
    )
    digest = _write_model(repo, "author", payload)

    with pytest.raises(FullFileViolation, match="parent is not bound"):
        materialize(repo, GOAL_FILE, "author", digest)
    assert (repo / "src/atp/research/base.py").read_text() == "VALUE = 1\n"
    assert not (repo / "src/atp/research/missing").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "extra": "forbidden"},
        lambda value: {**value, "author": "codex"},
        lambda value: {**value, "phase": "repair"},
        lambda value: {
            **value,
            "edits": [{**value["edits"][0], "path": "../outside.py"}],
        },
        lambda value: {
            **value,
            "edits": [{**value["edits"][0], "content": "no terminal newline"}],
        },
        lambda value: {
            **value,
            "edits": [{**value["edits"][0], "content": "VALUE = '\ufeff'\n"}],
        },
        lambda value: {
            **value,
            "edits": [{**value["edits"][0], "before_sha256": None}],
        },
        lambda value: {
            **value,
            "edits": [{**value["edits"][0], "op": "delete", "content": None}],
        },
    ],
)
def test_model_contract_rejects_extra_fields_wrong_identity_paths_and_content(mutation) -> None:
    value = {
        "contract_version": CONTRACT_VERSION,
        "author": "claude",
        "phase": "author",
        "base_sha": "a" * 40,
        "input_state_sha256": "b" * 64,
        "parent_patch_sha256": None,
        "edits": [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": "c" * 64,
                "content": "VALUE = 2\n",
            }
        ],
    }
    with pytest.raises(FullFileViolation):
        validate_edit_output(mutation(value), "author")


def test_policy_and_preimage_are_checked_against_the_bound_state(tmp_path: Path) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    denied = _edit(
        repo,
        "author",
        [
            {
                "op": "create",
                "path": "src/atp/execution/order.py",
                "before_sha256": None,
                "content": "UNSAFE = True\n",
            }
        ],
    )
    with pytest.raises(FullFileViolation, match="denied"):
        validate_output_against_state(repo, GOAL_FILE, "author", denied)

    wrong = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": "f" * 64,
                "content": "VALUE = 2\n",
            }
        ],
    )
    with pytest.raises(FullFileViolation, match="preimage"):
        validate_output_against_state(repo, GOAL_FILE, "author", wrong)

    no_op = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 1\n",
            }
        ],
    )
    with pytest.raises(FullFileViolation, match="must change"):
        validate_output_against_state(repo, GOAL_FILE, "author", no_op)


def test_symlinks_hardlinks_and_unsafe_modes_fail_closed(tmp_path: Path) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    payload = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 2\n",
            }
        ],
    )
    digest = _write_model(repo, "author", payload)
    external = tmp_path / "external.py"
    external.write_text("VALUE = 1\n")
    (repo / "src/atp/research/base.py").unlink()
    os.link(external, repo / "src/atp/research/base.py")

    with pytest.raises(FullFileViolation, match="unsafe type or mode|unsafe type, mode"):
        materialize(repo, GOAL_FILE, "author", digest)


def test_parent_directory_swap_is_detected_before_atomic_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    payload = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 2\n",
            }
        ],
    )
    digest = _write_model(repo, "author", payload)
    original_write_temp = full_file._write_temp
    moved = tmp_path / "moved-parent"
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    swapped = False

    def swap_after_temp(parent_descriptor: int, content: str) -> str:
        nonlocal swapped
        temporary = original_write_temp(parent_descriptor, content)
        if not swapped:
            (repo / "src/atp/research").rename(moved)
            (repo / "src/atp/research").symlink_to(outside, target_is_directory=True)
            swapped = True
        return temporary

    monkeypatch.setattr(full_file, "_write_temp", swap_after_temp)
    with pytest.raises(FullFileViolation, match="edit parent"):
        materialize(repo, GOAL_FILE, "author", digest)
    assert not (outside / "base.py").exists()
    assert (moved / "base.py").read_text() == "VALUE = 1\n"
    assert not list(moved.glob(".autopilot-*.tmp"))


def test_repair_state_is_identical_before_and_after_trusted_baseline_commit(
    tmp_path: Path,
) -> None:
    repo, base_sha = _repo(tmp_path)
    (repo / "src/atp/research/base.py").write_text("CANDIDATE = True\n")
    parent_patch = _git(
        repo,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--full-index",
        "--binary",
        "HEAD",
    )
    (repo / ".autopilot/candidate.patch").write_bytes(parent_patch)
    prepare_state(repo, GOAL_FILE, "repair", base_sha, CONTROL_SHA)
    uncommitted_state = (repo / ".autopilot/repair-edit-state.json").read_bytes()
    (repo / ".autopilot/repair-edit-state.json").unlink()

    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Temporary candidate baseline")
    prepare_state(repo, GOAL_FILE, "repair", base_sha, CONTROL_SHA)
    assert (repo / ".autopilot/repair-edit-state.json").read_bytes() == uncommitted_state

    payload = _edit(
        repo,
        "repair",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "repair", "src/atp/research/base.py"),
                "content": "REPAIRED = True\n",
            }
        ],
    )
    digest = _write_model(repo, "repair", payload)
    assert materialize(repo, GOAL_FILE, "repair", digest) == [
        "src/atp/research/base.py"
    ]
    assert _git(
        repo,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--full-index",
        "--binary",
        "HEAD^",
    ).startswith(b"diff --git ")


def test_model_digest_and_state_artifacts_are_fail_closed(tmp_path: Path) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    payload = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 2\n",
            }
        ],
    )
    digest = _write_model(repo, "author", payload)
    with pytest.raises(FullFileViolation, match="digest"):
        materialize(repo, GOAL_FILE, "author", "0" * 64)

    state_path = repo / ".autopilot/author-edit-state.json"
    state_path.write_text(json.dumps(_state(repo, "author"), indent=2))
    with pytest.raises(FullFileViolation, match="not canonical"):
        materialize(repo, GOAL_FILE, "author", digest)


@pytest.mark.parametrize(
    "raw,match",
    [
        (
            b'{"input_state":{},"input_state":{},"input_state_sha256":"'
            + b"a" * 64
            + b'"}\n',
            "duplicate",
        ),
        (b"\xef\xbb\xbf{}", "plain UTF-8"),
        (
            b'{"input_state":{},"input_state_sha256":"x' + b"\x00" + b'"}',
            "plain UTF-8",
        ),
    ],
)
def test_state_json_rejects_duplicate_keys_bom_and_nul(
    tmp_path: Path, raw: bytes, match: str
) -> None:
    repo, base_sha = _repo(tmp_path)
    prepare_state(repo, GOAL_FILE, "author", base_sha, CONTROL_SHA)
    payload = _edit(
        repo,
        "author",
        [
            {
                "op": "modify",
                "path": "src/atp/research/base.py",
                "before_sha256": _before(repo, "author", "src/atp/research/base.py"),
                "content": "VALUE = 2\n",
            }
        ],
    )
    digest = _write_model(repo, "author", payload)
    (repo / ".autopilot/author-edit-state.json").write_bytes(raw)
    with pytest.raises(FullFileViolation, match=match):
        materialize(repo, GOAL_FILE, "author", digest)
