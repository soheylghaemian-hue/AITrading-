"""Validate and safely materialize goal-bound persistent review feedback.

The input is trusted repository control data, but is still treated as hostile:
strict JSON, exact keys, bounded values, P0/P1 only, and goal-policy confinement.
The model receives only the canonicalized findings file; no instructions, tool
grants, allowed-path changes, patches, or arbitrary output locations are accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Goal
from .policy import AutopilotPolicy
from .queue import GoalViolation, load_goal

MAX_FEEDBACK_BYTES = 16_384
MAX_FINDINGS = 16
MAX_SOURCES_PER_FINDING = 8
LEARN_GOAL_ID = "trader-brain-learn-v1"
LEARN_MAX_FEEDBACK_BYTES = 32_768
LEARN_MAX_FINDINGS = 17
LEARN_MAX_SOURCES_PER_FINDING = 19
MAX_PATH_BYTES = 240
ALLOWED_SEVERITIES = frozenset({"P0", "P1"})
# ``artifact_audit`` means trusted independent reproduction against the immutable
# artifact associated with the referenced run/job.  It never claims that the
# model review emitted the finding text.
ALLOWED_STAGES = frozenset(
    {"artifact_audit", "gate", "initial_review", "final_review"}
)
TOP_LEVEL_KEYS = frozenset({"schema_version", "kind", "goal_id", "findings"})
FINDING_KEYS = frozenset({"id", "severity", "title", "detail", "location", "sources"})
LOCATION_REQUIRED_KEYS = frozenset({"path"})
LOCATION_OPTIONAL_KEYS = frozenset({"line"})
SOURCE_KEYS = frozenset({"run_id", "job_id", "stage", "base_sha"})
FINDING_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
FEEDBACK_KIND = "unresolved_review_findings"
OUTPUT_RELATIVE = Path(".autopilot/prior-final-review.json")


class FeedbackViolation(ValueError):
    """Raised when persistent feedback fails a fail-closed check."""


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    found: bool
    goal_id: str
    finding_count: int
    sha256: str
    output: str | None

    def summary(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "goal_id": self.goal_id,
            "finding_count": self.finding_count,
            "sha256": self.sha256,
            "output": self.output,
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FeedbackViolation("feedback JSON contains a duplicate object key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise FeedbackViolation("feedback JSON contains a non-finite number")


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FeedbackViolation(f"{field} must be a positive integer")
    return value


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise FeedbackViolation(f"{field} must be a bounded non-empty string")
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        raise FeedbackViolation(f"{field} must be stripped and NFC-normalized")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise FeedbackViolation(f"{field} must not contain control characters")
    return value


def _canonical_location_path(value: Any, goal: Goal, policy: AutopilotPolicy) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise FeedbackViolation("location.path must be a bounded repository-relative path")
    if "\\" in value or any(unicodedata.category(c).startswith("C") for c in value):
        raise FeedbackViolation("location.path contains forbidden characters")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or candidate.as_posix() != value:
        raise FeedbackViolation("location.path must be canonical repository-relative POSIX")
    if value.endswith("/") or any(part in {"", ".", ".."} for part in candidate.parts):
        raise FeedbackViolation("location.path must not contain traversal or empty components")
    decision = policy.classify_path(value, goal_paths=goal.allowed_paths)
    if not decision.allowed:
        raise FeedbackViolation("location.path is not permitted by the selected goal and policy")
    return value


def _validate_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SOURCE_KEYS:
        raise FeedbackViolation("each source must use the exact trusted schema")
    stage = value["stage"]
    if not isinstance(stage, str) or stage not in ALLOWED_STAGES:
        raise FeedbackViolation("source.stage is not an allowed evidence stage")
    base_sha = value["base_sha"]
    if not isinstance(base_sha, str) or not COMMIT_SHA.fullmatch(base_sha):
        raise FeedbackViolation("source.base_sha must be a lowercase full commit SHA")
    return {
        "run_id": _positive_int(value["run_id"], "source.run_id"),
        "job_id": _positive_int(value["job_id"], "source.job_id"),
        "stage": stage,
        "base_sha": base_sha,
    }


def _source_identity(source: Mapping[str, Any]) -> tuple[int, int, str, str]:
    return (
        source["run_id"],
        source["job_id"],
        source["stage"],
        source["base_sha"],
    )


def _feedback_bounds(goal: Goal) -> tuple[int, int, int]:
    if goal.goal_id == LEARN_GOAL_ID:
        return (
            LEARN_MAX_FEEDBACK_BYTES,
            LEARN_MAX_FINDINGS,
            LEARN_MAX_SOURCES_PER_FINDING,
        )
    return MAX_FEEDBACK_BYTES, MAX_FINDINGS, MAX_SOURCES_PER_FINDING


def validate_feedback(
    payload: Any,
    goal: Goal,
    policy: AutopilotPolicy | None = None,
) -> dict[str, Any]:
    """Return a canonical-order validated payload or fail closed."""
    if not isinstance(payload, dict) or set(payload) != TOP_LEVEL_KEYS:
        raise FeedbackViolation("feedback must use the exact trusted top-level schema")
    if (
        isinstance(payload["schema_version"], bool)
        or not isinstance(payload["schema_version"], int)
        or payload["schema_version"] != 1
    ):
        raise FeedbackViolation("unsupported feedback schema_version")
    if payload["kind"] != FEEDBACK_KIND:
        raise FeedbackViolation("unsupported feedback kind")
    if payload["goal_id"] != goal.goal_id:
        raise FeedbackViolation("feedback goal_id does not match the selected goal")
    findings = payload["findings"]
    _, max_findings, max_sources = _feedback_bounds(goal)
    if not isinstance(findings, list) or not 1 <= len(findings) <= max_findings:
        raise FeedbackViolation("findings count is outside the trusted bound")

    path_policy = policy or AutopilotPolicy()
    normalized: list[dict[str, Any]] = []
    finding_ids: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != FINDING_KEYS:
            raise FeedbackViolation("each finding must use the exact trusted schema")
        finding_id = finding["id"]
        if not isinstance(finding_id, str) or not FINDING_ID.fullmatch(finding_id):
            raise FeedbackViolation("finding.id must be a safe identifier")
        if finding_id in finding_ids:
            raise FeedbackViolation("finding.id values must be unique")
        finding_ids.add(finding_id)
        severity = finding["severity"]
        if not isinstance(severity, str) or severity not in ALLOWED_SEVERITIES:
            raise FeedbackViolation("only unresolved P0/P1 findings may be persisted")
        location = finding["location"]
        if not isinstance(location, dict):
            raise FeedbackViolation("finding.location must be an object")
        location_keys = set(location)
        if not LOCATION_REQUIRED_KEYS <= location_keys or not location_keys <= (
            LOCATION_REQUIRED_KEYS | LOCATION_OPTIONAL_KEYS
        ):
            raise FeedbackViolation("finding.location must use the exact trusted schema")
        normalized_location: dict[str, Any] = {
            "path": _canonical_location_path(location["path"], goal, path_policy)
        }
        if "line" in location:
            normalized_location["line"] = _positive_int(location["line"], "location.line")

        sources = finding["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= max_sources:
            raise FeedbackViolation("finding.sources count is outside the trusted bound")
        normalized_sources = [_validate_source(source) for source in sources]
        source_identities = [_source_identity(source) for source in normalized_sources]
        if len(source_identities) != len(set(source_identities)):
            raise FeedbackViolation("finding.sources must not contain duplicates")
        normalized_sources.sort(key=_source_identity)

        normalized.append(
            {
                "id": finding_id,
                "severity": severity,
                "title": _bounded_text(finding["title"], "finding.title", 200),
                "detail": _bounded_text(finding["detail"], "finding.detail", 3000),
                "location": normalized_location,
                "sources": normalized_sources,
            }
        )
    normalized.sort(key=lambda finding: finding["id"])
    return {
        "schema_version": 1,
        "kind": FEEDBACK_KIND,
        "goal_id": goal.goal_id,
        "findings": normalized,
    }


def canonical_feedback_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize an already validated payload into stable model-context bytes."""
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _read_feedback_file(path: Path, *, max_bytes: int) -> bytes:
    try:
        initial = path.lstat()
    except OSError as exc:
        raise FeedbackViolation("feedback must be a readable non-symlink regular file") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise FeedbackViolation("feedback must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FeedbackViolation("feedback must be a readable non-symlink regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FeedbackViolation("feedback must be a regular file")
        if metadata.st_size > max_bytes:
            raise FeedbackViolation("feedback exceeds the size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(4096, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise FeedbackViolation("feedback exceeds the size limit")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_feedback(
    path: Path,
    goal: Goal,
    policy: AutopilotPolicy | None = None,
) -> dict[str, Any]:
    """Load and validate one confined feedback file."""
    max_bytes, _, _ = _feedback_bounds(goal)
    raw = _read_feedback_file(path, max_bytes=max_bytes)
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise FeedbackViolation("feedback must be plain UTF-8 JSON")
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedbackViolation("feedback is not valid UTF-8 JSON") from exc
    normalized = validate_feedback(payload, goal, policy)
    if len(canonical_feedback_bytes(normalized)) > max_bytes:
        raise FeedbackViolation("canonical feedback exceeds the size limit")
    return normalized


def _safe_relative(value: str, field: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise FeedbackViolation(f"{field} must be a safe relative POSIX path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise FeedbackViolation(f"{field} must be a safe relative POSIX path")
    return Path(*pure.parts)


def _existing_root(path: Path, field: str) -> Path:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise FeedbackViolation(f"{field} must be an existing directory") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FeedbackViolation(f"{field} must be a non-symlink directory")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise FeedbackViolation(f"{field} must be an existing directory") from exc
    if resolved != absolute:
        raise FeedbackViolation(f"{field} path must not contain symlinked components")
    return absolute


def _check_confined_components(root: Path, relative: Path, *, leaf_file: bool) -> Path:
    current = root
    for index, component in enumerate(relative.parts):
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise FeedbackViolation("trusted control path is missing or unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise FeedbackViolation("trusted control path must not contain symlinks")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf and leaf_file:
            if not stat.S_ISREG(metadata.st_mode):
                raise FeedbackViolation("trusted control leaf must be a regular file")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise FeedbackViolation("trusted control parent must be a directory")
    return current


def _feedback_path(control_root: Path, goal: Goal) -> Path | None:
    relative = Path(".github", "autopilot", "feedback", f"{goal.goal_id}.json")
    current = control_root
    for index, component in enumerate(relative.parts):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FeedbackViolation("feedback control path is unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise FeedbackViolation("feedback control path must not contain symlinks")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf:
            if not stat.S_ISREG(metadata.st_mode):
                raise FeedbackViolation("feedback control leaf must be a regular file")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise FeedbackViolation("feedback control parent must be a directory")
    return current


def _assert_no_stale_output(repo: Path) -> None:
    parent = repo / OUTPUT_RELATIVE.parent
    output = repo / OUTPUT_RELATIVE
    try:
        parent_metadata = parent.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise FeedbackViolation("fixed feedback output parent is unsafe")
    try:
        output.lstat()
    except FileNotFoundError:
        return
    raise FeedbackViolation("fixed feedback output already exists or is stale")


def _create_output_parent(repo: Path) -> Path:
    parent = repo / OUTPUT_RELATIVE.parent
    try:
        os.mkdir(parent, 0o700)
    except FileExistsError:
        pass
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise FeedbackViolation("fixed feedback output parent is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FeedbackViolation("fixed feedback output parent is unsafe")
    return parent


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _atomic_create_output(repo: Path, content: bytes) -> Path:
    parent = _create_output_parent(repo)
    output = repo / OUTPUT_RELATIVE
    _assert_no_stale_output(repo)
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        for counter in range(100):
            candidate = parent / f".prior-final-review.{os.getpid()}.{counter}.tmp"
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
            raise FeedbackViolation("could not reserve a fixed feedback output")
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError as exc:
            raise FeedbackViolation("fixed feedback output already exists or is stale") from exc
        os.unlink(temporary)
        temporary = None
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return output
    except OSError as exc:
        raise FeedbackViolation("could not safely materialize fixed feedback output") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def materialize_feedback(
    repo: Path,
    control_root: Path,
    goal_path: str,
    expected_sha256: str | None = None,
    *,
    write_output: bool = True,
) -> FeedbackResult:
    """Inspect or materialize canonical findings at one fixed candidate path."""
    candidate_root = _existing_root(repo, "repo")
    trusted_root = _existing_root(control_root, "control_root")
    goal_relative = _safe_relative(goal_path, "goal")
    trusted_goal = _check_confined_components(candidate_root, goal_relative, leaf_file=True)
    try:
        goal = load_goal(trusted_goal)
    except GoalViolation as exc:
        raise FeedbackViolation("selected candidate goal is invalid") from exc
    source = _feedback_path(trusted_root, goal)
    if source is None:
        content = b""
        normalized = None
    else:
        normalized = load_feedback(source, goal)
        content = canonical_feedback_bytes(normalized)
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise FeedbackViolation("expected feedback SHA-256 must be 64 lowercase hex characters")
        if digest != expected_sha256:
            raise FeedbackViolation("feedback digest changed after prepare")
    output: str | None = None
    if write_output:
        if source is None:
            _assert_no_stale_output(candidate_root)
        else:
            _atomic_create_output(candidate_root, content)
            output = OUTPUT_RELATIVE.as_posix()
    return FeedbackResult(
        source is not None,
        goal.goal_id,
        len(normalized["findings"]) if normalized else 0,
        digest,
        output,
    )


def _append_github_output(path: Path, result: FeedbackResult) -> None:
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FeedbackViolation("github output must be an existing non-symlink regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FeedbackViolation("github output must be a regular file")
        values = {
            "has_feedback": "true" if result.found else "false",
            "feedback_count": str(result.finding_count),
            "feedback_sha256": result.sha256,
            "feedback_path": result.output or "",
        }
        _write_all(
            descriptor,
            "".join(f"{key}={value}\n" for key, value in values.items()).encode("ascii"),
        )
    finally:
        os.close(descriptor)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--github-output")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--materialize", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.materialize != (args.expected_sha256 is not None):
            raise FeedbackViolation(
                "--materialize and --expected-sha256 must be supplied together"
            )
        result = materialize_feedback(
            Path(args.repo),
            Path(args.control_root),
            args.goal,
            args.expected_sha256,
            write_output=args.materialize,
        )
        if args.github_output:
            _append_github_output(Path(args.github_output), result)
        print(json.dumps(result.summary(), sort_keys=True, separators=(",", ":")))
        return 0
    except (FeedbackViolation, OSError) as exc:
        print(
            json.dumps(
                {"error": "feedback_validation_failed", "message": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
