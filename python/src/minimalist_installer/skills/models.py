"""Immutable host, evidence, and destination values for skill planning."""

from __future__ import annotations

import ntpath
from dataclasses import dataclass, field
from enum import StrEnum
from os.path import isabs as posix_isabs
from pathlib import Path

from ..core.models import JsonObject


class SupportTier(StrEnum):
    """Declared installation-support level for a host adapter."""

    VERIFIED = "verified"
    LAYOUT_ONLY = "layout-only"
    EXTERNAL = "external"


class Scope(StrEnum):
    """Whether destinations are resolved against the user home or a project."""

    USER = "user"
    PROJECT = "project"


class EvidenceKind(StrEnum):
    """Read-only signals that a host may be present.

    Weights are strictly ordered: executable presence outranks environment,
    which outranks an existing installer manifest, which outranks a config
    directory. Detection never executes host binaries.
    """

    EXECUTABLE = "executable"
    ENVIRONMENT = "environment"
    MANIFEST = "manifest"
    CONFIG_DIRECTORY = "config-directory"

    @property
    def weight(self) -> int:
        return {
            EvidenceKind.EXECUTABLE: 40,
            EvidenceKind.ENVIRONMENT: 30,
            EvidenceKind.MANIFEST: 20,
            EvidenceKind.CONFIG_DIRECTORY: 10,
        }[self]


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise TypeError(f"{label} must be a sequence of strings")
    items = tuple(value)
    if any(not isinstance(item, str) or not item for item in items):
        raise ValueError(f"{label} must contain non-empty strings")
    return items


def _relative_destination(value: str, label: str) -> str:
    if value in {".", ".."} or _is_absolute_path(value):
        raise ValueError(f"{label} must be a relative destination without parent traversal")
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} must be a relative destination without parent traversal")
    return value


def _basename_only(value: str, label: str) -> str:
    if (
        value in {".", ".."}
        or _is_absolute_path(value)
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise ValueError(f"{label} must be a basename, not a path")
    return value


def _environment_pattern(value: str, label: str) -> str:
    if value in {".", ".."} or (value.endswith("*") and not value[:-1]):
        raise ValueError(f"{label} must not use an empty prefix")
    return value


def _is_absolute_path(value: str) -> bool:
    return posix_isabs(value) or ntpath.isabs(value)


@dataclass(frozen=True, slots=True)
class HostDestinations:
    """Relative user and project skill roots declared by one host."""

    user: tuple[str, ...]
    project: tuple[str, ...]

    def __post_init__(self) -> None:
        user = _string_tuple(self.user, "user destinations")
        project = _string_tuple(self.project, "project destinations")
        if not user or not project:
            raise ValueError("host destinations must declare user and project paths")
        object.__setattr__(
            self,
            "user",
            tuple(_relative_destination(item, "user destinations") for item in user),
        )
        object.__setattr__(
            self,
            "project",
            tuple(_relative_destination(item, "project destinations") for item in project),
        )

    def to_dict(self) -> JsonObject:
        return {"user": list(self.user), "project": list(self.project)}


@dataclass(frozen=True, slots=True)
class DetectionSignals:
    """Declarative, read-only probes used to estimate host presence."""

    executables: tuple[str, ...] = ()
    environment: tuple[str, ...] = ()
    config_dirs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        executables = _string_tuple(self.executables, "executables")
        environment = _string_tuple(self.environment, "environment")
        config_dirs = _string_tuple(self.config_dirs, "config_dirs")
        object.__setattr__(
            self,
            "executables",
            tuple(_basename_only(item, "executables") for item in executables),
        )
        object.__setattr__(
            self,
            "environment",
            tuple(_environment_pattern(item, "environment") for item in environment),
        )
        object.__setattr__(
            self,
            "config_dirs",
            tuple(_relative_destination(item, "config_dirs") for item in config_dirs),
        )

    def to_dict(self) -> JsonObject:
        return {
            "executables": list(self.executables),
            "environment": list(self.environment),
            "config_dirs": list(self.config_dirs),
        }


@dataclass(frozen=True, slots=True)
class HostLayout:
    """Skill leaf naming used later when rendering a bundle into a destination."""

    skill_file: str = "SKILL.md"
    discovery_depth: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.skill_file, str) or not self.skill_file.strip():
            raise ValueError("skill_file must be non-empty text")
        if isinstance(self.discovery_depth, bool) or not isinstance(self.discovery_depth, int):
            raise TypeError("discovery_depth must be an integer")
        if self.discovery_depth < 1:
            raise ValueError("discovery_depth must be positive")

    def to_dict(self) -> JsonObject:
        return {"skill_file": self.skill_file, "discovery_depth": self.discovery_depth}


