"""Fail-closed filesystem access rooted at a trusted directory.

The installer stores paths supplied by consumers and persisted manifests.  This
module keeps those paths relative, refuses link-like entries, and performs
mutations through the strongest standard-library primitives exposed by the
current platform.  POSIX mutations are anchored to directory file descriptors;
Win32 reparse points can be classified, but mutation fails closed until a fully
handle-relative Windows backend is implemented and verified.
"""

from __future__ import annotations

import errno
import json
import ntpath
import os
import secrets
import stat
import sys
import threading
import weakref
from collections.abc import Mapping
from contextlib import contextmanager
from enum import StrEnum
from math import isfinite
from pathlib import Path
from types import TracebackType
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


class SafeFilesystemBackendStatus(StrEnum):
    """Whether this process has a backend meeting the fail-closed contract."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


_POSIX_PLATFORMS = (
    "linux",
    "darwin",
    "freebsd",
    "openbsd",
    "netbsd",
    "aix",
    "cygwin",
)


def _posix_backend_available() -> bool:
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.rename)
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and hasattr(os, "O_DIRECTORY")
        and all(function in os.supports_dir_fd for function in required_dir_fd)
        and os.stat in os.supports_follow_symlinks
        and os.listdir in os.supports_fd
    )


def safe_filesystem_backend_status(
    *,
    platform_name: str | None = None,
) -> SafeFilesystemBackendStatus:
    """Report safe-backend capability without attempting a mutation.

    Win32 intentionally remains unavailable until the cross-platform release
    work supplies and verifies fully handle-relative traversal and mutation, as
    required by design decision D10.  Reparse classification alone is not a
    safe mutation backend.
    """

    platform = platform_name or sys.platform
    if platform.startswith(_POSIX_PLATFORMS) and _posix_backend_available():
        return SafeFilesystemBackendStatus.AVAILABLE
    return SafeFilesystemBackendStatus.UNAVAILABLE


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


def _stat_path_identity(path: Path) -> os.stat_result:
    return os.stat(path, follow_symlinks=False)


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {token}")


def _parse_finite_json_float(token: str) -> float:
    value = float(token)
    if not isfinite(value):
        raise ValueError(f"non-finite JSON number is not allowed: {token}")
    return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key is not allowed: {key}")
        value[key] = item
    return value


def _strict_json_value(value: object) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            converted[key] = _strict_json_value(item)
        return converted
    if isinstance(value, list | tuple):
        return [_strict_json_value(item) for item in value]
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


class _Backend(Protocol):
    base: Path
    uses_dir_fd: bool

    @property
    def closed(self) -> bool: ...

    def close(self) -> None: ...

    def read_bytes(self, parts: tuple[str, ...]) -> bytes: ...

    def directory_exists(self, parts: tuple[str, ...]) -> bool: ...

    def list_directory(
        self, parts: tuple[str, ...]
    ) -> tuple[tuple[str, PathEntryKind], ...]: ...

    def ensure_directory(self, parts: tuple[str, ...]) -> None: ...

    def atomic_write_bytes(
        self,
        parts: tuple[str, ...],
        data: bytes,
        *,
        mode: int,
    ) -> None: ...

    def unlink(self, parts: tuple[str, ...], *, missing_ok: bool) -> bool: ...

    def rmdir_empty(self, parts: tuple[str, ...], *, missing_ok: bool) -> bool: ...

    def prune_empty_parents(self, parts: tuple[str, ...]) -> tuple[Path, ...]: ...


class _PosixBackend:
    uses_dir_fd = True

    def __init__(self, base: Path, platform_name: str) -> None:
        if not _posix_backend_available():
            raise _unsafe(
                f"safe filesystem backend is unavailable for {platform_name}",
                base,
            )
        self.platform_name = platform_name
        self._lifecycle_lock = threading.Lock()
        try:
            base_descriptor = os.open(base, self._directory_flags)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise _unsafe("trusted base is link-like or not a directory", base) from error
            raise _unsafe("trusted base could not be opened safely", base) from error

        try:
            opened_entry = os.fstat(base_descriptor)
            if not stat.S_ISDIR(opened_entry.st_mode):
                raise _unsafe("opened descriptor is not a directory", base)
            canonical_base = base.resolve(strict=True)
            path_entry = _stat_path_identity(canonical_base)
            path_kind = classify_entry(path_entry, platform_name=platform_name)
            _raise_if_link_like(path_kind, canonical_base)
            if path_kind is not PathEntryKind.DIRECTORY:
                raise _unsafe("trusted base path is not a directory", canonical_base)
            if not os.path.samestat(opened_entry, path_entry):
                raise _unsafe(
                    "trusted base identity changed while it was being opened",
                    canonical_base,
                )
        except UnsafePathError:
            os.close(base_descriptor)
            raise
        except (OSError, RuntimeError, ValueError) as error:
            os.close(base_descriptor)
            raise _unsafe("trusted base could not be validated safely", base) from error

        self.base = canonical_base
        self._base_descriptor = base_descriptor
        self._finalizer = weakref.finalize(self, os.close, base_descriptor)

    @property
    def _directory_flags(self) -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def _open_base(self) -> int:
        with self._lifecycle_lock:
            if not self._finalizer.alive:
                raise _unsafe("safe filesystem is closed", self.base)
            try:
                return os.dup(self._base_descriptor)
            except OSError as error:
                raise _unsafe("held base descriptor is unavailable", self.base) from error

    @property
    def closed(self) -> bool:
        with self._lifecycle_lock:
            return not self._finalizer.alive

    def close(self) -> None:
        with self._lifecycle_lock:
            self._finalizer()

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
            flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | getattr(os, "O_CLOEXEC", 0)
            )
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

    def directory_exists(self, parts: tuple[str, ...]) -> bool:
        display = self.base.joinpath(*parts)
        try:
            with self._open_parent(parts, create=False) as (parent_fd, leaf):
                kind = self._entry_kind(parent_fd, leaf, display)
                if kind is None:
                    return False
                if kind is not PathEntryKind.DIRECTORY:
                    raise _unsafe("expected parent path is not a directory", display)
                return True
        except FileNotFoundError:
            return False

    def list_directory(
        self, parts: tuple[str, ...]
    ) -> tuple[tuple[str, PathEntryKind], ...]:
        """List one real directory through held no-follow descriptors."""

        descriptors = [self._open_base()]
        current_fd = descriptors[0]
        walked = self.base
        try:
            for component in parts:
                walked /= component
                child_fd = self._open_directory_at(current_fd, component, walked)
                descriptors.append(child_fd)
                current_fd = child_fd
            entries: list[tuple[str, PathEntryKind]] = []
            for name in os.listdir(current_fd):
                if not isinstance(name, str):
                    raise _unsafe("directory entry name is not text", walked)
                try:
                    entry = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                entries.append(
                    (name, classify_entry(entry, platform_name=self.platform_name))
                )
            return tuple(
                sorted(
                    entries,
                    key=lambda item: item[0].encode("utf-8", "surrogateescape"),
                )
            )
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def ensure_directory(self, parts: tuple[str, ...]) -> None:
        descriptors = [self._open_base()]
        current_fd = descriptors[0]
        walked = self.base
        try:
            for component in parts:
                walked /= component
                try:
                    child_fd = self._open_directory_at(
                        current_fd, component, walked
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    if directory_fsync_supported(
                        platform_name=self.platform_name
                    ):
                        _fsync_directory_descriptor(current_fd)
                    child_fd = self._open_directory_at(
                        current_fd, component, walked
                    )
                descriptors.append(child_fd)
                current_fd = child_fd
        finally:
            for descriptor in reversed(descriptors):
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

    def rmdir_empty(self, parts: tuple[str, ...], *, missing_ok: bool) -> bool:
        display = self.base.joinpath(*parts)
        try:
            with self._open_parent(parts, create=False) as (parent_fd, leaf):
                kind = self._entry_kind(parent_fd, leaf, display)
                if kind is None:
                    if missing_ok:
                        return False
                    raise FileNotFoundError(display)
                if kind is not PathEntryKind.DIRECTORY:
                    raise _unsafe("safe rmdir requires a real directory", display)
                try:
                    os.rmdir(leaf, dir_fd=parent_fd)
                except OSError as error:
                    if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                        return False
                    raise
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
        if (
            safe_filesystem_backend_status(platform_name=platform)
            is SafeFilesystemBackendStatus.UNAVAILABLE
        ):
            raise _unsafe(
                f"safe filesystem backend is unavailable for {platform}",
                lexical_base,
            )
        self._backend: _Backend = _PosixBackend(lexical_base, platform)
        self.base = self._backend.base

    @property
    def uses_dir_fd(self) -> bool:
        """Whether operations are anchored to directory descriptors."""

        return self._backend.uses_dir_fd

    @property
    def closed(self) -> bool:
        """Whether the held base descriptor has been released."""

        return self._backend.closed

    def close(self) -> None:
        """Release the held base descriptor; repeated calls are harmless."""

        self._backend.close()

    def __enter__(self) -> SafeFilesystem:
        if self.closed:
            raise _unsafe("safe filesystem is closed", self.base)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _parts(self, relative: os.PathLike[str] | str) -> tuple[str, ...]:
        return _lexical_parts(self.base, relative)

    def read_bytes(self, relative: os.PathLike[str] | str) -> bytes:
        """Read a regular file without following link-like path entries."""

        return self._backend.read_bytes(self._parts(relative))

    def directory_exists(self, relative: os.PathLike[str] | str) -> bool:
        """Check for a real directory without following link-like entries."""

        return self._backend.directory_exists(self._parts(relative))

    def list_directory(
        self, relative: os.PathLike[str] | str
    ) -> tuple[tuple[str, PathEntryKind], ...]:
        """List direct children and no-follow kinds below the trusted base."""

        return self._backend.list_directory(self._parts(relative))

    def ensure_directory(self, relative: os.PathLike[str] | str) -> None:
        """Create one directory path safely and idempotently below the base."""

        self._backend.ensure_directory(self._parts(relative))

    def read_json(self, relative: os.PathLike[str] | str) -> Any:
        """Read UTF-8 JSON through the safe byte reader."""

        return json.loads(
            self.read_bytes(relative).decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_json_float,
        )

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
            _strict_json_value(value),
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

    def rmdir_empty(
        self,
        relative: os.PathLike[str] | str,
        *,
        missing_ok: bool = False,
    ) -> bool:
        """Remove exactly one empty real directory below the trusted base."""

        return self._backend.rmdir_empty(self._parts(relative), missing_ok=missing_ok)

    def prune_empty_parents(
        self,
        relative_leaf: os.PathLike[str] | str,
    ) -> tuple[Path, ...]:
        """Prune empty parents of a leaf, stopping strictly before ``base``."""

        return self._backend.prune_empty_parents(self._parts(relative_leaf))


__all__ = [
    "PathEntryKind",
    "SafeFilesystem",
    "SafeFilesystemBackendStatus",
    "classify_entry",
    "directory_fsync_supported",
    "safe_filesystem_backend_status",
]
