"""Validate Agent Skills bundles and render declared variables."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import InvalidDistributionError, UnsafePathError
from ..core.models import JsonObject
from ..core.path_safety import PathEntryKind, SafeFilesystem

_TOKEN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_LEFTOVER = re.compile(r"\{\{[^{}]*\}\}|\{\{")
_SECRET_NAME = re.compile(r"API_KEY|SECRET|TOKEN|PASSWORD", re.IGNORECASE)
_MAX_DEPTH = 64


def _invalid(message: str, *, path: Path | None = None, **details: str) -> InvalidDistributionError:
    return InvalidDistributionError(message, path=path, details=details)


def reject_secret_variable(name: str) -> None:
    """Refuse credential-like variable names before they can be rendered."""

    if _SECRET_NAME.search(name):
        raise _invalid(
            "skill variables must not include API_KEY, SECRET, TOKEN, or PASSWORD names",
            name=name,
        )


def _list_entries(
    filesystem: SafeFilesystem, relative: str
) -> tuple[tuple[str, PathEntryKind], ...]:
    """List one directory through the held SafeFilesystem descriptor."""

    return filesystem.list_directory(relative)


def _join_relative(prefix: str, name: str) -> str:
    return name if not prefix else f"{prefix}/{name}"


def _inventory(filesystem: SafeFilesystem) -> tuple[BundleFile, ...]:
    collected: list[BundleFile] = []

    def walk(prefix: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise _invalid("skill bundle exceeded the maximum directory depth", path=filesystem.base)
        for name, kind in _list_entries(filesystem, prefix):
            relative = _join_relative(prefix, name)
            display = filesystem.base / relative
            if kind in {PathEntryKind.SYMLINK, PathEntryKind.REPARSE}:
                raise UnsafePathError(
                    "symbolic links and reparse points are not safe installer paths",
                    path=display,
                )
            if kind is PathEntryKind.DIRECTORY:
                walk(relative, depth + 1)
                continue
            if kind is not PathEntryKind.FILE:
                raise UnsafePathError("skill bundle refuses non-regular entries", path=display)
            collected.append(BundleFile(path=relative, data=filesystem.read_bytes(relative)))

    walk("", 0)
    collected.sort(key=lambda item: item.path.encode("utf-8"))
    return tuple(collected)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_skill_frontmatter(data: bytes, *, path: Path | None = None) -> SkillFrontmatter:
    """Extract optional ``name`` and ``description`` without a document parser."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _invalid("SKILL.md must be valid UTF-8", path=path) from error

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip("\r\n") != "---":
        return SkillFrontmatter(name=None, description=None)

    closing: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip("\r\n") == "---":
            closing = index
            break
    if closing is None:
        raise _invalid("SKILL.md frontmatter is not closed", path=path)

    fields: dict[str, str] = {}
    for raw in "".join(lines[1:closing]).splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise _invalid("SKILL.md frontmatter is invalid", path=path)
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = _unquote(value.strip())
        if key in fields:
            raise _invalid(f"duplicate frontmatter key {key!r}", path=path, field=key)
        fields[key] = value

    if "name" not in fields:
        raise _invalid("SKILL.md frontmatter is missing name", path=path)
    name = fields["name"]
    if not name:
        raise _invalid("SKILL.md frontmatter name is empty", path=path)
    description = fields.get("description")
    if description is not None and not description:
        description = None
    return SkillFrontmatter(name=name, description=description)


def render_text(text: str, variables: Mapping[str, str]) -> str:
    """Replace explicit ``{{NAME}}`` tokens and fail closed on leftovers."""

    for name in variables:
        reject_secret_variable(name)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise _invalid(f"unknown render token {name!r}", token=name)
        return variables[name]

    rendered = _TOKEN.sub(replace, text)
    leftover = _LEFTOVER.search(rendered)
    if leftover is not None:
        raise _invalid("undeclared leftover render token", token=leftover.group(0))
    return rendered


def render_bytes(data: bytes, variables: Mapping[str, str]) -> bytes:
    """Render UTF-8 text; copy non-text bytes unchanged and unrendered."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return render_text(text, variables).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SkillFrontmatter:
    """Minimum Agent Skills identity parsed from ``SKILL.md``."""

    name: str | None
    description: str | None

    def to_dict(self) -> JsonObject:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True, slots=True)
class BundleFile:
    """One regular file inventoried below the bundle root."""

    path: str
    data: bytes

    def to_dict(self) -> JsonObject:
        return {"path": self.path, "size": len(self.data)}


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """Immutable inventory of a validated Agent Skills directory."""

    root: Path
    frontmatter: SkillFrontmatter
    files: tuple[BundleFile, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "files", tuple(self.files))

    def to_dict(self) -> JsonObject:
        return {
            "root": str(self.root),
            "frontmatter": self.frontmatter.to_dict(),
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class RenderedFile:
    """Bundle path and exact bytes after declared-variable substitution."""

    path: str
    data: bytes

    def to_dict(self) -> JsonObject:
        return {"path": self.path, "size": len(self.data)}


def load_bundle(root: Path | str) -> SkillBundle:
    """Inventory every regular file below ``root`` without following links."""

    bundle_root = Path(root)
    try:
        filesystem = SafeFilesystem(bundle_root)
    except UnsafePathError as error:
        raise _invalid("skill bundle could not be opened", path=bundle_root) from error

    try:
        files = _inventory(filesystem)
        canonical = filesystem.base
    finally:
        filesystem.close()

    skill = next((item for item in files if item.path == "SKILL.md"), None)
    if skill is None:
        raise _invalid("skill bundle requires SKILL.md", path=canonical / "SKILL.md")
    frontmatter = parse_skill_frontmatter(skill.data, path=canonical / "SKILL.md")
    return SkillBundle(root=canonical, frontmatter=frontmatter, files=files)


def render_bundle(
    bundle: SkillBundle, variables: Mapping[str, str] | None = None
) -> tuple[RenderedFile, ...]:
    """Render declared tokens in UTF-8 files and copy any other bytes as-is."""

    mapping = variables or {}
    for name in mapping:
        reject_secret_variable(name)
    rendered: list[RenderedFile] = []
    for item in bundle.files:
        if item.path == "SKILL.md":
            try:
                text = item.data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise _invalid("SKILL.md must be valid UTF-8", path=bundle.root / item.path) from error
            data = render_text(text, mapping).encode("utf-8")
        else:
            data = render_bytes(item.data, mapping)
        rendered.append(RenderedFile(path=item.path, data=data))
    return tuple(rendered)


__all__ = [
    "BundleFile",
    "RenderedFile",
    "SkillBundle",
    "SkillFrontmatter",
    "load_bundle",
    "parse_skill_frontmatter",
    "reject_secret_variable",
    "render_bundle",
    "render_bytes",
    "render_text",
]
