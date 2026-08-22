import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = (Path(__file__).resolve().parents[1] / ".github/workflows/autopilot.yml").read_text()


def _job(name: str, next_name: str | None) -> str:
    start = WORKFLOW.index(f"  {name}:\n")
    end = WORKFLOW.index(f"  {next_name}:\n", start) if next_name else len(WORKFLOW)
    return WORKFLOW[start:end]


def _duplicate_yaml_mapping_keys(document: str) -> list[tuple[int, str]]:
    """Detect duplicate simple mapping keys without interpreting block scalars."""
    scopes: dict[int, set[str]] = {}
    duplicates: list[tuple[int, str]] = []
    block_indent: int | None = None
    key_pattern = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)")

    for line_number, raw_line in enumerate(document.splitlines(), start=1):
        stripped = raw_line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(stripped)
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None

        for level in tuple(scopes):
            if level > indent:
                del scopes[level]

        if stripped.startswith("- "):
            key_indent = indent + 2
            for level in tuple(scopes):
                if level >= key_indent:
                    del scopes[level]
            candidate = stripped[2:]
        else:
            key_indent = indent
            candidate = stripped

        match = key_pattern.match(candidate)
        if match is None:
            continue
        key = match.group(1)
        seen = scopes.setdefault(key_indent, set())
        if key in seen:
            duplicates.append((line_number, key))
        seen.add(key)

        value = candidate[match.end():].strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block_indent = indent

    return duplicates


def test_workflow_has_no_duplicate_yaml_mapping_keys():
    assert _duplicate_yaml_mapping_keys(WORKFLOW) == []
    assert _duplicate_yaml_mapping_keys("root:\n  key: one\n  key: two\n") == [(3, "key")]
    assert _duplicate_yaml_mapping_keys("steps:\n  - name: one\n  - name: two\n") == []


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


def test_pinned_codex_action_only_prepares_runtime_and_receives_no_model_io():
    marker = (
        "      - name: Prepare the private Codex runtime without a prompt\n"
        "        uses: openai/codex-action@c385816875cc2fc8e033ed9d1cba96f8c331210e\n"
        "        with:\n"
    )
    setup_blocks = []
    cursor = 0
    while (start := WORKFLOW.find(marker, cursor)) != -1:
        start += len(marker)
        end = WORKFLOW.index("      - name:", start)
        setup_blocks.append(WORKFLOW[start:end])
        cursor = end

    assert len(setup_blocks) == 3
    expected_inputs = {
        "openai-api-key",
        "codex-home",
        "permission-profile",
        "safety-strategy",
        "allow-bots",
    }
    for setup in setup_blocks:
        input_keys = {
            match.group(1)
            for match in re.finditer(r"^          ([a-z][a-z0-9-]*):", setup, re.MULTILINE)
        }
        assert input_keys == expected_inputs
        assert "openai-api-key: ${{ secrets.OPENAI_API_KEY }}" in setup
        assert "codex-home: ${{ runner.temp }}/codex-home" in setup
        assert 'permission-profile: ":read-only"' in setup
        assert "safety-strategy: drop-sudo" in setup
        assert "allow-bots: true" in setup
        for forbidden in (
            "prompt:",
            "prompt-file:",
            "output-file:",
            "output-schema:",
            "output-schema-file:",
            "working-directory:",
            "codex-args:",
            "effort:",
            "model:",
        ):
            assert forbidden not in setup


