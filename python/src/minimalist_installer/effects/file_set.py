"""Prepared, checkpointed three-hash reconciliation for ordinary files."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from ..core.errors import (
    GreenfieldConflictError,
    InvalidEffectError,
    ModifiedContentError,
)
from ..core.locks import canonical_resource_identity
from ..core.models import (
    CheckpointWriter,
    EffectContext,
    JsonObject,
    JsonValue,
    Operation,
    PreparedEffect,
    _json_value,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STATE_VERSION = 1
_MUTATING = frozenset({"write", "write_missing", "replace", "delete"})
_CHECKPOINTED = _MUTATING | frozenset({"missing"})


class _EffectFilesystem(Protocol):
    base: Path

    @property
    def closed(self) -> bool: ...

    def read_bytes(self, relative: str) -> bytes: ...

    def directory_exists(self, relative: str) -> bool: ...

    def ensure_directory(self, relative: str) -> None: ...

    def atomic_write_bytes(
        self, relative: str, data: bytes, *, mode: int = 0o600
    ) -> None: ...

    def unlink(self, relative: str, *, missing_ok: bool = False) -> bool: ...

    def rmdir_empty(self, relative: str, *, missing_ok: bool = False) -> bool: ...


class FileDecision(StrEnum):
    """Deterministic result of comparing desired, installed, and disk hashes."""

    WRITE = "write"
    ADOPT = "adopt"
    CONFLICT = "conflict"
    UNCHANGED = "unchanged"
    REPLACE = "replace"
    WRITE_MISSING = "write_missing"
    ALREADY_DESIRED = "already_desired"
    PRESERVE_MODIFIED = "preserve_modified"
    DELETE = "delete"
    PRESERVE_ORPHAN = "preserve_orphan"
    MISSING = "missing"


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact bytes."""

    if not isinstance(data, bytes):
        raise TypeError("file content must be bytes")
    return hashlib.sha256(data).hexdigest()


