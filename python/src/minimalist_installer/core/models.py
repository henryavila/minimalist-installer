"""Immutable public values and extension protocols for the installer core."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Protocol, Sequence, TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class Operation(StrEnum):
    """Mutation and inspection operations understood by the engine."""

    INSTALL = "install"
    UPDATE = "update"
    STATUS = "status"
    REPAIR = "repair"
    UNINSTALL = "uninstall"


class OperationStatus(StrEnum):
    """Stable high-level state reported by an operation."""

    PLANNED = "planned"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


def _json_value(value: object) -> JsonValue:
    """Convert supported public values into JSON-compatible primitives."""

    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item) for item in value]
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class EffectPlan:
    """A provider's stable, declarative request for one effect."""

    id: str
    type: str
    version: int
    args: Mapping[str, JsonValue]
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Read-only context supplied while providers construct a plan."""

    base_path: Path
    operation: Operation
    installation_id: str | None = None


@dataclass(frozen=True, slots=True)
class EffectContext:
    """Paths and stable identities available to an effect."""

    base_path: Path
    manifest_dir: Path
    operation: Operation
    transaction_id: str
    effect_id: str


@dataclass(frozen=True, slots=True)
class PreparedEffect:
    """Serializable state persisted before an effect may mutate resources."""

    before_state: JsonValue
    payload: JsonValue
    resources: tuple[str, ...] = ()
    recoverable: bool = True


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Normalized immutable result shared by all mutating operations."""

    operation: Operation
    status: OperationStatus
    transaction_id: str | None = None
    installation_id: str | None = None
    planned: tuple[str, ...] = ()
    applied: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    reverted: tuple[str, ...] = ()
    selected_hosts: tuple[str, ...] = ()
    resolved_destinations: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:
        """Return the stable JSON representation used by presentation layers."""

        return {
            "operation": self.operation.value,
            "status": self.status.value,
            "transaction_id": self.transaction_id,
            "installation_id": self.installation_id,
            "planned": list(self.planned),
            "applied": list(self.applied),
            "preserved": list(self.preserved),
            "conflicts": list(self.conflicts),
            "stale": list(self.stale),
            "missing": list(self.missing),
            "reverted": list(self.reverted),
            "selected_hosts": list(self.selected_hosts),
            "resolved_destinations": [str(path) for path in self.resolved_destinations],
            "warnings": list(self.warnings),
        }


InstallResult = OperationResult
UpdateResult = OperationResult
RepairResult = OperationResult
UninstallResult = OperationResult


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Read-only description of a committed installation and its recovery state."""

    status: OperationStatus
    installed: bool
    installation_id: str | None = None
    incomplete_transaction_id: str | None = None
    effect_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> JsonObject:
        """Return the stable JSON representation used by presentation layers."""

        return {
            "status": self.status.value,
            "installed": self.installed,
            "installation_id": self.installation_id,
            "incomplete_transaction_id": self.incomplete_transaction_id,
            "effect_ids": list(self.effect_ids),
            "warnings": list(self.warnings),
        }


class CheckpointWriter(Protocol):
    """Durably records fine-grained progress inside an applied effect."""

    def write(self, checkpoint: str, state: JsonValue) -> None: ...


class Provider(Protocol):
    """Pure planner extension contract."""

    def plan(
        self,
        config: Mapping[str, object],
        context: PlanContext,
    ) -> Sequence[EffectPlan]: ...


class Effect(Protocol):
    """Prepared, reversible mutation extension contract."""

    type: str
    version: int

    def prepare(
        self,
        args: JsonObject,
        previous: JsonValue | None,
        context: EffectContext,
    ) -> PreparedEffect: ...

    def apply(
        self,
        prepared: PreparedEffect,
        checkpoint: CheckpointWriter,
    ) -> JsonValue: ...

    def revert(
        self,
        context: EffectContext,
        before_state: JsonValue,
    ) -> None: ...
