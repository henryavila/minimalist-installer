"""Strict write-ahead transaction journal and checkpoint persistence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, cast

from .errors import CorruptTransactionError, IncompleteTransactionError
from .locks import canonicalize_resources
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


class BlobStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"


class ActiveTransactionState(StrEnum):
    ACTIVE = "active"
    CLEANUP = "cleanup"


class CleanupProofKind(StrEnum):
    COMMITTED_MANIFEST = "committed_manifest"
    MANIFEST_REMOVED = "manifest_removed"


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
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["digest", "status"],
                "properties": {
                    "digest": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                    "status": {"enum": ["pending", "ready"]},
                },
            },
        },
    },
}

ACTIVE_TRANSACTION_V1_SCHEMA: JsonObject = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/henryavila/minimalist-installer/spec/schemas/python-active-transaction-v1.schema.json",
    "title": "Minimalist Installer Python active transaction authority v1",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "state", "transaction_id"],
            "properties": {
                "schema_version": {"const": 1},
                "state": {"const": "active"},
                "transaction_id": {"type": "string", "minLength": 1},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "schema_version",
                "state",
                "transaction_id",
                "operation",
                "proof",
                "blobs",
            ],
            "properties": {
                "schema_version": {"const": 1},
                "state": {"const": "cleanup"},
                "transaction_id": {"type": "string", "minLength": 1},
                "operation": {
                    "enum": ["install", "update", "repair", "uninstall"]
                },
                "proof": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "transaction_id"],
                    "properties": {
                        "kind": {
                            "enum": ["committed_manifest", "manifest_removed"]
                        },
                        "transaction_id": {"type": "string", "minLength": 1},
                    },
                },
                "blobs": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
        },
    ],
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
_BLOB_KEYS = frozenset({"digest", "status"})
_ACTIVE_KEYS = frozenset({"schema_version", "state", "transaction_id"})
_CLEANUP_KEYS = frozenset(
    {"schema_version", "state", "transaction_id", "operation", "proof", "blobs"}
)
_PROOF_KEYS = frozenset({"kind", "transaction_id"})


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
class TransactionBlobRecord:
    digest: str
    status: BlobStatus

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or _DIGEST.fullmatch(self.digest) is None:
            raise ValueError("blob digest must be a SHA-256 digest")
        if not isinstance(self.status, BlobStatus):
            raise TypeError("blob status must be a BlobStatus")

    def to_dict(self) -> JsonObject:
        return {"digest": self.digest, "status": self.status.value}

    @classmethod
    def from_dict(cls, value: object) -> TransactionBlobRecord:
        data = _object(value, "blob")
        _exact(data, _BLOB_KEYS, "blob")
        try:
            status = BlobStatus(_text(data["status"], "blob.status"))
        except ValueError as error:
            raise ValueError("blob.status is unsupported") from error
        digest = _text(data["digest"], "blob.digest")
        return cls(digest=digest, status=status)


@dataclass(frozen=True, slots=True)
class CleanupTombstone:
    transaction_id: str
    operation: Operation
    proof_kind: CleanupProofKind
    proof_transaction_id: str
    blobs: tuple[str, ...]
    schema_version: int = TRANSACTION_SCHEMA_VERSION
    state: ActiveTransactionState = ActiveTransactionState.CLEANUP

    def __post_init__(self) -> None:
        if self.schema_version != TRANSACTION_SCHEMA_VERSION:
            raise ValueError("unsupported cleanup tombstone schema")
        if self.state is not ActiveTransactionState.CLEANUP:
            raise ValueError("cleanup tombstone state must be cleanup")
        _identifier(self.transaction_id, "transaction_id")
        _identifier(self.proof_transaction_id, "proof.transaction_id")
        if self.proof_transaction_id != self.transaction_id:
            raise ValueError("cleanup proof transaction id must match tombstone")
        if not isinstance(self.operation, Operation) or self.operation is Operation.STATUS:
            raise ValueError("cleanup operation is invalid")
        if not isinstance(self.proof_kind, CleanupProofKind):
            raise TypeError("cleanup proof kind is invalid")
        forward = self.operation in {Operation.INSTALL, Operation.UPDATE}
        expected = (
            CleanupProofKind.COMMITTED_MANIFEST
            if forward
            else CleanupProofKind.MANIFEST_REMOVED
        )
        if self.proof_kind is not expected:
            raise ValueError("cleanup proof kind does not match operation")
        object.__setattr__(self, "blobs", tuple(self.blobs))
        if len(self.blobs) != len(set(self.blobs)) or any(
            not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
            for digest in self.blobs
        ):
            raise ValueError("cleanup blob ids must be unique SHA-256 digests")

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "transaction_id": self.transaction_id,
            "operation": self.operation.value,
            "proof": {
                "kind": self.proof_kind.value,
                "transaction_id": self.proof_transaction_id,
            },
            "blobs": list(self.blobs),
        }

    @classmethod
    def from_dict(cls, value: object) -> CleanupTombstone:
        data = _object(value, "cleanup tombstone")
        _exact(data, _CLEANUP_KEYS, "cleanup tombstone")
        if data["schema_version"] != TRANSACTION_SCHEMA_VERSION:
            raise ValueError("unsupported cleanup tombstone schema")
        if data["state"] != ActiveTransactionState.CLEANUP.value:
            raise ValueError("cleanup tombstone state is invalid")
        proof = _object(data["proof"], "cleanup proof")
        _exact(proof, _PROOF_KEYS, "cleanup proof")
        try:
            operation = Operation(_text(data["operation"], "cleanup operation"))
            proof_kind = CleanupProofKind(
                _text(proof["kind"], "cleanup proof kind")
            )
        except ValueError as error:
            raise ValueError("cleanup tombstone enum is unsupported") from error
        return cls(
            transaction_id=_identifier(data["transaction_id"], "transaction_id"),
            operation=operation,
            proof_kind=proof_kind,
            proof_transaction_id=_identifier(
                proof["transaction_id"], "proof.transaction_id"
            ),
            blobs=tuple(
                _text(item, "cleanup blob")
                for item in _array(data["blobs"], "cleanup blobs")
            ),
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
        canonical_resources = canonicalize_resources(self.resources)
        if canonical_resources != self.resources:
            raise ValueError("effect resources must be canonical, unique, and sorted")
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
            resources=canonicalize_resources(plan.resources),
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
    blobs: tuple[TransactionBlobRecord, ...] = ()
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
        if not isinstance(self.operation, Operation) or self.operation is Operation.STATUS:
            raise ValueError("status is not a transaction operation")
        if not isinstance(self.phase, TransactionPhase):
            raise TypeError("phase must be a TransactionPhase")
        object.__setattr__(self, "planned_effect_ids", tuple(self.planned_effect_ids))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "operation_checkpoints", tuple(self.operation_checkpoints))
        object.__setattr__(self, "blobs", tuple(self.blobs))
        canonical_resources = canonicalize_resources(self.resources)
        if canonical_resources != self.resources:
            raise ValueError(
                "transaction resources must be canonical, unique, and sorted"
            )
        if self.planned_effect_ids != tuple(effect.id for effect in self.effects):
            raise ValueError("planned effect ids must match effect records")
        if len(self.planned_effect_ids) != len(set(self.planned_effect_ids)):
            raise ValueError("planned effect ids must be unique")
        if len(self.operation_checkpoints) != len(set(self.operation_checkpoints)):
            raise ValueError("operation checkpoints must be unique")
        blob_digests = [blob.digest for blob in self.blobs]
        if len(blob_digests) != len(set(blob_digests)):
            raise ValueError("blob digests must be unique")
        self._validate_semantics()

    def _validate_semantics(self) -> None:
        transaction_resources = set(self.resources)
        for effect in self.effects:
            if not set(effect.resources).issubset(transaction_resources):
                raise ValueError(
                    "transaction resource union does not cover effect resources"
                )
            prepared = effect.prepared
            if prepared is not None:
                canonical_prepared = canonicalize_resources(prepared.resources)
                if canonical_prepared != prepared.resources:
                    raise ValueError(
                        "prepared resources must be canonical, unique, and sorted"
                    )
                if not set(canonical_prepared).issubset(transaction_resources):
                    raise ValueError(
                        "transaction resource union does not cover prepared resources"
                    )
            if effect.status is EffectProgress.PLANNED:
                if prepared is not None or effect.checkpoints or effect.result is not None:
                    raise ValueError("planned effect contains durable progress")
            elif effect.status is EffectProgress.PREPARED:
                if prepared is None or effect.result is not None:
                    raise ValueError("prepared effect has inconsistent state")
            elif prepared is None:
                raise ValueError("terminal effect status requires prepared state")

        forward = self.operation in {Operation.INSTALL, Operation.UPDATE}
        allowed = (
            {EffectProgress.PLANNED, EffectProgress.PREPARED, EffectProgress.APPLIED}
            if forward
            else {EffectProgress.PLANNED, EffectProgress.PREPARED, EffectProgress.REVERTED}
        )
        statuses = tuple(effect.status for effect in self.effects)
        if any(status not in allowed for status in statuses):
            raise ValueError("effect status has the wrong transaction direction")
        if statuses.count(EffectProgress.PREPARED) > 1:
            raise ValueError("at most one effect may be in prepared progress")
        ranks = (
            {
                EffectProgress.APPLIED: 0,
                EffectProgress.PREPARED: 1,
                EffectProgress.PLANNED: 2,
            }
            if forward
            else {
                EffectProgress.PLANNED: 0,
                EffectProgress.PREPARED: 1,
                EffectProgress.REVERTED: 2,
            }
        )
        status_ranks = tuple(ranks[status] for status in statuses)
        if status_ranks != tuple(sorted(status_ranks)):
            raise ValueError("effect progress order is impossible")

        terminal = EffectProgress.APPLIED if forward else EffectProgress.REVERTED
        all_terminal = all(status is terminal for status in statuses)
        expected = (
            ("effects_applied", "committing", "manifest_committed")
            if forward
            else ("effects_reverted", "committing", "manifest_removed")
        )
        checkpoints = self.operation_checkpoints
        if checkpoints != expected[: len(checkpoints)]:
            raise ValueError("operation checkpoint order is impossible")
        if checkpoints and not all_terminal:
            raise ValueError("operation checkpoint precedes terminal effects")

        if self.phase is TransactionPhase.PLANNED:
            if statuses and any(
                status is not EffectProgress.PLANNED for status in statuses
            ):
                raise ValueError("planned phase contains effect progress")
            if checkpoints:
                raise ValueError("planned phase contains operation checkpoints")
        elif self.phase is TransactionPhase.APPLYING:
            if not forward or len(checkpoints) > 1:
                raise ValueError("applying phase is inconsistent")
            if not checkpoints and all(
                status is EffectProgress.PLANNED for status in statuses
            ):
                raise ValueError("applying phase has no effect progress")
        elif self.phase is TransactionPhase.REVERTING:
            if forward or len(checkpoints) > 1:
                raise ValueError("reverting phase is inconsistent")
            if not checkpoints and all(
                status is EffectProgress.PLANNED for status in statuses
            ):
                raise ValueError("reverting phase has no effect progress")
        elif self.phase is TransactionPhase.COMMITTING:
            if len(checkpoints) < 2 or not all_terminal:
                raise ValueError("committing phase lacks terminal proof")
        elif self.phase is TransactionPhase.COMMITTED:
            if len(checkpoints) != len(expected) or not all_terminal:
                raise ValueError("committed phase lacks completion proof")

        if any(blob.status is BlobStatus.PENDING for blob in self.blobs) and not any(
            status is EffectProgress.PREPARED for status in statuses
        ):
            raise ValueError("pending blob exists without a prepared effect")
        if self.blobs and not any(
            status is not EffectProgress.PLANNED for status in statuses
        ):
            raise ValueError("blob descriptor exists before an effect is prepared")

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
            "blobs": [blob.to_dict() for blob in self.blobs],
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
                TransactionBlobRecord.from_dict(item)
                for item in _array(data["blobs"], "blobs")
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
        self._snapshot = self._load_snapshot()

    def _load_snapshot(self) -> Mapping[str, JsonValue]:
        journal = self._repository.read(self._transaction_id)
        matches = tuple(
            effect for effect in journal.effects if effect.id == self._effect_id
        )
        if len(matches) != 1:
            raise CorruptTransactionError(
                "checkpoint writer effect is missing or duplicated",
                details={
                    "transaction_id": self._transaction_id,
                    "effect_id": self._effect_id,
                },
            )
        return MappingProxyType(
            {checkpoint.name: checkpoint.state for checkpoint in matches[0].checkpoints}
        )

    def write(self, checkpoint: str, state: JsonValue) -> None:
        self._repository.effect_checkpoint(
            self._transaction_id, self._effect_id, checkpoint, state
        )
        self._snapshot = self._load_snapshot()

    def snapshot(self) -> Mapping[str, JsonValue]:
        return self._snapshot

    def read(self, checkpoint: str) -> JsonValue | None:
        return self._snapshot.get(checkpoint)

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

    def active(self) -> TransactionJournal | CleanupTombstone | None:
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
            state = ActiveTransactionState(
                _text(pointer.get("state"), "active transaction state")
            )
            if state is ActiveTransactionState.CLEANUP:
                return CleanupTombstone.from_dict(pointer)
            _exact(pointer, _ACTIVE_KEYS, "active transaction")
            if pointer["schema_version"] != TRANSACTION_SCHEMA_VERSION:
                raise ValueError("unsupported active transaction schema")
            transaction_id = _identifier(
                pointer["transaction_id"], "transaction_id"
            )
        except (TypeError, ValueError, KeyError) as error:
            raise CorruptTransactionError(
                f"active transaction pointer is invalid: {error}",
                path=self.filesystem.base / self.active_path,
            ) from error
        journal = self.read(transaction_id)
        if journal.phase is TransactionPhase.COMMITTED:
            raise CorruptTransactionError(
                "active transaction pointer references a committed journal",
                path=self.filesystem.base / self.active_path,
                details={"transaction_id": transaction_id},
            )
        return journal

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
            {
                "schema_version": TRANSACTION_SCHEMA_VERSION,
                "state": ActiveTransactionState.ACTIVE.value,
                "transaction_id": transaction_id,
            },
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

        def transition(effect: TransactionEffectRecord) -> TransactionEffectRecord:
            if effect.status is not EffectProgress.PLANNED:
                raise CorruptTransactionError(
                    "effect progress transition to prepared is invalid",
                    details={
                        "transaction_id": transaction_id,
                        "effect_id": effect_id,
                        "status": effect.status.value,
                    },
                )
            return replace(
                effect, status=EffectProgress.PREPARED, prepared=prepared
            )

        return self._replace_effect(
            transaction_id,
            effect_id,
            transition,
            phase=phase,
        )

    def record_applied(
        self, transaction_id: str, effect_id: str, result: JsonValue
    ) -> TransactionJournal:
        def transition(effect: TransactionEffectRecord) -> TransactionEffectRecord:
            if effect.status is not EffectProgress.PREPARED:
                raise CorruptTransactionError(
                    "effect progress transition to applied is invalid",
                    details={
                        "transaction_id": transaction_id,
                        "effect_id": effect_id,
                        "status": effect.status.value,
                    },
                )
            return replace(
                effect, status=EffectProgress.APPLIED, result=_freeze_json(result)
            )

        return self._replace_effect(
            transaction_id,
            effect_id,
            transition,
        )

    def record_reverted(self, transaction_id: str, effect_id: str) -> TransactionJournal:
        def transition(effect: TransactionEffectRecord) -> TransactionEffectRecord:
            if effect.status is not EffectProgress.PREPARED:
                raise CorruptTransactionError(
                    "effect progress transition to reverted is invalid",
                    details={
                        "transaction_id": transaction_id,
                        "effect_id": effect_id,
                        "status": effect.status.value,
                    },
                )
            return replace(effect, status=EffectProgress.REVERTED)

        return self._replace_effect(
            transaction_id,
            effect_id,
            transition,
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
            if effect.status is not EffectProgress.PREPARED:
                raise CorruptTransactionError(
                    "effect checkpoint requires prepared progress",
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
        if name == "effects_applied":
            phase = TransactionPhase.APPLYING
        elif name == "effects_reverted":
            phase = TransactionPhase.REVERTING
        elif name == "committing":
            phase = TransactionPhase.COMMITTING
        changed = replace(journal, operation_checkpoints=checkpoints, phase=phase)
        self._write(transaction_id, changed)
        return changed

    def checkpoint_writer(self, transaction_id: str, effect_id: str) -> CheckpointWriter:
        return _DurableCheckpointWriter(self, transaction_id, effect_id)

    def write_blob(self, transaction_id: str, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError("blob data must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        journal = self.read(transaction_id)
        existing = next(
            (blob for blob in journal.blobs if blob.digest == digest), None
        )
        if existing is not None and existing.status is BlobStatus.READY:
            self.read_blob(transaction_id, digest)
            return digest
        pending = TransactionBlobRecord(digest=digest, status=BlobStatus.PENDING)
        if existing is None:
            pending_journal = replace(journal, blobs=(*journal.blobs, pending))
        else:
            pending_journal = replace(
                journal,
                blobs=tuple(
                    pending if blob.digest == digest else blob
                    for blob in journal.blobs
                ),
            )
        self._write(transaction_id, pending_journal)
        self.filesystem.atomic_write_bytes(self._blob_path(transaction_id, digest), data)
        ready = TransactionBlobRecord(digest=digest, status=BlobStatus.READY)
        ready_journal = replace(
            pending_journal,
            blobs=tuple(
                ready if blob.digest == digest else blob
                for blob in pending_journal.blobs
            ),
        )
        self._write(transaction_id, ready_journal)
        return digest

    def read_blob(self, transaction_id: str, digest: str) -> bytes:
        journal = self.read(transaction_id)
        descriptor = next(
            (blob for blob in journal.blobs if blob.digest == digest), None
        )
        if descriptor is None:
            raise CorruptTransactionError(
                "transaction blob has no journal descriptor",
                details={"transaction_id": transaction_id, "digest": digest},
            )
        if descriptor.status is not BlobStatus.READY:
            raise IncompleteTransactionError(
                "transaction blob write is incomplete",
                details={"transaction_id": transaction_id, "digest": digest},
            )
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

    def complete(
        self,
        transaction_id: str,
        *,
        committed_transaction_id: str | None = None,
    ) -> None:
        requested_id = _identifier(transaction_id, "transaction_id")
        active = self.active()
        if active is None or active.transaction_id != requested_id:
            raise CorruptTransactionError("completed transaction is not the active transaction")
        if isinstance(active, CleanupTombstone):
            tombstone = active
            self._verify_cleanup_proof(
                tombstone,
                committed_transaction_id=committed_transaction_id,
            )
        else:
            journal = active
            tombstone = self._prepare_cleanup_tombstone(
                journal,
                committed_transaction_id=committed_transaction_id,
            )
            # This durable descriptor contains everything needed after journal
            # deletion. No destructive cleanup may precede it.
            self.filesystem.atomic_write_json(
                self.active_path, tombstone.to_dict()
            )

        for digest in tombstone.blobs:
            self.filesystem.unlink(
                self._blob_path(requested_id, digest), missing_ok=True
            )
        blob_directory = f"{self.transactions_directory}/{requested_id}/blobs"
        self.filesystem.rmdir_empty(blob_directory, missing_ok=True)
        self.filesystem.unlink(self._journal_path(requested_id), missing_ok=True)
        self.filesystem.rmdir_empty(
            f"{self.transactions_directory}/{requested_id}", missing_ok=True
        )
        # This pointer is the cleanup authority and must be the final unlink.
        self.filesystem.unlink(self.active_path)
        self.filesystem.rmdir_empty(self.transactions_directory, missing_ok=True)
        self.filesystem.rmdir_empty(self.manifest_directory, missing_ok=True)

    def _prepare_cleanup_tombstone(
        self,
        journal: TransactionJournal,
        *,
        committed_transaction_id: str | None,
    ) -> CleanupTombstone:
        if any(blob.status is not BlobStatus.READY for blob in journal.blobs):
            raise IncompleteTransactionError(
                "transaction contains an incomplete blob write",
                details={"transaction_id": journal.transaction_id},
            )
        forward = journal.operation in {Operation.INSTALL, Operation.UPDATE}
        required_checkpoint = "manifest_committed" if forward else "manifest_removed"
        if required_checkpoint not in journal.operation_checkpoints:
            raise CorruptTransactionError(
                "transaction cleanup proof checkpoint is missing",
                details={"transaction_id": journal.transaction_id},
            )
        tombstone = CleanupTombstone(
            transaction_id=journal.transaction_id,
            operation=journal.operation,
            proof_kind=(
                CleanupProofKind.COMMITTED_MANIFEST
                if forward
                else CleanupProofKind.MANIFEST_REMOVED
            ),
            proof_transaction_id=journal.transaction_id,
            blobs=tuple(blob.digest for blob in journal.blobs),
        )
        self._verify_cleanup_proof(
            tombstone,
            committed_transaction_id=committed_transaction_id,
        )
        return tombstone

    def _verify_cleanup_proof(
        self,
        tombstone: CleanupTombstone,
        *,
        committed_transaction_id: str | None,
    ) -> None:
        from .manifest import ManifestRepository

        committed = ManifestRepository(
            self.filesystem,
            manifest_directory=self.manifest_directory,
        ).read()
        if tombstone.proof_kind is CleanupProofKind.COMMITTED_MANIFEST:
            valid = (
                committed is not None
                and committed.transaction_id == tombstone.proof_transaction_id
            )
        else:
            valid = committed is None
        if committed_transaction_id is not None:
            valid = valid and committed_transaction_id == tombstone.transaction_id
        if not valid:
            raise CorruptTransactionError(
                "committed manifest transaction proof does not match cleanup",
                details={
                    "transaction_id": tombstone.transaction_id,
                    "committed_transaction_id": committed_transaction_id,
                    "proof_kind": tombstone.proof_kind.value,
                },
            )


__all__ = [
    "ACTIVE_TRANSACTION_V1_SCHEMA",
    "ActiveTransactionState",
    "BlobStatus",
    "CleanupProofKind",
    "CleanupTombstone",
    "EffectProgress",
    "JournalCheckpoint",
    "TRANSACTION_ENGINE_NAME",
    "TRANSACTION_SCHEMA_VERSION",
    "TRANSACTION_V1_SCHEMA",
    "TransactionEffectRecord",
    "TransactionBlobRecord",
    "TransactionJournal",
    "TransactionPhase",
    "TransactionRepository",
]