@dataclass(frozen=True, slots=True)
class HostAdapter:
    """A host's identity, support tier, destinations, and detection signals."""

    id: str
    display_name: str
    support_tier: SupportTier
    destinations: HostDestinations
    detection: DetectionSignals = field(default_factory=DetectionSignals)
    layout: HostLayout = field(default_factory=HostLayout)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("host id must be non-empty text")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ValueError("display_name must be non-empty text")
        if not isinstance(self.support_tier, SupportTier):
            object.__setattr__(self, "support_tier", SupportTier(self.support_tier))
        if not isinstance(self.destinations, HostDestinations):
            raise TypeError("destinations must be HostDestinations")
        if not isinstance(self.detection, DetectionSignals):
            raise TypeError("detection must be DetectionSignals")
        if not isinstance(self.layout, HostLayout):
            raise TypeError("layout must be HostLayout")

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "support_tier": self.support_tier.value,
            "destinations": self.destinations.to_dict(),
            "detection": self.detection.to_dict(),
            "layout": self.layout.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Evidence:
    """One read-only observation contributing to a host's confidence."""

    kind: EvidenceKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            object.__setattr__(self, "kind", EvidenceKind(self.kind))
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("evidence value must be non-empty text")

    def to_dict(self) -> JsonObject:
        return {"kind": self.kind.value, "value": self.value}


@dataclass(frozen=True, slots=True)
class HostDetection:
    """A host that produced at least one detection signal."""

    host: HostAdapter
    confidence: int
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int):
            raise TypeError("confidence must be an integer")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_dict(self) -> JsonObject:
        return {
            "id": self.host.id,
            "display_name": self.host.display_name,
            "support_tier": self.host.support_tier.value,
            "confidence": self.confidence,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class PlannedDestination:
    """One physical skill root attributed to every compatible detected host."""

    path: Path
    scope: Scope
    host_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        if not isinstance(self.scope, Scope):
            object.__setattr__(self, "scope", Scope(self.scope))
        object.__setattr__(self, "host_ids", tuple(self.host_ids))

    def to_dict(self) -> JsonObject:
        return {
            "path": str(self.path),
            "scope": self.scope.value,
            "host_ids": list(self.host_ids),
        }


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Per-host evidence plus physically deduplicated destinations."""

    scope: Scope
    detections: tuple[HostDetection, ...]
    destinations: tuple[PlannedDestination, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            object.__setattr__(self, "scope", Scope(self.scope))
        object.__setattr__(self, "detections", tuple(self.detections))
        object.__setattr__(self, "destinations", tuple(self.destinations))

    def to_dict(self) -> JsonObject:
        return {
            "scope": self.scope.value,
            "detections": [item.to_dict() for item in self.detections],
            "destinations": [item.to_dict() for item in self.destinations],
        }


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """A validated user-home or project-root base for destination planning."""

    scope: Scope
    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            object.__setattr__(self, "scope", Scope(self.scope))
        object.__setattr__(self, "root", Path(self.root))

    def to_dict(self) -> JsonObject:
        return {"scope": self.scope.value, "root": str(self.root)}


__all__ = [
    "DetectionResult",
    "DetectionSignals",
    "Evidence",
    "EvidenceKind",
    "HostAdapter",
    "HostDestinations",
    "HostDetection",
    "HostLayout",
    "PlannedDestination",
    "ResolvedScope",
    "Scope",
    "SupportTier",
]
