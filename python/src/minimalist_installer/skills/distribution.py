"""Load skill distributions and plan generic file-set installs."""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..core.errors import (
    InvalidDistributionError,
    NoHostDetectedError,
    UnsafePathError,
    UnsupportedHostError,
)
from ..core.models import EffectPlan, JsonObject, Operation, PlanContext
from ..providers import FileSetProvider
from .bundle import load_bundle, reject_secret_variable, render_bundle
from .detector import detect_hosts
from .models import DetectionResult, HostAdapter, ResolvedScope, Scope, _is_absolute_path
from .registry import HostRegistry
from .scope import resolve_scope

_DISTRIBUTION_KEYS = frozenset({"name", "version", "bundle", "variables"})


DISTRIBUTION_V1_SCHEMA: JsonObject = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/henryavila/minimalist-installer/spec/schemas/skill-distribution-v1.schema.json",
    "title": "Minimalist Installer skill distribution v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "version", "bundle"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "string", "minLength": 1},
        "bundle": {"type": "string", "minLength": 1},
        "variables": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
}


def _invalid(message: str, *, path: Path | None = None, **details: str) -> InvalidDistributionError:
    return InvalidDistributionError(message, path=path, details=details)


def _reject_unknown(mapping: Mapping[str, object], allowed: frozenset[str], origin: str) -> None:
    extra = [key for key in mapping if key not in allowed]
    if extra:
        raise _invalid(f"unknown distribution field {extra[0]!r}", origin=origin, field=extra[0])


def _required_text(mapping: Mapping[str, object], key: str, origin: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} must be non-empty text", origin=origin)
    return value


def _basename(value: str, label: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or _is_absolute_path(value)
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise _invalid(f"{label} must be a basename")
    return value


def _skill_leaf(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or _is_absolute_path(value)
        or "/" in value
        or "\\" in value
        or Path(value).name != value
        or ".." in Path(value).parts
    ):
        raise UnsafePathError(
            "layout.skill_file must be a basename without parent traversal",
            path=Path(value),
        )
    return value


def _freeze_variables(value: object) -> Mapping[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError("variables must be a mapping")
    frozen: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise _invalid("variable names must be non-empty text")
        reject_secret_variable(key)
        if isinstance(item, Path):
            item = str(item)
        if not isinstance(item, str):
            raise _invalid(f"variable {key!r} must be text", name=key)
        frozen[key] = item
    return MappingProxyType(frozen)


def _resolve_bundle(value: str, origin_dir: Path | None) -> Path:
    path = Path(value)
    if any(part == ".." for part in path.parts):
        raise UnsafePathError("bundle path must not escape", path=path)
    if not path.is_absolute() and origin_dir is not None:
        path = origin_dir / path
    return path


@dataclass(frozen=True, slots=True)
class SkillDistribution:
    """Declarative skill name, bundle root, and declared render variables."""

    name: str
    version: str
    bundle: Path
    variables: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _basename(self.name, "name"))
        if not isinstance(self.version, str) or not self.version.strip():
            raise _invalid("version must be non-empty text")
        object.__setattr__(self, "bundle", Path(self.bundle))
        object.__setattr__(self, "variables", _freeze_variables(self.variables))

    def to_dict(self) -> JsonObject:
        return {
            "name": self.name,
            "version": self.version,
            "bundle": str(self.bundle),
            "variables": dict(self.variables),
        }


def distribution_from_mapping(
    data: Mapping[str, object],
    *,
    origin: str,
    origin_dir: Path | None = None,
) -> SkillDistribution:
    """Validate a distribution mapping and fail closed on unknown fields."""

    _reject_unknown(data, _DISTRIBUTION_KEYS, origin)
    for required in ("name", "version", "bundle"):
        if required not in data:
            raise _invalid(f"missing distribution field {required!r}", origin=origin)
    bundle = data["bundle"]
    if not isinstance(bundle, str) or not bundle:
        raise _invalid("bundle must be non-empty text", origin=origin)
    variables = data.get("variables", {})
    if variables is None:
        variables = {}
    if not isinstance(variables, Mapping):
        raise _invalid("variables must be a table", origin=origin)
    try:
        return SkillDistribution(
            name=_required_text(data, "name", origin),
            version=_required_text(data, "version", origin),
            bundle=_resolve_bundle(bundle, origin_dir),
            variables=variables,
        )
    except (TypeError, ValueError) as error:
        raise _invalid(str(error), origin=origin) from error


def load_distribution(source: str | Path, *, origin: str | None = None) -> SkillDistribution:
    """Parse one skill distribution from TOML text or a TOML file path."""

    origin_dir: Path | None = None
    if isinstance(source, Path):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as error:
            raise _invalid("skill distribution could not be read", origin=str(source)) from error
        label = origin or str(source)
        origin_dir = source.parent
    else:
        text = source
        label = origin or "<toml>"
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise _invalid("skill distribution is not valid TOML", origin=label) from error
    if not isinstance(data, dict):
        raise _invalid("skill distribution must be a TOML table", origin=label)
    return distribution_from_mapping(data, origin=label, origin_dir=origin_dir)


@dataclass(frozen=True, slots=True)
class PlannedSkillFile:
    """One physical destination attributed to every host that shares it."""

    path: Path
    relative: str
    bundle_path: str
    host_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "host_ids", tuple(self.host_ids))

    def to_dict(self) -> JsonObject:
        return {
            "path": str(self.path),
            "relative": self.relative,
            "bundle_path": self.bundle_path,
            "host_ids": list(self.host_ids),
        }


