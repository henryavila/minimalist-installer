"""Resolve user and project roots without executing git or following escapes."""

from __future__ import annotations

import os
from pathlib import Path

from ..core.errors import UnsafePathError
from ..core.path_safety import PathEntryKind, classify_entry
from .models import ResolvedScope, Scope

_MAX_WALK = 256


def _unsafe(message: str, path: Path | None = None) -> UnsafePathError:
    return UnsafePathError(message, path=path)


def _lstat_kind(path: Path) -> PathEntryKind | None:
    try:
        entry = os.lstat(path)
    except OSError:
        return None
    return classify_entry(entry)


def _is_filesystem_root(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(path)))
    return absolute.parent == absolute


def _config_is_bare(config_path: Path) -> bool:
    if _lstat_kind(config_path) is not PathEntryKind.FILE:
        return False
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower().replace(" ", "")
        if line == "bare=true":
            return True
    return False


def _looks_like_git_dir(path: Path) -> bool:
    head = _lstat_kind(path / "HEAD")
    objects = _lstat_kind(path / "objects")
    refs = _lstat_kind(path / "refs")
    files = _lstat_kind(path / "files")
    if head is PathEntryKind.FILE and (
        objects is PathEntryKind.DIRECTORY or files is PathEntryKind.DIRECTORY
    ):
        if refs is PathEntryKind.DIRECTORY or _config_is_bare(path / "config"):
            return True
    return _config_is_bare(path / "config")


def _require_gitfile(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise _unsafe("gitfile could not be read", path) from error
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    if not line.lower().startswith("gitdir:"):
        raise _unsafe("unrecognized gitfile", path)


def _refuse_if_bare_gitdir(git_dir: Path, project: Path) -> None:
    if _config_is_bare(git_dir / "config"):
        raise _unsafe("project scope refuses a bare git repository", project)
    if _lstat_kind(git_dir / "HEAD") is not PathEntryKind.FILE:
        raise _unsafe("git directory is missing a HEAD file", git_dir)


def _accept_project(root: Path, *, home: Path) -> Path:
    if _is_filesystem_root(root):
        raise _unsafe("project scope refuses the filesystem root", root)

    canonical = Path(os.path.abspath(os.fspath(root)))
    try:
        resolved = root.resolve()
    except OSError as error:
        raise _unsafe("project root could not be resolved", root) from error

    if _is_filesystem_root(canonical) or _is_filesystem_root(resolved):
        raise _unsafe("project scope refuses the filesystem root", root)

    home_canonical = Path(os.path.abspath(os.fspath(home)))
    try:
        home_resolved = home.resolve()
    except OSError:
        home_resolved = home_canonical
    if canonical == home_canonical or resolved == home_resolved:
        raise _unsafe("project scope refuses home as a project root", root)

    if not os.access(root, os.W_OK):
        raise _unsafe("project destination is unwritable", root)
    return canonical


def resolve_project_root(start: Path, *, home: Path | None = None) -> Path:
    """Walk up from ``start`` to a git work tree without following link escapes."""

    home_path = Path(home) if home is not None else Path.home()
    origin = Path(os.path.abspath(os.fspath(start)))
    current = origin

    for _ in range(_MAX_WALK):
        if _is_filesystem_root(current):
            if current == origin:
                raise _unsafe("project scope refuses the filesystem root", current)
            raise _unsafe("project scope could not resolve a git root", origin)

        kind = _lstat_kind(current)
        if kind is None:
            parent = current.parent
            if parent == current:
                raise _unsafe("project scope could not resolve a git root", origin)
            current = parent
            continue
        if kind in {PathEntryKind.SYMLINK, PathEntryKind.REPARSE}:
            raise _unsafe("project scope refuses symlink traversal", current)
        if kind is PathEntryKind.FILE:
            current = current.parent
            continue
        if kind is not PathEntryKind.DIRECTORY:
            raise _unsafe("project scope refuses a non-directory path", current)

        git = current / ".git"
        git_kind = _lstat_kind(git)
        if git_kind in {PathEntryKind.SYMLINK, PathEntryKind.REPARSE}:
            raise _unsafe("project scope refuses a symbolic .git", git)
        if git_kind is PathEntryKind.FILE:
            _require_gitfile(git)
            return _accept_project(current, home=home_path)
        if git_kind is PathEntryKind.DIRECTORY:
            _refuse_if_bare_gitdir(git, current)
            return _accept_project(current, home=home_path)

        if _looks_like_git_dir(current):
            raise _unsafe("project scope refuses a bare git repository", current)

        parent = current.parent
        if parent == current:
            raise _unsafe("project scope could not resolve a git root", origin)
        current = parent

    raise _unsafe("project scope could not resolve a git root", Path(start))


def resolve_scope(
    scope: Scope | str,
    *,
    start: Path | None = None,
    home: Path | None = None,
) -> ResolvedScope:
    """Return the user home or a validated project git root."""

    kind = scope if isinstance(scope, Scope) else Scope(scope)
    home_path = Path(home) if home is not None else Path.home()
    if kind is Scope.USER:
        return ResolvedScope(scope=kind, root=home_path)
    start_path = Path(start) if start is not None else Path.cwd()
    return ResolvedScope(scope=kind, root=resolve_project_root(start_path, home=home_path))


__all__ = ["resolve_project_root", "resolve_scope"]
