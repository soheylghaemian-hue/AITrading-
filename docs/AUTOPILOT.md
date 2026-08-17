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

Builder and reviewer implement a small provider-neutral interface. The repository package itself has no network
client, API-key handling or external destination. Its included provider is deterministic and offline. A later host
adapter may connect a model only after explicit destination approval and redaction rules; missing responses produce
`BLOCKED_AUTH`, not an interactive prompt.

## Evidence

Each run writes an append-only JSONL event stream and an atomic final JSON report under `.autopilot/` (git-ignored).
The report contains the base commit, changed files, iteration count, policy reasons, review findings, check commands,
exit codes, hashed outputs, bounded output tails and a stable result checksum.

## Running locally

```bash
gigbay-autopilot autopilot/goals/example.json approved-responses.json --repo .
```

The command replays an approved response bundle and never asks for credentials interactively. GitHub's Autopilot
Safety Gate is read-only and verifies these boundaries; it cannot push, merge or deploy. Continuous model-driven work
requires a separately authorized host adapter so repository contents are not silently sent to an external service.

## Trader Brain boundary

`atp.brain` defines the first research-only vocabulary for temporal evidence, beliefs, falsifiable scenarios and
proposals. It has no order action. Its constitution rejects autonomous execution, real money, leverage and self-relaxed
limits. Later phases can build SENSE, THINK, PROVE and LEARN behind these contracts while preserving this boundary.
