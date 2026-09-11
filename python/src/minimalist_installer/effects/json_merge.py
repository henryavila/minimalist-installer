"""Additive, reversible JSON object merge with byte-aware rollback."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from math import isfinite
from typing import cast

from ..core.errors import InvalidEffectError, ModifiedContentError
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
from .file_set import (
    _EffectFilesystem,
    _exact_keys,
    _filesystem,
    _normalize_relative,
    _parent_paths,
    _read_optional,
    _sorted_parents,
    sha256_bytes,
)

_VERSION = 1
_DIGEST_LENGTH = 64


def _is_object(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_array(value: object) -> bool:
    return isinstance(value, list | tuple)


def _is_scalar(value: object) -> bool:
    return value is None or (
        isinstance(value, str | bool | int | float)
        and not (isinstance(value, float) and not isfinite(value))
    )


def _equal(left: object, right: object) -> bool:
    """Compare JSON values without Python's ``True == 1`` coercion."""

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if _is_object(left) and _is_object(right):
        left_map = cast(Mapping[str, object], left)
        right_map = cast(Mapping[str, object], right)
        return set(left_map) == set(right_map) and all(
            _equal(left_map[key], right_map[key]) for key in left_map
        )
    if _is_array(left) and _is_array(right):
        left_items = cast(Sequence[object], left)
        right_items = cast(Sequence[object], right)
        return len(left_items) == len(right_items) and all(
            _equal(a, b) for a, b in zip(left_items, right_items, strict=True)
        )
    return type(left) is type(right) and left == right


def _format_path(path: Sequence[str]) -> str:
    return ".".join(path) if path else "<root>"


def _plain_json(value: object, path: tuple[str, ...] = ()) -> JsonValue:
    if _is_scalar(value):
        return cast(JsonValue, value)
    if _is_array(value):
        return [_plain_json(item, path) for item in cast(Sequence[object], value)]
    if _is_object(value):
        converted: JsonObject = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object key at {_format_path(path)} must be text")
            converted[key] = _plain_json(item, (*path, key))
        return converted
    raise TypeError(f"unsupported JSON value at {_format_path(path)}")


def _strict_load(data: bytes, path: str) -> JsonObject:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {token}")

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON object key is not allowed: {key}")
            value[key] = item
        return value

    def finite_float(token: str) -> float:
        value = float(token)
        if not isfinite(value):
            raise ValueError(f"non-finite JSON number is not allowed: {token}")
        return value

    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeDecodeError, ValueError) as error:
        error.add_note(f'while reading JSON merge target "{path}"')
        raise
    if not isinstance(value, dict):
        raise ValueError(f'JSON merge target must be an object at "{path}"')
    return cast(JsonObject, value)


def _encoded(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2).encode("utf-8")
        + b"\n"
    )


def _dedupe(items: Sequence[JsonValue]) -> list[JsonValue]:
    result: list[JsonValue] = []
    for item in items:
        if not any(_equal(existing, item) for existing in result):
            result.append(_plain_json(item))
    return result