@dataclass(frozen=True, slots=True)
class SkillDistributionPlan:
    """One file-set plan plus host attribution for shared destinations."""

    plans: tuple[EffectPlan, ...]
    files: tuple[PlannedSkillFile, ...]
    scope: ResolvedScope
    selected_hosts: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "plans", tuple(self.plans))
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "selected_hosts", tuple(self.selected_hosts))

    def __iter__(self) -> Iterator[EffectPlan]:
        return iter(self.plans)

    def __len__(self) -> int:
        return len(self.plans)

    def to_dict(self) -> JsonObject:
        return {
            "plans": [plan.to_dict() for plan in self.plans],
            "files": [item.to_dict() for item in self.files],
            "scope": self.scope.to_dict(),
            "selected_hosts": list(self.selected_hosts),
        }


def _utf8_text(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid(
            f"skill file {path!r} must be valid UTF-8",
            field=path,
        ) from error


def _select_hosts(
    hosts: Sequence[str] | HostRegistry | DetectionResult,
    *,
    scope: Scope,
    registry: HostRegistry | None,
    home: Path | None,
    project: Path | None,
    environ: Mapping[str, str] | None,
    search_path: str | None,
) -> tuple[HostAdapter, ...]:
    if isinstance(hosts, DetectionResult):
        selected = tuple(item.host for item in hosts.detections)
    elif isinstance(hosts, HostRegistry):
        detection = detect_hosts(
            scope=scope,
            registry=hosts,
            home=home,
            project=project,
            environ=environ,
            search_path=search_path,
        )
        selected = tuple(item.host for item in detection.detections)
    else:
        loaded = registry if registry is not None else HostRegistry.bundled()
        selected_hosts: list[HostAdapter] = []
        seen: set[str] = set()
        for host_id in hosts:
            if not isinstance(host_id, str) or not host_id:
                raise UnsupportedHostError("host id must be non-empty text")
            adapter = loaded.get(host_id)
            if adapter is None:
                raise UnsupportedHostError(
                    f'unsupported host "{host_id}"',
                    details={"host": host_id},
                )
            if host_id in seen:
                continue
            seen.add(host_id)
            selected_hosts.append(adapter)
        selected = tuple(selected_hosts)
    if not selected:
        raise NoHostDetectedError("no host detected")
    return selected


def _destination_relative(bundle_path: str, skill_file: str) -> str:
    leaf = _skill_leaf(skill_file)
    mapped = leaf if bundle_path == "SKILL.md" else bundle_path
    parts = mapped.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafePathError(
            "skill destination must be a relative path without parent traversal",
            path=Path(mapped),
        )
    return "/".join(parts)


def _host_destination_relatives(
    host: HostAdapter, *, scope: Scope
) -> tuple[str, ...]:
    return host.destinations.user if scope is Scope.USER else host.destinations.project


def plan_distribution(
    distribution: SkillDistribution,
    *,
    hosts: Sequence[str] | HostRegistry | DetectionResult,
    scope: Scope | str,
    home: Path | None = None,
    project: Path | None = None,
    registry: HostRegistry | None = None,
    environ: Mapping[str, str] | None = None,
    search_path: str | None = None,
) -> SkillDistributionPlan:
    """Render a bundle and emit one ``reconcile_file_set`` plan per destination root.

    Shared physical destinations are written once. Host attribution is retained
    even when Codex, Gemini, and Grok share ``.agents/skills``. Each plan locks
    only that destination root (for example ``$HOME/.agents/skills``), not the
    whole home or project root. Non-UTF-8 assets fail closed here because the
    generic file-set effect stores UTF-8 text; inventory and rendering still
    copy those bytes unchanged.
    """

    if not isinstance(distribution, SkillDistribution):
        raise TypeError("distribution must be a SkillDistribution")
    resolved_scope = scope if isinstance(scope, Scope) else Scope(scope)
    resolved = resolve_scope(resolved_scope, start=project, home=home)
    selected = _select_hosts(
        hosts,
        scope=resolved_scope,
        registry=registry,
        home=home,
        project=project,
        environ=environ,
        search_path=search_path,
    )
    rendered = render_bundle(load_bundle(distribution.bundle), distribution.variables)

    # (destination_root, path_under_dest) -> physical, scope-relative, bundle, text, hosts
    pending: dict[tuple[str, str], tuple[Path, str, str, str, list[str]]] = {}
    for host in selected:
        leaf = _skill_leaf(host.layout.skill_file)
        for destination in _host_destination_relatives(host, scope=resolved_scope):
            dest_root = resolved.root / destination
            for item in rendered:
                dest_rel = _destination_relative(item.path, leaf)
                under_dest = f"{distribution.name}/{dest_rel}"
                physical = dest_root / distribution.name / dest_rel
                try:
                    scope_relative = physical.relative_to(resolved.root).as_posix()
                except ValueError as error:
                    raise UnsafePathError(
                        "skill destination escapes the resolved scope",
                        path=physical,
                    ) from error
                text = _utf8_text(item.path, item.data)
                key = (destination, under_dest)
                existing = pending.get(key)
                if existing is None:
                    pending[key] = (physical, scope_relative, item.path, text, [host.id])
                    continue
                _existing_path, _scope_relative, bundle_path, content, host_ids = existing
                if bundle_path != item.path or content != text:
                    raise _invalid(
                        "colliding skill destinations",
                        path=physical,
                        destination=scope_relative,
                    )
                if host.id not in host_ids:
                    host_ids.append(host.id)

    ordered = tuple(
        PlannedSkillFile(
            path=physical,
            relative=scope_relative,
            bundle_path=bundle_path,
            host_ids=tuple(sorted(dict.fromkeys(host_ids))),
        )
        for (_destination, _under_dest), (
            physical,
            scope_relative,
            bundle_path,
            _content,
            host_ids,
        ) in sorted(
            pending.items(),
            key=lambda item: (item[0][0].encode("utf-8"), item[0][1].encode("utf-8")),
        )
    )

    by_destination: dict[str, list[dict[str, str]]] = {}
    for (destination, under_dest), (_physical, _scope_relative, _bundle, content, _hosts) in sorted(
        pending.items(),
        key=lambda item: (item[0][0].encode("utf-8"), item[0][1].encode("utf-8")),
    ):
        by_destination.setdefault(destination, []).append(
            {"path": under_dest, "content": content}
        )

    plans: list[EffectPlan] = []
    for destination, files in sorted(
        by_destination.items(),
        key=lambda item: item[0].encode("utf-8"),
    ):
        plans.extend(
            FileSetProvider(
                effect_id=(
                    f"skills:{distribution.name}:{resolved_scope.value}:{destination}"
                ),
                destination=destination,
            ).plan(
                {"files": files},
                PlanContext(base_path=resolved.root, operation=Operation.INSTALL),
            )
        )
    return SkillDistributionPlan(
        plans=tuple(plans),
        files=ordered,
        scope=resolved,
        selected_hosts=tuple(sorted({host.id for host in selected})),
    )


__all__ = [
    "DISTRIBUTION_V1_SCHEMA",
    "PlannedSkillFile",
    "SkillDistribution",
    "SkillDistributionPlan",
    "distribution_from_mapping",
    "load_distribution",
    "plan_distribution",
]
