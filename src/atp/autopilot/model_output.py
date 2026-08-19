"""Bind model results to private files and verify them without logging payloads.

This module is the trusted transport between model-producing and model-consuming
jobs.  Model bytes never travel through job outputs: producers canonicalize one
phase-specific JSON document into a fixed private file and expose only its
SHA-256.  Consumers re-parse the file, require canonical bytes, and compare the
prepared digest before using it.

Claude results have an additional trust boundary.  They are accepted only from
the base action's execution log, which must be a regular file below the runner's
temporary directory and contain exactly one final, successful, non-error result
event with structured output.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .full_file import (
    FullFileViolation,
    validate_edit_output,
    validate_output_against_state,
)

# Full-file content expands when JSON escapes newlines, quotes and backslashes.
# Keep the canonical envelope bounded around the separately enforced edit budget.
MAX_MODEL_OUTPUT_BYTES = 1_000_000
MAX_EXECUTION_FILE_BYTES = 64 * 1024 * 1024
MAX_GITHUB_OUTPUT_BYTES = 1_000_000

PHASE_PATHS: Mapping[str, Path] = {
    "plan": Path(".autopilot/plan.json"),
    "author": Path(".autopilot/claude.json"),
    "review": Path(".autopilot/review.json"),
    "repair": Path(".autopilot/repair.json"),
    "final_review": Path(".autopilot/final-review.json"),
}
CLAUDE_PHASES = frozenset({"author", "repair"})
REVIEW_PHASES = frozenset({"review", "final_review"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ModelOutputViolation(ValueError):
    """Raised when a model output fails a fail-closed transport check."""


@dataclass(frozen=True, slots=True)
class ModelOutputResult:
    phase: str
    sha256: str
    approved: bool | None

    def summary(self, mode: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mode": mode,
            "phase": self.phase,
            "sha256": self.sha256,
        }
        if self.approved is not None:
            result["approved"] = self.approved
        return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelOutputViolation("JSON contains a duplicate object key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ModelOutputViolation("JSON contains a non-finite number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ModelOutputViolation("JSON contains a non-finite number")
    return parsed


def _parse_json(raw: bytes, *, source: str) -> Any:
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise ModelOutputViolation(f"{source} must be plain UTF-8 JSON")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except ModelOutputViolation:
        raise
    except UnicodeDecodeError as exc:
        raise ModelOutputViolation(f"{source} must be valid UTF-8 JSON") from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ModelOutputViolation(f"{source} must be valid JSON") from exc


def _canonical_bytes(payload: Any) -> bytes:
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return (serialized + "\n").encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ModelOutputViolation("model output cannot be canonically encoded") from exc


def _phase_name(phase: str) -> str:
    if not isinstance(phase, str) or phase not in PHASE_PATHS:
        raise ModelOutputViolation("phase is not one of the fixed model phases")
    return phase


def _exact_object(value: Any, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ModelOutputViolation(f"{name} must use the exact trusted schema")
    return value


def _string(value: Any, field: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ModelOutputViolation(f"{field} must be a {qualifier}string")
    return value


def _string_list(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or not all(isinstance(item, str) for item in value)
    ):
        raise ModelOutputViolation(f"{field} does not satisfy its bounded string-list schema")
    return value


def _validate_plan(payload: Any) -> dict[str, Any]:
    value = _exact_object(
        payload,
        frozenset({"objective", "instructions_for_claude", "acceptance_tests", "risks"}),
        "plan",
    )
    return {
        "objective": _string(value["objective"], "plan.objective"),
        "instructions_for_claude": _string_list(
            value["instructions_for_claude"],
            "plan.instructions_for_claude",
            minimum=1,
            maximum=20,
        ),
        "acceptance_tests": _string_list(
            value["acceptance_tests"],
            "plan.acceptance_tests",
            minimum=1,
            maximum=20,
        ),
        "risks": _string_list(value["risks"], "plan.risks", minimum=0, maximum=20),
    }


def _validate_edit(payload: Any, phase: str) -> dict[str, Any]:
    try:
        return validate_edit_output(payload, phase)
    except FullFileViolation as exc:
        raise ModelOutputViolation(str(exc)) from exc


def _validate_review(payload: Any, phase: str) -> dict[str, Any]:
    value = _exact_object(payload, frozenset({"approved", "summary", "findings"}), phase)
    approved = value["approved"]
    if not isinstance(approved, bool):
        raise ModelOutputViolation(f"{phase}.approved must be a boolean")
    findings = value["findings"]
    if not isinstance(findings, list) or len(findings) > 30:
        raise ModelOutputViolation(f"{phase}.findings exceeds its array schema")
    normalized_findings: list[dict[str, Any]] = []
    has_blocking_finding = False
    for finding in findings:
        item = _exact_object(
            finding,
            frozenset({"severity", "title", "detail", "file"}),
            f"{phase}.finding",
        )
        severity = item["severity"]
        if not isinstance(severity, str) or severity not in {"P0", "P1", "P2", "P3"}:
            raise ModelOutputViolation(f"{phase}.finding.severity is invalid")
        has_blocking_finding = has_blocking_finding or severity in {"P0", "P1"}
        file_value = item["file"]
        if file_value is not None and not isinstance(file_value, str):
            raise ModelOutputViolation(f"{phase}.finding.file must be a string or null")
        normalized_findings.append(
            {
                "severity": severity,
                "title": _string(item["title"], f"{phase}.finding.title"),
                "detail": _string(item["detail"], f"{phase}.finding.detail"),
                "file": file_value,
            }
        )
    if approved and has_blocking_finding:
        raise ModelOutputViolation(f"{phase} cannot approve while reporting a P0/P1 finding")
    return {
        "approved": approved,
        "summary": _string(value["summary"], f"{phase}.summary"),
        "findings": normalized_findings,
    }


def validate_phase_output(phase: str, payload: Any) -> dict[str, Any]:
    """Validate and return one phase payload in deterministic key order."""
    selected = _phase_name(phase)
    validators: Mapping[str, Callable[[Any], dict[str, Any]]] = {
        "plan": _validate_plan,
        "author": lambda value: _validate_edit(value, "author"),
        "review": lambda value: _validate_review(value, "review"),
        "repair": lambda value: _validate_edit(value, "repair"),
        "final_review": lambda value: _validate_review(value, "final_review"),
    }
    return validators[selected](payload)


def canonical_phase_bytes(phase: str, payload: Any) -> bytes:
    """Validate and serialize one phase payload to its sole accepted byte form."""
    content = _canonical_bytes(validate_phase_output(phase, payload))
    if len(content) > MAX_MODEL_OUTPUT_BYTES:
        raise ModelOutputViolation("canonical model output exceeds the size limit")
    return content


def _existing_root(path: Path, field: str) -> Path:
    if not path.is_absolute():
        path = path.absolute()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ModelOutputViolation(f"{field} must be an existing directory") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ModelOutputViolation(f"{field} must be a non-symlink directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ModelOutputViolation(f"{field} must be an existing directory") from exc
    if resolved != path:
        raise ModelOutputViolation(f"{field} path must not contain symlinked components")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ModelOutputViolation(f"{field} must not be group/world writable")
    return path


def _relative_below(root: Path, path: Path, field: str) -> Path:
    if not path.is_absolute():
        raise ModelOutputViolation(f"{field} must be an absolute path")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ModelOutputViolation(f"{field} must be confined to its trusted root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ModelOutputViolation(f"{field} must be confined to its trusted root")
    return relative


def _check_existing_components(root: Path, relative: Path, *, leaf_file: bool) -> Path:
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ModelOutputViolation("trusted model-output path is missing or unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ModelOutputViolation("trusted model-output path must not contain symlinks")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf and leaf_file:
            if not stat.S_ISREG(metadata.st_mode):
                raise ModelOutputViolation("trusted model-output leaf must be a regular file")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ModelOutputViolation("trusted model-output parent must be a directory")
    return current


def _acceptable_input_mode(mode: int, source: str) -> None:
    permissions = stat.S_IMODE(mode)
    if permissions & 0o022 or permissions & 0o7111:
        raise ModelOutputViolation(f"{source} has unsafe permissions")


def _read_regular_file(
    root: Path,
    relative: Path,
    *,
    maximum: int,
    source: str,
) -> tuple[bytes, os.stat_result]:
    path = _check_existing_components(root, relative, leaf_file=True)
    initial = path.lstat()
    _acceptable_input_mode(initial.st_mode, source)
    if initial.st_size > maximum:
        raise ModelOutputViolation(f"{source} exceeds the size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModelOutputViolation(f"{source} must be a readable regular file") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ModelOutputViolation(f"{source} must be a regular file")
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise ModelOutputViolation(f"{source} changed while being opened")
        _acceptable_input_mode(opened.st_mode, source)
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = maximum + 1 - total
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ModelOutputViolation(f"{source} exceeds the size limit")
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            or total != opened.st_size
        ):
            raise ModelOutputViolation(f"{source} changed while being read")
        return b"".join(chunks), opened
    finally:
        os.close(descriptor)


def _output_parent(repo: Path) -> Path:
    parent = repo / ".autopilot"
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise ModelOutputViolation("fixed model-output parent is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ModelOutputViolation("fixed model-output parent is unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ModelOutputViolation("fixed model-output parent must not be group/world writable")
    return parent


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _temporary_file(parent: Path, phase: str, content: bytes) -> Path:
    descriptor: int | None = None
    temporary: Path | None = None
    complete = False
    try:
        for counter in range(100):
            candidate = parent / f".{phase}.{os.getpid()}.{counter}.tmp"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(candidate, flags, 0o600)
                temporary = candidate
                break
            except FileExistsError:
                continue
        if descriptor is None or temporary is None:
            raise ModelOutputViolation("could not reserve a private model-output file")
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise ModelOutputViolation("private model-output mode could not be established")
        os.close(descriptor)
        descriptor = None
        complete = True
        return temporary
    except OSError as exc:
        raise ModelOutputViolation("could not write a private model-output file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not complete and temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _sync_directory(parent: Path) -> None:
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_create(repo: Path, phase: str, content: bytes) -> Path:
    parent = _output_parent(repo)
    output = repo / PHASE_PATHS[phase]
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ModelOutputViolation("fixed model output already exists or is stale")
    temporary = _temporary_file(parent, phase, content)
    try:
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError as exc:
            raise ModelOutputViolation("fixed model output already exists or is stale") from exc
        os.unlink(temporary)
        _sync_directory(parent)
        return output
    except OSError as exc:
        raise ModelOutputViolation("could not atomically create fixed model output") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_replace(
    repo: Path,
    phase: str,
    content: bytes,
    original: os.stat_result,
) -> Path:
    parent = _output_parent(repo)
    output = repo / PHASE_PATHS[phase]
    current = output.lstat()
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise ModelOutputViolation("fixed model output changed before canonicalization")
    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
        raise ModelOutputViolation("fixed model output changed before canonicalization")
    temporary = _temporary_file(parent, phase, content)
    try:
        os.replace(temporary, output)
        _sync_directory(parent)
        return output
    except OSError as exc:
        raise ModelOutputViolation("could not atomically canonicalize fixed model output") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _extract_claude_output(execution_raw: bytes) -> Any:
    log = _parse_json(execution_raw, source="Claude execution file")
    if not isinstance(log, list) or not log:
        raise ModelOutputViolation("Claude execution file must contain a non-empty event array")
    if not all(isinstance(event, dict) for event in log):
        raise ModelOutputViolation("Claude execution events must be objects")
    results = [event for event in log if event.get("type") == "result"]
    if len(results) != 1 or results[0] is not log[-1]:
        raise ModelOutputViolation("Claude execution must contain exactly one final result event")
    result = results[0]
    if result.get("subtype") != "success" or result.get("is_error") is not False:
        raise ModelOutputViolation("Claude final result must be a non-error success")
    structured = result.get("structured_output")
    if not isinstance(structured, dict):
        raise ModelOutputViolation("Claude final result must contain structured_output")
    return structured


def _read_execution_file(execution_file: Path, runner_temp: Path) -> bytes:
    temp_root = _existing_root(runner_temp, "runner_temp")
    relative = _relative_below(temp_root, execution_file, "execution_file")
    raw, _ = _read_regular_file(
        temp_root,
        relative,
        maximum=MAX_EXECUTION_FILE_BYTES,
        source="Claude execution file",
    )
    return raw


def bind_model_output(
    repo: Path,
    phase: str,
    *,
    goal_file: str | None = None,
    execution_file: Path | None = None,
    runner_temp: Path | None = None,
) -> ModelOutputResult:
    """Canonicalize one producer result into its fixed, private phase file."""
    selected = _phase_name(phase)
    candidate_root = _existing_root(repo, "repo")
    if selected in CLAUDE_PHASES:
        if execution_file is None or runner_temp is None:
            raise ModelOutputViolation(
                "Claude phases require execution_file and runner_temp"
            )
        raw_execution = _read_execution_file(execution_file, runner_temp)
        payload = _extract_claude_output(raw_execution)
        if goal_file is None:
            raise ModelOutputViolation("Claude phases require the trusted goal path")
        try:
            payload = validate_output_against_state(
                candidate_root, goal_file, selected, payload
            )
        except FullFileViolation as exc:
            raise ModelOutputViolation(str(exc)) from exc
        content = canonical_phase_bytes(selected, payload)
        _atomic_create(candidate_root, selected, content)
    else:
        if execution_file is not None or runner_temp is not None or goal_file is not None:
            raise ModelOutputViolation(
                "non-Claude phases must not accept Claude context arguments"
            )
        relative = PHASE_PATHS[selected]
        raw, original = _read_regular_file(
            candidate_root,
            relative,
            maximum=MAX_MODEL_OUTPUT_BYTES,
            source=f"{selected} model output",
        )
        payload = _parse_json(raw, source=f"{selected} model output")
        content = canonical_phase_bytes(selected, payload)
        _atomic_replace(candidate_root, selected, content, original)
    digest = hashlib.sha256(content).hexdigest()
    approved = payload["approved"] if selected in REVIEW_PHASES else None
    return ModelOutputResult(selected, digest, approved)


def verify_model_output(
    repo: Path,
    phase: str,
    expected_sha256: str,
    *,
    goal_file: str | None = None,
) -> ModelOutputResult:
    """Verify the fixed file's schema, canonical bytes, safe mode and digest."""
    selected = _phase_name(phase)
    if not isinstance(expected_sha256, str) or not SHA256_PATTERN.fullmatch(expected_sha256):
        raise ModelOutputViolation("expected SHA-256 must be 64 lowercase hex characters")
    candidate_root = _existing_root(repo, "repo")
    raw, _ = _read_regular_file(
        candidate_root,
        PHASE_PATHS[selected],
        maximum=MAX_MODEL_OUTPUT_BYTES,
        source=f"{selected} model output",
    )
    payload = _parse_json(raw, source=f"{selected} model output")
    if selected in CLAUDE_PHASES:
        if goal_file is None:
            raise ModelOutputViolation("Claude phases require the trusted goal path")
        try:
            payload = validate_output_against_state(
                candidate_root, goal_file, selected, payload
            )
        except FullFileViolation as exc:
            raise ModelOutputViolation(str(exc)) from exc
    elif goal_file is not None:
        raise ModelOutputViolation("non-Claude phases must not accept a trusted goal path")
    canonical = canonical_phase_bytes(selected, payload)
    if raw != canonical:
        raise ModelOutputViolation("model output is not in canonical byte form")
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256):
        raise ModelOutputViolation("model output digest does not match the bound SHA-256")
    approved = payload["approved"] if selected in REVIEW_PHASES else None
    return ModelOutputResult(selected, digest, approved)


