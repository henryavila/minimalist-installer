"""Strict models and safe persistence for the last committed installation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, cast

from .errors import CorruptManifestError
from .models import JsonObject, JsonValue, _freeze_json, _json_value
from .path_safety import SafeFilesystem

MANIFEST_SCHEMA_VERSION: Final = 1
MANIFEST_ENGINE_NAME: Final = "minimalist-installer"
MANIFEST_FILENAME: Final = "manifest.json"

MANIFEST_V1_SCHEMA: JsonObject = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/henryavila/minimalist-installer/spec/schemas/python-manifest-v1.schema.json",
    "title": "Minimalist Installer Python committed manifest v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "engine",
        "installation",
        "transaction_id",
        "effects",
        "installed_at",
        "updated_at",
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
        "installation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "consumer", "consumer_version"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "consumer": {"type": "string", "minLength": 1},
                "consumer_version": {"type": "string", "minLength": 1},
            },
        },
        "transaction_id": {"type": "string", "minLength": 1},
        "effects": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "type", "effect_version", "before_state"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "minLength": 1},
                    "effect_version": {"type": "integer", "minimum": 1},
                    "before_state": {},
                },
            },
        },
        "installed_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
    },
}

_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "engine",
        "installation",
        "transaction_id",
        "effects",
        "installed_at",
        "updated_at",
    }
)
_ENGINE_KEYS = frozenset({"name", "version"})
_INSTALLATION_KEYS = frozenset({"id", "consumer", "consumer_version"})
_EFFECT_KEYS = frozenset({"id", "type", "effect_version", "before_state"})


def _require_exact_keys(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            f"{label} has invalid fields (missing={missing}, unexpected={unexpected})"
        )


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return cast(Mapping[str, object], value)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _parse_timestamp(value: object, label: str) -> str:
    timestamp = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    return timestamp


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("manifest clock must return a timezone-aware datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class ManifestEffectRecord:
    """Stable effect identity and the before-state needed for reversal."""

    id: str
    type: str
    effect_version: int
    before_state: JsonValue

    def __post_init__(self) -> None:
        _require_text(self.id, "effect.id")
        _require_text(self.type, "effect.type")
        _require_positive_integer(self.effect_version, "effect.effect_version")
        object.__setattr__(self, "before_state", _freeze_json(self.before_state))

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "type": self.type,
            "effect_version": self.effect_version,
            "before_state": _json_value(self.before_state),
        }

    @classmethod
    def from_dict(cls, value: object) -> ManifestEffectRecord:
        effect = _require_mapping(value, "effect")
        _require_exact_keys(effect, _EFFECT_KEYS, "effect")
        return cls(
            id=_require_text(effect["id"], "effect.id"),
            type=_require_text(effect["type"], "effect.type"),
            effect_version=_require_positive_integer(
                effect["effect_version"], "effect.effect_version"
            ),
            before_state=cast(JsonValue, effect["before_state"]),
        )


@dataclass(frozen=True, slots=True)
class CommittedManifest:
    """A complete installation state that never represents partial work."""

    installation_id: str
    consumer: str
    consumer_version: str
    transaction_id: str
    engine_version: str
    effects: tuple[ManifestEffectRecord, ...]
    installed_at: str
    updated_at: str
    schema_version: int = MANIFEST_SCHEMA_VERSION
    engine_name: str = MANIFEST_ENGINE_NAME

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema: {self.schema_version}")
        if self.engine_name != MANIFEST_ENGINE_NAME:
            raise ValueError(f"foreign manifest engine: {self.engine_name}")
        _require_text(self.engine_version, "engine.version")
        _require_text(self.installation_id, "installation.id")
        _require_text(self.consumer, "installation.consumer")
        _require_text(self.consumer_version, "installation.consumer_version")
        _require_text(self.transaction_id, "transaction_id")
        object.__setattr__(self, "effects", tuple(self.effects))
        identifiers = [effect.id for effect in self.effects]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("effect ids must be unique")
        installed = _parse_timestamp(self.installed_at, "installed_at")
        updated = _parse_timestamp(self.updated_at, "updated_at")
        installed_value = datetime.fromisoformat(installed.replace("Z", "+00:00"))
        updated_value = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if updated_value < installed_value:
            raise ValueError("updated_at cannot precede installed_at")

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "engine": {"name": self.engine_name, "version": self.engine_version},
            "installation": {
                "id": self.installation_id,
                "consumer": self.consumer,
                "consumer_version": self.consumer_version,
            },
            "transaction_id": self.transaction_id,
            "effects": [effect.to_dict() for effect in self.effects],
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> CommittedManifest:
        manifest = _require_mapping(value, "manifest")
        _require_exact_keys(manifest, _MANIFEST_KEYS, "manifest")
        schema_version = manifest["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise TypeError("schema_version must be an integer")
        if schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema: {schema_version}")

        engine = _require_mapping(manifest["engine"], "engine")
        _require_exact_keys(engine, _ENGINE_KEYS, "engine")
        engine_name = _require_text(engine["name"], "engine.name")
        if engine_name != MANIFEST_ENGINE_NAME:
            raise ValueError(f"foreign manifest engine: {engine_name}")

        installation = _require_mapping(manifest["installation"], "installation")
        _require_exact_keys(installation, _INSTALLATION_KEYS, "installation")
        effects_value = manifest["effects"]
        if not isinstance(effects_value, list | tuple):
            raise TypeError("effects must be an array")
        effects = tuple(
            ManifestEffectRecord.from_dict(effect) for effect in effects_value
        )
        return cls(
            schema_version=schema_version,
            engine_name=engine_name,
            engine_version=_require_text(engine["version"], "engine.version"),
            installation_id=_require_text(installation["id"], "installation.id"),
            consumer=_require_text(installation["consumer"], "installation.consumer"),
            consumer_version=_require_text(
                installation["consumer_version"], "installation.consumer_version"
            ),
            transaction_id=_require_text(manifest["transaction_id"], "transaction_id"),
            effects=effects,
            installed_at=_parse_timestamp(manifest["installed_at"], "installed_at"),
            updated_at=_parse_timestamp(manifest["updated_at"], "updated_at"),
        )


class ManifestRepository:
    """Read and atomically replace a manifest through an existing safe base."""

    def __init__(
        self,
        filesystem: SafeFilesystem,
        *,
        manifest_directory: str = ".minimalist-installer",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(filesystem, SafeFilesystem):
            raise TypeError("filesystem must be a SafeFilesystem")
        if not isinstance(manifest_directory, str) or not manifest_directory:
            raise ValueError("manifest_directory must be non-empty text")
        self.filesystem = filesystem
        self.manifest_directory = manifest_directory.rstrip("/\\")
        self.manifest_path = f"{self.manifest_directory}/{MANIFEST_FILENAME}"
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    @property
    def display_path(self) -> Path:
        return self.filesystem.base / self.manifest_path

    def read(self) -> CommittedManifest | None:
        """Return the committed manifest, failing closed on malformed data."""

        try:
            value = self.filesystem.read_json(self.manifest_path)
        except FileNotFoundError:
            return None
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise CorruptManifestError(
                "committed manifest contains invalid JSON",
                path=self.display_path,
                details={"reason": type(error).__name__},
            ) from error
        try:
            return CommittedManifest.from_dict(value)
        except (ValueError, TypeError, KeyError) as error:
            raise CorruptManifestError(
                f"committed manifest is invalid: {error}",
                path=self.display_path,
                details={"reason": str(error)},
            ) from error

    def write(self, manifest: CommittedManifest) -> None:
        """Validate and atomically replace the committed manifest."""

        if not isinstance(manifest, CommittedManifest):
            raise TypeError("manifest must be a CommittedManifest")
        # Round-trip through validation so persistence and schema cannot drift.
        value = manifest.to_dict()
        CommittedManifest.from_dict(value)
        self.filesystem.atomic_write_json(self.manifest_path, value)

    def commit(
        self,
        *,
        installation_id: str,
        consumer: str,
        consumer_version: str,
        transaction_id: str,
        engine_version: str,
        effects: Iterable[ManifestEffectRecord],
    ) -> CommittedManifest:
        """Create and persist a complete manifest with stable install time."""

        previous = self.read()
        now = _timestamp(self._clock())
        installed_at = (
            previous.installed_at
            if previous is not None and previous.installation_id == installation_id
            else now
        )
        manifest = CommittedManifest(
            installation_id=installation_id,
            consumer=consumer,
            consumer_version=consumer_version,
            transaction_id=transaction_id,
            engine_version=engine_version,
            effects=tuple(effects),
            installed_at=installed_at,
            updated_at=now,
        )
        self.write(manifest)
        return manifest

    def remove(self) -> bool:
        """Remove the file and, only if empty, its single owned directory."""

        removed = self.filesystem.unlink(self.manifest_path, missing_ok=True)
        self.filesystem.rmdir_empty(self.manifest_directory, missing_ok=True)
        return removed


__all__ = [
    "MANIFEST_ENGINE_NAME",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_V1_SCHEMA",
    "CommittedManifest",
    "ManifestEffectRecord",
    "ManifestRepository",
]