def _merge(
    target: JsonObject,
    delta: Mapping[str, JsonValue],
    *,
    path: tuple[str, ...],
    owned: list[JsonValue],
    containers: list[JsonValue],
) -> None:
    for key, delta_value in delta.items():
        key_path = (*path, key)
        if key not in target:
            if _is_object(delta_value):
                target[key] = {}
                containers.append(list(key_path))
                _merge(
                    cast(JsonObject, target[key]),
                    cast(Mapping[str, JsonValue], delta_value),
                    path=key_path,
                    owned=owned,
                    containers=containers,
                )
            elif _is_array(delta_value):
                target[key] = []
                containers.append(list(key_path))
                _append_array(
                    cast(list[JsonValue], target[key]),
                    cast(Sequence[JsonValue], delta_value),
                    path=key_path,
                    owned=owned,
                )
            elif _is_scalar(delta_value):
                target[key] = deepcopy(delta_value)
                owned.append(
                    {"kind": "key", "path": list(path), "key": key, "value": deepcopy(delta_value)}
                )
            else:
                raise TypeError(f"unsupported JSON delta value at {_format_path(key_path)}")
            continue

        current = target[key]
        if _is_object(delta_value):
            if not _is_object(current):
                raise ValueError(
                    f"cannot merge object into existing non-object at {_format_path(key_path)}"
                )
            _merge(
                cast(JsonObject, current),
                cast(Mapping[str, JsonValue], delta_value),
                path=key_path,
                owned=owned,
                containers=containers,
            )
        elif _is_array(delta_value):
            if not _is_array(current):
                raise ValueError(
                    f"cannot merge array into existing non-array at {_format_path(key_path)}"
                )
            _append_array(
                cast(list[JsonValue], current),
                cast(Sequence[JsonValue], delta_value),
                path=key_path,
                owned=owned,
            )
        elif not _is_scalar(delta_value):
            raise TypeError(f"unsupported JSON delta value at {_format_path(key_path)}")
        elif not _equal(current, delta_value):
            raise ValueError(f"cannot overwrite existing scalar at {_format_path(key_path)}")


def _append_array(
    target: list[JsonValue],
    delta: Sequence[JsonValue],
    *,
    path: tuple[str, ...],
    owned: list[JsonValue],
) -> None:
    for item in delta:
        plain = _plain_json(item, path)
        if any(_equal(existing, plain) for existing in target):
            continue
        target.append(deepcopy(plain))
        owned.append({"kind": "array_item", "path": list(path), "value": deepcopy(plain)})


def _navigate(root: object, path: Sequence[str]) -> object | None:
    current = root
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _delete_path(root: JsonObject, path: Sequence[str]) -> None:
    if not path:
        return
    parent = _navigate(root, path[:-1])
    if isinstance(parent, dict):
        parent.pop(path[-1], None)


def _subtract(target: JsonObject, owned: Sequence[Mapping[str, JsonValue]], containers: Sequence[Sequence[str]]) -> bool:
    changed = False
    for item in reversed(owned):
        kind = item["kind"]
        path = cast(Sequence[str], item["path"])
        if kind == "key":
            parent = _navigate(target, path)
            key = item["key"]
            if isinstance(parent, dict) and isinstance(key, str) and key in parent and _equal(parent[key], item["value"]):
                del parent[key]
                changed = True
        elif kind == "array_item":
            array = _navigate(target, path)
            if isinstance(array, list):
                for index, candidate in enumerate(array):
                    if _equal(candidate, item["value"]):
                        array.pop(index)
                        changed = True
                        break
    for path in sorted(containers, key=lambda item: (-len(item), tuple(item))):
        container = _navigate(target, path)
        if (isinstance(container, dict) and not container) or (isinstance(container, list) and not container):
            _delete_path(target, path)
            changed = True
    return changed


