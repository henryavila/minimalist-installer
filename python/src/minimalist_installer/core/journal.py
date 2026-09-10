"""Strict write-ahead transaction journal and checkpoint persistence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from .errors import CorruptTransactionError, IncompleteTransactionError
from .models import (
    CheckpointWriter,
    EffectPlan,
    JsonObject,
    JsonValue,
    Operation,
    PreparedEffect,
    _freeze_json,
    _json_value,
)
from .path_safety import SafeFilesystem

TRANSACTION_SCHEMA_VERSION: Final = 1
TRANSACTION_ENGINE_NAME: Final = "minimalist-installer"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EFFECT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class TransactionPhase(StrEnum):
    PLANNED = "planned"
    APPLYING = "applying"
    REVERTING = "reverting"
    COMMITTING = "committing"
    COMMITTED = "committed"


class EffectProgress(StrEnum):
    PLANNED = "planned"
    PREPARED = "prepared"
    APPLIED = "applied"
    REVERTED = "reverted"


TRANSACTION_V1_SCHEMA: JsonObject = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/henryavila/minimalist-installer/spec/schemas/python-transaction-v1.schema.json",
    "title": "Minimalist Installer Python transaction journal v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "engine",
        "transaction_id",
        "installation_id",
        "operation",
        "phase",
        "planned_effect_ids",
        "resources",
        "effects",
        "operation_checkpoints",
        "blobs",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "engine": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "version"],
            "properties": {
                "name": {"const": "minimalist-installer"},
                "version": {"type": "string", "minLength": 1},
            },
        },
        "transaction_id": {"type": "string", "minLength": 1},
        "installation_id": {"type": "string", "minLength": 1},
        "operation": {"enum": ["install", "update", "repair", "uninstall"]},
        "phase": {"enum": [phase.value for phase in TransactionPhase]},
        "planned_effect_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "resources": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "effects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "type",
                    "effect_version",
                    "args",
                    "resources",
                    "status",
                    "prepared",
                    "result",
                    "checkpoints",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "minLength": 1},
                    "effect_version": {"type": "integer", "minimum": 1},
                    "args": {"type": "object"},
                    "resources": {"type": "array", "items": {"type": "string"}},
                    "status": {"enum": [progress.value for progress in EffectProgress]},
                    "prepared": {"type": ["object", "null"]},
                    "result": {},
                    "checkpoints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "state"],
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "state": {},
                            },
                        },
                    },
                },
            },
        },
        "operation_checkpoints": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "blobs": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
    },
}

_JOURNAL_KEYS = frozenset(
    {
        "schema_version",
        "engine",
        "transaction_id",
        "installation_id",
        "operation",
        "phase",
        "planned_effect_ids",
        "resources",
        "effects",
        "operation_checkpoints",
        "blobs",
    }
)
_EFFECT_KEYS = frozenset(
    {
        "id",
        "type",
        "effect_version",
        "args",
        "resources",
        "status",
        "prepared",
        "result",
        "checkpoints",
    }
)
_PREPARED_KEYS = frozenset({"before_state", "payload", "resources", "recoverable"})
_CHECKPOINT_KEYS = frozenset({"name", "state"})


def _identifier(value: object, label: str, *, effect: bool = False) -> str:
    pattern = _EFFECT_IDENTIFIER if effect else _IDENTIFIER
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe stable identifier")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object with string keys")
    return cast(Mapping[str, object], value)


def _array(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        raise TypeError(f"{label} must be an array")
    return cast(Sequence[object], value)


def _exact(value: Mapping[str, object], keys: frozenset[str], label: str) -> None:
    if frozenset(value) != keys:
        raise ValueError(f"{label} has invalid fields")


@dataclass(frozen=True, slots=True)
class JournalCheckpoint:
    name: str
    state: JsonValue

    def __post_init__(self) -> None:
        _text(self.name, "checkpoint.name")
        object.__setattr__(self, "state", _freeze_json(self.state))

    def to_dict(self) -> JsonObject:
        return {"name": self.name, "state": _json_value(self.state)}

    @classmethod
    def from_dict(cls, value: object) -> JournalCheckpoint:
        data = _object(value, "checkpoint")
        _exact(data, _CHECKPOINT_KEYS, "checkpoint")
        return cls(
            name=_text(data["name"], "checkpoint.name"),
            state=cast(JsonValue, data["state"]),
        )


@dataclass(frozen=True, slots=True)
class TransactionEffectRecord:
    id: str
    type: str
    effect_version: int
    args: Mapping[str, JsonValue]
    resources: tuple[str, ...]
    status: EffectProgress = EffectProgress.PLANNED
    prepared: PreparedEffect | None = None
    result: JsonValue = None
    checkpoints: tuple[JournalCheckpoint, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.id, "effect.id", effect=True)
        _text(self.type, "effect.type")
        _positive_integer(self.effect_version, "effect.effect_version")
        frozen_args = _freeze_json(self.args)
        if not isinstance(frozen_args, Mapping):
            raise TypeError("effect.args must be an object")
        object.__setattr__(self, "args", frozen_args)
        object.__setattr__(
            self,
            "resources",
            tuple(_text(item, "effect.resource") for item in self.resources),
        )
        object.__setattr__(self, "result", _freeze_json(self.result))
        object.__setattr__(self, "checkpoints", tuple(self.checkpoints))
        names = [checkpoint.name for checkpoint in self.checkpoints]
        if len(names) != len(set(names)):
            raise ValueError("effect checkpoint names must be unique")

    @classmethod
    def from_plan(cls, plan: EffectPlan) -> TransactionEffectRecord:
        return cls(
            id=plan.id,
            type=plan.type,
            effect_version=plan.version,
            args=plan.args,
            resources=plan.resources,
        )

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "type": self.type,
            "effect_version": self.effect_version,
            "args": _json_value(self.args),
            "resources": list(self.resources),
            "status": self.status.value,
            "prepared": self.prepared.to_dict() if self.prepared is not None else None,
            "result": _json_value(self.result),
            "checkpoints": [checkpoint.to_dict() for checkpoint in self.checkpoints],
        }

    @classmethod
    def from_dict(cls, value: object) -> TransactionEffectRecord:
        data = _object(value, "transaction effect")
        _exact(data, _EFFECT_KEYS, "transaction effect")
        args = _object(data["args"], "effect.args")
        prepared_value = data["prepared"]
        prepared: PreparedEffect | None = None
        if prepared_value is not None:
            prepared_data = _object(prepared_value, "effect.prepared")
            _exact(prepared_data, _PREPARED_KEYS, "effect.prepared")
            prepared = PreparedEffect(
                before_state=cast(JsonValue, prepared_data["before_state"]),
                payload=cast(JsonValue, prepared_data["payload"]),
                resources=tuple(
                    _text(item, "prepared.resource")
                    for item in _array(prepared_data["resources"], "prepared.resources")
                ),
                recoverable=cast(bool, prepared_data["recoverable"]),
            )
        try:
            status = EffectProgress(_text(data["status"], "effect.status"))
        except ValueError as error:
            raise ValueError("effect.status is unsupported") from error
        return cls(
            id=_identifier(data["id"], "effect.id", effect=True),
            type=_text(data["type"], "effect.type"),
            effect_version=_positive_integer(
                data["effect_version"], "effect.effect_version"
            ),
            args=cast(Mapping[str, JsonValue], args),
            resources=tuple(
                _text(item, "effect.resource")
                for item in _array(data["resources"], "effect.resources")
            ),
            status=status,
            prepared=prepared,
            result=cast(JsonValue, data["result"]),
            checkpoints=tuple(
                JournalCheckpoint.from_dict(item)
                for item in _array(data["checkpoints"], "effect.checkpoints")
            ),
        )


@dataclass(frozen=True, slots=True)
class TransactionJournal:
    transaction_id: str
    installation_id: str
    operation: Operation
    engine_version: str
    phase: TransactionPhase
    planned_effect_ids: tuple[str, ...]
    resources: tuple[str, ...]
    effects: tuple[TransactionEffectRecord, ...]
    operation_checkpoints: tuple[str, ...] = ()
    blobs: tuple[str, ...] = ()
    schema_version: int = TRANSACTION_SCHEMA_VERSION
    engine_name: str = TRANSACTION_ENGINE_NAME

    def __post_init__(self) -> None:
        if self.schema_version != TRANSACTION_SCHEMA_VERSION:
            raise ValueError(f"unsupported transaction schema: {self.schema_version}")
        if self.engine_name != TRANSACTION_ENGINE_NAME:
            raise ValueError(f"foreign transaction engine: {self.engine_name}")
        _identifier(self.transaction_id, "transaction_id")
        _identifier(self.installation_id, "installation_id")
        _text(self.engine_version, "engine.version")
        if self.operation is Operation.STATUS:
            raise ValueError("status is not a transaction operation")
        object.__setattr__(self, "planned_effect_ids", tuple(self.planned_effect_ids))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "operation_checkpoints", tuple(self.operation_checkpoints))
        object.__setattr__(self, "blobs", tuple(self.blobs))
        if self.planned_effect_ids != tuple(effect.id for effect in self.effects):
            raise ValueError("planned effect ids must match effect records")
        if len(self.planned_effect_ids) != len(set(self.planned_effect_ids)):
            raise ValueError("planned effect ids must be unique")
        if len(self.operation_checkpoints) != len(set(self.operation_checkpoints)):
            raise ValueError("operation checkpoints must be unique")
        if len(self.blobs) != len(set(self.blobs)) or any(
            _DIGEST.fullmatch(blob) is None for blob in self.blobs
        ):
            raise ValueError("blob ids must be unique SHA-256 digests")

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "engine": {"name": self.engine_name, "version": self.engine_version},
            "transaction_id": self.transaction_id,
            "installation_id": self.installation_id,
            "operation": self.operation.value,
            "phase": self.phase.value,
            "planned_effect_ids": list(self.planned_effect_ids),
            "resources": list(self.resources),
            "effects": [effect.to_dict() for effect in self.effects],
            "operation_checkpoints": list(self.operation_checkpoints),
            "blobs": list(self.blobs),
        }

    @classmethod
    def from_dict(cls, value: object) -> TransactionJournal:
        data = _object(value, "transaction")
        _exact(data, _JOURNAL_KEYS, "transaction")
        schema = data["schema_version"]
        if schema != TRANSACTION_SCHEMA_VERSION or isinstance(schema, bool):
            raise ValueError(f"unsupported transaction schema: {schema}")
        engine = _object(data["engine"], "engine")
        _exact(engine, frozenset({"name", "version"}), "engine")
        if engine["name"] != TRANSACTION_ENGINE_NAME:
            raise ValueError(f"foreign transaction engine: {engine['name']}")
        try:
            operation = Operation(_text(data["operation"], "operation"))
            phase = TransactionPhase(_text(data["phase"], "phase"))
        except ValueError as error:
            raise ValueError("transaction operation or phase is unsupported") from error
        return cls(
            schema_version=schema,
            engine_name=engine["name"],
            engine_version=_text(engine["version"], "engine.version"),
            transaction_id=_identifier(data["transaction_id"], "transaction_id"),
            installation_id=_identifier(data["installation_id"], "installation_id"),
            operation=operation,
            phase=phase,
            planned_effect_ids=tuple(
                _identifier(item, "planned_effect_id", effect=True)
                for item in _array(data["planned_effect_ids"], "planned_effect_ids")
            ),
            resources=tuple(
                _text(item, "resource")
                for item in _array(data["resources"], "resources")
            ),
            effects=tuple(
                TransactionEffectRecord.from_dict(item)
                for item in _array(data["effects"], "effects")
            ),
            operation_checkpoints=tuple(
                _text(item, "operation checkpoint")
                for item in _array(data["operation_checkpoints"], "operation_checkpoints")
            ),
            blobs=tuple(
                _text(item, "blob") for item in _array(data["blobs"], "blobs")
            ),
        )


class _DurableCheckpointWriter:
    def __init__(
        self,
        repository: TransactionRepository,
        transaction_id: str,
        effect_id: str,
    ) -> None:
        self._repository = repository
        self._transaction_id = transaction_id
        self._effect_id = effect_id

    def write(self, checkpoint: str, state: JsonValue) -> None:
        self._repository.effect_checkpoint(
            self._transaction_id, self._effect_id, checkpoint, state
        )

    def write_blob(self, data: bytes) -> str:
        return self._repository.write_blob(self._transaction_id, data)

    def read_blob(self, digest: str) -> bytes:
        return self._repository.read_blob(self._transaction_id, digest)


class TransactionRepository:
    """Persist a single discoverable active WAL below a manifest directory."""

    def __init__(
        self,
        filesystem: SafeFilesystem,
        *,
        manifest_directory: str = ".minimalist-installer",
    ) -> None:
        if not isinstance(filesystem, SafeFilesystem):
            raise TypeError("filesystem must be a SafeFilesystem")
        if not isinstance(
            manifest_directory, str
        ) or not manifest_directory.rstrip("/\\"):
            raise ValueError("manifest_directory must be non-empty text")
        self.filesystem = filesystem
        self.manifest_directory = manifest_directory.rstrip("/\\")
        self.transactions_directory = f"{self.manifest_directory}/transactions"
        self.active_path = f"{self.transactions_directory}/active.json"

    def _journal_path(self, transaction_id: str) -> str:
        safe_id = _identifier(transaction_id, "transaction_id")
        return f"{self.transactions_directory}/{safe_id}/journal.json"

    def _blob_path(self, transaction_id: str, digest: str) -> str:
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError("blob id must be a SHA-256 digest")
        safe_id = _identifier(transaction_id, "transaction_id")
        return f"{self.transactions_directory}/{safe_id}/blobs/{digest}.blob"

    def _write(
        self, transaction_id: str, journal: TransactionJournal
    ) -> None:
        requested_id = _identifier(transaction_id, "transaction_id")
        if journal.transaction_id != requested_id:
            raise CorruptTransactionError(
                "transaction journal id does not match the requested path",
                details={
                    "requested_transaction_id": requested_id,
                    "journal_transaction_id": journal.transaction_id,
                },
            )
        value = journal.to_dict()
        TransactionJournal.from_dict(value)
        self.filesystem.atomic_write_json(self._journal_path(requested_id), value)

    def read(self, transaction_id: str) -> TransactionJournal:
        path = self._journal_path(transaction_id)
        try:
            value = self.filesystem.read_json(path)
            journal = TransactionJournal.from_dict(value)
            if journal.transaction_id != transaction_id:
                raise CorruptTransactionError(
                    "transaction journal id does not match the requested path",
                    path=self.filesystem.base / path,
                    details={
                        "requested_transaction_id": transaction_id,
                        "journal_transaction_id": journal.transaction_id,
                    },
                )
            return journal
        except CorruptTransactionError:
            raise
        except (FileNotFoundError, UnicodeDecodeError, TypeError, ValueError, KeyError) as error:
            raise CorruptTransactionError(
                f"transaction journal is invalid: {error}",
                path=self.filesystem.base / path,
                details={"transaction_id": transaction_id, "reason": str(error)},
            ) from error

    def active(self) -> TransactionJournal | None:
        try:
            pointer = _object(self.filesystem.read_json(self.active_path), "active transaction")
        except FileNotFoundError:
            return None
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise CorruptTransactionError(
                "active transaction pointer is invalid",
                path=self.filesystem.base / self.active_path,
            ) from error
        try:
            _exact(pointer, frozenset({"schema_version", "transaction_id"}), "active transaction")
            if pointer["schema_version"] != TRANSACTION_SCHEMA_VERSION:
                raise ValueError("unsupported active transaction schema")
            transaction_id = _identifier(pointer["transaction_id"], "transaction_id")
        except (TypeError, ValueError, KeyError) as error:
            raise CorruptTransactionError(
                f"active transaction pointer is invalid: {error}",
                path=self.filesystem.base / self.active_path,
            ) from error
        return self.read(transaction_id)

    def begin(
        self,
        *,
        transaction_id: str,
        installation_id: str,
        operation: Operation,
        engine_version: str,
        plans: Sequence[EffectPlan],
        resources: Sequence[str],
    ) -> TransactionJournal:
        active = self.active()
        if active is not None:
            raise IncompleteTransactionError(
                f"transaction {active.transaction_id} is incomplete",
                operation=operation,
                details={"transaction_id": active.transaction_id},
            )
        journal = TransactionJournal(
            transaction_id=transaction_id,
            installation_id=installation_id,
            operation=operation,
            engine_version=engine_version,
            phase=TransactionPhase.PLANNED,
            planned_effect_ids=tuple(plan.id for plan in plans),
            resources=tuple(resources),
            effects=tuple(TransactionEffectRecord.from_plan(plan) for plan in plans),
        )
        # The full journal lands first. The active pointer becomes discoverable
        # before callers receive permission to prepare or mutate.
        self._write(transaction_id, journal)
        self.filesystem.atomic_write_json(
            self.active_path,
            {"schema_version": TRANSACTION_SCHEMA_VERSION, "transaction_id": transaction_id},
        )
        return journal

    def _replace_effect(
        self,
        transaction_id: str,
        effect_id: str,
        update: Callable[[TransactionEffectRecord], TransactionEffectRecord],
        *,
        phase: TransactionPhase | None = None,
    ) -> TransactionJournal:
        journal = self.read(transaction_id)
        matches = [index for index, effect in enumerate(journal.effects) if effect.id == effect_id]
        if len(matches) != 1:
            raise CorruptTransactionError(
                f'transaction does not contain exactly one effect "{effect_id}"',
                details={"transaction_id": transaction_id, "effect_id": effect_id},
            )
        effects = list(journal.effects)
        index = matches[0]
        effects[index] = update(effects[index])
        changed = replace(journal, effects=tuple(effects), phase=phase or journal.phase)
        self._write(transaction_id, changed)
        return changed

    def record_prepared(
        self, transaction_id: str, effect_id: str, prepared: PreparedEffect
    ) -> TransactionJournal:
        if not isinstance(prepared, PreparedEffect):
            raise TypeError("prepared must be a PreparedEffect")
        journal = self.read(transaction_id)
        phase = (
            TransactionPhase.REVERTING
            if journal.operation in {Operation.UNINSTALL, Operation.REPAIR}
            else TransactionPhase.APPLYING
        )
        return self._replace_effect(
            transaction_id,
            effect_id,
            lambda effect: replace(
                effect, status=EffectProgress.PREPARED, prepared=prepared
            ),
            phase=phase,
        )

    def record_applied(
        self, transaction_id: str, effect_id: str, result: JsonValue
    ) -> TransactionJournal:
        return self._replace_effect(
            transaction_id,
            effect_id,
            lambda effect: replace(
                effect, status=EffectProgress.APPLIED, result=_freeze_json(result)
            ),
        )

    def record_reverted(self, transaction_id: str, effect_id: str) -> TransactionJournal:
        return self._replace_effect(
            transaction_id,
            effect_id,
            lambda effect: replace(effect, status=EffectProgress.REVERTED),
        )

    def effect_checkpoint(
        self,
        transaction_id: str,
        effect_id: str,
        checkpoint: str,
        state: JsonValue,
    ) -> TransactionJournal:
        name = _text(checkpoint, "checkpoint")

        def update(effect: TransactionEffectRecord) -> TransactionEffectRecord:
            if effect.prepared is None:
                raise CorruptTransactionError(
                    "cannot checkpoint an effect before prepared state is durable",
                    details={"transaction_id": transaction_id, "effect_id": effect_id},
                )
            replacement = JournalCheckpoint(name, state)
            checkpoints = list(effect.checkpoints)
            for index, existing in enumerate(checkpoints):
                if existing.name == name:
                    checkpoints[index] = replacement
                    break
            else:
                checkpoints.append(replacement)
            return replace(effect, checkpoints=tuple(checkpoints))

        return self._replace_effect(transaction_id, effect_id, update)

    def checkpoint(self, transaction_id: str, checkpoint: str) -> TransactionJournal:
        name = _text(checkpoint, "operation checkpoint")
        journal = self.read(transaction_id)
        checkpoints = journal.operation_checkpoints
        if name not in checkpoints:
            checkpoints = (*checkpoints, name)
        phase = journal.phase
        if name == "committing":
            phase = TransactionPhase.COMMITTING
        elif name in {"manifest_committed", "manifest_removed"}:
            phase = TransactionPhase.COMMITTED
        changed = replace(journal, operation_checkpoints=checkpoints, phase=phase)
        self._write(transaction_id, changed)
        return changed

    def checkpoint_writer(self, transaction_id: str, effect_id: str) -> CheckpointWriter:
        return _DurableCheckpointWriter(self, transaction_id, effect_id)

    def write_blob(self, transaction_id: str, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError("blob data must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        self.filesystem.atomic_write_bytes(self._blob_path(transaction_id, digest), data)
        journal = self.read(transaction_id)
        if digest not in journal.blobs:
            self._write(
                transaction_id,
                replace(journal, blobs=(*journal.blobs, digest)),
            )
        return digest

    def read_blob(self, transaction_id: str, digest: str) -> bytes:
        path = self._blob_path(transaction_id, digest)
        try:
            data = self.filesystem.read_bytes(path)
        except FileNotFoundError as error:
            raise CorruptTransactionError(
                "transaction blob is missing",
                path=self.filesystem.base / path,
                details={"transaction_id": transaction_id, "digest": digest},
            ) from error
        if hashlib.sha256(data).hexdigest() != digest:
            raise CorruptTransactionError(
                "transaction blob digest does not match its content",
                path=self.filesystem.base / path,
                details={"transaction_id": transaction_id, "digest": digest},
            )
        return data

    def complete(self, transaction_id: str) -> None:
        journal = self.read(transaction_id)
        active = self.active()
        if active is None or active.transaction_id != transaction_id:
            raise CorruptTransactionError("completed transaction is not the active transaction")
        self.filesystem.unlink(self.active_path)
        for digest in journal.blobs:
            self.filesystem.unlink(self._blob_path(transaction_id, digest), missing_ok=True)
        blob_directory = f"{self.transactions_directory}/{transaction_id}/blobs"
        self.filesystem.rmdir_empty(blob_directory, missing_ok=True)
        self.filesystem.unlink(self._journal_path(transaction_id))
        self.filesystem.rmdir_empty(
            f"{self.transactions_directory}/{transaction_id}", missing_ok=True
        )
        self.filesystem.rmdir_empty(self.transactions_directory, missing_ok=True)
        self.filesystem.rmdir_empty(self.manifest_directory, missing_ok=True)


__all__ = [
    "EffectProgress",
    "JournalCheckpoint",
    "TRANSACTION_ENGINE_NAME",
    "TRANSACTION_SCHEMA_VERSION",
    "TRANSACTION_V1_SCHEMA",
    "TransactionEffectRecord",
    "TransactionJournal",
    "TransactionPhase",
    "TransactionRepository",
]