def _optional_digest(value: str | None, label: str) -> str | None:
    if value is not None and (
        not isinstance(value, str) or _DIGEST.fullmatch(value) is None
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest or null")
    return value


def classify_file(
    *,
    desired_hash: str | None,
    installed_hash: str | None,
    disk_hash: str | None,
    adopt_identical: bool = False,
) -> FileDecision:
    """Classify one path without filesystem access or mutation."""

    desired = _optional_digest(desired_hash, "desired_hash")
    installed = _optional_digest(installed_hash, "installed_hash")
    disk = _optional_digest(disk_hash, "disk_hash")
    if not isinstance(adopt_identical, bool):
        raise TypeError("adopt_identical must be a boolean")
    if desired is None and installed is None:
        raise ValueError("classification requires desired or installed ownership")

    if installed is None:
        if disk is None:
            return FileDecision.WRITE
        if adopt_identical and disk == desired:
            return FileDecision.ADOPT
        return FileDecision.CONFLICT

    if desired is None:
        if disk is None:
            return FileDecision.MISSING
        if disk == installed:
            return FileDecision.DELETE
        return FileDecision.PRESERVE_ORPHAN

    if disk is None:
        return FileDecision.WRITE_MISSING
    if disk == installed:
        if desired == installed:
            return FileDecision.UNCHANGED
        return FileDecision.REPLACE
    if disk == desired:
        return FileDecision.ALREADY_DESIRED
    if desired == installed:
        return FileDecision.PRESERVE_MODIFIED
    return FileDecision.CONFLICT


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ValueError(
            f"{label} keys must be exactly {sorted(expected)}; got {sorted(actual)}"
        )


def _normalize_relative(value: object, label: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty relative text path")
    portable = value.replace("\\", "/")
    if allow_dot and portable == ".":
        return "."
    parts = portable.split("/")
    if portable.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must be a normalized relative path")
    return "/".join(parts)


def _collision_key(path: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.split("/"))


def _validate_collisions(paths: Sequence[str], label: str) -> None:
    normalized_seen: set[str] = set()
    portable_seen: dict[tuple[str, ...], str] = {}
    portable_paths: list[tuple[tuple[str, ...], str]] = []
    for path in paths:
        if path in normalized_seen:
            raise ValueError(f"{label} contains duplicate path: {path}")
        normalized_seen.add(path)
        key = _collision_key(path)
        if key in portable_seen:
            raise ValueError(
                f"{label} contains colliding paths: {portable_seen[key]} and {path}"
            )
        portable_seen[key] = path
        portable_paths.append((key, path))
    portable_paths.sort()
    for index, (key, path) in enumerate(portable_paths):
        for other_key, other_path in portable_paths[index + 1 :]:
            if len(other_key) <= len(key):
                continue
            if other_key[: len(key)] == key:
                raise ValueError(
                    f"{label} contains colliding file and descendant paths: "
                    f"{path} and {other_path}"
                )
            if other_key[:1] != key[:1]:
                break


def _filesystem(value: object, label: str) -> _EffectFilesystem:
    required = (
        "base",
        "closed",
        "read_bytes",
        "directory_exists",
        "ensure_directory",
        "atomic_write_bytes",
        "unlink",
        "rmdir_empty",
    )
    if value is None or any(not hasattr(value, name) for name in required):
        raise InvalidEffectError(f"{label} requires a held safe filesystem")
    filesystem = cast(_EffectFilesystem, value)
    if filesystem.closed:
        raise InvalidEffectError(f"{label} filesystem is closed")
    return filesystem


def _read_optional(filesystem: _EffectFilesystem, path: str) -> bytes | None:
    try:
        return filesystem.read_bytes(path)
    except FileNotFoundError:
        return None


def _joined_path(destination: str, path: str) -> str:
    return path if destination == "." else f"{destination}/{path}"


def _parent_paths(path: str) -> tuple[str, ...]:
    parts = path.split("/")[:-1]
    return tuple("/".join(parts[:length]) for length in range(1, len(parts) + 1))


def _is_parent(parent: str, path: str) -> bool:
    return path.startswith(f"{parent}/")


def _sorted_parents(paths: Sequence[str], *, deepest_first: bool = False) -> tuple[str, ...]:
    unique = set(paths)
    if deepest_first:
        return tuple(sorted(unique, key=lambda path: (-path.count("/"), path.encode())))
    return tuple(sorted(unique, key=str.encode))


def _parse_owned_parents(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise TypeError(f"{label} must be an array")
    normalized = tuple(
        _normalize_relative(item, f"{label}[{index}]")
        for index, item in enumerate(value)
    )
    portable: dict[tuple[str, ...], str] = {}
    for path in normalized:
        key = _collision_key(path)
        if key in portable:
            raise ValueError(
                f"{label} contains duplicate or colliding parents: "
                f"{portable[key]} and {path}"
            )
        portable[key] = path
    ordered = _sorted_parents(normalized)
    if ordered != normalized:
        raise ValueError(f"{label} must be unique and sorted")
    return ordered


def _remove_owned_parents(
    filesystem: _EffectFilesystem, parents: Sequence[str]
) -> None:
    for parent in _sorted_parents(parents, deepest_first=True):
        filesystem.rmdir_empty(parent, missing_ok=True)


def _parse_desired(
    value: object, destination: str
) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list | tuple):
        raise TypeError("args.desired must be an array")
    parsed: list[dict[str, object]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise TypeError(f"args.desired[{index}] must be an object")
        _exact_keys(entry, frozenset({"path", "content"}), f"args.desired[{index}]")
        relative = _normalize_relative(entry["path"], f"args.desired[{index}].path")
        content = entry["content"]
        if not isinstance(content, str):
            raise TypeError(f"args.desired[{index}].content must be text")
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError(
                f"args.desired[{index}].content must be valid Unicode"
            ) from error
        parsed.append(
            {
                "path": _joined_path(destination, relative),
                "content": content,
                "content_bytes": encoded,
                "desired_hash": sha256_bytes(encoded),
            }
        )
    _validate_collisions([cast(str, item["path"]) for item in parsed], "desired")
    return tuple(sorted(parsed, key=lambda item: cast(str, item["path"]).encode()))


def _parse_state(
    value: JsonValue | None,
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    if value is None:
        return (), ()
    if not isinstance(value, Mapping):
        raise TypeError("previous file-set state must be an object")
    keys = frozenset(value)
    legacy_keys = frozenset({"version", "files"})
    current_keys = frozenset({"version", "files", "created_parents"})
    if keys not in {legacy_keys, current_keys}:
        raise ValueError(
            "previous state keys must describe files and created parents"
        )
    if value["version"] != _STATE_VERSION or isinstance(value["version"], bool):
        raise ValueError("previous file-set state has an unsupported version")
    files = value["files"]
    if not isinstance(files, list | tuple):
        raise TypeError("previous state files must be an array")
    parsed: list[dict[str, str]] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, Mapping):
            raise TypeError(f"previous state files[{index}] must be an object")
        _exact_keys(
            entry,
            frozenset({"path", "installed_hash"}),
            f"previous state files[{index}]",
        )
        path = _normalize_relative(entry["path"], f"previous state files[{index}].path")
        digest = entry["installed_hash"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError(
                f"previous state files[{index}].installed_hash must be a SHA-256 digest"
            )
        parsed.append({"path": path, "installed_hash": digest})
    _validate_collisions([item["path"] for item in parsed], "previous state")
    ordered_files = tuple(sorted(parsed, key=lambda item: item["path"].encode()))
    parents = (
        _parse_owned_parents(value["created_parents"], "previous created_parents")
        if "created_parents" in value
        else ()
    )
    if any(not any(_is_parent(parent, item["path"]) for item in ordered_files) for parent in parents):
        raise ValueError("previous created parent does not own a tracked file path")
    return ordered_files, parents


def _decision_payload(
    *,
    path: str,
    decision: FileDecision,
    desired_hash: str | None,
    installed_hash: str | None,
    disk_hash: str | None,
    content: str | None,
    before_content: bytes | None,
    created_parents: Sequence[str],
    owned_parents: Sequence[str],
    released_parents: Sequence[str],
) -> JsonObject:
    return {
        "path": path,
        "decision": decision.value,
        "desired_hash": desired_hash,
        "installed_hash": installed_hash,
        "disk_hash": disk_hash,
        "content": content,
        "before_content": (
            base64.b64encode(before_content).decode("ascii")
            if before_content is not None and decision.value in _MUTATING
            else None
        ),
        "created_parents": list(created_parents),
        "owned_parents": list(owned_parents),
        "released_parents": list(released_parents),
    }


def _parse_prepared(prepared: PreparedEffect) -> tuple[_EffectFilesystem, tuple[Mapping[str, JsonValue], ...]]:
    if not isinstance(prepared, PreparedEffect):
        raise TypeError("prepared must be a PreparedEffect")
    filesystem = _filesystem(prepared.filesystem, "file-set apply")
    payload = prepared.payload
    if not isinstance(payload, Mapping):
        raise InvalidEffectError("file-set prepared payload must be an object")
    _exact_keys(payload, frozenset({"version", "decisions"}), "prepared payload")
    if payload["version"] != _STATE_VERSION or isinstance(payload["version"], bool):
        raise InvalidEffectError("file-set prepared payload has an unsupported version")
    decisions = payload["decisions"]
    if not isinstance(decisions, list | tuple):
        raise InvalidEffectError("file-set prepared decisions must be an array")
    parsed: list[Mapping[str, JsonValue]] = []
    prior_path: bytes | None = None
    expected = frozenset(
        {
            "path",
            "decision",
            "desired_hash",
            "installed_hash",
            "disk_hash",
            "content",
            "before_content",
            "created_parents",
            "owned_parents",
            "released_parents",
        }
    )
    for item in decisions:
        if not isinstance(item, Mapping):
            raise InvalidEffectError("file-set prepared decision must be an object")
        _exact_keys(item, expected, "prepared decision")
        path = _normalize_relative(item["path"], "prepared decision path")
        encoded_path = path.encode()
        if prior_path is not None and encoded_path <= prior_path:
            raise InvalidEffectError("file-set prepared decisions are not deterministic")
        prior_path = encoded_path
        try:
            FileDecision(str(item["decision"]))
        except ValueError as error:
            raise InvalidEffectError("file-set prepared decision is unsupported") from error
        for name in ("desired_hash", "installed_hash", "disk_hash"):
            value = item[name]
            if value is not None and (
                not isinstance(value, str) or _DIGEST.fullmatch(value) is None
            ):
                raise InvalidEffectError(f"prepared decision {name} is invalid")
        content = item["content"]
        if content is not None and not isinstance(content, str):
            raise InvalidEffectError("prepared decision content must be text or null")
        backup = item["before_content"]
        if backup is not None and not isinstance(backup, str):
            raise InvalidEffectError("prepared decision backup must be base64 text or null")
        for name in ("created_parents", "owned_parents", "released_parents"):
            try:
                parents = _parse_owned_parents(item[name], f"prepared decision {name}")
            except (TypeError, ValueError) as error:
                raise InvalidEffectError(str(error)) from error
            if any(not _is_parent(parent, path) for parent in parents):
                raise InvalidEffectError(
                    f"prepared decision {name} contains a non-parent path"
                )
        parsed.append(item)
    _validate_collisions(
        [cast(str, item["path"]) for item in parsed], "prepared decisions"
    )
    return filesystem, tuple(parsed)


def _validate_tracking_state(
    before_state: JsonValue,
    decisions: Sequence[Mapping[str, JsonValue]],
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    files, created_parents = _parse_state(before_state)
    expected: list[dict[str, str]] = []
    expected_parents: list[str] = []
    for item in decisions:
        desired_hash = item["desired_hash"]
        if desired_hash is None:
            continue
        decision = FileDecision(cast(str, item["decision"]))
        tracking_hash = (
            item["installed_hash"]
            if decision in {FileDecision.CONFLICT, FileDecision.PRESERVE_MODIFIED}
            else desired_hash
        )
        if not isinstance(tracking_hash, str):
            raise InvalidEffectError("prepared decision has no tracking hash")
        expected.append(
            {"path": cast(str, item["path"]), "installed_hash": tracking_hash}
        )
        owned = item["owned_parents"]
        if not isinstance(owned, list | tuple):
            raise InvalidEffectError("prepared decision owned_parents must be an array")
        expected_parents.extend(cast(Sequence[str], owned))
    if tuple(expected) != files:
        raise InvalidEffectError(
            "file-set tracking state does not match prepared decisions"
        )
    if _sorted_parents(expected_parents) != created_parents:
        raise InvalidEffectError(
            "file-set created parent state does not match prepared decisions"
        )
    return files, created_parents


def _decode_content(decision: Mapping[str, JsonValue]) -> bytes:
    content = decision["content"]
    desired_hash = decision["desired_hash"]
    if not isinstance(content, str) or not isinstance(desired_hash, str):
        raise InvalidEffectError("mutating desired decision lacks content or hash")
    try:
        data = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise InvalidEffectError("prepared decision content is invalid Unicode") from error
    if sha256_bytes(data) != desired_hash:
        raise InvalidEffectError("prepared desired content does not match its hash")
    return data


def _decode_backup(decision: Mapping[str, JsonValue]) -> bytes | None:
    encoded = decision["before_content"]
    if encoded is None:
        return None
    if not isinstance(encoded, str):
        raise InvalidEffectError("prepared backup is not base64 text")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise InvalidEffectError("prepared backup is invalid base64") from error
    disk_hash = decision["disk_hash"]
    if not isinstance(disk_hash, str) or sha256_bytes(data) != disk_hash:
        raise InvalidEffectError("prepared backup does not match its disk hash")
    return data


def _checkpoint_state(
    decision: Mapping[str, JsonValue], *, phase: str, backup_digest: str | None
) -> JsonObject:
    return {
        "phase": phase,
        "path": decision["path"],
        "decision": decision["decision"],
        "before_hash": decision["disk_hash"],
        "after_hash": decision["desired_hash"],
        "backup_digest": backup_digest,
        "created_parents": decision["created_parents"],
        "released_parents": decision["released_parents"],
    }


def _validate_checkpoint(
    state: object,
    decision: Mapping[str, JsonValue],
    *,
    phases: frozenset[str],
) -> Mapping[str, JsonValue]:
    if not isinstance(state, Mapping):
        raise InvalidEffectError("file checkpoint must be an object")
    expected = frozenset(
        {
            "phase",
            "path",
            "decision",
            "before_hash",
            "after_hash",
            "backup_digest",
            "created_parents",
            "released_parents",
        }
    )
    _exact_keys(state, expected, "file checkpoint")
    if state["phase"] not in phases:
        raise InvalidEffectError("file checkpoint has an invalid phase")
    comparisons = {
        "path": decision["path"],
        "decision": decision["decision"],
        "before_hash": decision["disk_hash"],
        "after_hash": decision["desired_hash"],
        "created_parents": decision["created_parents"],
        "released_parents": decision["released_parents"],
    }
    if any(state[key] != value for key, value in comparisons.items()):
        raise InvalidEffectError("file checkpoint does not match prepared decision")
    digest = state["backup_digest"]
    if digest is not None and (
        not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
    ):
        raise InvalidEffectError("file checkpoint backup digest is invalid")
    return cast(Mapping[str, JsonValue], state)


class ReconcileFileSetEffect:
    """Safely own, update, orphan, and uninstall a deterministic file set."""

    type = "reconcile_file_set"
    version = 1

    def prepare(
        self,
        args: JsonObject,
        previous: JsonValue | None,
        context: EffectContext,
    ) -> PreparedEffect:
        if not isinstance(args, Mapping):
            raise TypeError("file-set args must be an object")
        allowed = frozenset({"desired", "destination", "adopt_identical"})
        unknown = frozenset(args) - allowed
        missing = frozenset({"desired"}) - frozenset(args)
        if unknown or missing:
            raise ValueError(
                f"file-set args keys are invalid: unknown={sorted(unknown)}, "
                f"missing={sorted(missing)}"
            )
        destination = _normalize_relative(
            args.get("destination", "."), "args.destination", allow_dot=True
        )
        adopt = args.get("adopt_identical", False)
        if not isinstance(adopt, bool):
            raise TypeError("args.adopt_identical must be a boolean")
        filesystem = _filesystem(context.filesystem, "file-set prepare")
        desired = _parse_desired(args["desired"], destination)
        previous_files, previous_created_parents = _parse_state(previous)
        desired_by_path = {cast(str, entry["path"]): entry for entry in desired}
        previous_by_path = {entry["path"]: entry for entry in previous_files}
        all_paths = sorted(
            desired_by_path.keys() | previous_by_path.keys(), key=str.encode
        )
        _validate_collisions(all_paths, "combined desired and previous state")

        desired_parent_paths = {
            path: _parent_paths(path) for path in desired_by_path
        }
        all_desired_parents = _sorted_parents(
            tuple(
                parent
                for parents in desired_parent_paths.values()
                for parent in parents
            )
        )
        absent_parents = frozenset(
            parent
            for parent in all_desired_parents
            if not filesystem.directory_exists(parent)
        )
        next_created_parents = _sorted_parents(
            tuple(
                parent
                for parent in (*previous_created_parents, *absent_parents)
                if any(_is_parent(parent, path) for path in desired_by_path)
            )
        )
        released_parents = frozenset(previous_created_parents) - frozenset(
            next_created_parents
        )

        decisions: list[JsonValue] = []
        next_files: list[JsonValue] = []
        for path in all_paths:
            desired_entry = desired_by_path.get(path)
            previous_entry = previous_by_path.get(path)
            disk_bytes = _read_optional(filesystem, path)
            disk_hash = sha256_bytes(disk_bytes) if disk_bytes is not None else None
            desired_hash = (
                cast(str, desired_entry["desired_hash"])
                if desired_entry is not None
                else None
            )
            installed_hash = (
                previous_entry["installed_hash"]
                if previous_entry is not None
                else None
            )
            decision = classify_file(
                desired_hash=desired_hash,
                installed_hash=installed_hash,
                disk_hash=disk_hash,
                adopt_identical=adopt,
            )
            if installed_hash is None and decision is FileDecision.CONFLICT:
                raise GreenfieldConflictError(
                    f'greenfield path already exists and was not adopted: "{path}"',
                    operation=context.operation,
                    path=filesystem.base / path,
                    details={"path": path, "disk_hash": disk_hash},
                )
            content = (
                cast(str, desired_entry["content"])
                if desired_entry is not None
                else None
            )
            parents = desired_parent_paths.get(path, ())
            created_for_path = _sorted_parents(
                tuple(parent for parent in parents if parent in absent_parents)
            )
            owned_for_path = _sorted_parents(
                tuple(parent for parent in parents if parent in next_created_parents)
            )
            released_for_path = _sorted_parents(
                tuple(
                    parent
                    for parent in _parent_paths(path)
                    if parent in released_parents
                )
            )
            decisions.append(
                _decision_payload(
                    path=path,
                    decision=decision,
                    desired_hash=desired_hash,
                    installed_hash=installed_hash,
                    disk_hash=disk_hash,
                    content=content,
                    before_content=disk_bytes,
                    created_parents=created_for_path,
                    owned_parents=owned_for_path,
                    released_parents=released_for_path,
                )
            )
            if desired_entry is not None:
                tracked_hash = (
                    installed_hash
                    if decision
                    in {FileDecision.CONFLICT, FileDecision.PRESERVE_MODIFIED}
                    else desired_hash
                )
                if tracked_hash is None:
                    raise InvalidEffectError("desired file lacks a tracking hash")
                next_files.append(
                    {"path": path, "installed_hash": tracked_hash}
                )

        state: JsonObject = {
            "version": _STATE_VERSION,
            "files": next_files,
            "created_parents": list(next_created_parents),
        }
        payload: JsonObject = {"version": _STATE_VERSION, "decisions": decisions}
        resource_path = filesystem.base
        if destination != ".":
            resource_path = filesystem.base.joinpath(*destination.split("/"))
        return PreparedEffect(
            before_state=state,
            payload=payload,
            resources=(canonical_resource_identity("path", resource_path),),
            filesystem=filesystem,
        )

    def apply(
        self,
        prepared: PreparedEffect,
        checkpoint: CheckpointWriter,
    ) -> JsonValue:
        filesystem, decisions = _parse_prepared(prepared)
        files, _created_parents = _validate_tracking_state(
            prepared.before_state, decisions
        )
        for index, decision in enumerate(decisions):
            action = str(decision["decision"])
            released = decision["released_parents"]
            if not isinstance(released, list | tuple):
                raise InvalidEffectError("released_parents must be an array")
            directory_only = (
                action == FileDecision.MISSING.value and bool(released)
            )
            if action not in _MUTATING and not directory_only:
                continue
            name = f"apply:{index:06d}"
            existing = checkpoint.read(name)
            if existing is not None:
                state = _validate_checkpoint(
                    existing,
                    decision,
                    phases=frozenset({"ready", "done"}),
                )
                if state["phase"] == "done":
                    continue
                backup_digest = cast(str | None, state["backup_digest"])
            else:
                backup = _decode_backup(decision)
                backup_digest = None
                if backup is not None:
                    backup_digest = checkpoint.write_blob(backup)
                    if backup_digest != sha256_bytes(backup):
                        raise InvalidEffectError(
                            "checkpoint blob writer returned a non-content digest"
                        )
                checkpoint.write(
                    name,
                    _checkpoint_state(
                        decision, phase="ready", backup_digest=backup_digest
                    ),
                )

            path = cast(str, decision["path"])
            current = _read_optional(filesystem, path)
            current_hash = sha256_bytes(current) if current is not None else None
            before_hash = cast(str | None, decision["disk_hash"])
            after_hash = cast(str | None, decision["desired_hash"])
            if action == FileDecision.DELETE.value:
                if current_hash == before_hash:
                    filesystem.unlink(path)
                elif current_hash is not None:
                    raise ModifiedContentError(
                        f'file changed after prepare: "{path}"',
                        path=filesystem.base / path,
                    )
                _remove_owned_parents(filesystem, cast(Sequence[str], released))
            elif action in {
                FileDecision.WRITE.value,
                FileDecision.WRITE_MISSING.value,
                FileDecision.REPLACE.value,
            }:
                data = _decode_content(decision)
                if current_hash != after_hash:
                    if current_hash != before_hash:
                        raise ModifiedContentError(
                            f'file changed after prepare: "{path}"',
                            path=filesystem.base / path,
                        )
                    filesystem.atomic_write_bytes(path, data)
            elif action == FileDecision.MISSING.value:
                _remove_owned_parents(
                    filesystem, cast(Sequence[str], released)
                )
            checkpoint.write(
                name,
                _checkpoint_state(
                    decision, phase="done", backup_digest=backup_digest
                ),
            )

        payload = prepared.payload
        if not isinstance(payload, Mapping):
            raise InvalidEffectError("file-set prepared payload must be an object")
        return _json_value({"files": files, "decisions": payload["decisions"]})

    def revert(
        self,
        context: EffectContext,
        before_state: JsonValue,
        checkpoint: CheckpointWriter,
    ) -> None:
        filesystem = _filesystem(context.filesystem, "file-set revert")
        apply_states: list[tuple[str, Mapping[str, JsonValue]]] = []
        for name, state in checkpoint.snapshot().items():
            if name.startswith("apply:"):
                if not isinstance(state, Mapping):
                    raise InvalidEffectError("apply checkpoint must be an object")
                apply_states.append((name, cast(Mapping[str, JsonValue], state)))
        if context.operation is not Operation.UNINSTALL:
            self._rollback(filesystem, checkpoint, apply_states)
            return
        self._uninstall(filesystem, before_state, checkpoint)

    def _rollback(
        self,
        filesystem: _EffectFilesystem,
        checkpoint: CheckpointWriter,
        apply_states: Sequence[tuple[str, Mapping[str, JsonValue]]],
    ) -> None:
        for apply_name, apply_state in sorted(apply_states, reverse=True):
            phase = apply_state.get("phase")
            if phase not in {"ready", "done"}:
                raise InvalidEffectError("apply checkpoint phase cannot be rolled back")
            path = apply_state.get("path")
            action = apply_state.get("decision")
            before_hash = apply_state.get("before_hash")
            after_hash = apply_state.get("after_hash")
            backup_digest = apply_state.get("backup_digest")
            created_parents = apply_state.get("created_parents")
            released_parents = apply_state.get("released_parents")
            if not isinstance(path, str) or action not in _CHECKPOINTED:
                raise InvalidEffectError("apply checkpoint cannot be rolled back")
            for digest in (before_hash, after_hash, backup_digest):
                if digest is not None and (
                    not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
                ):
                    raise InvalidEffectError("apply checkpoint contains an invalid digest")
            try:
                created = _parse_owned_parents(
                    created_parents, "rollback created_parents"
                )
                released = _parse_owned_parents(
                    released_parents, "rollback released_parents"
                )
            except (TypeError, ValueError) as error:
                raise InvalidEffectError(str(error)) from error
            if any(not _is_parent(parent, path) for parent in (*created, *released)):
                raise InvalidEffectError("rollback checkpoint contains a non-parent path")
            name = f"rollback:{apply_name.removeprefix('apply:')}"
            existing = checkpoint.read(name)
            if existing is not None:
                if not isinstance(existing, Mapping):
                    raise InvalidEffectError("rollback checkpoint must be an object")
                if existing.get("phase") == "done":
                    continue
                if existing.get("phase") != "ready" or existing.get("path") != path:
                    raise InvalidEffectError("rollback checkpoint is inconsistent")
            else:
                checkpoint.write(
                    name,
                    {
                        "phase": "ready",
                        "path": path,
                        "decision": action,
                        "before_hash": before_hash,
                        "after_hash": after_hash,
                        "backup_digest": backup_digest,
                        "created_parents": list(created),
                        "released_parents": list(released),
                    },
                )

            current = _read_optional(filesystem, path)
            current_hash = sha256_bytes(current) if current is not None else None
            outcome = "already_restored"
            if action == FileDecision.MISSING.value:
                for parent in _sorted_parents(released):
                    filesystem.ensure_directory(parent)
                outcome = "restored_directories"
            elif before_hash is None:
                if current_hash == after_hash:
                    filesystem.unlink(path)
                    outcome = "removed_created"
                elif current_hash is not None:
                    outcome = "preserved_modified"
            elif current_hash != before_hash:
                if current_hash == after_hash or (
                    action == FileDecision.DELETE.value and current_hash is None
                ):
                    if not isinstance(backup_digest, str):
                        raise InvalidEffectError("rollback backup digest is missing")
                    backup = checkpoint.read_blob(backup_digest)
                    if sha256_bytes(backup) != before_hash:
                        raise InvalidEffectError("rollback blob does not match before hash")
                    filesystem.atomic_write_bytes(path, backup)
                    outcome = "restored"
                else:
                    outcome = "preserved_modified"
            _remove_owned_parents(filesystem, created)
            checkpoint.write(
                name,
                {
                    "phase": "done",
                    "path": path,
                    "decision": action,
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "backup_digest": backup_digest,
                    "created_parents": list(created),
                    "released_parents": list(released),
                    "outcome": outcome,
                },
            )

    def _uninstall(
        self,
        filesystem: _EffectFilesystem,
        before_state: JsonValue,
        checkpoint: CheckpointWriter,
    ) -> None:
        files, created_parents = _parse_state(before_state)
        for index, entry in enumerate(files):
            path = entry["path"]
            expected_hash = entry["installed_hash"]
            name = f"uninstall:{index:06d}"
            existing = checkpoint.read(name)
            if existing is not None:
                if not isinstance(existing, Mapping):
                    raise InvalidEffectError("uninstall checkpoint must be an object")
                if existing.get("path") != path or existing.get("expected_hash") != expected_hash:
                    raise InvalidEffectError("uninstall checkpoint is inconsistent")
                if existing.get("phase") == "done":
                    continue
                if existing.get("phase") != "ready":
                    raise InvalidEffectError("uninstall checkpoint phase is invalid")
            else:
                checkpoint.write(
                    name,
                    {
                        "phase": "ready",
                        "path": path,
                        "expected_hash": expected_hash,
                    },
                )
            current = _read_optional(filesystem, path)
            current_hash = sha256_bytes(current) if current is not None else None
            outcome = "missing"
            if current_hash == expected_hash:
                filesystem.unlink(path)
                outcome = "removed"
            elif current_hash is not None:
                outcome = "preserved_modified"
            checkpoint.write(
                name,
                {
                    "phase": "done",
                    "path": path,
                    "expected_hash": expected_hash,
                    "outcome": outcome,
                },
            )

        for index, parent in enumerate(
            _sorted_parents(created_parents, deepest_first=True)
        ):
            name = f"uninstall-dir:{index:06d}"
            existing = checkpoint.read(name)
            if existing is not None:
                if not isinstance(existing, Mapping):
                    raise InvalidEffectError(
                        "uninstall directory checkpoint must be an object"
                    )
                if existing.get("path") != parent:
                    raise InvalidEffectError(
                        "uninstall directory checkpoint is inconsistent"
                    )
                if existing.get("phase") == "done":
                    continue
                if existing.get("phase") != "ready":
                    raise InvalidEffectError(
                        "uninstall directory checkpoint phase is invalid"
                    )
            else:
                checkpoint.write(
                    name,
                    {"phase": "ready", "path": parent},
                )
            removed = filesystem.rmdir_empty(parent, missing_ok=True)
            checkpoint.write(
                name,
                {
                    "phase": "done",
                    "path": parent,
                    "outcome": "removed" if removed else "preserved_nonempty",
                },
            )


__all__ = [
    "FileDecision",
    "ReconcileFileSetEffect",
    "classify_file",
    "sha256_bytes",
]