def _parse_owned(value: object) -> tuple[Mapping[str, JsonValue], ...]:
    if not isinstance(value, list | tuple):
        raise TypeError("JSON merge owned state must be an array")
    result: list[Mapping[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("JSON merge owned entry must be an object")
        kind = item.get("kind")
        expected = {"kind", "path", "value", "key"} if kind == "key" else {"kind", "path", "value"}
        if set(item) != expected or kind not in {"key", "array_item"}:
            raise ValueError("JSON merge owned entry is invalid")
        path = item["path"]
        if not isinstance(path, list | tuple) or any(not isinstance(part, str) for part in path):
            raise TypeError("JSON merge owned path must be an array of strings")
        if kind == "key" and not isinstance(item["key"], str):
            raise TypeError("JSON merge owned key must be text")
        _plain_json(item["value"])
        result.append(cast(Mapping[str, JsonValue], item))
    return tuple(result)


def _parse_containers(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list | tuple):
        raise TypeError("JSON merge containers must be an array")
    result: list[tuple[str, ...]] = []
    for item in value:
        if not isinstance(item, list | tuple) or not item or any(not isinstance(part, str) for part in item):
            raise ValueError("JSON merge container path must be a non-empty string array")
        result.append(tuple(item))
    return tuple(result)


def _parse_state(value: JsonValue | None) -> Mapping[str, JsonValue] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("previous JSON merge state must be an object")
    expected = {
        "version", "path", "file_created", "original", "installed_hash",
        "exact_restore_hash", "owned", "created_containers", "created_parents",
        "apply",
    }
    if set(value) != expected or value["version"] != _VERSION or isinstance(value["version"], bool):
        raise ValueError("previous JSON merge state is invalid")
    _normalize_relative(value["path"], "previous JSON merge path")
    if not isinstance(value["file_created"], bool):
        raise TypeError("previous file_created must be boolean")
    original = value["original"]
    if original is not None and not isinstance(original, str):
        raise TypeError("previous original bytes must be text or null")
    for key in ("installed_hash", "exact_restore_hash"):
        digest = value[key]
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != _DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"previous {key} is invalid")
    if not isinstance(value["installed_hash"], str):
        raise ValueError("previous installed_hash is required")
    if value["file_created"] and original is not None:
        raise ValueError("created JSON merge file cannot have original bytes")
    if not value["file_created"] and original is None:
        raise ValueError("pre-existing JSON merge file requires original bytes")
    _parse_owned(value["owned"])
    _parse_containers(value["created_containers"])
    parents = value["created_parents"]
    if not isinstance(parents, list | tuple):
        raise TypeError("previous created parents must be an array")
    for index, parent in enumerate(parents):
        normalized = _normalize_relative(parent, f"previous created_parents[{index}]")
        if not cast(str, value["path"]).startswith(f"{normalized}/"):
            raise ValueError("previous created parent is outside JSON path")
    if tuple(parents) != _sorted_parents(cast(Sequence[str], parents)):
        raise ValueError("previous created parents must be unique and sorted")
    authorization = value["apply"]
    if authorization is not None:
        if not isinstance(authorization, Mapping) or set(authorization) != {
            "action",
            "path",
            "before_hash",
            "after_hash",
            "blob",
            "created_parents",
        }:
            raise ValueError("previous JSON merge apply authorization is invalid")
        if (
            authorization["action"] != "write"
            or authorization["path"] != value["path"]
            or authorization["after_hash"] != value["installed_hash"]
            or authorization["blob"] != authorization["before_hash"]
        ):
            raise ValueError("previous JSON merge apply authorization is inconsistent")
        apply_parents = authorization["created_parents"]
        if not isinstance(apply_parents, list | tuple):
            raise TypeError("previous JSON merge apply parents must be an array")
        parsed_apply_parents = tuple(
            _normalize_relative(parent, f"previous apply parent[{index}]")
            for index, parent in enumerate(apply_parents)
        )
        if (
            parsed_apply_parents != _sorted_parents(parsed_apply_parents)
            or not set(parsed_apply_parents).issubset(cast(Sequence[str], parents))
        ):
            raise ValueError("previous JSON merge apply parents are invalid")
        for key in ("before_hash", "after_hash", "blob"):
            digest = authorization[key]
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != _DIGEST_LENGTH
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("previous JSON merge apply digest is invalid")
    return value


def _checkpoint(payload: Mapping[str, JsonValue], phase: str) -> JsonObject:
    source = payload["checkpoint"]
    if not isinstance(source, Mapping):
        raise InvalidEffectError("JSON merge checkpoint template is invalid")
    state = {key: _json_value(value) for key, value in source.items()}
    state["phase"] = phase
    return state


class JsonMergeEffect:
    """Merge absent/equal JSON structure and later subtract only owned values."""

    type = "json_merge"
    version = 1

    def prepare(self, args: JsonObject, previous: JsonValue | None, context: EffectContext) -> PreparedEffect:
        if not isinstance(args, Mapping):
            raise TypeError("JSON merge args must be an object")
        _exact_keys(args, frozenset({"path", "delta"}), "JSON merge args")
        path = _normalize_relative(args["path"], "args.path")
        delta_value = _plain_json(args["delta"])
        if not isinstance(delta_value, Mapping):
            raise TypeError("JSON merge delta must be an object")
        filesystem = _filesystem(context.filesystem, "JSON merge prepare")
        prior = _parse_state(previous)
        if prior is not None and prior["path"] != path:
            raise ValueError("previous JSON merge path does not match args.path")
        before = _read_optional(filesystem, path)
        file_created = before is None
        target: JsonObject = {} if before is None else _strict_load(before, path)
        new_owned: list[JsonValue] = []
        new_containers: list[JsonValue] = []
        _merge(target, cast(Mapping[str, JsonValue], delta_value), path=(), owned=new_owned, containers=new_containers)
        prior_owned = cast(Sequence[JsonValue], prior["owned"]) if prior is not None else ()
        prior_containers = cast(Sequence[JsonValue], prior["created_containers"]) if prior is not None else ()
        owned = _dedupe([*prior_owned, *new_owned])
        containers = _dedupe([*prior_containers, *new_containers])
        after = _encoded(target)
        write_required = before != after and (before is None or bool(new_owned or new_containers))
        if not write_required and before is not None:
            after = before
        before_hash = sha256_bytes(before) if before is not None else None
        after_hash = sha256_bytes(after)
        original = prior["original"] if prior is not None else (before.decode("latin1") if before is not None else None)
        sticky_created = bool(prior["file_created"]) if prior is not None else file_created
        prior_exact = cast(str | None, prior["exact_restore_hash"]) if prior is not None else None
        unedited = prior is None or before_hash == cast(str, prior["installed_hash"])
        exact_restore_hash = after_hash if unedited else prior_exact if before_hash == prior_exact else None
        created_parents = set(cast(Sequence[str], prior["created_parents"])) if prior is not None else set()
        apply_created_parents: set[str] = set()
        if write_required:
            apply_created_parents.update(
                parent
                for parent in _parent_paths(path)
                if not filesystem.directory_exists(parent)
            )
            created_parents.update(apply_created_parents)
        ordered_created_parents: list[JsonValue] = [
            cast(JsonValue, parent)
            for parent in _sorted_parents(tuple(created_parents))
        ]
        ordered_apply_parents: list[JsonValue] = [
            cast(JsonValue, parent)
            for parent in _sorted_parents(tuple(apply_created_parents))
        ]
        authorization: JsonValue = (
            {
                "action": "write",
                "path": path,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "blob": before_hash,
                "created_parents": ordered_apply_parents,
            }
            if write_required
            else None
        )
        state: JsonObject = {
            "version": _VERSION,
            "path": path,
            "file_created": sticky_created,
            "original": original,
            "installed_hash": after_hash,
            "exact_restore_hash": exact_restore_hash,
            "owned": owned,
            "created_containers": containers,
            "created_parents": ordered_created_parents,
            "apply": authorization,
        }
        checkpoint: JsonObject = {
            "action": "write",
            "path": path,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "blob": before_hash,
            "created_parents": ordered_apply_parents,
        }
        payload: JsonObject = {
            "version": _VERSION,
            "path": path,
            "write_required": write_required,
            "before_bytes": before.decode("latin1") if before is not None else None,
            "after_bytes": after.decode("latin1"),
            "checkpoint": checkpoint,
        }
        return PreparedEffect(
            before_state=state,
            payload=payload,
            resources=(canonical_resource_identity("path", filesystem.base / path),),
            filesystem=filesystem,
        )

    def apply(self, prepared: PreparedEffect, checkpoint: CheckpointWriter) -> JsonValue:
        filesystem, payload = self._prepared(prepared)
        if not payload["write_required"]:
            return prepared.before_state
        current_checkpoint = checkpoint.read("apply")
        blob: str | None = None
        if current_checkpoint is None:
            before_text = payload["before_bytes"]
            if before_text is not None:
                blob = checkpoint.write_blob(cast(str, before_text).encode("latin1"))
                if blob != cast(Mapping[str, JsonValue], payload["checkpoint"])["blob"]:
                    raise InvalidEffectError("JSON merge blob writer returned a non-content digest")
            checkpoint.write("apply", _checkpoint(payload, "ready"))
        else:
            blob = self._validate_checkpoint(current_checkpoint, payload, {"ready", "done"})
            if cast(Mapping[str, JsonValue], current_checkpoint)["phase"] == "done":
                return prepared.before_state
        path = cast(str, payload["path"])
        current = _read_optional(filesystem, path)
        current_hash = sha256_bytes(current) if current is not None else None
        before_hash = cast(str | None, cast(Mapping[str, JsonValue], payload["checkpoint"])["before_hash"])
        after_hash = cast(str, cast(Mapping[str, JsonValue], payload["checkpoint"])["after_hash"])
        if current_hash != after_hash:
            if current_hash != before_hash:
                raise ModifiedContentError(f'JSON changed after prepare: "{path}"', path=filesystem.base / path)
            filesystem.atomic_write_bytes(path, cast(str, payload["after_bytes"]).encode("latin1"))
        checkpoint.write("apply", _checkpoint(payload, "done"))
        return prepared.before_state

    def revert(self, context: EffectContext, before_state: JsonValue, checkpoint: CheckpointWriter) -> None:
        filesystem = _filesystem(context.filesystem, "JSON merge revert")
        state = _parse_state(before_state)
        if state is None:
            return
        if context.operation is not Operation.UNINSTALL:
            self._rollback(filesystem, state, checkpoint)
            return
        name = "uninstall"
        existing = checkpoint.read(name)
        leaf_done = False
        if existing is not None:
            if not isinstance(existing, Mapping) or existing.get("path") != state["path"]:
                raise InvalidEffectError("JSON merge uninstall checkpoint is invalid")
            phase = existing.get("phase")
            expected = {"phase", "path", "outcome"} if phase == "done" else {"phase", "path"}
            if phase not in {"ready", "done"} or set(existing) != expected:
                raise InvalidEffectError("JSON merge uninstall checkpoint is invalid")
            leaf_done = phase == "done"
        directory_states: list[
            tuple[str, str, Mapping[str, JsonValue] | None]
        ] = []
        parents = _sorted_parents(
            cast(Sequence[str], state["created_parents"]),
            deepest_first=True,
        )
        for index, parent in enumerate(parents):
            directory_name = f"uninstall-dir:{index:06d}"
            raw = checkpoint.read(directory_name)
            if raw is not None:
                if not isinstance(raw, Mapping):
                    raise InvalidEffectError(
                        "JSON merge uninstall directory checkpoint is invalid"
                    )
                phase = raw.get("phase")
                expected = (
                    {"phase", "path", "outcome"}
                    if phase == "done"
                    else {"phase", "path"}
                )
                if (
                    phase not in {"ready", "done"}
                    or set(raw) != expected
                    or raw.get("path") != parent
                ):
                    raise InvalidEffectError(
                        "JSON merge uninstall directory checkpoint is invalid"
                    )
            directory_states.append(
                (
                    directory_name,
                    parent,
                    cast(Mapping[str, JsonValue] | None, raw),
                )
            )
        if existing is None:
            checkpoint.write(name, {"phase": "ready", "path": state["path"]})
        if not leaf_done:
            outcome = self._uninstall_leaf(filesystem, state)
            checkpoint.write(
                name,
                {"phase": "done", "path": state["path"], "outcome": outcome},
            )
        for directory_name, parent, raw in directory_states:
            if raw is not None and raw["phase"] == "done":
                continue
            if raw is None:
                checkpoint.write(
                    directory_name,
                    {"phase": "ready", "path": parent},
                )
            removed = filesystem.rmdir_empty(parent, missing_ok=True)
            checkpoint.write(
                directory_name,
                {
                    "phase": "done",
                    "path": parent,
                    "outcome": "removed" if removed else "preserved_nonempty",
                },
            )

    @staticmethod
    def _prepared(prepared: PreparedEffect) -> tuple[_EffectFilesystem, Mapping[str, JsonValue]]:
        if not isinstance(prepared, PreparedEffect):
            raise TypeError("prepared must be a PreparedEffect")
        filesystem = _filesystem(prepared.filesystem, "JSON merge apply")
        payload = prepared.payload
        expected = {"version", "path", "write_required", "before_bytes", "after_bytes", "checkpoint"}
        if not isinstance(payload, Mapping) or set(payload) != expected or payload.get("version") != _VERSION:
            raise InvalidEffectError("JSON merge prepared payload is invalid")
        path = _normalize_relative(payload["path"], "prepared JSON merge path")
        if not isinstance(payload["write_required"], bool) or not isinstance(payload["after_bytes"], str):
            raise InvalidEffectError("JSON merge prepared bytes are invalid")
        if payload["before_bytes"] is not None and not isinstance(payload["before_bytes"], str):
            raise InvalidEffectError("JSON merge prepared before bytes are invalid")
        template = payload["checkpoint"]
        if not isinstance(template, Mapping) or set(template) != {
            "action", "path", "before_hash", "after_hash", "blob", "created_parents"
        }:
            raise InvalidEffectError("JSON merge checkpoint template is invalid")
        after = cast(str, payload["after_bytes"]).encode("latin1")
        before_value = payload["before_bytes"]
        before = cast(str, before_value).encode("latin1") if before_value is not None else None
        if (
            template["action"] != "write"
            or template["path"] != payload["path"]
            or template["after_hash"] != sha256_bytes(after)
            or template["before_hash"] != (sha256_bytes(before) if before is not None else None)
            or template["blob"] != template["before_hash"]
        ):
            raise InvalidEffectError("JSON merge prepared hashes are invalid")
        try:
            state = _parse_state(prepared.before_state)
        except (ValueError, TypeError, KeyError) as error:
            raise InvalidEffectError(
                "JSON merge prepared state is invalid"
            ) from error
        if (
            state is None
            or state["path"] != path
            or state["installed_hash"] != template["after_hash"]
            or (
                state["apply"] != template
                if payload["write_required"]
                else state["apply"] is not None
            )
        ):
            raise InvalidEffectError("JSON merge prepared state is inconsistent")
        return filesystem, cast(Mapping[str, JsonValue], payload)

    @staticmethod
    def _validate_checkpoint(value: object, payload: Mapping[str, JsonValue], phases: set[str]) -> str | None:
        if not isinstance(value, Mapping) or set(value) != {
            "phase", "action", "path", "before_hash", "after_hash", "blob", "created_parents"
        }:
            raise InvalidEffectError("JSON merge apply checkpoint is invalid")
        template = cast(Mapping[str, JsonValue], payload["checkpoint"])
        if value["phase"] not in phases or any(
            not _equal(value[key], template[key]) for key in template
        ):
            raise InvalidEffectError("JSON merge apply checkpoint is inconsistent")
        blob = value["blob"]
        if blob is not None and (not isinstance(blob, str) or len(blob) != _DIGEST_LENGTH):
            raise InvalidEffectError("JSON merge checkpoint blob is invalid")
        return cast(str | None, blob)

    @staticmethod
    def _rollback(
        filesystem: _EffectFilesystem,
        state: Mapping[str, JsonValue],
        checkpoint: CheckpointWriter,
    ) -> None:
        applied = checkpoint.read("apply")
        if applied is None:
            return
        authorization = state["apply"]
        if not isinstance(authorization, Mapping):
            raise InvalidEffectError("JSON merge checkpoint is not in authorized state")
        if not isinstance(applied, Mapping) or set(applied) != set(authorization) | {"phase"}:
            raise InvalidEffectError("JSON merge checkpoint is not in authorized state")
        if any(not _equal(applied[key], authorization[key]) for key in authorization):
            raise InvalidEffectError("JSON merge checkpoint is not in authorized state")
        path = _normalize_relative(applied["path"], "JSON merge rollback path")
        if applied["phase"] not in {"ready", "done"}:
            raise InvalidEffectError("JSON merge rollback source phase is invalid")
        if applied["action"] != "write":
            raise InvalidEffectError("JSON merge rollback action is invalid")
        for key in ("before_hash", "after_hash", "blob"):
            value = applied[key]
            if value is not None and (
                not isinstance(value, str)
                or len(value) != _DIGEST_LENGTH
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise InvalidEffectError("JSON merge rollback digest is invalid")
        if not isinstance(applied["after_hash"], str):
            raise InvalidEffectError("JSON merge rollback after hash is missing")
        existing = checkpoint.read("rollback")
        rollback_done = False
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise InvalidEffectError("JSON merge rollback checkpoint is invalid")
            phase = existing.get("phase")
            expected = (
                set(authorization) | {"phase", "outcome"}
                if phase == "done"
                else set(authorization) | {"phase"}
            )
            if (
                phase not in {"ready", "done"}
                or set(existing) != expected
                or any(
                    not _equal(existing.get(key), authorization[key])
                    for key in authorization
                )
            ):
                raise InvalidEffectError("JSON merge rollback checkpoint is invalid")
            rollback_done = phase == "done"
        parents = cast(Sequence[str], authorization["created_parents"])
        directory_states: list[tuple[str, str, Mapping[str, JsonValue] | None]] = []
        for index, parent in enumerate(_sorted_parents(parents, deepest_first=True)):
            name = f"rollback-dir:{index:06d}"
            raw = checkpoint.read(name)
            if raw is not None:
                if not isinstance(raw, Mapping):
                    raise InvalidEffectError("JSON merge rollback directory checkpoint is invalid")
                phase = raw.get("phase")
                expected = {"phase", "path", "outcome"} if phase == "done" else {"phase", "path"}
                if phase not in {"ready", "done"} or set(raw) != expected or raw.get("path") != parent:
                    raise InvalidEffectError("JSON merge rollback directory checkpoint is invalid")
            directory_states.append((name, parent, cast(Mapping[str, JsonValue] | None, raw)))

        if existing is None:
            ready = {key: _json_value(value) for key, value in applied.items()}
            ready["phase"] = "ready"
            checkpoint.write("rollback", ready)
        if not rollback_done:
            current = _read_optional(filesystem, path)
            current_hash = sha256_bytes(current) if current is not None else None
            before_hash = cast(str | None, applied["before_hash"])
            after_hash = cast(str, applied["after_hash"])
            outcome = "already_restored"
            if current_hash == after_hash and before_hash is None:
                filesystem.unlink(path)
                outcome = "removed_created"
            elif current_hash == after_hash and before_hash is not None:
                blob = applied["blob"]
                if not isinstance(blob, str):
                    raise InvalidEffectError("JSON merge rollback blob is missing")
                original = checkpoint.read_blob(blob)
                if sha256_bytes(original) != before_hash:
                    raise InvalidEffectError("JSON merge rollback blob is corrupt")
                filesystem.atomic_write_bytes(path, original)
                outcome = "restored"
            elif current_hash not in {before_hash, None}:
                outcome = "preserved_modified"
            done = {key: _json_value(value) for key, value in applied.items()}
            done.update({"phase": "done", "outcome": outcome})
            checkpoint.write("rollback", done)
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
    def _uninstall_leaf(
        filesystem: _EffectFilesystem,
        state: Mapping[str, JsonValue],
    ) -> str:
        path = cast(str, state["path"])
        current = _read_optional(filesystem, path)
        if current is None:
            return "missing"
        current_hash = sha256_bytes(current)
        exact = state["exact_restore_hash"]
        original = state["original"]
        if current_hash == exact:
            if bool(state["file_created"]):
                filesystem.unlink(path)
                return "removed_created"
            if isinstance(original, str):
                filesystem.atomic_write_bytes(path, original.encode("latin1"))
                return "restored_exact"
        target = _strict_load(current, path)
        changed = _subtract(
            target,
            _parse_owned(state["owned"]),
            _parse_containers(state["created_containers"]),
        )
        if not changed:
            return "preserved"
        if bool(state["file_created"]) and not target:
            filesystem.unlink(path)
            return "removed_created"
        filesystem.atomic_write_bytes(path, _encoded(target))
        return "subtracted"


__all__ = ["JsonMergeEffect"]
