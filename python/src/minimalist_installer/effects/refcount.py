"""Crash-safe per-owner marker claims for shared resources."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from ..core.errors import InvalidEffectError, ModifiedContentError
from ..core.locks import canonical_resource_identity
from ..core.manifest import CommittedManifest
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
    _parent_paths,
    _read_optional,
    _remove_owned_parents,
    _sorted_parents,
    sha256_bytes,
)

_VERSION = 1
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class _RefcountFilesystem(_EffectFilesystem, Protocol):
    def list_directory(
        self, relative: str
    ) -> tuple[tuple[str, PathEntryKind], ...]: ...


def owner_key(owner_id: str) -> str:
    """Return the portable marker filename for an owner identity."""

    if not isinstance(owner_id, str) or not owner_id or "\x00" in owner_id:
        raise ValueError("owner_id must be non-empty text without NUL")
    return hashlib.sha256(owner_id.encode("utf-8", errors="strict")).hexdigest()


def _refcount_filesystem(value: object, label: str) -> _RefcountFilesystem:
    filesystem = _filesystem(value, label)
    if not callable(getattr(filesystem, "list_directory", None)):
        raise InvalidEffectError(f"{label} requires safe directory listing")
    return cast(_RefcountFilesystem, filesystem)


def _marker_bytes(owner_id: str, manifest_path: str) -> bytes:
    return (
        json.dumps(
            {
                "version": _VERSION,
                "owner_id": owner_id,
                "manifest_path": manifest_path,
            },
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _strict_object(data: bytes, label: str) -> Mapping[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in items:
            if key in value:
                raise ValueError(f"{label} contains duplicate key {key}")
            value[key] = item
        return value

    value = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=pairs)
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _parse_marker(data: bytes) -> tuple[str, str]:
    value = _strict_object(data, "owner marker")
    if set(value) != {"version", "owner_id", "manifest_path"} or value["version"] != _VERSION:
        raise ValueError("owner marker schema is invalid")
    owner_id = value["owner_id"]
    manifest_path = value["manifest_path"]
    if not isinstance(owner_id, str):
        raise TypeError("owner marker owner_id must be text")
    owner_key(owner_id)
    normalized = _normalize_relative(manifest_path, "owner marker manifest_path")
    return owner_id, normalized


def _parse_state(value: JsonValue | None) -> Mapping[str, JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("previous refcount state must be an object")
    expected = {
        "version",
        "owners_dir",
        "owner_id",
        "owner_key",
        "owner_manifest_path",
        "marker_hash",
        "owns_marker",
        "created_parents",
        "apply",
    }
    if set(value) != expected or value["version"] != _VERSION or isinstance(value["version"], bool):
        raise ValueError("previous refcount state is invalid")
    owners_dir = _normalize_relative(value["owners_dir"], "previous owners_dir")
    owner_id = value["owner_id"]
    key = value["owner_key"]
    manifest_path = _normalize_relative(value["owner_manifest_path"], "previous owner_manifest_path")
    if not isinstance(owner_id, str) or key != owner_key(owner_id):
        raise ValueError("previous refcount owner identity is invalid")
    marker_hash = value["marker_hash"]
    if not isinstance(marker_hash, str) or _DIGEST.fullmatch(marker_hash) is None:
        raise ValueError("previous refcount marker hash is invalid")
    if marker_hash != sha256_bytes(_marker_bytes(owner_id, manifest_path)):
        raise ValueError("previous refcount marker hash does not match its claim")
    if not isinstance(value["owns_marker"], bool):
        raise TypeError("previous owns_marker must be boolean")
    parents = value["created_parents"]
    if not isinstance(parents, list | tuple):
        raise TypeError("previous created_parents must be an array")
    parsed_parents = tuple(
        _normalize_relative(parent, f"previous created_parents[{index}]")
        for index, parent in enumerate(parents)
    )
    if parsed_parents != _sorted_parents(parsed_parents):
        raise ValueError("previous created_parents must be unique and sorted")
    marker_path = f"{owners_dir}/{key}"
    if any(not marker_path.startswith(f"{parent}/") for parent in parsed_parents):
        raise ValueError("previous created parent is outside marker path")
    authorization = value["apply"]
    if authorization is not None:
        if not isinstance(authorization, Mapping) or set(authorization) != {
            "action",
            "marker_path",
            "before_hash",
            "after_hash",
            "blob",
            "created_parents",
        }:
            raise ValueError("previous refcount apply authorization is invalid")
        if (
            authorization["action"] != "register"
            or authorization["marker_path"] != marker_path
            or authorization["before_hash"] is not None
            or authorization["after_hash"] != marker_hash
            or authorization["blob"] is not None
        ):
            raise ValueError("previous refcount apply authorization is inconsistent")
        apply_parents = authorization["created_parents"]
        if not isinstance(apply_parents, list | tuple):
            raise TypeError("previous refcount apply parents must be an array")
        parsed_apply_parents = tuple(
            _normalize_relative(parent, f"previous apply parent[{index}]")
            for index, parent in enumerate(apply_parents)
        )
        if (
            parsed_apply_parents != _sorted_parents(parsed_apply_parents)
            or not set(parsed_apply_parents).issubset(parsed_parents)
        ):
            raise ValueError("previous refcount apply parents are invalid")
    return value


def _checkpoint(template: Mapping[str, JsonValue], phase: str) -> JsonObject:
    return {
        "phase": phase,
        "action": template["action"],
        "marker_path": template["marker_path"],
        "before_hash": template["before_hash"],
        "after_hash": template["after_hash"],
        "blob": template["blob"],
        "created_parents": template["created_parents"],
    }


def _validate_checkpoint(value: object, template: Mapping[str, JsonValue], phases: set[str]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping) or set(value) != set(template) | {"phase"}:
        raise InvalidEffectError("refcount checkpoint is invalid")
    if value["phase"] not in phases or any(value[key] != template[key] for key in template):
        raise InvalidEffectError("refcount checkpoint is inconsistent")
    return cast(Mapping[str, JsonValue], value)


def _manifest_proves_owner(
    filesystem: _RefcountFilesystem,
    *,
    owner_id: str,
    owner_manifest_path: str,
    key: str,
    owners_dir: str,
) -> bool:
    try:
        data = filesystem.read_bytes(owner_manifest_path)
        manifest = CommittedManifest.from_dict(_strict_object(data, "owner manifest"))
    except (FileNotFoundError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return False
    if manifest.installation_id != owner_id:
        return False
    for effect in manifest.effects:
        if effect.type != "refcount" or effect.effect_version != _VERSION:
            continue
        try:
            state = _parse_state(effect.before_state)
        except (ValueError, TypeError, KeyError):
            continue
        if state is None:
            continue
        if (
            state["version"] == _VERSION
            and state["owner_id"] == owner_id
            and state["owner_key"] == key
            and state["owners_dir"] == owners_dir
            and state["owner_manifest_path"] == owner_manifest_path
            and state["marker_hash"]
            == sha256_bytes(_marker_bytes(owner_id, owner_manifest_path))
            and state["owns_marker"] is True
        ):
            return True
    return False


class RefcountEffect:
    """Register claims idempotently and reclaim only after manifest proof."""

    type = "refcount"
    version = 1

    def prepare(self, args: JsonObject, previous: JsonValue | None, context: EffectContext) -> PreparedEffect:
        if not isinstance(args, Mapping):
            raise TypeError("refcount args must be an object")
        _exact_keys(
            args,
            frozenset({"owners_dir", "owner_id", "owner_manifest_path"}),
            "refcount args",
        )
        owners_dir = _normalize_relative(args["owners_dir"], "args.owners_dir")
        manifest_path = _normalize_relative(args["owner_manifest_path"], "args.owner_manifest_path")
        owner_id_value = args["owner_id"]
        if not isinstance(owner_id_value, str):
            raise TypeError("args.owner_id must be text")
        key = owner_key(owner_id_value)
        filesystem = _refcount_filesystem(context.filesystem, "refcount prepare")
        # Validate every existing path component without requiring the new owner's
        # not-yet-committed manifest to exist.
        _read_optional(filesystem, manifest_path)
        prior = _parse_state(previous)
        if prior is not None and (
            prior["owners_dir"] != owners_dir
            or prior["owner_id"] != owner_id_value
            or prior["owner_manifest_path"] != manifest_path
        ):
            raise ValueError("previous refcount claim does not match args")
        marker_path = f"{owners_dir}/{key}"
        desired = _marker_bytes(owner_id_value, manifest_path)
        existing = _read_optional(filesystem, marker_path)
        if existing is not None:
            try:
                parsed_owner, parsed_manifest = _parse_marker(existing)
            except (UnicodeDecodeError, ValueError, TypeError) as error:
                raise ValueError("existing owner marker is corrupt") from error
            if parsed_owner != owner_id_value or parsed_manifest != manifest_path or existing != desired:
                raise ValueError("existing owner marker conflicts with requested claim")
        before_hash = sha256_bytes(existing) if existing is not None else None
        marker_hash = sha256_bytes(desired)
        write_required = existing is None
        owns_marker = bool(prior["owns_marker"]) if prior is not None else write_required
        created = set(cast(Sequence[str], prior["created_parents"])) if prior is not None else set()
        apply_created: set[str] = set()
        if write_required:
            apply_created.update(
                parent
                for parent in _parent_paths(marker_path)
                if not filesystem.directory_exists(parent)
            )
            created.update(apply_created)
        ordered_created_parents: list[JsonValue] = [
            cast(JsonValue, parent)
            for parent in _sorted_parents(tuple(created))
        ]
        ordered_apply_parents: list[JsonValue] = [
            cast(JsonValue, parent)
            for parent in _sorted_parents(tuple(apply_created))
        ]
        authorization: JsonValue = (
            {
                "action": "register",
                "marker_path": marker_path,
                "before_hash": before_hash,
                "after_hash": marker_hash,
                "blob": None,
                "created_parents": ordered_apply_parents,
            }
            if write_required
            else None
        )
        state: JsonObject = {
            "version": _VERSION,
            "owners_dir": owners_dir,
            "owner_id": owner_id_value,
            "owner_key": key,
            "owner_manifest_path": manifest_path,
            "marker_hash": marker_hash,
            "owns_marker": owns_marker,
            "created_parents": ordered_created_parents,
            "apply": authorization,
        }
        template: JsonObject = {
            "action": "register",
            "marker_path": marker_path,
            "before_hash": before_hash,
            "after_hash": marker_hash,
            "blob": None,
            "created_parents": ordered_apply_parents,
        }
        return PreparedEffect(
            before_state=state,
            payload={
                "version": _VERSION,
                "marker_path": marker_path,
                "marker_bytes": desired.decode("latin1"),
                "write_required": write_required,
                "checkpoint": template,
            },
            resources=(canonical_resource_identity("path", filesystem.base / owners_dir),),
            filesystem=filesystem,
        )

    def apply(self, prepared: PreparedEffect, checkpoint: CheckpointWriter) -> JsonValue:
        filesystem, payload = self._prepared(prepared)
        if not payload["write_required"]:
            return prepared.before_state
        template = cast(Mapping[str, JsonValue], payload["checkpoint"])
        existing = checkpoint.read("apply")
        if existing is None:
            checkpoint.write("apply", _checkpoint(template, "ready"))
        else:
            state = _validate_checkpoint(existing, template, {"ready", "done"})
            if state["phase"] == "done":
                return prepared.before_state
        marker_path = cast(str, payload["marker_path"])
        current = _read_optional(filesystem, marker_path)
        current_hash = sha256_bytes(current) if current is not None else None
        if current_hash != template["after_hash"]:
            if current_hash != template["before_hash"]:
                raise ModifiedContentError(f'owner marker changed after prepare: "{marker_path}"', path=filesystem.base / marker_path)
            filesystem.atomic_write_bytes(marker_path, cast(str, payload["marker_bytes"]).encode("latin1"))
        checkpoint.write("apply", _checkpoint(template, "done"))
        return prepared.before_state

    def revert(self, context: EffectContext, before_state: JsonValue, checkpoint: CheckpointWriter) -> None:
        filesystem = _refcount_filesystem(context.filesystem, "refcount revert")
        state = _parse_state(before_state)
        if state is None:
            return
        if context.operation is not Operation.UNINSTALL:
            self._rollback(filesystem, state, checkpoint)
            return
        if not state["owns_marker"]:
            return
        self._release(filesystem, state, checkpoint)

    @staticmethod
    def _prepared(prepared: PreparedEffect) -> tuple[_RefcountFilesystem, Mapping[str, JsonValue]]:
        if not isinstance(prepared, PreparedEffect):
            raise TypeError("prepared must be a PreparedEffect")
        filesystem = _refcount_filesystem(prepared.filesystem, "refcount apply")
        payload = prepared.payload
        if not isinstance(payload, Mapping) or set(payload) != {"version", "marker_path", "marker_bytes", "write_required", "checkpoint"} or payload.get("version") != _VERSION:
            raise InvalidEffectError("refcount prepared payload is invalid")
        marker_path = _normalize_relative(payload["marker_path"], "prepared marker_path")
        if not isinstance(payload["marker_bytes"], str) or not isinstance(payload["write_required"], bool):
            raise InvalidEffectError("refcount prepared marker is invalid")
        template = payload["checkpoint"]
        if not isinstance(template, Mapping) or set(template) != {
            "action", "marker_path", "before_hash", "after_hash", "blob", "created_parents"
        }:
            raise InvalidEffectError("refcount prepared checkpoint is invalid")
        marker = cast(str, payload["marker_bytes"]).encode("latin1")
        if (
            template["action"] != "register"
            or template["marker_path"] != marker_path
            or template["after_hash"] != sha256_bytes(marker)
            or template["blob"] is not None
        ):
            raise InvalidEffectError("refcount prepared marker hash is invalid")
        before_hash = template["before_hash"]
        if before_hash is not None and (not isinstance(before_hash, str) or _DIGEST.fullmatch(before_hash) is None):
            raise InvalidEffectError("refcount prepared before hash is invalid")
        state = _parse_state(prepared.before_state)
        if (
            state is None
            or f"{state['owners_dir']}/{state['owner_key']}" != marker_path
            or state["marker_hash"] != template["after_hash"]
            or bool(payload["write_required"]) != (template["before_hash"] is None)
            or (
                state["apply"] != template
                if payload["write_required"]
                else state["apply"] is not None
            )
        ):
            raise InvalidEffectError("refcount prepared state is inconsistent")
        return filesystem, cast(Mapping[str, JsonValue], payload)

    @staticmethod
    def _rollback(
        filesystem: _RefcountFilesystem,
        state: Mapping[str, JsonValue],
        checkpoint: CheckpointWriter,
    ) -> None:
        applied = checkpoint.read("apply")
        if applied is None:
            return
        authorization = state["apply"]
        if not isinstance(authorization, Mapping):
            raise InvalidEffectError("refcount checkpoint is not in authorized state")
        if not isinstance(applied, Mapping) or set(applied) != set(authorization) | {"phase"}:
            raise InvalidEffectError("refcount checkpoint is not in authorized state")
        if any(applied[key] != authorization[key] for key in authorization):
            raise InvalidEffectError("refcount checkpoint is not in authorized state")
        template = cast(Mapping[str, JsonValue], authorization)
        _validate_checkpoint(applied, template, {"ready", "done"})
        existing = checkpoint.read("rollback")
        rollback_done = False
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise InvalidEffectError("refcount rollback checkpoint is invalid")
            phase = existing.get("phase")
            expected = (
                set(template) | {"phase", "outcome"}
                if phase == "done"
                else set(template) | {"phase"}
            )
            if (
                phase not in {"ready", "done"}
                or set(existing) != expected
                or any(existing.get(key) != template[key] for key in template)
            ):
                raise InvalidEffectError("refcount rollback checkpoint is invalid")
            rollback_done = phase == "done"
        parents = cast(Sequence[str], authorization["created_parents"])
        directory_states: list[tuple[str, str, Mapping[str, JsonValue] | None]] = []
        for index, parent in enumerate(_sorted_parents(parents, deepest_first=True)):
            name = f"rollback-dir:{index:06d}"
            raw = checkpoint.read(name)
            if raw is not None:
                if not isinstance(raw, Mapping):
                    raise InvalidEffectError("refcount rollback directory checkpoint is invalid")
                phase = raw.get("phase")
                expected = {"phase", "path", "outcome"} if phase == "done" else {"phase", "path"}
                if phase not in {"ready", "done"} or set(raw) != expected or raw.get("path") != parent:
                    raise InvalidEffectError("refcount rollback directory checkpoint is invalid")
            directory_states.append((name, parent, cast(Mapping[str, JsonValue] | None, raw)))
        if existing is None:
            checkpoint.write("rollback", _checkpoint(template, "ready"))
        marker_path = cast(str, applied["marker_path"])
        if not rollback_done:
            current = _read_optional(filesystem, marker_path)
            current_hash = sha256_bytes(current) if current is not None else None
            outcome = "already_restored"
            if applied["before_hash"] is None and current_hash == applied["after_hash"]:
                filesystem.unlink(marker_path)
                outcome = "removed_created"
            elif current_hash not in {applied["before_hash"], None}:
                outcome = "preserved_modified"
            checkpoint.write("rollback", {**_checkpoint(template, "done"), "outcome": outcome})
        for name, parent, raw in directory_states:
            if raw is not None and raw["phase"] == "done":
                continue
            if raw is None:
                checkpoint.write(name, {"phase": "ready", "path": parent})
            removed = filesystem.rmdir_empty(parent, missing_ok=True)
            checkpoint.write(
                name,
                {
                    "phase": "done",
                    "path": parent,
                    "outcome": "removed" if removed else "preserved_nonempty",
                },
            )

    @staticmethod
    def _release(filesystem: _RefcountFilesystem, state: Mapping[str, JsonValue], checkpoint: CheckpointWriter) -> None:
        owners_dir = cast(str, state["owners_dir"])
        marker_path = f"{owners_dir}/{state['owner_key']}"
        release = checkpoint.read("release")
        release_done = False
        if release is not None:
            if not isinstance(release, Mapping):
                raise InvalidEffectError("refcount release checkpoint is invalid")
            phase = release.get("phase")
            expected = (
                {"phase", "marker_path", "marker_hash", "outcome"}
                if phase == "done"
                else {"phase", "marker_path", "marker_hash"}
            )
            if (
                phase not in {"ready", "done"}
                or set(release) != expected
                or release.get("marker_path") != marker_path
                or release.get("marker_hash") != state["marker_hash"]
            ):
                raise InvalidEffectError("refcount release checkpoint is invalid")
            release_done = phase == "done"
        if not release_done:
            if release is None:
                checkpoint.write("release", {"phase": "ready", "marker_path": marker_path, "marker_hash": state["marker_hash"]})
            current = _read_optional(filesystem, marker_path)
            current_hash = sha256_bytes(current) if current is not None else None
            outcome = "missing"
            if current_hash == state["marker_hash"]:
                filesystem.unlink(marker_path)
                outcome = "removed"
            elif current is not None:
                outcome = "preserved_modified"
            checkpoint.write("release", {"phase": "done", "marker_path": marker_path, "marker_hash": state["marker_hash"], "outcome": outcome})

        if not filesystem.directory_exists(owners_dir):
            _remove_owned_parents(filesystem, cast(Sequence[str], state["created_parents"]))
            return
        for name, kind in filesystem.list_directory(owners_dir):
            path = f"{owners_dir}/{name}"
            if kind is not PathEntryKind.FILE or _DIGEST.fullmatch(name) is None:
                continue
            try:
                marker = filesystem.read_bytes(path)
                owner_id, manifest_path = _parse_marker(marker)
            except (FileNotFoundError, UnicodeDecodeError, ValueError, TypeError):
                continue
            if owner_key(owner_id) != name:
                continue
            if _manifest_proves_owner(
                filesystem,
                owner_id=owner_id,
                owner_manifest_path=manifest_path,
                key=name,
                owners_dir=owners_dir,
            ):
                continue
            checkpoint_name = f"orphan:{name}"
            existing = checkpoint.read(checkpoint_name)
            expected_hash = sha256_bytes(marker)
            if existing is not None:
                if not isinstance(existing, Mapping):
                    raise InvalidEffectError("refcount orphan checkpoint is invalid")
                phase = existing.get("phase")
                expected = (
                    {"phase", "path", "hash", "outcome"}
                    if phase == "done"
                    else {"phase", "path", "hash"}
                )
                if (
                    phase not in {"ready", "done"}
                    or set(existing) != expected
                    or existing.get("path") != path
                    or existing.get("hash") != expected_hash
                ):
                    raise InvalidEffectError("refcount orphan checkpoint is invalid")
                if phase == "done":
                    continue
            if existing is None:
                checkpoint.write(checkpoint_name, {"phase": "ready", "path": path, "hash": expected_hash})
            current = _read_optional(filesystem, path)
            outcome = "missing"
            if current is not None and sha256_bytes(current) == expected_hash:
                filesystem.unlink(path)
                outcome = "removed"
            elif current is not None:
                outcome = "preserved_modified"
            checkpoint.write(checkpoint_name, {"phase": "done", "path": path, "hash": expected_hash, "outcome": outcome})

        removed = filesystem.rmdir_empty(owners_dir, missing_ok=True)
        if removed:
            _remove_owned_parents(
                filesystem,
                tuple(parent for parent in cast(Sequence[str], state["created_parents"]) if parent != owners_dir),
            )


__all__ = ["RefcountEffect", "owner_key"]
