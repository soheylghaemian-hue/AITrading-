from __future__ import annotations

import json
import subprocess
from pathlib import Path

from atp.autopilot import AutopilotPolicy, DevelopmentAutopilot, Goal, RunStatus
from atp.autopilot.providers import ScriptedProvider
from atp.autopilot.verifier import VerificationCommand, Verifier


def _repo(tmp_path: Path) -> Path:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "config", "user.name", "Test"), check=True)
    (tmp_path / "src/atp/autopilot").mkdir(parents=True)
    (tmp_path / "src/atp/autopilot/base.py").write_text("VALUE = 1\n")
    (tmp_path / ".gitignore").write_text(".state/\n")
    subprocess.run(("git", "-C", str(tmp_path), "add", "."), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "commit", "-qm", "base"), check=True)
    return tmp_path


def _goal() -> Goal:
    return Goal("g1", "Change value", ("VALUE is 2",), ("src/atp/autopilot/",), 2)


def test_green_patch_completes_with_evidence(tmp_path):
    repo = _repo(tmp_path)
    patch = """diff --git a/src/atp/autopilot/base.py b/src/atp/autopilot/base.py
--- a/src/atp/autopilot/base.py
+++ b/src/atp/autopilot/base.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    builder = ScriptedProvider([{"summary": "done", "patch": patch,
                                 "changed_files": ["src/atp/autopilot/base.py"]}])
    reviewer = ScriptedProvider([{"approved": True, "findings": []}])
    verifier = Verifier(repo, (VerificationCommand("syntax", ("python3", "-m", "py_compile",
                                                                "src/atp/autopilot/base.py")),))
    runner = DevelopmentAutopilot(repo=repo, builder=builder, reviewer=reviewer, verifier=verifier,
                                  state_dir=repo / ".state")
    report = runner.run(_goal())
    assert report.status is RunStatus.COMPLETED
    assert report.result_checksum and report.changed_files == ["src/atp/autopilot/base.py"]
    saved = json.loads((repo / ".state" / f"{report.run_id}.report.json").read_text())
    assert saved["result_checksum"] == report.result_checksum


def test_red_trading_path_is_blocked_without_applying(tmp_path):
    repo = _repo(tmp_path)
    builder = ScriptedProvider([{"summary": "unsafe", "patch": "",
                                 "changed_files": ["src/atp/execution/engine.py"]}])
    runner = DevelopmentAutopilot(repo=repo, builder=builder,
                                  reviewer=ScriptedProvider([]), state_dir=repo / ".state")
    report = runner.run(Goal("g2", "Enable trading", ("trades",), (), 1))
    assert report.status is RunStatus.BLOCKED_POLICY
    assert "trading/production" in report.policy_reasons[0]


def test_undeclared_red_file_in_patch_is_detected_and_removed(tmp_path):
    repo = _repo(tmp_path)
    patch = """diff --git a/src/atp/execution/unsafe.py b/src/atp/execution/unsafe.py
new file mode 100644
--- /dev/null
+++ b/src/atp/execution/unsafe.py
@@ -0,0 +1 @@
+UNSAFE = True
"""
    builder = ScriptedProvider([{"summary": "misdeclared", "patch": patch,
                                 "changed_files": ["src/atp/autopilot/base.py"]}])
    runner = DevelopmentAutopilot(repo=repo, builder=builder, reviewer=ScriptedProvider([]),
                                  state_dir=repo / ".state")
    report = runner.run(_goal())
    assert report.status is RunStatus.BLOCKED_POLICY
    assert not (repo / "src/atp/execution/unsafe.py").exists()


def test_dirty_worktree_is_not_mixed_into_unattended_run(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src/atp/autopilot/base.py").write_text("USER_CHANGE = True\n")
    runner = DevelopmentAutopilot(repo=repo, builder=ScriptedProvider([]), reviewer=ScriptedProvider([]),
                                  state_dir=repo / ".state")
    report = runner.run(_goal())
    assert report.status is RunStatus.BLOCKED_POLICY
    assert "clean" in report.policy_reasons[0]


def test_missing_provider_response_blocks_cleanly(tmp_path):
    repo = _repo(tmp_path)
    runner = DevelopmentAutopilot(repo=repo, builder=ScriptedProvider([]),
                                  reviewer=ScriptedProvider([]), state_dir=repo / ".state")
    report = runner.run(_goal())
    assert report.status is RunStatus.BLOCKED_AUTH
    assert not report.changed_files


def test_constitution_never_allows_sensitive_authority():
    c = AutopilotPolicy.immutable_constitution()
    assert c and not any(c.values())
    for path in ("/opt/atp/atp.env", "src/atp/brokers/ibkr.py", "src/atp/risk/engine.py"):
        assert not AutopilotPolicy().classify_path(path).allowed
