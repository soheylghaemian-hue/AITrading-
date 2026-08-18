import os
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = (Path(__file__).resolve().parents[1] / ".github/workflows/autopilot.yml").read_text()


def _job(name: str, next_name: str | None) -> str:
    start = WORKFLOW.index(f"  {name}:\n")
    end = WORKFLOW.index(f"  {next_name}:\n", start) if next_name else len(WORKFLOW)
    return WORKFLOW[start:end]


def test_only_claude_action_can_author_and_it_has_no_write_or_shell_tools():
    author = _job("author", "gate")
    repair = _job("repair", "gate_repair")
    for job in (author, repair):
        assert "anthropics/claude-code-base-action@" in job
        assert '--allowedTools "Read,Glob,Grep"' in job
        assert "Bash,Edit,Write" in job
        assert "contents: write" not in job
        assert "openai/codex-action@" not in job
    assert "anthropics/claude-code-action@" not in WORKFLOW


def test_codex_is_read_only_and_never_in_publish_job():
    for name, following in (("plan", "author"), ("review", "repair"), ("final_review", "verdict")):
        job = _job(name, following)
        assert 'permission-profile: ":read-only"' in job
        assert "safety-strategy: drop-sudo" in job
        assert "contents: write" not in job
    publish = _job("publish", None)
    assert "openai-api-key" not in publish
    assert "anthropic_api_key" not in publish
    assert "python -m pytest" not in publish
    assert "npm test" not in publish


def test_no_untrusted_event_or_automatic_merge():
    assert "pull_request_target" not in WORKFLOW
    assert "issue_comment" not in WORKFLOW
    assert "\n  issues:" not in WORKFLOW
    assert "pr merge" not in WORKFLOW
    assert "--draft" in WORKFLOW
    assert WORKFLOW.count("contents: write") == 1


def test_model_and_support_actions_are_pinned_to_full_shas():
    for line in WORKFLOW.splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:"):
            ref = stripped.rsplit("@", 1)[-1]
            assert len(ref) == 40 and all(char in "0123456789abcdef" for char in ref)


def test_control_plane_is_separate_from_the_immutable_candidate():
    assert "base_ref" not in WORKFLOW
    assert "path: .autopilot/control" not in WORKFLOW
    assert WORKFLOW.count("\n          path: candidate\n") == 9
    assert WORKFLOW.count("Verify immutable control and candidate checkouts") == 7
    assert "PYTHONPATH=src python -m atp.autopilot.queue --repo candidate" in WORKFLOW


def test_models_work_only_in_candidate_but_use_control_prompts_and_guard():
    assert WORKFLOW.count("CLAUDE_WORKING_DIR: ${{ github.workspace }}/candidate") == 2
    assert WORKFLOW.count("working-directory: ${{ github.workspace }}/candidate") == 3
    assert WORKFLOW.count(
        'PYTHONPATH="${GITHUB_WORKSPACE}/src" python -m atp.autopilot.guard'
    ) == 4
    assert "prompt_file: ${{ github.workspace }}/.github/autopilot/prompts/claude-author.md" in WORKFLOW
    assert "prompt_file: ${{ github.workspace }}/.github/autopilot/prompts/claude-repair.md" in WORKFLOW
    assert "--unidiff-zero" not in WORKFLOW


def test_think_boundary_requirements_are_in_both_authoring_prompts():
    root = Path(__file__).resolve().parents[1]
    prompts = [
        (root / ".github/autopilot/prompts/plan.md").read_text(),
        (root / ".github/autopilot/prompts/claude-author.md").read_text(),
    ]
    for prompt in prompts:
        assert "SenseResult" in prompt
        assert "constructed" in prompt
        assert "future, stale, duplicate or inconsistent usable evidence" in prompt
        assert "deterministic regression tests" in prompt


def test_terminal_verdict_owns_routing_and_publish_depends_only_on_it():
    verdict = _job("verdict", "publish")
    publish = _job("publish", None)
    assert "needs: [prepare, gate, review, repair, gate_repair, final_review]" in verdict
    assert "always() &&\n      !cancelled()" in verdict
    assert "needs.prepare.outputs.has_goal == 'true'" in verdict
    assert "permissions: {}" in verdict
    assert "needs: [prepare, verdict]\n    runs-on:" in publish
    assert "always()" not in publish
    assert "needs.review" not in publish
    assert "needs.gate" not in publish
    assert "name: ${{ needs.verdict.outputs.artifact_name }}" in publish
    assert "APPROVED_FIRST: ${{ needs.verdict.outputs.approved_first }}" in publish


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param(
            {
                "INITIAL_APPROVED": "true",
                "REPAIR_RESULT": "skipped",
                "REPAIR_GATE_RESULT": "skipped",
                "FINAL_REVIEW_RESULT": "skipped",
                "FINAL_APPROVED": "",
            },
            {"approved_first": "true", "artifact_name": "candidate-123"},
            id="initial-approved",
        ),
        pytest.param({}, {"approved_first": "false", "artifact_name": "repaired-123"},
                     id="repair-approved"),
        pytest.param({"FINAL_APPROVED": "false"}, None, id="repair-rejected"),
        pytest.param({"REPAIR_GATE_RESULT": "failure"}, None, id="repair-gate-failed"),
        pytest.param({"INITIAL_APPROVED": ""}, None, id="missing-initial-verdict"),
        pytest.param({"INITIAL_APPROVED": "true"}, None, id="ambiguous-both-paths"),
        pytest.param({"REVIEW_RESULT": "failure"}, None, id="review-job-failed"),
    ],
)
def test_terminal_verdict_truth_table(tmp_path, changes, expected):
    verdict = _job("verdict", "publish")
    script = textwrap.dedent(verdict.split("        run: |\n", 1)[1])
    output = tmp_path / "github-output"
    output.touch()
    env = os.environ.copy()
    env.update(
        {
            "GATE_RESULT": "success",
            "REVIEW_RESULT": "success",
            "INITIAL_APPROVED": "false",
            "REPAIR_RESULT": "success",
            "REPAIR_GATE_RESULT": "success",
            "FINAL_REVIEW_RESULT": "success",
            "FINAL_APPROVED": "true",
            "GITHUB_RUN_ID": "123",
            "GITHUB_OUTPUT": str(output),
        }
    )
    env.update(changes)
    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if expected is None:
        assert result.returncode != 0
        assert output.read_text() == ""
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        actual = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert actual == expected


def test_publisher_refuses_a_stale_workbench():
    publish = _job("publish", None)
    assert "EXPECTED_BASE_SHA: ${{ needs.prepare.outputs.base_sha }}" in publish
    assert "Workbench advanced after review; refusing stale publication" in publish