def _append_github_output(path: Path, result: ModelOutputResult) -> None:
    if not path.is_absolute():
        raise ModelOutputViolation("github_output must be an absolute path")
    try:
        initial = path.lstat()
    except OSError as exc:
        raise ModelOutputViolation("github_output must be an existing regular file") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise ModelOutputViolation("github_output must be a non-symlink regular file")
    _acceptable_input_mode(initial.st_mode, "github_output")
    if initial.st_size > MAX_GITHUB_OUTPUT_BYTES:
        raise ModelOutputViolation("github_output exceeds the size limit")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModelOutputViolation("github_output must be an appendable regular file") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ModelOutputViolation("github_output must be a regular file")
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise ModelOutputViolation("github_output changed while being opened")
        _acceptable_input_mode(opened.st_mode, "github_output")
        values = {"model_sha256": result.sha256}
        if result.approved is not None:
            values["approved"] = "true" if result.approved else "false"
        content = "".join(f"{key}={value}\n" for key, value in values.items()).encode("ascii")
        if opened.st_size + len(content) > MAX_GITHUB_OUTPUT_BYTES:
            raise ModelOutputViolation("github_output exceeds the size limit")
        _write_all(descriptor, content)
    finally:
        os.close(descriptor)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--phase", required=True, choices=tuple(PHASE_PATHS))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bind", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--github-output")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--execution-file")
    parser.add_argument("--runner-temp")
    parser.add_argument("--goal")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.bind:
            if not args.github_output:
                raise ModelOutputViolation("--bind requires --github-output")
            if args.expected_sha256 is not None:
                raise ModelOutputViolation("--bind must not receive --expected-sha256")
            result = bind_model_output(
                Path(args.repo),
                args.phase,
                goal_file=args.goal,
                execution_file=Path(args.execution_file) if args.execution_file else None,
                runner_temp=Path(args.runner_temp) if args.runner_temp else None,
            )
            _append_github_output(Path(args.github_output), result)
            mode = "bind"
        else:
            if args.github_output is not None:
                raise ModelOutputViolation("--verify must not receive --github-output")
            if args.expected_sha256 is None:
                raise ModelOutputViolation("--verify requires --expected-sha256")
            if args.execution_file is not None or args.runner_temp is not None:
                raise ModelOutputViolation("--verify must not receive execution arguments")
            result = verify_model_output(
                Path(args.repo),
                args.phase,
                args.expected_sha256,
                goal_file=args.goal,
            )
            mode = "verify"
        print(json.dumps(result.summary(mode), sort_keys=True, separators=(",", ":")))
        return 0
    except (ModelOutputViolation, OSError) as exc:
        print(
            json.dumps(
                {"error": "model_output_validation_failed", "message": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
