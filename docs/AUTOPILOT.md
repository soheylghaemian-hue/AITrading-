# GIGBAY Development Autopilot

## Purpose

The autopilot removes the human copy/paste loop between development models. A builder proposes a bounded patch,
a deterministic verifier runs the repository's evidence suite, and an independent reviewer accepts or returns
structured findings. The loop stops after a fixed number of iterations.

This phase is **development automation only**. It cannot deploy production, read or write secrets, access a broker,
enable execution, enable leverage, relax risk limits, or trade real money.

## Flow

```text
Goal JSON
  -> builder (structured patch)
  -> path-policy gate
  -> git apply --check
  -> deterministic verification
  -> independent reviewer
  -> repair, complete, or bounded failure
  -> immutable evidence report
  -> draft pull request
```

## Permission constitution

Green paths are research, brain contracts, autopilot code, tests and documentation. Yellow paths such as workflow
or general infrastructure changes require an explicit policy flag. Trading runtime, broker, execution, live, risk,
service, environment and credential paths are red and cannot be authorized by this package.

The policy is checked twice: against the model's declared file list before applying a patch and against Git's actual
changed-file list afterward. Model output is never executed as a shell command. Verification commands are supplied
by trusted repository configuration and run with `shell=False`.

## Provider isolation

The repository core remains provider-neutral and contains no API keys. The GitHub host workflow implements the
explicitly authorized role split: Codex plans and reviews in read-only, ephemeral jobs; Claude is the sole code
author and returns a structured unified patch without shell or checkout-write tools. A separate secret-free gate
applies the patch, enforces path policy and runs deterministic tests. A final publisher has GitHub write permission
but receives no model credentials and can only update a draft workbench pull request.

## Evidence

Each run writes an append-only JSONL event stream and an atomic final JSON report under `.autopilot/` (git-ignored).
The report contains the base commit, changed files, iteration count, policy reasons, review findings, check commands,
exit codes, hashed outputs, bounded output tails and a stable result checksum.

## Running locally

```bash
gigbay-autopilot autopilot/goals/example.json approved-responses.json --repo .
```

The command replays an approved response bundle and never asks for credentials interactively. The hosted workflow
runs once per day and selects the next unfinished, committed goal from `autopilot/queue.json`. Work accumulates on
`claude/autopilot-workbench` and in one draft pull request. It never merges or deploys.

Two one-time GitHub secrets are required: `OPENAI_API_KEY` for the Codex planner/reviewer and either
`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` for Claude. They are supplied only to their isolated model jobs
and never written to the repository or evidence artifacts. Missing authentication stops the workflow.

The trusted queue currently advances through SENSE, THINK, PROVE and LEARN research contracts. Completion markers
are deterministic publisher metadata under `docs/autopilot/completed/`; they are not model-authored code.

## Trader Brain boundary

`atp.brain` defines the first research-only vocabulary for temporal evidence, beliefs, falsifiable scenarios and
proposals. It has no order action. Its constitution rejects autonomous execution, real money, leverage and self-relaxed
limits. Later phases can build SENSE, THINK, PROVE and LEARN behind these contracts while preserving this boundary.