def test_private_codex_exec_preserves_exact_read_only_contract_without_api_key():
    private_steps = re.findall(
        r"      - name: Codex [^\n]*without log disclosure\n"
        r"(?P<step>.*?)(?=      - (?:name:|uses:)|\n  [a-z_]+:)",
        WORKFLOW,
        re.DOTALL,
    )
    assert len(private_steps) == 3

    expected_args = (
        "codex exec \\\n",
        "--skip-git-repo-check \\\n",
        '--cd "${GITHUB_WORKSPACE}/candidate" \\\n',
        '--output-last-message "${CODEX_OUTPUT_FILE}" \\\n',
        '--output-schema "${CODEX_OUTPUT_SCHEMA_FILE}" \\\n',
        "--config 'model_reasoning_effort=\"high\"' \\\n",
        "--ephemeral \\\n",
        "--config 'default_permissions=\":read-only\"' \\\n",
        '< "${CODEX_PROMPT_FILE}" \\\n',
        '> "${private_log}" 2>&1\n',
    )
    for step in private_steps:
        assert "working-directory: ${{ github.workspace }}/candidate" in step
        assert "CODEX_HOME: ${{ runner.temp }}/codex-home" in step
        assert "CODEX_INTERNAL_ORIGINATOR_OVERRIDE: codex_github_action" in step
        assert 'FORCE_COLOR: "1"' in step
        assert "set -euo pipefail" in step
        assert 'test -f "${CODEX_PROMPT_FILE}"' in step
        assert 'test ! -L "${CODEX_PROMPT_FILE}"' in step
        assert 'test -f "${CODEX_OUTPUT_SCHEMA_FILE}"' in step
        assert 'test ! -L "${CODEX_OUTPUT_SCHEMA_FILE}"' in step
        assert 'test ! -e "${CODEX_OUTPUT_FILE}"' in step
        assert 'test ! -L "${CODEX_OUTPUT_FILE}"' in step
        assert 'test -s "${CODEX_OUTPUT_FILE}"' in step
        assert "secrets." not in step
        assert "OPENAI_API_KEY" not in step
        assert "openai-api-key" not in step
        assert "|| true" not in step
        for argument in expected_args:
            assert step.count(argument) == 1

    assert WORKFLOW.count(
        "CODEX_PROMPT_FILE: ${{ github.workspace }}/.github/autopilot/prompts/plan.md"
    ) == 1
    assert WORKFLOW.count(
        "CODEX_PROMPT_FILE: ${{ github.workspace }}/.github/autopilot/prompts/review.md"
    ) == 2
    assert WORKFLOW.count(
        "CODEX_OUTPUT_SCHEMA_FILE: ${{ github.workspace }}/.github/autopilot/schemas/plan.schema.json"
    ) == 1
    assert WORKFLOW.count(
        "CODEX_OUTPUT_SCHEMA_FILE: ${{ github.workspace }}/.github/autopilot/schemas/review.schema.json"
    ) == 2


