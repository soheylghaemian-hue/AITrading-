"""Strict, state-bound full-file edit transport for Claude author and repair phases."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Goal
from .policy import AutopilotPolicy

CONTRACT_VERSION = "full-file-edit/v1"
STATE_CONTRACT_VERSION = "full-file-edit-state/v1"
EDIT_PHASES = frozenset({"author", "repair"})
STATE_PATHS = {
    "author": Path(".autopilot/author-edit-state.json"),
    "repair": Path(".autopilot/repair-edit-state.json"),
}
MODEL_PATHS = {
    "author": Path(".autopilot/claude.json"),
    "repair": Path(".autopilot/repair.json"),
}
PARENT_PATCH_PATH = Path(".autopilot/candidate.patch")
MAX_EDIT_BYTES = 400_000
MAX_FILES = 80
MAX_PATH_BYTES = 240
MAX_STATE_BYTES = 4_000_000
MAX_MODEL_BYTES = 1_000_000
SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class FullFileViolation(ValueError):
    """Raised when a full-file state, model result or mutation is unsafe."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FullFileViolation("JSON contains a duplicate object key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise FullFileViolation("JSON contains a non-finite number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise FullFileViolation("JSON contains a non-finite number")
    return parsed


def _parse_json(raw: bytes, source: str) -> Any:
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
        raise FullFileViolation(f"{source} must be plain UTF-8 JSON")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except FullFileViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise FullFileViolation(f"{source} must be valid JSON") from exc


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return (encoded + "\n").encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FullFileViolation("value cannot be canonically encoded") from exc


def _exact_object(value: Any, keys: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise FullFileViolation(f"{name} must use the exact trusted schema")
    return value


def _phase(value: Any) -> str:
    if not isinstance(value, str) or value not in EDIT_PHASES:
        raise FullFileViolation("phase must be author or repair")
    return value


def _sha1(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA1_PATTERN.fullmatch(value):
        raise FullFileViolation(f"{field} must be 40 lowercase hex characters")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise FullFileViolation(f"{field} must be 64 lowercase hex characters")
    return value


def _repo_path(value: Any, field: str) -> str:
    try:
        size = len(value.encode("utf-8")) if isinstance(value, str) else 0
    except UnicodeEncodeError as exc:
        raise FullFileViolation(f"{field} must be a canonical repository path") from exc
    if (
        not isinstance(value, str)
        or not value
        or size > MAX_PATH_BYTES
        or "\\" in value
        or value != unicodedata.normalize("NFC", value)
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise FullFileViolation(f"{field} must be a canonical repository path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or value.endswith("/")
        or any(part in {"", ".", "..", ".git", ".autopilot"} for part in pure.parts)
    ):
        raise FullFileViolation(f"{field} must be a safe repository-relative path")
    return value


def _text_content(value: Any, field: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise FullFileViolation(f"{field} must be complete UTF-8 text")
    if "\ufeff" in value or "\x00" in value or "\r" in value:
        raise FullFileViolation(f"{field} contains forbidden binary or newline markers")
    if any(
        (ord(character) < 32 or unicodedata.category(character).startswith("C"))
        and character not in {"\n", "\t"}
        for character in value
    ):
        raise FullFileViolation(f"{field} contains forbidden control characters")
    if "\x7f" in value or not value.endswith("\n") or value.endswith("\n\n"):
        raise FullFileViolation(f"{field} must end in exactly one LF")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FullFileViolation(f"{field} must be complete UTF-8 text") from exc
    return value, len(encoded)


def validate_edit_output(payload: Any, phase: str) -> dict[str, Any]:
    """Validate the exact model-facing full-file edit protocol."""
    selected = _phase(phase)
    value = _exact_object(
        payload,
        frozenset(
            {
                "contract_version",
                "author",
                "phase",
                "base_sha",
                "input_state_sha256",
                "parent_patch_sha256",
                "edits",
            }
        ),
        selected,
    )
    if value["contract_version"] != CONTRACT_VERSION:
        raise FullFileViolation(f"{selected}.contract_version is unsupported")
    if value["author"] != "claude":
        raise FullFileViolation(f"{selected}.author must be exactly claude")
    if value["phase"] != selected:
        raise FullFileViolation(f"{selected}.phase does not match its trusted phase")
    base_sha = _sha1(value["base_sha"], f"{selected}.base_sha")
    state_sha = _sha256(value["input_state_sha256"], f"{selected}.input_state_sha256")
    parent = value["parent_patch_sha256"]
    if selected == "author":
        if parent is not None:
            raise FullFileViolation("author.parent_patch_sha256 must be null")
    else:
        parent = _sha256(parent, "repair.parent_patch_sha256")
    edits = value["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_FILES:
        raise FullFileViolation(f"{selected}.edits must be a bounded non-empty array")
    normalized: list[dict[str, Any]] = []
    total = 0
    for index, edit in enumerate(edits):
        item = _exact_object(
            edit,
            frozenset({"op", "path", "before_sha256", "content"}),
            f"{selected}.edits[{index}]",
        )
        op = item["op"]
        if op not in {"create", "modify"}:
            raise FullFileViolation(f"{selected}.edits[{index}].op is unsupported")
        path = _repo_path(item["path"], f"{selected}.edits[{index}].path")
        before = item["before_sha256"]
        content = item["content"]
        if op == "create":
            if before is not None:
                raise FullFileViolation("create.before_sha256 must be null")
            content, length = _text_content(content, f"{selected}.edits[{index}].content")
            total += length
        elif op == "modify":
            before = _sha256(before, f"{selected}.edits[{index}].before_sha256")
            content, length = _text_content(content, f"{selected}.edits[{index}].content")
            total += length
        normalized.append(
            {"op": op, "path": path, "before_sha256": before, "content": content}
        )
    paths = [item["path"] for item in normalized]
    if paths != sorted(set(paths)):
        raise FullFileViolation(f"{selected}.edits paths must be unique and sorted")
    if total > MAX_EDIT_BYTES:
        raise FullFileViolation(f"{selected}.edits exceed the total content budget")
    return {
        "contract_version": CONTRACT_VERSION,
        "author": "claude",
        "phase": selected,
        "base_sha": base_sha,
        "input_state_sha256": state_sha,
        "parent_patch_sha256": parent,
        "edits": normalized,
    }


def _existing_root(repo: Path) -> Path:
    path = repo if repo.is_absolute() else repo.absolute()
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FullFileViolation("repo must be an existing directory") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or resolved != path:
        raise FullFileViolation("repo must be a real non-symlink directory")
    return path


def _safe_components(repo: Path, relative: str, *, leaf_may_be_missing: bool) -> Path:
    current = repo
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        leaf = index == len(parts) - 1
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if leaf and leaf_may_be_missing:
                return current
            raise FullFileViolation(f"unsafe or missing parent for {relative}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise FullFileViolation(f"symlink component is forbidden for {relative}")
        if not leaf and not stat.S_ISDIR(metadata.st_mode):
            raise FullFileViolation(f"non-directory parent is forbidden for {relative}")
    return current


def _read_regular(path: Path, *, maximum: int, source: str) -> bytes:
    try:
        initial = path.lstat()
    except OSError as exc:
        raise FullFileViolation(f"{source} is missing or unreadable") from exc
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise FullFileViolation(f"{source} must be a regular non-symlink file")
    if initial.st_size > maximum:
        raise FullFileViolation(f"{source} exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FullFileViolation(f"{source} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FullFileViolation(f"{source} must be a regular file")
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise FullFileViolation(f"{source} changed while opening")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise FullFileViolation(f"{source} exceeds its size limit")
        final = os.fstat(descriptor)
        if (
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            or total != opened.st_size
        ):
            raise FullFileViolation(f"{source} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _git(repo: Path, *args: str) -> bytes:
    process = subprocess.run(
        ("git", *args),
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        shell=False,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if process.returncode:
        raise FullFileViolation("trusted git inspection failed")
    return process.stdout


def _goal(repo: Path, goal_file: str) -> tuple[Goal, str]:
    relative = _repo_path(goal_file, "goal_file")
    path = _safe_components(repo, relative, leaf_may_be_missing=False)
    raw = _read_regular(path, maximum=200_000, source="goal file")
    value = _parse_json(raw, "goal file")
    if not isinstance(value, dict):
        raise FullFileViolation("goal file must contain an object")
    try:
        goal = Goal(
            goal_id=str(value["goal_id"]),
            objective=str(value["objective"]),
            success_criteria=tuple(str(item) for item in value["success_criteria"]),
            allowed_paths=tuple(str(item) for item in value.get("allowed_paths", ())),
            max_iterations=int(value.get("max_iterations", 2)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FullFileViolation("goal file is malformed") from exc
    return goal, hashlib.sha256(raw).hexdigest()


def _tracked_modes(repo: Path) -> dict[str, str]:
    raw = _git(repo, "--literal-pathspecs", "ls-files", "--stage", "-z")
    modes: dict[str, str] = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        try:
            header, encoded_path = record.split(b"\t", 1)
            mode = header.split(b" ", 1)[0].decode("ascii")
            path = encoded_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise FullFileViolation("git index contains an invalid path record") from exc
        if path in modes:
            raise FullFileViolation("git index contains multiple stages for a path")
        modes[path] = mode
    return modes


def _scope_snapshot(repo: Path, goal: Goal) -> tuple[list[dict[str, str]], list[str]]:
    raw = _git(
        repo,
        "--literal-pathspecs",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    modes = _tracked_modes(repo)
    policy = AutopilotPolicy()
    names: set[str] = set()
    for encoded in raw.split(b"\x00"):
        if not encoded:
            continue
        try:
            decoded = encoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FullFileViolation("repository contains a non-UTF-8 path") from exc
        if any(part in {".git", ".autopilot"} for part in PurePosixPath(decoded).parts):
            continue
        path = _repo_path(decoded, "repository path")
        if policy.classify_path(path, goal_paths=goal.allowed_paths).allowed:
            names.add(path)
    result: list[dict[str, str]] = []
    directories: set[str] = set()
    for path in sorted(names):
        target = _safe_components(repo, path, leaf_may_be_missing=True)
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o644
            or metadata.st_nlink != 1
            or modes.get(path, "100644") != "100644"
        ):
            raise FullFileViolation(f"allowed-scope file has an unsafe type or mode: {path}")
        content = _read_regular(target, maximum=MAX_STATE_BYTES, source="allowed-scope file")
        result.append(
            {"path": path, "mode": "100644", "sha256": hashlib.sha256(content).hexdigest()}
        )
        for parent in PurePosixPath(path).parents:
            if parent.as_posix() == ".":
                break
            directory = _safe_components(repo, parent.as_posix(), leaf_may_be_missing=False)
            metadata = directory.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise FullFileViolation(f"allowed-scope directory is unsafe: {parent}")
            directories.add(parent.as_posix())
    return result, sorted(directories)


def _parent_patch_digest(repo: Path, phase: str) -> str | None:
    if phase == "author":
        return None
    patch = _safe_components(repo, PARENT_PATCH_PATH.as_posix(), leaf_may_be_missing=False)
    raw = _read_regular(patch, maximum=MAX_EDIT_BYTES, source="parent candidate patch")
    if not raw.startswith(b"diff --git "):
        raise FullFileViolation("parent candidate patch is not a unified git patch")
    return hashlib.sha256(raw).hexdigest()


def _state_body(
    repo: Path,
    goal_file: str,
    phase: str,
    base_sha: str,
    control_sha: str,
) -> dict[str, Any]:
    selected = _phase(phase)
    expected_base = _sha1(base_sha, "base_sha")
    trusted_control = _sha1(control_sha, "control_sha")
    actual_head = _git(repo, "rev-parse", "HEAD").decode("ascii", errors="strict").strip()
    parent_digest = _parent_patch_digest(repo, selected)
    if selected == "author":
        if actual_head != expected_base:
            raise FullFileViolation("candidate HEAD does not match the bound base SHA")
    else:
        comparison = "HEAD"
        if actual_head != expected_base:
            parent_head = _git(repo, "rev-parse", "HEAD^").decode("ascii", errors="strict").strip()
            if parent_head != expected_base:
                raise FullFileViolation("repair baseline is not a direct child of the bound base SHA")
            comparison = "HEAD^"
        live_parent = _git(
            repo,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--full-index",
            "--binary",
            comparison,
        )
        if not hmac.compare_digest(hashlib.sha256(live_parent).hexdigest(), parent_digest):
            raise FullFileViolation("repair checkout does not match the bound parent patch")
    goal, goal_sha = _goal(repo, goal_file)
    files, directories = _scope_snapshot(repo, goal)
    return {
        "contract_version": STATE_CONTRACT_VERSION,
        "phase": selected,
        "base_sha": expected_base,
        "control_sha": trusted_control,
        "goal_id": goal.goal_id,
        "goal_sha256": goal_sha,
        "parent_patch_sha256": parent_digest,
        "files": files,
        "directories": directories,
    }


def _state_wrapper(body: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(_canonical_bytes(body)).hexdigest()
    return {"input_state": body, "input_state_sha256": digest}


def _private_create(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise FullFileViolation("state parent is unavailable") from exc
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise FullFileViolation("state parent is unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise FullFileViolation("state file already exists or cannot be created") from exc
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short write")
            view = view[count:]
        os.fsync(descriptor)
    except OSError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise FullFileViolation("state file could not be written") from exc
    finally:
        os.close(descriptor)


def prepare_state(
    repo: Path,
    goal_file: str,
    phase: str,
    base_sha: str,
    control_sha: str,
) -> str:
    """Create the fixed private state artifact and return its safe digest."""
    root = _existing_root(repo)
    selected = _phase(phase)
    wrapper = _state_wrapper(_state_body(root, goal_file, selected, base_sha, control_sha))
    content = _canonical_bytes(wrapper)
    if len(content) > MAX_STATE_BYTES:
        raise FullFileViolation("full-file state exceeds its size limit")
    _private_create(root / STATE_PATHS[selected], content)
    return wrapper["input_state_sha256"]


def _validate_state_schema(value: Any, phase: str) -> tuple[dict[str, Any], str]:
    selected = _phase(phase)
    wrapper = _exact_object(value, frozenset({"input_state", "input_state_sha256"}), "state")
    digest = _sha256(wrapper["input_state_sha256"], "state.input_state_sha256")
    body = _exact_object(
        wrapper["input_state"],
        frozenset(
            {
                "contract_version",
                "phase",
                "base_sha",
                "control_sha",
                "goal_id",
                "goal_sha256",
                "parent_patch_sha256",
                "files",
                "directories",
            }
        ),
        "state.input_state",
    )
    if body["contract_version"] != STATE_CONTRACT_VERSION or body["phase"] != selected:
        raise FullFileViolation("state contract or phase does not match")
    _sha1(body["base_sha"], "state.base_sha")
    _sha1(body["control_sha"], "state.control_sha")
    _sha256(body["goal_sha256"], "state.goal_sha256")
    if not isinstance(body["goal_id"], str) or not body["goal_id"]:
        raise FullFileViolation("state.goal_id must be non-empty")
    parent = body["parent_patch_sha256"]
    if selected == "author" and parent is not None:
        raise FullFileViolation("author state must not have a parent patch")
    if selected == "repair":
        _sha256(parent, "state.parent_patch_sha256")
    files = body["files"]
    if not isinstance(files, list):
        raise FullFileViolation("state.files must be an array")
    paths: list[str] = []
    for index, item in enumerate(files):
        entry = _exact_object(item, frozenset({"path", "mode", "sha256"}), f"state.files[{index}]")
        paths.append(_repo_path(entry["path"], f"state.files[{index}].path"))
        if entry["mode"] != "100644":
            raise FullFileViolation("state files must be regular non-executable blobs")
        _sha256(entry["sha256"], f"state.files[{index}].sha256")
    if paths != sorted(set(paths)):
        raise FullFileViolation("state file paths must be unique and sorted")
    directories = body["directories"]
    if not isinstance(directories, list):
        raise FullFileViolation("state.directories must be an array")
    normalized_directories = [
        _repo_path(path, f"state.directories[{index}]")
        for index, path in enumerate(directories)
    ]
    if normalized_directories != sorted(set(normalized_directories)):
        raise FullFileViolation("state directories must be unique and sorted")
    if not hmac.compare_digest(hashlib.sha256(_canonical_bytes(body)).hexdigest(), digest):
        raise FullFileViolation("state digest does not match its canonical body")
    return body, digest


def live_state(repo: Path, goal_file: str, phase: str) -> tuple[dict[str, Any], str]:
    """Read the fixed state artifact and prove it still matches the live checkout."""
    root = _existing_root(repo)
    selected = _phase(phase)
    path = _safe_components(root, STATE_PATHS[selected].as_posix(), leaf_may_be_missing=False)
    raw = _read_regular(path, maximum=MAX_STATE_BYTES, source="full-file state")
    value = _parse_json(raw, "full-file state")
    body, digest = _validate_state_schema(value, selected)
    if raw != _canonical_bytes(value):
        raise FullFileViolation("full-file state is not canonical")
    rebuilt = _state_body(
        root,
        goal_file,
        selected,
        body["base_sha"],
        body["control_sha"],
    )
    if not hmac.compare_digest(_canonical_bytes(rebuilt), _canonical_bytes(body)):
        raise FullFileViolation("full-file state no longer matches the live candidate")
    return body, digest


def validate_output_against_state(
    repo: Path,
    goal_file: str,
    phase: str,
    payload: Any,
) -> dict[str, Any]:
    """Bind one strict model edit result to the live trusted state and policy."""
    root = _existing_root(repo)
    selected = _phase(phase)
    output = validate_edit_output(payload, selected)
    state, state_digest = live_state(root, goal_file, selected)
    if output["base_sha"] != state["base_sha"]:
        raise FullFileViolation("model base SHA does not match the trusted state")
    if output["input_state_sha256"] != state_digest:
        raise FullFileViolation("model input-state SHA does not match the trusted state")
    if output["parent_patch_sha256"] != state["parent_patch_sha256"]:
        raise FullFileViolation("model parent-patch SHA does not match the trusted state")
    goal, _ = _goal(root, goal_file)
    paths = [edit["path"] for edit in output["edits"]]
    decisions = AutopilotPolicy().authorize_files(paths, goal_paths=goal.allowed_paths)
    denied = [path for path, decision in zip(paths, decisions) if not decision.allowed]
    if denied:
        raise FullFileViolation(f"model edits include denied paths: {denied!r}")
    files = {item["path"]: item for item in state["files"]}
    directories = set(state["directories"])
    for edit in output["edits"]:
        existing = files.get(edit["path"])
        if edit["op"] == "create":
            if existing is not None:
                raise FullFileViolation("create operation targets an existing state file")
            parent = PurePosixPath(edit["path"]).parent.as_posix()
            if parent not in directories:
                raise FullFileViolation("create parent is not bound by the trusted state")
        elif existing is None or edit["before_sha256"] != existing["sha256"]:
            raise FullFileViolation("edit preimage does not match the trusted state")
        elif edit["op"] == "modify" and hashlib.sha256(
            edit["content"].encode("utf-8")
        ).hexdigest() == existing["sha256"]:
            raise FullFileViolation("modify operation must change the bound file content")
    return output


def _model_output(repo: Path, goal_file: str, phase: str, expected_sha256: str) -> dict[str, Any]:
    selected = _phase(phase)
    expected = _sha256(expected_sha256, "expected model SHA-256")
    path = _safe_components(repo, MODEL_PATHS[selected].as_posix(), leaf_may_be_missing=False)
    raw = _read_regular(path, maximum=MAX_MODEL_BYTES, source="model edit output")
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected):
        raise FullFileViolation("model edit output digest does not match")
    payload = _parse_json(raw, "model edit output")
    normalized = validate_output_against_state(repo, goal_file, selected, payload)
    if raw != _canonical_bytes(normalized):
        raise FullFileViolation("model edit output is not canonical")
    return normalized


def _open_parent(root_descriptor: int, path: str) -> tuple[int, str]:
    parts = PurePosixPath(path).parts
    descriptor = os.dup(root_descriptor)
    try:
        for component in parts[:-1]:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                os.close(child)
                raise FullFileViolation(f"edit parent has unsafe permissions: {path}")
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except OSError as exc:
        os.close(descriptor)
        raise FullFileViolation(f"unsafe or missing edit parent: {path}") from exc
    except Exception:
        os.close(descriptor)
        raise


def _read_target(
    parent_descriptor: int,
    leaf: str,
    path: str,
) -> tuple[os.stat_result | None, str | None]:
    try:
        initial = os.stat(leaf, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if (
        not stat.S_ISREG(initial.st_mode)
        or stat.S_IMODE(initial.st_mode) != 0o644
        or initial.st_nlink != 1
        or initial.st_size > MAX_STATE_BYTES
    ):
        raise FullFileViolation(f"edit target has an unsafe type, mode or link count: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise FullFileViolation(f"edit target cannot be opened safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o644
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
        ):
            raise FullFileViolation(f"edit target changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, MAX_STATE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_STATE_BYTES:
                raise FullFileViolation(f"edit target exceeds its size limit: {path}")
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
            raise FullFileViolation(f"edit target changed while reading: {path}")
        raw = b"".join(chunks)
        if b"\xef\xbb\xbf" in raw or b"\x00" in raw or b"\r" in raw:
            raise FullFileViolation(f"edit target is not plain UTF-8 text: {path}")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise FullFileViolation(f"edit target is not plain UTF-8 text: {path}") from exc
        if any(
            (ord(character) < 32 or unicodedata.category(character).startswith("C"))
            and character not in {"\n", "\t"}
            for character in text
        ):
            raise FullFileViolation(f"edit target contains forbidden controls: {path}")
        return opened, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(descriptor)


def _require_parent_anchor(root_descriptor: int, path: str, pinned_descriptor: int) -> None:
    reopened, _ = _open_parent(root_descriptor, path)
    try:
        pinned = os.fstat(pinned_descriptor)
        current = os.fstat(reopened)
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise FullFileViolation(f"edit parent changed after preflight: {path}")
    finally:
        os.close(reopened)


def _write_temp(parent_descriptor: int, content: str) -> str:
    encoded = content.encode("utf-8", errors="strict")
    for counter in range(100):
        temporary = f".autopilot-{os.getpid()}-{counter}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        try:
            view = memoryview(encoded)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise OSError("short write")
                view = view[count:]
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        except OSError as exc:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except OSError:
                pass
            raise FullFileViolation("temporary edit file could not be written") from exc
        os.close(descriptor)
        return temporary
    raise FullFileViolation("temporary edit file could not be reserved")


def materialize(
    repo: Path,
    goal_file: str,
    phase: str,
    expected_model_sha256: str,
) -> list[str]:
    """Preflight all bound edits, then create or replace only via pinned parent FDs."""
    root = _existing_root(repo)
    selected = _phase(phase)
    output = _model_output(root, goal_file, selected, expected_model_sha256)
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_descriptor = os.open(root, root_flags)
    root_metadata = os.fstat(root_descriptor)
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_IMODE(root_metadata.st_mode) & 0o022:
        os.close(root_descriptor)
        raise FullFileViolation("repo root has unsafe permissions")
    preflight: dict[str, tuple[int, str, os.stat_result | None, str | None]] = {}
    temporaries: dict[str, str] = {}
    try:
        for edit in output["edits"]:
            parent_descriptor, leaf = _open_parent(root_descriptor, edit["path"])
            try:
                metadata, digest = _read_target(parent_descriptor, leaf, edit["path"])
            except Exception:
                os.close(parent_descriptor)
                raise
            if edit["op"] == "create":
                if metadata is not None:
                    os.close(parent_descriptor)
                    raise FullFileViolation("create target appeared after state binding")
            elif metadata is None or digest != edit["before_sha256"]:
                os.close(parent_descriptor)
                raise FullFileViolation("edit target does not match its bound preimage")
            preflight[edit["path"]] = (parent_descriptor, leaf, metadata, digest)
        for edit in output["edits"]:
            parent_descriptor = preflight[edit["path"]][0]
            temporaries[edit["path"]] = _write_temp(parent_descriptor, edit["content"])
        for edit in output["edits"]:
            parent_descriptor, leaf, expected_metadata, expected_digest = preflight[
                edit["path"]
            ]
            _require_parent_anchor(root_descriptor, edit["path"], parent_descriptor)
            metadata, digest = _read_target(parent_descriptor, leaf, edit["path"])
            if expected_metadata is None:
                if metadata is not None:
                    raise FullFileViolation("create target changed during materialization")
            elif (
                metadata is None
                or digest != expected_digest
                or (metadata.st_dev, metadata.st_ino)
                != (expected_metadata.st_dev, expected_metadata.st_ino)
            ):
                raise FullFileViolation("edit target changed during materialization")
        for edit in output["edits"]:
            parent_descriptor, leaf, _, _ = preflight[edit["path"]]
            temporary = temporaries[edit["path"]]
            _require_parent_anchor(root_descriptor, edit["path"], parent_descriptor)
            if edit["op"] == "create":
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=parent_descriptor)
            else:
                os.replace(
                    temporary,
                    leaf,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            del temporaries[edit["path"]]
            os.fsync(parent_descriptor)
            _require_parent_anchor(root_descriptor, edit["path"], parent_descriptor)
        for edit in output["edits"]:
            parent_descriptor, leaf, _, _ = preflight[edit["path"]]
            metadata, digest = _read_target(parent_descriptor, leaf, edit["path"])
            expected = hashlib.sha256(edit["content"].encode("utf-8")).hexdigest()
            if metadata is None or digest != expected:
                raise FullFileViolation("materialized content does not match the bound output")
        return [edit["path"] for edit in output["edits"]]
    except OSError as exc:
        raise FullFileViolation("full-file edit could not be materialized safely") from exc
    finally:
        for path, temporary in temporaries.items():
            parent_descriptor = preflight[path][0]
            try:
                os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        for parent_descriptor, _, _, _ in preflight.values():
            os.close(parent_descriptor)
        os.close(root_descriptor)


def _args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gigbay-autopilot-full-file")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--phase", required=True, choices=tuple(sorted(EDIT_PHASES)))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-state", action="store_true")
    mode.add_argument("--materialize", action="store_true")
    parser.add_argument("--base-sha")
    parser.add_argument("--control-sha")
    parser.add_argument("--expected-model-sha256")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    try:
        if args.prepare_state:
            if not args.base_sha or not args.control_sha or args.expected_model_sha256:
                raise FullFileViolation("state preparation requires only base and control SHAs")
            digest = prepare_state(
                Path(args.repo), args.goal, args.phase, args.base_sha, args.control_sha
            )
            result = {"mode": "prepare-state", "phase": args.phase, "input_state_sha256": digest}
        else:
            if args.base_sha or args.control_sha or not args.expected_model_sha256:
                raise FullFileViolation("materialization requires only the expected model SHA-256")
            paths = materialize(
                Path(args.repo), args.goal, args.phase, args.expected_model_sha256
            )
            result = {"mode": "materialize", "phase": args.phase, "changed_file_count": len(paths)}
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (FullFileViolation, OSError, UnicodeError) as exc:
        print(
            json.dumps(
                {"error": "full_file_validation_failed", "message": str(exc)},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
