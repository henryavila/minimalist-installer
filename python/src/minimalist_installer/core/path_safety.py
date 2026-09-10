"""Fail-closed filesystem access rooted at a trusted directory.

The installer stores paths supplied by consumers and persisted manifests.  This
module keeps those paths relative, refuses link-like entries, and performs
mutations through the strongest standard-library primitives exposed by the
current platform.  POSIX mutations are anchored to directory file descriptors;
Windows uses component-by-component reparse-point inspection and same-directory
atomic replacement.
"""

from __future__ import annotations

import errno
import json
import ntpath
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Protocol

from .errors import UnsafePathError

_REPARSE_POINT_ATTRIBUTE = 0x0400
_TEMP_PREFIX = ".minimalist-installer-"
_TEMP_SUFFIX = ".tmp"
_WINDOWS_RESERVED_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_DIRECTORY_FSYNC_UNSUPPORTED = {
    errno.EBADF,
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


class PathEntryKind(StrEnum):
    """Security-relevant classification of an ``lstat`` result."""

    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    REPARSE = "reparse"
    OTHER = "other"


def classify_entry(entry: object, *, platform_name: str | None = None) -> PathEntryKind:
    """Classify an entry without following it.

    ``platform_name`` is deliberately injectable so reparse-point handling can
    be verified on non-Windows CI hosts.
    """

    platform = platform_name or sys.platform
    mode = int(getattr(entry, "st_mode"))
    if platform == "win32" and (
        int(getattr(entry, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE
    ):
        return PathEntryKind.REPARSE
    if stat.S_ISLNK(mode):
        return PathEntryKind.SYMLINK
    if stat.S_ISREG(mode):
        return PathEntryKind.FILE
    if stat.S_ISDIR(mode):
        return PathEntryKind.DIRECTORY
    return PathEntryKind.OTHER


def directory_fsync_supported(*, platform_name: str | None = None) -> bool:
    """Return whether directory descriptors can be flushed on this platform."""

    platform = platform_name or sys.platform
    return platform != "win32" and os.name == "posix" and hasattr(os, "O_DIRECTORY")


def _unsafe(message: str, path: Path | str | None = None) -> UnsafePathError:
    return UnsafePathError(message, path=Path(path) if path is not None else None)


def _lexical_parts(base: Path, relative: os.PathLike[str] | str) -> tuple[str, ...]:
    try:
        raw = os.fspath(relative)
    except TypeError as error:
        raise _unsafe("path must be a text path relative to the trusted base") from error
    if not isinstance(raw, str):
        raise _unsafe("byte paths are not accepted")
    if not raw or "\x00" in raw:
        raise _unsafe("path must name an entry below the trusted base")
    if os.path.isabs(raw) or ntpath.isabs(raw) or ntpath.splitdrive(raw)[0]:
        raise _unsafe("absolute paths are not accepted", raw)

    portable = raw.replace("\\", "/")
    parts = tuple(portable.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise _unsafe("empty, current-directory, and parent path components are unsafe", raw)
    for part in parts:
        if (
            part.endswith((".", " "))
            or any(character in _WINDOWS_RESERVED_CHARACTERS for character in part)
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise _unsafe("path component is not portable to a canonical Windows name", raw)

    candidate = Path(os.path.abspath(os.path.join(os.fspath(base), *parts)))
    try:
        contained = os.path.commonpath((os.fspath(base), os.fspath(candidate))) == os.fspath(base)
    except ValueError:
        contained = False
    if not contained:
        raise _unsafe("path escapes the trusted base", candidate)
    return parts


def _raise_if_link_like(kind: PathEntryKind, path: Path) -> None:
    if kind in {PathEntryKind.SYMLINK, PathEntryKind.REPARSE}:
        raise _unsafe("symbolic links and reparse points are not safe installer paths", path)


def _write_all(file_descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(file_descriptor, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "short write while persisting installer data")
        remaining = remaining[written:]


def _read_all(file_descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_descriptor, 128 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _fsync_directory_descriptor(file_descriptor: int) -> None:
    try:
        os.fsync(file_descriptor)
    except OSError as error:
        if error.errno not in _DIRECTORY_FSYNC_UNSUPPORTED:
            raise


class _Backend(Protocol):
    uses_dir_fd: bool

    def read_bytes(self, parts: tuple[str, ...]) -> bytes: ...

    def atomic_write_bytes(
        self,
        parts: tuple[str, ...],
        data: bytes,
        *,
        mode: int,
    ) -> None: ...

    def unlink(self, parts: tuple[str, ...], *, missing_ok: bool) -> bool: ...

    def prune_empty_parents(self, parts: tuple[str, ...]) -> tuple[Path, ...]: ...


class _PosixBackend:
    uses_dir_fd = True

    def __init__(self, base: Path, platform_name: str) -> None:
        required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.rename)
        available = (
            os.name == "posix"
            and hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
            and all(function in os.supports_dir_fd for function in required_dir_fd)
            and os.stat in os.supports_follow_symlinks
        )
        if not available:
            raise _unsafe(
                f"safe filesystem backend is unavailable for {platform_name}",
                base,
            )
        self.base = base
        self.platform_name = platform_name

    @property
    def _directory_flags(self) -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def _open_base(self) -> int:
        try:
            return os.open(self.base, self._directory_flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe("trusted base became a link or ceased to be a directory", self.base) from error
            raise

    def _open_directory_at(self, parent_fd: int, name: str, display: Path) -> int:
        try:
            return os.open(name, self._directory_flags, dir_fd=parent_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe("path component is link-like or not a directory", display) from error
            raise

    @contextmanager
    def _open_parent(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> Iterator[tuple[int, str]]:
        descriptors: list[int] = []
        current_fd = self._open_base()
        descriptors.append(current_fd)
        walked = self.base
        try:
            for component in parts[:-1]:
                walked /= component
                try:
                    child_fd = self._open_directory_at(current_fd, component, walked)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    if directory_fsync_supported(platform_name=self.platform_name):
                        _fsync_directory_descriptor(current_fd)
                    child_fd = self._open_directory_at(current_fd, component, walked)
                descriptors.append(child_fd)
                current_fd = child_fd
            yield current_fd, parts[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _entry_kind(self, parent_fd: int, leaf: str, display: Path) -> PathEntryKind | None:
        try:
            entry = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        kind = classify_entry(entry, platform_name=self.platform_name)
        _raise_if_link_like(kind, display)
        return kind

    def read_bytes(self, parts: tuple[str, ...]) -> bytes:
        display = self.base.joinpath(*parts)
        with self._open_parent(parts, create=False) as (parent_fd, leaf):
            kind = self._entry_kind(parent_fd, leaf, display)
            if kind is None:
                raise FileNotFoundError(display)
            if kind is not PathEntryKind.FILE:
                raise _unsafe("safe reads require a regular file", display)
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(leaf, flags, dir_fd=parent_fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise _unsafe("file became link-like during safe read", display) from error
                raise
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise _unsafe("safe reads require a regular file", display)
                return _read_all(descriptor)
            finally:
                os.close(descriptor)

    def _exclusive_temp(self, parent_fd: int, mode: int) -> tuple[int, str]:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        for _ in range(128):
            name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}{_TEMP_SUFFIX}"
            try:
                return os.open(name, flags, mode, dir_fd=parent_fd), name
            except FileExistsError:
                continue
        raise _unsafe("could not create an exclusive temporary file", self.base)

    def atomic_write_bytes(
        self,
        parts: tuple[str, ...],
        data: bytes,
        *,
        mode: int,
    ) -> None:
        display = self.base.joinpath(*parts)
        with self._open_parent(parts, create=True) as (parent_fd, leaf):
            kind = self._entry_kind(parent_fd, leaf, display)
            if kind is not None and kind is not PathEntryKind.FILE:
                raise _unsafe("atomic writes can only replace regular files", display)

            descriptor, temp_name = self._exclusive_temp(parent_fd, mode)
            temp_exists = True
            try:
                try:
                    _write_all(descriptor, data)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

                # A final no-follow inspection narrows the leaf replacement race.
                kind = self._entry_kind(parent_fd, leaf, display)
                if kind is not None and kind is not PathEntryKind.FILE:
                    raise _unsafe("atomic writes can only replace regular files", display)
                os.replace(
                    temp_name,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temp_exists = False
                if directory_fsync_supported(platform_name=self.platform_name):
                    _fsync_directory_descriptor(parent_fd)
            finally:
                if temp_exists:
                    try:
                        os.unlink(temp_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass

    def unlink(self, parts: tuple[str, ...], *, missing_ok: bool) -> bool:
        display = self.base.joinpath(*parts)
        try:
            with self._open_parent(parts, create=False) as (parent_fd, leaf):
                kind = self._entry_kind(parent_fd, leaf, display)
                if kind is None:
                    if missing_ok:
                        return False
                    raise FileNotFoundError(display)
                if kind is not PathEntryKind.FILE:
                    raise _unsafe("safe unlink only removes regular files", display)
                os.unlink(leaf, dir_fd=parent_fd)
                if directory_fsync_supported(platform_name=self.platform_name):
                    _fsync_directory_descriptor(parent_fd)
                return True
        except FileNotFoundError:
            if missing_ok:
                return False
            raise

    def prune_empty_parents(self, parts: tuple[str, ...]) -> tuple[Path, ...]:
        pruned: list[Path] = []
        for length in range(len(parts) - 1, 0, -1):
            directory_parts = parts[:length]
            display = self.base.joinpath(*directory_parts)
            try:
                with self._open_parent(directory_parts, create=False) as (parent_fd, leaf):
                    kind = self._entry_kind(parent_fd, leaf, display)
                    if kind is None:
                        continue
                    if kind is not PathEntryKind.DIRECTORY:
                        raise _unsafe("only real empty directories can be pruned", display)
                    try:
                        os.rmdir(leaf, dir_fd=parent_fd)
                    except OSError as error:
                        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                            break
                        if error.errno == errno.ENOENT:
                            continue
                        raise
                    if directory_fsync_supported(platform_name=self.platform_name):
                        _fsync_directory_descriptor(parent_fd)
                    pruned.append(Path(*directory_parts))
            except FileNotFoundError:
                continue
        return tuple(pruned)


class SafeFilesystem:
    """Safe, synchronous filesystem operations bounded by one trusted base."""

    def __init__(
        self,
        base: os.PathLike[str] | str,
        *,
        platform_name: str | None = None,
    ) -> None:
        platform = platform_name or sys.platform
        try:
            raw_base = os.fspath(base)
        except (TypeError, ValueError) as error:
            raise _unsafe("trusted base must be a valid filesystem path") from error
        if not isinstance(raw_base, str) or not raw_base or "\x00" in raw_base:
            raise _unsafe("trusted base must be a non-empty text path")
        lexical_base = Path(os.path.abspath(raw_base))
        try:
            base_path = lexical_base.resolve(strict=True)
            base_entry = os.lstat(base_path)
        except (OSError, RuntimeError, ValueError) as error:
            raise _unsafe("trusted base must be an existing real directory", lexical_base) from error
        base_kind = classify_entry(base_entry, platform_name=platform)
        _raise_if_link_like(base_kind, base_path)
        if base_kind is not PathEntryKind.DIRECTORY:
            raise _unsafe("trusted base must be an existing real directory", base_path)

        self.base = base_path
        if platform == "win32":
            raise _unsafe(
                f"safe filesystem backend is unavailable for {platform}",
                base_path,
            )
        elif platform.startswith(
            ("linux", "darwin", "freebsd", "openbsd", "netbsd", "aix", "cygwin")
        ):
            self._backend = _PosixBackend(base_path, platform)
        else:
            raise _unsafe(
                f"safe filesystem backend is unavailable for {platform}",
                base_path,
            )

    @property
    def uses_dir_fd(self) -> bool:
        """Whether operations are anchored to directory descriptors."""

        return self._backend.uses_dir_fd

    def _parts(self, relative: os.PathLike[str] | str) -> tuple[str, ...]:
        return _lexical_parts(self.base, relative)

    def read_bytes(self, relative: os.PathLike[str] | str) -> bytes:
        """Read a regular file without following link-like path entries."""

        return self._backend.read_bytes(self._parts(relative))

    def read_json(self, relative: os.PathLike[str] | str) -> Any:
        """Read UTF-8 JSON through the safe byte reader."""

        return json.loads(self.read_bytes(relative).decode("utf-8"))

    def atomic_write_bytes(
        self,
        relative: os.PathLike[str] | str,
        data: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        """Atomically replace a regular file after flushing its new bytes."""

        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        self._backend.atomic_write_bytes(self._parts(relative), data, mode=mode)

    def atomic_write_json(
        self,
        relative: os.PathLike[str] | str,
        value: Any,
        *,
        mode: int = 0o600,
    ) -> None:
        """Serialize deterministic UTF-8 JSON and replace it atomically."""

        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        self.atomic_write_bytes(relative, encoded, mode=mode)

    def unlink(
        self,
        relative: os.PathLike[str] | str,
        *,
        missing_ok: bool = False,
    ) -> bool:
        """Unlink a regular file, refusing links, reparses, and directories."""

        return self._backend.unlink(self._parts(relative), missing_ok=missing_ok)

    def prune_empty_parents(
        self,
        relative_leaf: os.PathLike[str] | str,
    ) -> tuple[Path, ...]:
        """Prune empty parents of a leaf, stopping strictly before ``base``."""

        return self._backend.prune_empty_parents(self._parts(relative_leaf))


__all__ = [
    "PathEntryKind",
    "SafeFilesystem",
    "classify_entry",
    "directory_fsync_supported",
]