def test_private_codex_logs_are_0600_runner_temp_only_and_never_disclosed():
    assert WORKFLOW.count("umask 077") == 3
    assert WORKFLOW.count(
        'private_log="$(mktemp "${RUNNER_TEMP}/codex-${GITHUB_JOB}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.XXXXXX")"'
    ) == 3
    assert WORKFLOW.count('chmod 600 "${private_log}"') == 3
    assert WORKFLOW.count('test "$(stat -c \'%a\' "${private_log}")" = "600"') == 3
    assert WORKFLOW.count("trap cleanup_private_codex_log EXIT") == 3
    assert WORKFLOW.count("trap 'exit 129' HUP") == 3
    assert WORKFLOW.count("trap 'exit 130' INT") == 3
    assert WORKFLOW.count("trap 'exit 143' TERM") == 3
    assert WORKFLOW.count('rm -f -- "${private_log}"') == 3
    assert WORKFLOW.count('> "${private_log}" 2>&1') == 3
    assert 'cat "${private_log}"' not in WORKFLOW
    assert 'tee "${private_log}"' not in WORKFLOW
    assert 'tee -a "${private_log}"' not in WORKFLOW

    upload_blocks = WORKFLOW.split(
        "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    )[1:]
    assert len(upload_blocks) == 7
    for block in upload_blocks:
        block = block.split("      - ", 1)[0]
        assert "private_log" not in block
        assert "codex-${GITHUB_JOB}" not in block
        assert ".log" not in block


def test_no_untrusted_event_or_automatic_merge():
    assert "pull_request_target" not in WORKFLOW
    assert "issue_comment" not in WORKFLOW
    assert "\n  issues:" not in WORKFLOW
    assert "pr merge" not in WORKFLOW
    assert "gh pr create" not in WORKFLOW
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


def test_claude_output_schema_is_strict_full_file_only_and_guard_remains_final():
    required = (
        '"required":["contract_version","author","phase","base_sha",'
        '"input_state_sha256","parent_patch_sha256","edits"]'
    )
    edit_required = '"required":["op","path","before_sha256","content"]'
    assert WORKFLOW.count(required) == 2
    assert WORKFLOW.count(edit_required) == 2
    assert WORKFLOW.count('"enum":["full-file-edit/v1"]') == 2
    assert WORKFLOW.count('"enum":["create","modify"]') == 2
    assert '"delete"' not in WORKFLOW
    for name, following, phase in (
        ("author", "gate", "author"),
        ("repair", "gate_repair", "repair"),
    ):
        job = _job(name, following)
        assert f'"phase":{{"type":"string","enum":["{phase}"]}}' in job
        assert '"summary"' not in job
        assert '"patch"' not in job
        assert '"changed_files"' not in job
        assert '"uniqueItems"' not in job
        assert '"maxItems"' not in job
    assert WORKFLOW.count("--declared-only") == 2
    assert WORKFLOW.count("--declared-json") == 4


def test_claude_prompts_require_runtime_structured_output_without_relaxing_bounds():
    root = Path(__file__).resolve().parents[1]
    for name in ("claude-author.md", "claude-repair.md"):
        prompt = (root / ".github/autopilot/prompts" / name).read_text()
        normalized = " ".join(prompt.split())
        assert "Structured-output completion is mandatory" in normalized
        assert "before the turn budget ends" in normalized
        assert "stop exploring and return immediately" in normalized
        assert "final response must consist only of the runtime structured_output object" in normalized
        assert "Do not finish with prose, Markdown, a code fence" in normalized
        assert "runtime structured_output object" in normalized
        assert "without structured_output is invalid and must fail closed" in normalized

    assert WORKFLOW.count('show_full_output: "false"') == 2
    assert WORKFLOW.count('--allowedTools "Read,Glob,Grep"') == 2
    assert WORKFLOW.count(
        '--disallowedTools "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,mcp__*"'
    ) == 2
    assert WORKFLOW.count("--json-schema") == 2
    assert WORKFLOW.count("--max-turns 30") == 1
    assert WORKFLOW.count("--max-budget-usd 25") == 1
    assert WORKFLOW.count("--max-turns 25") == 1
    assert WORKFLOW.count("--max-budget-usd 20") == 1


def test_claude_prompts_require_a_final_per_edit_transport_preflight():
    root = Path(__file__).resolve().parents[1]
    for name in ("claude-author.md", "claude-repair.md"):
        prompt = (root / ".github/autopilot/prompts" / name).read_text()
        normalized = " ".join(prompt.split())
        assert "Canonical edit construction" in normalized
        assert "complete exact set of touched paths" in normalized
        assert "never choose first-wins or last-wins" in normalized
        assert "Freeze that unique path set" in normalized
        assert "sort it by raw Python/Unicode string order" in normalized
        assert "Move each whole edit object together" in normalized
        assert "Never use discovery, Read or implementation order" in normalized
        assert "never append another edit after the ordered array is built" in normalized
        assert "After any correction, recheck the complete array" in normalized
        assert "`edits[i - 1].path < edits[i].path`" in normalized
        assert "Final pre-submit preflight" in normalized
        assert "every `edits[i]`, including the last edit" in normalized
        assert "strictly increasing in lexicographic order with no duplicate path" in normalized
        assert r"every `edits[i].content` ends with `\n` but not `\n\n`" in normalized
        assert "Never rely on the trusted binder to normalize model output" in normalized


def test_full_file_state_is_bound_for_author_and_repair_before_materialization():
    assert WORKFLOW.count("python -m atp.autopilot.full_file") == 6
    assert WORKFLOW.count("            --prepare-state ") == 4
    assert WORKFLOW.count("            --expected-model-sha256 ") == 2
    assert WORKFLOW.count("            --control-sha ") == 4
    assert WORKFLOW.count("            --base-sha ") == 4
    assert "--recount" not in WORKFLOW
    assert ".autopilot/repair.patch" not in WORKFLOW
    assert "json.loads(source.read_text" not in WORKFLOW
    trusted_diff = "git diff --no-ext-diff --no-textconv --no-renames --full-index --binary"
    assert WORKFLOW.count(trusted_diff) == 4

    gate = _job("gate", "review")
    assert gate.index("--prepare-state") < gate.index("python -m atp.autopilot.model_output")
    assert gate.index("--declared-only") < gate.index("--materialize")
    assert gate.index("--materialize") < gate.index(f"{trusted_diff} HEAD")
    assert gate.index(f"{trusted_diff} HEAD") < gate.index("--canonical-patch")

    gate_repair = _job("gate_repair", "final_review")
    assert gate_repair.index("Temporary candidate baseline") < gate_repair.index("--prepare-state")
    assert gate_repair.index("--declared-only") < gate_repair.index("--materialize")
    assert gate_repair.index("--materialize") < gate_repair.index(f"{trusted_diff} HEAD^")
    assert gate_repair.index(f"{trusted_diff} HEAD^") < gate_repair.index("--canonical-patch")


def test_claude_token_normalization_removes_only_cr_lf_and_rejects_other_whitespace(
    tmp_path: Path,
):
    assert "tr -d" not in WORKFLOW
    assert WORKFLOW.count('token = raw.replace("\\r", "").replace("\\n", "")') == 2
    assert WORKFLOW.count("any(character.isspace() for character in token)") == 2
    assert WORKFLOW.count("Claude OAuth token contains unsupported whitespace") == 2

    marker = "          python - <<'PY'\n"
    scripts = []
    cursor = 0
    while (start := WORKFLOW.find(marker, cursor)) != -1:
        start += len(marker)
        end = WORKFLOW.index("\n          PY", start)
        script = textwrap.dedent(WORKFLOW[start:end])
        if "RAW_CLAUDE_TOKEN" in script:
            scripts.append(script)
        cursor = end + 1
    assert len(scripts) == 2

    for index, script in enumerate(scripts):
        output = tmp_path / f"github-output-{index}"
        output.touch()
        base_env = os.environ.copy()
        base_env["GITHUB_OUTPUT"] = str(output)
        valid_env = {**base_env, "RAW_CLAUDE_TOKEN": "sk-ant-oat01-example\r\n"}
        valid = subprocess.run(
            ["python3", "-c", script],
            env=valid_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert valid.returncode == 0
        assert output.read_text() == "token=sk-ant-oat01-example\n"
        for bad in ("sk-ant-oat01 bad", "sk-ant-oat01\tbad", "sk-ant-oat01\u00a0bad"):
            output.write_text("")
            rejected = subprocess.run(
                ["python3", "-c", script],
                env={**base_env, "RAW_CLAUDE_TOKEN": bad},
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert rejected.returncode != 0
            assert output.read_text() == ""


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
    assert "CANDIDATE_ARTIFACT_NAME: ${{ needs.gate.outputs.artifact_name }}" in verdict
    assert "REPAIRED_ARTIFACT_NAME: ${{ needs.gate_repair.outputs.artifact_name }}" in verdict


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
            {"approved_first": "true", "artifact_name": "candidate-123-2"},
            id="initial-approved",
        ),
        pytest.param({}, {"approved_first": "false", "artifact_name": "repaired-123-3"},
                     id="repair-approved"),
        pytest.param({"FINAL_APPROVED": "false"}, None, id="repair-rejected"),
        pytest.param({"REPAIR_GATE_RESULT": "failure"}, None, id="repair-gate-failed"),
        pytest.param({"INITIAL_APPROVED": ""}, None, id="missing-initial-verdict"),
        pytest.param({"INITIAL_APPROVED": "true"}, None, id="ambiguous-both-paths"),
        pytest.param({"REVIEW_RESULT": "failure"}, None, id="review-job-failed"),
        pytest.param(
            {"INITIAL_APPROVED": "true", "REPAIR_RESULT": "skipped",
             "REPAIR_GATE_RESULT": "skipped", "FINAL_REVIEW_RESULT": "skipped",
             "CANDIDATE_ARTIFACT_NAME": "candidate-999-2"},
            None,
            id="wrong-run-candidate-artifact",
        ),
        pytest.param(
            {"REPAIRED_ARTIFACT_NAME": "repaired-123-latest"},
            None,
            id="malformed-repaired-artifact",
        ),
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
            "CANDIDATE_ARTIFACT_NAME": "candidate-123-2",
            "REPAIRED_ARTIFACT_NAME": "repaired-123-3",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "4",
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


def test_publisher_is_bound_to_existing_draft_pr_three_before_push():
    publish = _job("publish", None)
    assert 'WORKBENCH_PR_NUMBER: "3"' in WORKFLOW
    assert 'gh pr view "${WORKBENCH_PR_NUMBER}"' in publish
    assert '.number == $expected_number' in publish
    assert '.state == "OPEN"' in publish
    assert '.isDraft == true' in publish
    assert '.headRefName == $expected_head' in publish
    assert '.baseRefName == $expected_base' in publish
    assert '.headRefOid == $expected_sha' in publish
    assert publish.index('gh pr view "${WORKBENCH_PR_NUMBER}"') < publish.index(
        'git push origin "HEAD:refs/heads/${WORKBENCH_BRANCH}"'
    )
    assert 'gh pr comment "${WORKBENCH_PR_NUMBER}"' in publish
    assert "gh pr list" not in publish
    assert "gh pr create" not in publish


def test_model_outputs_use_hash_bound_attempt_scoped_artifacts_only():
    for forbidden in (
        "plan_json",
        "patch_json",
        "review_json",
        "PLAN_JSON",
        "PATCH_JSON",
        "REVIEW_JSON",
        "REPAIR_JSON",
        "outputs.final-message",
        "outputs.structured_output",
    ):
        assert forbidden not in WORKFLOW

    assert WORKFLOW.count("python -m atp.autopilot.model_output") == 12
    assert WORKFLOW.count("            --bind ") == 5
    assert WORKFLOW.count("            --verify ") == 7
    assert WORKFLOW.count('            --expected-sha256 "${EXPECTED_') == 12

    assert WORKFLOW.count("retention-days: 1") == 7
    assert "retention-days: 7" not in WORKFLOW
    for prefix in (
        "model-plan",
        "model-author",
        "model-review",
        "model-repair",
        "model-final-review",
        "candidate",
        "repaired",
    ):
        assert f"artifact_name={prefix}-%s-%s" in WORKFLOW

    assert "approved: ${{ steps.bind-review.outputs.approved }}" in WORKFLOW
    assert "approved: ${{ steps.bind-final-review.outputs.approved }}" in WORKFLOW
    assert "fromJSON(" not in WORKFLOW

    expected_files = (
        "candidate/.autopilot/plan.json",
        "candidate/.autopilot/claude.json",
        "candidate/.autopilot/review.json",
        "candidate/.autopilot/repair.json",
        "candidate/.autopilot/final-review.json",
    )
    for path in expected_files:
        assert f"path: {path}" in WORKFLOW

    assert WORKFLOW.count("EXECUTION_FILE: ${{ steps.claude-") == 2
    assert WORKFLOW.count('--execution-file "${EXECUTION_FILE}"') == 2
    assert WORKFLOW.count('--runner-temp "${RUNNER_TEMP}"') == 2


def test_rejected_final_review_is_hash_bound_and_persisted_privately():
    final_review = _job("final_review", "verdict")
    verdict = _job("verdict", "publish")
    publish = _job("publish", None)

    assert "approved: ${{ steps.bind-final-review.outputs.approved }}" in final_review
    assert "artifact_name: ${{ steps.bind-final-review.outputs.artifact_name }}" in final_review
    assert "model_sha256: ${{ steps.bind-final-review.outputs.model_sha256 }}" in final_review
    assert "--phase final_review" in final_review
    assert "--bind" in final_review
    assert '--github-output "${GITHUB_OUTPUT}"' in final_review
    assert "artifact_name=model-final-review-%s-%s" in final_review

    upload = final_review.split(
        "      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        1,
    )[1]
    assert final_review.index("Bind trusted final review") < final_review.index(
        "actions/upload-artifact@"
    )
    assert "name: ${{ steps.bind-final-review.outputs.artifact_name }}" in upload
    assert "path: candidate/.autopilot/final-review.json" in upload
    assert "if-no-files-found: error" in upload
    assert "retention-days: 1" in upload
    assert "if: steps.bind-final-review.outputs.approved" not in upload
    assert "private_log" not in upload

    for consumer in (verdict, publish):
        assert "candidate/.autopilot/final-review.json" not in consumer
        assert "needs.final_review.outputs.artifact_name" not in consumer
        assert "needs.final_review.outputs.model_sha256" not in consumer
        assert "FINAL_REVIEW_JSON" not in consumer
        assert "final_review_json" not in consumer


def test_artifact_consumers_download_then_verify_before_model_or_guard_use():
    author = _job("author", "gate")
    assert author.index("name: ${{ needs.plan.outputs.artifact_name }}") < author.index(
        "--phase plan"
    ) < author.index("anthropics/claude-code-base-action@")

    gate = _job("gate", "review")
    assert gate.index("name: ${{ needs.author.outputs.artifact_name }}") < gate.index(
        "--phase author"
    ) < gate.index("python -m atp.autopilot.guard")

    review = _job("review", "repair")
    assert review.index("name: ${{ needs.plan.outputs.artifact_name }}") < review.index(
        "--phase plan"
    ) < review.index("openai/codex-action@")

    repair = _job("repair", "gate_repair")
    for producer, phase in (("plan", "plan"), ("review", "review")):
        assert repair.index(f"name: ${{{{ needs.{producer}.outputs.artifact_name }}}}") < repair.index(
            f"--phase {phase}"
        ) < repair.index("anthropics/claude-code-base-action@")

    gate_repair = _job("gate_repair", "final_review")
    assert gate_repair.index("name: ${{ needs.repair.outputs.artifact_name }}") < gate_repair.index(
        "--phase repair"
    ) < gate_repair.index("python -m atp.autopilot.guard")

    final_review = _job("final_review", "verdict")
    assert final_review.index("name: ${{ needs.plan.outputs.artifact_name }}") < final_review.index(
        "--phase plan"
    ) < final_review.index("openai/codex-action@")

    assert "path: ${{ steps.claude-author.outputs.execution_file }}" not in WORKFLOW
    assert "path: ${{ steps.claude-repair.outputs.execution_file }}" not in WORKFLOW

def test_goal_bound_review_feedback_is_materialized_for_every_model_phase():
    prepare = _job("prepare", "plan")
    assert "control_feedback_present: ${{ steps.feedback.outputs.has_feedback }}" in prepare
    assert "control_feedback_sha256: ${{ steps.feedback.outputs.feedback_sha256 }}" in prepare
    assert "Bind trusted goal-bound review feedback" in prepare
    assert "if: steps.goal.outputs.has_goal == 'true'" in prepare
    assert '--github-output "${GITHUB_OUTPUT}"' in prepare
    assert WORKFLOW.count("Materialize trusted goal-bound review feedback") == 5
    assert WORKFLOW.count("PYTHONPATH=src python -m atp.autopilot.feedback") == 6
    assert WORKFLOW.count("--control-root .") == 6
    assert WORKFLOW.count('--expected-sha256 "${EXPECTED_CONTROL_FEEDBACK_SHA256}"') == 5
    assert WORKFLOW.count("--materialize") == 7
    for name, following in (
        ("plan", "author"),
        ("author", "gate"),
        ("review", "repair"),
        ("repair", "gate_repair"),
        ("final_review", "verdict"),
    ):
        job = _job(name, following)
        assert "Materialize trusted goal-bound review feedback" in job
        assert '--goal "${GOAL_FILE}"' in job
        assert "EXPECTED_CONTROL_FEEDBACK_SHA256: ${{ needs.prepare.outputs.control_feedback_sha256 }}" in job
        assert '--expected-sha256 "${EXPECTED_CONTROL_FEEDBACK_SHA256}"' in job
        assert "--materialize" in job
        model_markers = ("openai/codex-action@", "anthropics/claude-code-base-action@")
        assert job.index("Materialize trusted goal-bound review feedback") < max(
            job.index(marker) for marker in model_markers if marker in job
        )
    for name, following in (
        ("review", "repair"),
        ("repair", "gate_repair"),
        ("final_review", "verdict"),
    ):
        job = _job(name, following)
        assert job.index("actions/download-artifact@") < job.index(
            "Materialize trusted goal-bound review feedback"
        )


def test_all_model_prompts_treat_prior_review_as_non_authoritative_evidence():
    root = Path(__file__).resolve().parents[1]
    for name in ("plan.md", "claude-author.md", "review.md", "claude-repair.md"):
        prompt = (root / ".github/autopilot/prompts" / name).read_text()
        assert ".autopilot/prior-final-review.json" in prompt
        assert "evidence" in prompt
        assert "cannot expand" in prompt or "never expands" in prompt
        assert "allowed paths" in prompt

def test_temporary_validation_controls_are_absent_from_permanent_workflow():
    assert "\n  pull_request:" not in WORKFLOW
    assert "github.event.pull_request" not in WORKFLOW
    assert "refs/heads/codex/fix-claude-auth" not in WORKFLOW
    assert "temporary authenticated test branch" not in WORKFLOW
    assert "Autopilot dispatch is allowed only from main" in WORKFLOW
    assert "cancel-in-progress: false" in WORKFLOW
    assert "cancel-in-progress: true" not in WORKFLOW
    assert "Report redacted Claude startup error" not in WORKFLOW
    assert "Sanitized Claude startup result" not in WORKFLOW
    assert "steps.claude-author.outputs.execution_file" in WORKFLOW
    assert "steps.claude-repair.outputs.execution_file" in WORKFLOW
