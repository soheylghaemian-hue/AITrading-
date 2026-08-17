from pathlib import Path

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
    for name, following in (("plan", "author"), ("review", "repair"), ("final_review", "publish")):
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
