"""Prepared removal and exact restoration of signed legacy files."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol, cast

from ..core.errors import InvalidEffectError, ModifiedContentError, UnsafePathError
from ..core.locks import canonical_resource_identity, canonicalize_resources
from ..core.models import (
    CheckpointWriter,
    EffectContext,
    JsonObject,
    JsonValue,
    Operation,
    PreparedEffect,
)
from ..core.path_safety import PathEntryKind
from .file_set import (
    _EffectFilesystem,
    _exact_keys,
    _filesystem,
    _normalize_relative,
    _read_optional,
    sha256_bytes,
)

_VERSION = 1
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_FRONTMATTER_NAME = re.compile(
    r"^name:\s*(?:([a-z][a-z0-9-]*)|\"([a-z][a-z0-9-]*)\"|'([a-z][a-z0-9-]*)')\s*$",
    re.MULTILINE,
)


class _LegacyFilesystem(_EffectFilesystem, Protocol):
    def list_directory(
        self, relative: str
    ) -> tuple[tuple[str, PathEntryKind], ...]: ...


def _legacy_filesystem(value: object, label: str) -> _LegacyFilesystem:
    filesystem = _filesystem(value, label)
    if not callable(getattr(filesystem, "list_directory", None)):
        raise InvalidEffectError(f"{label} requires safe directory listing")
    return cast(_LegacyFilesystem, filesystem)


def read_frontmatter_name(data: bytes) -> str | None:
    """Return a bounded, syntactically-known frontmatter name."""

    if not isinstance(data, bytes):
        raise TypeError("frontmatter content must be bytes")
    try:
        head = data[:4096].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if not head.startswith("---\n"):
        return None
    end = head.find("\n---\n", 4)
    if end < 0:
        return None
    matches = list(_FRONTMATTER_NAME.finditer(head[4:end]))
    if len(matches) != 1:
        return None
    return next(group for group in matches[0].groups() if group is not None)


def _parse_names(value: object) -> frozenset[str]:
    if not isinstance(value, list | tuple):
        raise TypeError("args.known_names must be an array")
    names: list[str] = []
    for index, name in enumerate(value):
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise ValueError(f"args.known_names[{index}] is not a canonical name")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("args.known_names must not contain duplicates")
    return frozenset(names)


def _parse_roots(value: object, namespace_name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise TypeError("args.legacy_namespace_dirs must be an array")
    roots = tuple(
        f"{_normalize_relative(item, f'args.legacy_namespace_dirs[{index}]')}/{namespace_name}"
        for index, item in enumerate(value)
    )
    if len(roots) != len(set(roots)):
        raise ValueError("legacy namespace roots must be unique")
    return tuple(sorted(roots, key=str.encode))


def _walk_files(filesystem: _LegacyFilesystem, root: str) -> tuple[str, ...]:
    if not filesystem.directory_exists(root):
        return ()
    pending = [root]
    files: list[str] = []
    while pending:
        directory = pending.pop()
        for name, kind in filesystem.list_directory(directory):
            path = f"{directory}/{name}"
            if kind is PathEntryKind.DIRECTORY:
                pending.append(path)
            elif kind is PathEntryKind.FILE:
                files.append(path)
            elif kind in {PathEntryKind.SYMLINK, PathEntryKind.REPARSE}:
                raise UnsafePathError(
                    "legacy namespace contains a link-like entry",
                    path=filesystem.base / path,
                )
    return tuple(sorted(files, key=str.encode))


def _entry(path: str, content: bytes, namespace_root: str) -> JsonObject:
    return {
        "path": path,
        "content": content.decode("latin1"),
        "sha256": sha256_bytes(content),
        "namespace_root": namespace_root,
    }


def _parse_entry(value: object, label: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    _exact_keys(
        value,
        frozenset({"path", "content", "sha256", "namespace_root"}),
        label,
    )
    path = _normalize_relative(value["path"], f"{label}.path")
    root = _normalize_relative(value["namespace_root"], f"{label}.namespace_root")
    content = value["content"]
    digest = value["sha256"]
    if not path.startswith(f"{root}/"):
        raise ValueError(f"{label} path is outside its namespace root")
    if not isinstance(content, str):
        raise TypeError(f"{label}.content must be byte text")
    try:
        data = content.encode("latin1")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label}.content is not exact byte text") from error
    if not isinstance(digest, str) or digest != sha256_bytes(data):
        raise ValueError(f"{label}.sha256 does not match content")
    return cast(Mapping[str, JsonValue], value)


def _parse_state(value: JsonValue | None) -> tuple[Mapping[str, JsonValue], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or set(value) != {"version", "pruned"}:
        raise TypeError("legacy prune state must be a versioned object")
    if value["version"] != _VERSION or isinstance(value["version"], bool):
        raise ValueError("legacy prune state version is unsupported")
    entries = value["pruned"]
    if not isinstance(entries, list | tuple):
        raise TypeError("legacy prune state pruned must be an array")
    parsed = tuple(_parse_entry(item, f"legacy pruned[{index}]") for index, item in enumerate(entries))
    paths = [cast(str, item["path"]) for item in parsed]
    if paths != sorted(paths, key=str.encode) or len(paths) != len(set(paths)):
        raise ValueError("legacy prune state paths must be unique and sorted")
    return parsed


def _prune_namespace_parents(filesystem: _LegacyFilesystem, path: str, root: str) -> None:
    parent = path.rsplit("/", 1)[0]
    while parent == root or parent.startswith(f"{root}/"):
        removed = filesystem.rmdir_empty(parent, missing_ok=True)
        if not removed:
            return
        if parent == root:
            return
        parent = parent.rsplit("/", 1)[0]


def _apply_checkpoint(entry: Mapping[str, JsonValue], phase: str, blob: str) -> JsonObject:
    return {
        "phase": phase,
        "path": entry["path"],
        "sha256": entry["sha256"],
        "namespace_root": entry["namespace_root"],
        "blob": blob,
    }


class LegacyPruneEffect:
    """Remove only configured frontmatter signatures and retain exact bytes."""

    type = "legacy_prune"
    version = 1

    def prepare(self, args: JsonObject, previous: JsonValue | None, context: EffectContext) -> PreparedEffect:
        if not isinstance(args, Mapping):
            raise TypeError("legacy prune args must be an object")
        _exact_keys(
            args,
            frozenset({"legacy_namespace_dirs", "namespace_name", "known_names"}),
            "legacy prune args",
        )
        namespace_name = args["namespace_name"]
        if not isinstance(namespace_name, str) or _NAME.fullmatch(namespace_name) is None:
            raise ValueError("args.namespace_name must be a canonical name")
        names = _parse_names(args["known_names"])
        roots = _parse_roots(args["legacy_namespace_dirs"], namespace_name)
        filesystem = _legacy_filesystem(context.filesystem, "legacy prune prepare")
        prior = {cast(str, item["path"]): item for item in _parse_state(previous)}
        seen: set[str] = set()
        candidates: dict[str, Mapping[str, JsonValue]] = {}
        for root in roots:
            for path in _walk_files(filesystem, root):
                seen.add(path)
                try:
                    content = filesystem.read_bytes(path)
                except (FileNotFoundError, PermissionError):
                    continue
                name = read_frontmatter_name(content)
                if name not in names:
                    continue
                previous_entry = prior.get(path)
                if previous_entry is not None and previous_entry["sha256"] != sha256_bytes(content):
                    # A file recreated or edited after a prior prune belongs to the
                    # user even when it retained an old frontmatter name.
                    continue
                candidates[path] = _entry(path, content, root)

        state_entries: dict[str, Mapping[str, JsonValue]] = dict(candidates)
        for path, prior_entry in prior.items():
            if path not in seen:
                state_entries[path] = prior_entry
        ordered_state: list[JsonValue] = [
            cast(JsonValue, state_entries[path])
            for path in sorted(state_entries, key=str.encode)
        ]
        payload_entries: list[JsonValue] = []
        for path in sorted(candidates, key=str.encode):
            item = candidates[path]
            payload_entries.append(
                {
                    **item,
                    "checkpoint": {
                        "path": item["path"],
                        "sha256": item["sha256"],
                        "namespace_root": item["namespace_root"],
                    },
                }
            )
        resources = canonicalize_resources(
            canonical_resource_identity("path", filesystem.base / root) for root in roots
        )
        return PreparedEffect(
            before_state={"version": _VERSION, "pruned": ordered_state},
            payload={"version": _VERSION, "entries": payload_entries},
            resources=resources,
            filesystem=filesystem,
        )

    def apply(self, prepared: PreparedEffect, checkpoint: CheckpointWriter) -> JsonValue:
        filesystem, entries = self._prepared(prepared)
        for index, entry in enumerate(entries):
            name = f"apply:{index:06d}"
            existing = checkpoint.read(name)
            blob: str
            if existing is None:
                content = cast(str, entry["content"]).encode("latin1")
                blob = checkpoint.write_blob(content)
                if blob != entry["sha256"]:
                    raise InvalidEffectError("legacy prune blob writer returned a non-content digest")
                checkpoint.write(name, _apply_checkpoint(entry, "ready", blob))
            else:
                state = self._validate_apply_checkpoint(existing, entry)
                blob = cast(str, state["blob"])
                if state["phase"] == "done":
                    continue
            path = cast(str, entry["path"])
            current = _read_optional(filesystem, path)
            if current is not None and sha256_bytes(current) != entry["sha256"]:
                raise ModifiedContentError(
                    f'legacy file changed after prepare: "{path}"',
                    path=filesystem.base / path,
                )
            if current is not None:
                filesystem.unlink(path)
            _prune_namespace_parents(
                filesystem,
                path,
                cast(str, entry["namespace_root"]),
            )
            checkpoint.write(name, _apply_checkpoint(entry, "done", blob))
        return prepared.before_state

    def revert(self, context: EffectContext, before_state: JsonValue, checkpoint: CheckpointWriter) -> None:
        filesystem = _legacy_filesystem(context.filesystem, "legacy prune revert")
        entries = _parse_state(before_state)
        if context.operation is not Operation.UNINSTALL:
            self._rollback(filesystem, checkpoint)
            return
        for index, entry in enumerate(entries):
            name = f"uninstall:{index:06d}"
            existing = checkpoint.read(name)
            if existing is not None:
                if not isinstance(existing, Mapping):
                    raise InvalidEffectError("legacy uninstall checkpoint is invalid")
                phase = existing.get("phase")
                expected = (
                    {"phase", "path", "sha256", "outcome"}
                    if phase == "done"
                    else {"phase", "path", "sha256"}
                )
                if (
                    phase not in {"ready", "done"}
                    or set(existing) != expected
                    or existing.get("path") != entry["path"]
                    or existing.get("sha256") != entry["sha256"]
                ):
                    raise InvalidEffectError("legacy uninstall checkpoint is invalid")
                if phase == "done":
                    continue
            if existing is None:
                checkpoint.write(name, {"phase": "ready", "path": entry["path"], "sha256": entry["sha256"]})
            path = cast(str, entry["path"])
            current = _read_optional(filesystem, path)
            outcome = "already_restored"
            if current is None:
                filesystem.atomic_write_bytes(path, cast(str, entry["content"]).encode("latin1"))
                outcome = "restored"
            elif sha256_bytes(current) != entry["sha256"]:
                outcome = "preserved_collision"
            checkpoint.write(name, {"phase": "done", "path": path, "sha256": entry["sha256"], "outcome": outcome})

    @staticmethod
    def _prepared(prepared: PreparedEffect) -> tuple[_LegacyFilesystem, tuple[Mapping[str, JsonValue], ...]]:
        if not isinstance(prepared, PreparedEffect):
            raise TypeError("prepared must be a PreparedEffect")
        filesystem = _legacy_filesystem(prepared.filesystem, "legacy prune apply")
        payload = prepared.payload
        if not isinstance(payload, Mapping) or set(payload) != {"version", "entries"} or payload.get("version") != _VERSION:
            raise InvalidEffectError("legacy prune prepared payload is invalid")
        raw_entries = payload["entries"]
        if not isinstance(raw_entries, list | tuple):
            raise InvalidEffectError("legacy prune prepared entries must be an array")
        entries: list[Mapping[str, JsonValue]] = []
        prior_path: bytes | None = None
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, Mapping) or set(raw) != {"path", "content", "sha256", "namespace_root", "checkpoint"}:
                raise InvalidEffectError("legacy prune prepared entry is invalid")
            entry = _parse_entry(
                {
                    key: raw[key]
                    for key in ("path", "content", "sha256", "namespace_root")
                },
                f"prepared entries[{index}]",
            )
            template = raw["checkpoint"]
            if not isinstance(template, Mapping) or set(template) != {"path", "sha256", "namespace_root"} or any(template[key] != entry[key] for key in template):
                raise InvalidEffectError("legacy prune checkpoint template is invalid")
            encoded = cast(str, entry["path"]).encode()
            if prior_path is not None and encoded <= prior_path:
                raise InvalidEffectError("legacy prune prepared paths are not sorted")
            prior_path = encoded
            entries.append(cast(Mapping[str, JsonValue], raw))
        state_entries = {
            cast(str, item["path"]): item
            for item in _parse_state(prepared.before_state)
        }
        for entry in entries:
            state_entry = state_entries.get(cast(str, entry["path"]))
            if state_entry is None or any(
                state_entry[key] != entry[key]
                for key in ("path", "content", "sha256", "namespace_root")
            ):
                raise InvalidEffectError("legacy prune prepared state is inconsistent")
        return filesystem, tuple(entries)

    @staticmethod
    def _validate_apply_checkpoint(value: object, entry: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        if not isinstance(value, Mapping) or set(value) != {"phase", "path", "sha256", "namespace_root", "blob"}:
            raise InvalidEffectError("legacy apply checkpoint is invalid")
        if value["phase"] not in {"ready", "done"} or any(
            value[key] != entry[key] for key in ("path", "sha256", "namespace_root")
        ):
            raise InvalidEffectError("legacy apply checkpoint is inconsistent")
        if value["blob"] != entry["sha256"]:
            raise InvalidEffectError("legacy apply checkpoint blob is invalid")
        return cast(Mapping[str, JsonValue], value)

    @staticmethod
    def _rollback(filesystem: _LegacyFilesystem, checkpoint: CheckpointWriter) -> None:
        apply_states = sorted(
            (
                (name, state)
                for name, state in checkpoint.snapshot().items()
                if name.startswith("apply:")
            ),
            reverse=True,
        )
        for apply_name, raw in apply_states:
            if not isinstance(raw, Mapping):
                raise InvalidEffectError("legacy rollback source is invalid")
            required = {"phase", "path", "sha256", "namespace_root", "blob"}
            if set(raw) != required or raw["phase"] not in {"ready", "done"} or raw["blob"] != raw["sha256"]:
                raise InvalidEffectError("legacy rollback source is invalid")
            suffix = apply_name.removeprefix("apply:")
            name = f"rollback:{suffix}"
            existing = checkpoint.read(name)
            if existing is not None:
                if not isinstance(existing, Mapping):
                    raise InvalidEffectError("legacy rollback checkpoint is invalid")
                phase = existing.get("phase")
                expected = required | ({"outcome"} if phase == "done" else set())
                if (
                    phase not in {"ready", "done"}
                    or set(existing) != expected
                    or any(existing.get(key) != raw[key] for key in required - {"phase"})
                ):
                    raise InvalidEffectError("legacy rollback checkpoint is invalid")
                if phase == "done":
                    continue
            if existing is None:
                checkpoint.write(name, {**raw, "phase": "ready"})
            path = cast(str, raw["path"])
            current = _read_optional(filesystem, path)
            outcome = "already_restored"
            if current is None:
                blob = checkpoint.read_blob(cast(str, raw["blob"]))
                if sha256_bytes(blob) != raw["sha256"]:
                    raise InvalidEffectError("legacy rollback blob is corrupt")
                filesystem.atomic_write_bytes(path, blob)
                outcome = "restored"
            elif sha256_bytes(current) != raw["sha256"]:
                outcome = "preserved_collision"
            checkpoint.write(name, {**raw, "phase": "done", "outcome": outcome})


__all__ = ["LegacyPruneEffect", "read_frontmatter_name"]
