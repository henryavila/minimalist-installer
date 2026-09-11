"""Load bundled host descriptors and optional entry-point adapters."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Iterator, Mapping
from importlib import metadata, resources
from pathlib import Path
from typing import Self

from ..core.errors import InvalidDistributionError
from .models import (
    DetectionSignals,
    HostAdapter,
    HostDestinations,
    HostLayout,
    SupportTier,
)

_ENTRY_POINT_GROUP = "minimalist_installer.hosts"
_HOST_KEYS = frozenset(
    {"id", "display_name", "support_tier", "destinations", "detection", "layout"}
)
_DESTINATION_KEYS = frozenset({"user", "project"})
_DETECTION_KEYS = frozenset({"executables", "environment", "config_dirs"})
_LAYOUT_KEYS = frozenset({"skill_file", "discovery_depth"})


def _invalid(message: str, **details: str) -> InvalidDistributionError:
    return InvalidDistributionError(message, details=details)


def _reject_unknown(mapping: Mapping[str, object], allowed: frozenset[str], origin: str) -> None:
    extra = [key for key in mapping if key not in allowed]
    if extra:
        raise _invalid(
            f"unknown host descriptor field {extra[0]!r}",
            origin=origin,
            field=extra[0],
        )


def _require_mapping(value: object, label: str, origin: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{label} must be a table", origin=origin)
    return value


def _string_tuple(value: object, label: str, origin: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _invalid(f"{label} must be an array of strings", origin=origin)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise _invalid(f"{label} must contain non-empty strings", origin=origin)
        items.append(item)
    return tuple(items)


def _required_text(mapping: Mapping[str, object], key: str, origin: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} must be non-empty text", origin=origin)
    return value


def adapter_from_mapping(data: Mapping[str, object], *, origin: str) -> HostAdapter:
    """Validate a descriptor mapping and fail closed on unknown fields."""

    _reject_unknown(data, _HOST_KEYS, origin)
    for required in ("id", "display_name", "support_tier", "destinations"):
        if required not in data:
            raise _invalid(f"missing host descriptor field {required!r}", origin=origin)

    destinations_data = _require_mapping(data["destinations"], "destinations", origin)
    _reject_unknown(destinations_data, _DESTINATION_KEYS, origin)
    if "user" not in destinations_data or "project" not in destinations_data:
        raise _invalid("destinations must declare user and project arrays", origin=origin)

    detection_data = _require_mapping(data.get("detection", {}), "detection", origin)
    _reject_unknown(detection_data, _DETECTION_KEYS, origin)

    layout_data = data.get("layout", {})
    layout_mapping = _require_mapping(layout_data, "layout", origin)
    _reject_unknown(layout_mapping, _LAYOUT_KEYS, origin)

    tier_text = _required_text(data, "support_tier", origin)
    try:
        support_tier = SupportTier(tier_text)
    except ValueError as error:
        raise _invalid(f"unsupported support_tier {tier_text!r}", origin=origin) from error

    skill_file = layout_mapping.get("skill_file", "SKILL.md")
    discovery_depth = layout_mapping.get("discovery_depth", 1)
    if not isinstance(skill_file, str):
        raise _invalid("layout.skill_file must be text", origin=origin)
    if isinstance(discovery_depth, bool) or not isinstance(discovery_depth, int):
        raise _invalid("layout.discovery_depth must be an integer", origin=origin)

    try:
        return HostAdapter(
            id=_required_text(data, "id", origin),
            display_name=_required_text(data, "display_name", origin),
            support_tier=support_tier,
            destinations=HostDestinations(
                user=_string_tuple(destinations_data.get("user"), "destinations.user", origin),
                project=_string_tuple(
                    destinations_data.get("project"), "destinations.project", origin
                ),
            ),
            detection=DetectionSignals(
                executables=_string_tuple(
                    detection_data.get("executables"), "detection.executables", origin
                ),
                environment=_string_tuple(
                    detection_data.get("environment"), "detection.environment", origin
                ),
                config_dirs=_string_tuple(
                    detection_data.get("config_dirs"), "detection.config_dirs", origin
                ),
            ),
            layout=HostLayout(skill_file=skill_file, discovery_depth=discovery_depth),
        )
    except (TypeError, ValueError) as error:
        raise _invalid(str(error), origin=origin) from error


def load_host_descriptor(source: str | Path, *, origin: str | None = None) -> HostAdapter:
    """Parse one host descriptor from TOML text or a TOML file path."""

    if isinstance(source, Path):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as error:
            raise _invalid("host descriptor could not be read", origin=str(source)) from error
        label = origin or str(source)
    else:
        text = source
        label = origin or "<toml>"
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise _invalid("host descriptor is not valid TOML", origin=label) from error
    if not isinstance(data, dict):
        raise _invalid("host descriptor must be a TOML table", origin=label)
    return adapter_from_mapping(data, origin=label)


def load_bundled_descriptors() -> tuple[HostAdapter, ...]:
    """Load every packaged host TOML from the installer distribution."""

    hosts_root = resources.files("minimalist_installer.skills") / "hosts"
    names = sorted(
        path.name for path in hosts_root.iterdir() if path.name.endswith(".toml")
    )
    return tuple(
        load_host_descriptor((hosts_root / name).read_text(encoding="utf-8"), origin=name)
        for name in names
    )


def _hosts_from_entry(loaded: object, *, name: str) -> tuple[HostAdapter, ...]:
    if isinstance(loaded, HostAdapter):
        return (loaded,)
    if callable(loaded) and not isinstance(loaded, type):
        return _hosts_from_entry(loaded(), name=name)
    if isinstance(loaded, Path):
        return (load_host_descriptor(loaded, origin=str(loaded)),)
    if isinstance(loaded, str):
        path = Path(loaded)
        if path.is_file():
            return (load_host_descriptor(path, origin=loaded),)
        return (load_host_descriptor(loaded, origin=name),)
    if isinstance(loaded, Mapping):
        return (adapter_from_mapping(loaded, origin=name),)
    raise _invalid(
        f'host entry point "{name}" must provide a HostAdapter or TOML path',
        entry_point=name,
        type=type(loaded).__name__,
    )


def load_entry_point_hosts() -> tuple[HostAdapter, ...]:
    """Load third-party adapters from the ``minimalist_installer.hosts`` group."""

    selected = metadata.entry_points(group=_ENTRY_POINT_GROUP)
    adapters: list[HostAdapter] = []
    for entry in selected:
        try:
            loaded = entry.load()
        except Exception as error:
            raise _invalid(
                f'host entry point "{entry.name}" could not be loaded',
                entry_point=entry.name,
            ) from error
        adapters.extend(_hosts_from_entry(loaded, name=entry.name))
    return tuple(adapters)


class HostRegistry:
    """In-memory index of host adapters keyed by stable id."""

    def __init__(self, hosts: Iterable[HostAdapter] = ()) -> None:
        self._hosts: dict[str, HostAdapter] = {}
        for host in hosts:
            self._add(host)

    def _add(self, host: HostAdapter) -> None:
        if not isinstance(host, HostAdapter):
            raise _invalid("registry entries must be HostAdapter values")
        if host.id in self._hosts:
            raise _invalid(f'duplicate host id "{host.id}"')
        self._hosts[host.id] = host

    @classmethod
    def bundled(cls, *, load_entry_points: bool = True) -> Self:
        """Load packaged descriptors, then optional entry-point extensions."""

        registry = cls(load_bundled_descriptors())
        if load_entry_points:
            for host in load_entry_point_hosts():
                registry._add(host)
        return registry

    @property
    def hosts(self) -> tuple[HostAdapter, ...]:
        return tuple(self._hosts[key] for key in sorted(self._hosts))

    def get(self, host_id: str) -> HostAdapter | None:
        return self._hosts.get(host_id)

    def __iter__(self) -> Iterator[HostAdapter]:
        return iter(self.hosts)

    def __len__(self) -> int:
        return len(self._hosts)


__all__ = [
    "HostRegistry",
    "adapter_from_mapping",
    "load_bundled_descriptors",
    "load_entry_point_hosts",
    "load_host_descriptor",
]
