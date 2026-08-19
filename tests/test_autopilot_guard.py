from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atp.autopilot.guard import (
    GuardViolation,
    changed_files,
    validate_canonical_patch,
    validate_declared,
    validate_worktree,
)
from atp.autopilot.models import Goal


def _goal() -> Goal:
    return Goal("g", "safe research change", ("tests pass",),
                ("src/atp/research/", "src/atp/brain/", "tests/", "docs/", "frontend/"), 2)


def _repo(tmp_path: Path) -> Path:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Test"), check=True)
    (tmp_path / "src/atp/research").mkdir(parents=True)
    (tmp_path / "src/atp/research/base.py").write_text("VALUE = 1\n")
    subprocess.run(("git", "-C", str(tmp_path), "add", "."), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-qm", "base"), check=True)
    return tmp_path


def _payload(author: str = "claude", path: str = "src/atp/research/base.py") -> dict:
    return {
        "contract_version": "full-file-edit/v1",
        "author": author,
        "phase": "author",
        "base_sha": "a" * 40,
        "input_state_sha256": "b" * 64,
        "parent_patch_sha256": None,
        "edits": [
            {
                "op": "modify",
                "path": path,
                "before_sha256": "c" * 64,
                "content": "VALUE = 2\n",
            }
        ],
    }


def test_only_claude_may_author_patch():
    with pytest.raises(GuardViolation, match="exactly claude"):
        validate_declared(_payload(author="codex"), _goal())


def test_declared_red_path_is_rejected():
    with pytest.raises(GuardViolation, match="trading/production"):
        validate_declared(_payload(path="src/atp/execution/orders.py"), _goal())


@pytest.mark.parametrize(
    "marker",
    (
        "GIT binary patch",
        "new file mode 100755",
        "new file mode 160000",
        "new file mode 120000",
        "deleted file mode 100644",
        "similarity index 100%",
    ),
)
def test_dangerous_canonical_patch_modes_are_rejected(tmp_path, marker):
    patch = tmp_path / "candidate.patch"
    patch.write_text(
        "diff --git a/src/atp/research/base.py b/src/atp/research/base.py\n"
        f"{marker}\n",
        encoding="utf-8",
    )
    with pytest.raises(GuardViolation, match="forbidden"):
        validate_canonical_patch(patch)


def test_safe_canonical_text_patch_is_accepted(tmp_path):
    patch = tmp_path / "candidate.patch"
    patch.write_text(
        "diff --git a/src/atp/research/base.py b/src/atp/research/base.py\n"
        "--- a/src/atp/research/base.py\n"
        "+++ b/src/atp/research/base.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n",
        encoding="utf-8",
    )
    validate_canonical_patch(patch)


def test_green_worktree_is_allowed(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src/atp/research/base.py").write_text("VALUE = 2\n")
    assert validate_worktree(repo, _goal()) == ["src/atp/research/base.py"]


def test_untracked_red_file_is_detected(tmp_path):
    repo = _repo(tmp_path)
    target = repo / "src/atp/brokers/unsafe.py"
    target.parent.mkdir(parents=True)
    target.write_text("UNSAFE = True\n")
    assert "src/atp/brokers/unsafe.py" in changed_files(repo)
    with pytest.raises(GuardViolation):
        validate_worktree(repo, _goal())


def test_symlink_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    target = repo / "docs/link"
    target.parent.mkdir()
    target.symlink_to(repo / "src/atp/research/base.py")
    with pytest.raises(GuardViolation, match="symlinks"):
        validate_worktree(repo, _goal())
