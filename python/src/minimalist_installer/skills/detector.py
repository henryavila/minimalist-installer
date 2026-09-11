"""Read-only host evidence collection. Never executes host binaries."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from ..core.errors import UnsafePathError
from ..core.path_safety import PathEntryKind, classify_entry
from .models import (
    DetectionResult,
    Evidence,
    EvidenceKind,
    HostAdapter,
    HostDetection,
    PlannedDestination,
    Scope,
    _is_absolute_path,
)
from .registry import HostRegistry
from .scope import resolve_project_root


def _lstat_kind(path: Path) -> PathEntryKind | None:
    try:
        entry = os.lstat(path)
    except OSError:
        return None
    return classify_entry(entry)


def _is_dir(path: Path) -> bool:
    kind = _lstat_kind(path)
    return kind is PathEntryKind.DIRECTORY


def _is_file(path: Path) -> bool:
    kind = _lstat_kind(path)
    return kind is PathEntryKind.FILE


def _matching_env_vars(pattern: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        return tuple(sorted(name for name in environ if name.startswith(prefix)))
    if pattern in environ:
        return (pattern,)
    return ()


def _which(name: str, search_path: str | None) -> str | None:
    if search_path is None:
        return shutil.which(name)
    return shutil.which(name, path=search_path)


def _collect_evidence(
    host: HostAdapter,
    *,
    home: Path,
    project: Path | None,
    environ: Mapping[str, str],
    search_path: str | None,
) -> tuple[Evidence, ...]:
    found: list[Evidence] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: EvidenceKind, value: str) -> None:
        key = (kind.value, value)
        if key in seen:
            return
        seen.add(key)
        found.append(Evidence(kind=kind, value=value))

    for name in host.detection.executables:
        if _which(name, search_path):
            add(EvidenceKind.EXECUTABLE, name)

    for pattern in host.detection.environment:
        for variable in _matching_env_vars(pattern, environ):
            add(EvidenceKind.ENVIRONMENT, variable)

    for relative in host.detection.config_dirs:
        if _is_dir(home / relative) or (project is not None and _is_dir(project / relative)):
            add(EvidenceKind.CONFIG_DIRECTORY, relative)

    for relative in host.destinations.user:
        manifest = home / relative / "manifest.json"
        if _is_file(manifest):
            add(EvidenceKind.MANIFEST, str(manifest))
    if project is not None:
        for relative in host.destinations.project:
            manifest = project / relative / "manifest.json"
            if _is_file(manifest):
                add(EvidenceKind.MANIFEST, str(manifest))

    return tuple(found)


def _resolve_relative(root: Path, relative: str) -> Path:
    if _is_absolute_path(relative) or any(part in {"", ".", ".."} for part in Path(relative).parts):
        raise UnsafePathError("host destination must be a relative path", path=Path(relative))
    return root / relative


def _plan_destinations(
    detections: tuple[HostDetection, ...],
    *,
    scope: Scope,
    root: Path,
) -> tuple[PlannedDestination, ...]:
    grouped: dict[Path, list[str]] = {}
    for detection in detections:
        relatives = (
            detection.host.destinations.user
            if scope is Scope.USER
            else detection.host.destinations.project
        )
        for relative in relatives:
            destination = _resolve_relative(root, relative)
            grouped.setdefault(destination, []).append(detection.host.id)

    planned = [
        PlannedDestination(
            path=path,
            scope=scope,
            host_ids=tuple(sorted(dict.fromkeys(host_ids))),
        )
        for path, host_ids in grouped.items()
    ]
    planned.sort(key=lambda item: (str(item.path), item.host_ids))
    return tuple(planned)


def detect_hosts(
    *,
    scope: Scope | str,
    registry: HostRegistry | None = None,
    home: Path | None = None,
    project: Path | None = None,
    environ: Mapping[str, str] | None = None,
    search_path: str | None = None,
) -> DetectionResult:
    """Return confidence and destinations for hosts with at least one signal.

    Missing hosts produce an empty result rather than an error. Callers still
    decide whether to write; detection never authorizes mutation.
    """

    resolved_scope = scope if isinstance(scope, Scope) else Scope(scope)
    loaded = registry if registry is not None else HostRegistry.bundled()
    home_path = Path(home) if home is not None else Path.home()
    env = dict(os.environ if environ is None else environ)

    project_root: Path | None = None
    if resolved_scope is Scope.PROJECT:
        start = Path(project) if project is not None else Path.cwd()
        project_root = resolve_project_root(start, home=home_path)
        root = project_root
    else:
        root = home_path
        if project is not None:
            project_root = Path(project)

    detections: list[HostDetection] = []
    for host in loaded.hosts:
        evidence = _collect_evidence(
            host,
            home=home_path,
            project=project_root,
            environ=env,
            search_path=search_path,
        )
        if not evidence:
            continue
        kinds = {item.kind for item in evidence}
        confidence = sum(kind.weight for kind in kinds)
        detections.append(
            HostDetection(host=host, confidence=confidence, evidence=evidence)
        )

    detections.sort(key=lambda item: (-item.confidence, item.host.id))
    ranked = tuple(detections)
    return DetectionResult(
        scope=resolved_scope,
        detections=ranked,
        destinations=_plan_destinations(ranked, scope=resolved_scope, root=root),
    )


__all__ = ["detect_hosts"]
