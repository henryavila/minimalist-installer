"""Deterministic, process-scoped advisory locks for installer resources."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import ntpath
import os
import re
import stat
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from math import isfinite
from numbers import Real
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Protocol, Self, cast

from .errors import LockTimeoutError

_RESOURCE_NAMESPACE = re.compile(r"^[a-z][a-z0-9._-]*$")
_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN}
_POSIX_PLATFORMS = ("linux", "darwin", "freebsd", "openbsd", "netbsd", "aix", "cygwin")


def _finite_real(value: object, label: str, *, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{label} must be a real number")
    converted = float(value)
    if not isfinite(converted) or converted < 0 or (not allow_zero and converted == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be a finite {qualifier} number")
    return converted


class LockBackend(Protocol):
    """Small platform seam around one held-open file lock."""

    def try_acquire(self, file: BinaryIO) -> bool: ...

    def release(self, file: BinaryIO) -> None: ...


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, count: int) -> None: ...


class FcntlLockBackend:
    """POSIX ``flock`` backend whose descriptor remains open while held."""

    def __init__(self, *, module: _FcntlModule | None = None) -> None:
        if module is None:
            imported = importlib.import_module("fcntl")
            module = imported
        self._module = module

    def try_acquire(self, file: BinaryIO) -> bool:
        try:
            self._module.flock(
                file.fileno(),
                self._module.LOCK_EX | self._module.LOCK_NB,
            )
        except (BlockingIOError, OSError) as error:
            if error.errno in _CONTENTION_ERRNOS:
                return False
            raise
        return True

    def release(self, file: BinaryIO) -> None:
        self._module.flock(file.fileno(), self._module.LOCK_UN)


class MsvcrtLockBackend:
    """Windows CRT backend using a non-blocking lock on the first byte."""

    def __init__(self, *, module: _MsvcrtModule | None = None) -> None:
        if module is None:
            imported = importlib.import_module("msvcrt")
            module = imported
        self._module = module

    def try_acquire(self, file: BinaryIO) -> bool:
        file.seek(0)
        try:
            self._module.locking(file.fileno(), self._module.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in _CONTENTION_ERRNOS:
                return False
            raise
        return True

    def release(self, file: BinaryIO) -> None:
        file.seek(0)
        self._module.locking(file.fileno(), self._module.LK_UNLCK, 1)


def lock_backend_for_platform(
    platform_name: str | None = None,
    *,
    fcntl_module: _FcntlModule | None = None,
    msvcrt_module: _MsvcrtModule | None = None,
) -> LockBackend:
    """Select a platform lock backend through an injectable test seam."""

    platform = platform_name or sys.platform
    if platform == "win32":
        return MsvcrtLockBackend(module=msvcrt_module)
    if platform.startswith(_POSIX_PLATFORMS):
        return FcntlLockBackend(module=fcntl_module)
    raise RuntimeError(f"unsupported advisory-lock platform: {platform}")


def canonical_resource_identity(
    namespace: str,
    value: os.PathLike[str] | str,
    *,
    base_path: os.PathLike[str] | str | None = None,
    platform_name: str | None = None,
) -> str:
    """Return one unambiguous UTF-8 resource identity.

    The ``path`` namespace is normalized lexically and made absolute without
    resolving descendants. Other namespaces remain opaque after validation.
    """

    if not isinstance(namespace, str):
        raise TypeError("resource namespace must be text")
    canonical_namespace = namespace.lower()
    if not _RESOURCE_NAMESPACE.fullmatch(canonical_namespace):
        raise ValueError("resource namespace must be a canonical ASCII token")
    try:
        raw_value = os.fspath(value)
    except TypeError as error:
        raise TypeError("resource value must be a text path or string") from error
    if not isinstance(raw_value, str):
        raise TypeError("byte resource values are not accepted")
    if not raw_value or "\x00" in raw_value:
        raise ValueError("resource value must be non-empty text without NUL")

    canonical_base: str | None = None
    if base_path is not None:
        canonical_base = os.fspath(base_path)
        if not isinstance(canonical_base, str):
            raise TypeError("byte base paths are not accepted")
        if "\x00" in canonical_base:
            raise ValueError("resource base path must not contain NUL")

    if canonical_namespace == "path":
        platform = platform_name or sys.platform
        path_module = ntpath if platform == "win32" else os.path
        if canonical_base is not None:
            raw_value = path_module.join(canonical_base, raw_value)
        canonical_value = path_module.normcase(
            path_module.abspath(path_module.normpath(raw_value))
        ).replace("\\", "/")
        if platform != "win32":
            canonical_value = f"/{canonical_value.lstrip('/')}"
    else:
        canonical_value = raw_value

    identity = f"{canonical_namespace}:{canonical_value}"
    if "\x00" in identity:
        raise ValueError("canonical resource identity must not contain NUL")
    try:
        identity.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("resource identity must be valid UTF-8") from error
    return identity


def _canonical_existing_identity(
    identity: str, *, platform_name: str | None = None
) -> str:
    if not isinstance(identity, str):
        raise TypeError("resource identities must be strings")
    namespace, separator, value = identity.partition(":")
    if not separator:
        raise ValueError("resource identity must contain a namespace")
    return canonical_resource_identity(namespace, value, platform_name=platform_name)


def canonicalize_resources(
    resources: Iterable[str], *, platform_name: str | None = None
) -> tuple[str, ...]:
    """Canonicalize, deduplicate, and total-sort identities by raw UTF-8."""

    canonical = {
        _canonical_existing_identity(resource, platform_name=platform_name)
        for resource in resources
    }
    return tuple(sorted(canonical, key=lambda resource: resource.encode("utf-8")))


def default_lock_root(
    *,
    platform_name: str | None = None,
    user_id: int | None = None,
    posix_home_resolver: Callable[[int], os.PathLike[str] | str] | None = None,
    windows_data_resolver: Callable[[], os.PathLike[str] | str] | None = None,
) -> Path:
    """Return a deterministic per-user directory for cross-process locks."""

    platform = platform_name or sys.platform
    if platform == "win32":
        windows_resolver = windows_data_resolver or _windows_user_data_directory
        return (
            _validated_user_directory(windows_resolver())
            / "minimalist-installer"
            / "locks"
        )
    identifier = user_id
    if identifier is None:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise RuntimeError("POSIX user identity is unavailable")
        identifier = int(getuid())
    posix_resolver = posix_home_resolver or _posix_account_home
    return (
        _validated_user_directory(posix_resolver(identifier))
        / ".cache/minimalist-installer/locks"
    )


def _validated_user_directory(value: os.PathLike[str] | str) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise TypeError("byte user directories are not accepted")
    if not raw or "\x00" in raw:
        raise ValueError("OS user directory must be non-empty text without NUL")
    return Path(raw)


def _posix_account_home(user_id: int) -> Path:
    module = importlib.import_module("pwd")
    entry = module.getpwuid(user_id)
    return Path(cast(str, entry.pw_dir))


def _windows_user_data_directory() -> Path:
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    windll = cast(Any, getattr(ctypes, "windll"))  # noqa: B009 - absent on POSIX
    result = int(windll.shell32.SHGetFolderPathW(None, 0x001C, None, 0, buffer))
    if result != 0 or not buffer.value:
        raise OSError(result, "could not resolve Local AppData for the OS user")
    return Path(buffer.value)


class _HeldLock:
    __slots__ = ("backend", "file", "resource")

    def __init__(self, resource: str, file: BinaryIO, backend: LockBackend) -> None:
        self.resource = resource
        self.file = file
        self.backend = backend

    def release(self) -> None:
        try:
            self.backend.release(self.file)
        finally:
            self.file.close()


class _DescriptorFile:
    """Minimal owned file object used to lock a duplicated directory FD."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def fileno(self) -> int:
        return self._descriptor

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1


class ResourceLockLease:
    """A group of locks released in the reverse acquisition order."""

    def __init__(self, resources: tuple[str, ...], held: list[_HeldLock]) -> None:
        self.resources = resources
        self._held = held
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        first_error: BaseException | None = None
        for lock in reversed(self._held):
            try:
                lock.release()
            except BaseException as error:  # noqa: BLE001 - release on cancellation too
                if first_error is None:
                    first_error = error
        self._held.clear()
        if first_error is not None:
            raise first_error

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class ResourceLockManager:
    """Acquire a complete sorted resource set before installer mutation."""

    def __init__(
        self,
        root: os.PathLike[str] | str | None = None,
        *,
        backend: LockBackend | None = None,
        poll_interval: float = 0.05,
        platform_name: str | None = None,
    ) -> None:
        validated_poll_interval = _finite_real(
            poll_interval, "poll_interval", allow_zero=False
        )
        self.platform_name = platform_name or sys.platform
        self.root = (
            Path(root)
            if root is not None
            else default_lock_root(platform_name=self.platform_name)
        )
        self.backend = (
            backend
            if backend is not None
            else lock_backend_for_platform(self.platform_name)
        )
        self.poll_interval = validated_poll_interval
        self._uses_custom_backend = backend is not None
        self._root_descriptor: int | None = None
        self._root_finalizer: Callable[[], object] | None = None
        self._root_lifecycle_lock = threading.Lock()
        self._ensure_root()

    def _ensure_root(self) -> None:
        with self._root_lifecycle_lock:
            if self._root_descriptor is not None:
                return
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            entry = self.root.lstat()
            if not stat.S_ISDIR(entry.st_mode) or self.root.is_symlink():
                raise RuntimeError(f"lock root is not a real directory: {self.root}")
            if os.name == "posix":
                if stat.S_IMODE(entry.st_mode) & 0o022:
                    raise RuntimeError(
                        f"lock root is group or world writable: {self.root}"
                    )
                if entry.st_uid != os.getuid():
                    raise RuntimeError(
                        f"lock root is not owned by the current user: {self.root}"
                    )
            if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
                return

            flags = (
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(self.root, flags)
            opened = os.fstat(descriptor)
            if not os.path.samestat(entry, opened):
                os.close(descriptor)
                raise RuntimeError(
                    f"lock root identity changed while opening: {self.root}"
                )
            self._root_descriptor = descriptor
            self._root_finalizer = weakref.finalize(self, os.close, descriptor)

    def close(self) -> None:
        """Release the held root descriptor; repeated calls are harmless."""

        with self._root_lifecycle_lock:
            if self._root_finalizer is not None:
                self._root_finalizer()
                self._root_finalizer = None
            self._root_descriptor = None

    def lock_path(self, resource: str) -> Path:
        canonical = canonicalize_resources(
            (resource,), platform_name=self.platform_name
        )[0]
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.lock"

    def _open_lock_file(self, resource: str) -> BinaryIO:
        digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()
        filename = f"{digest}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if self._root_descriptor is not None:
            descriptor = os.open(filename, flags, 0o600, dir_fd=self._root_descriptor)
        else:
            descriptor = os.open(self.root / filename, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("resource lock path is not a regular file")
            return os.fdopen(descriptor, "r+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise

    def _open_root_authority(self) -> tuple[BinaryIO, LockBackend] | None:
        if self._uses_custom_backend or self._root_descriptor is None:
            return None
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(".", flags, dir_fd=self._root_descriptor)
        file = cast(BinaryIO, _DescriptorFile(descriptor))
        return file, FcntlLockBackend()

    def _acquire_one(
        self,
        file: BinaryIO,
        backend: LockBackend,
        resource: str,
        *,
        deadline: float,
        timeout_seconds: float,
    ) -> None:
        while True:
            if backend.try_acquire(file):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockTimeoutError(
                    f"timed out acquiring resource lock: {resource}",
                    resource=resource,
                    details={
                        "lock_path": str(self.lock_path(resource)),
                        "timeout_seconds": timeout_seconds,
                    },
                )
            time.sleep(min(self.poll_interval, remaining))

    @staticmethod
    def _write_diagnostic_metadata(file: BinaryIO, resource: str) -> None:
        metadata = {
            "acquired_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "pid": os.getpid(),
            "resource": resource,
        }
        encoded = (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        try:
            file.seek(0)
            file.truncate()
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        except OSError:
            # Metadata never participates in ownership or stale-lock decisions.
            pass

    def acquire(
        self,
        resources: Iterable[str],
        *,
        timeout: float,
    ) -> ResourceLockLease:
        """Acquire every resource within one shared timeout budget."""

        timeout_seconds = _finite_real(timeout, "timeout", allow_zero=True)
        ordered = canonicalize_resources(resources, platform_name=self.platform_name)
        self._ensure_root()
        deadline = time.monotonic() + timeout_seconds
        held: list[_HeldLock] = []
        try:
            authority = self._open_root_authority() if ordered else None
            if authority is not None:
                authority_file, authority_backend = authority
                authority_acquired = False
                try:
                    self._acquire_one(
                        authority_file,
                        authority_backend,
                        ordered[0],
                        deadline=deadline,
                        timeout_seconds=timeout_seconds,
                    )
                    authority_acquired = True
                    held.append(
                        _HeldLock(ordered[0], authority_file, authority_backend)
                    )
                finally:
                    if not authority_acquired:
                        authority_file.close()
            for resource in ordered:
                file = self._open_lock_file(resource)
                acquired = False
                try:
                    self._acquire_one(
                        file,
                        self.backend,
                        resource,
                        deadline=deadline,
                        timeout_seconds=timeout_seconds,
                    )
                    acquired = True
                    held.append(_HeldLock(resource, file, self.backend))
                    self._write_diagnostic_metadata(file, resource)
                finally:
                    if not acquired:
                        file.close()
        except BaseException:
            ResourceLockLease(ordered, held).release()
            raise
        return ResourceLockLease(ordered, held)


__all__ = [
    "FcntlLockBackend",
    "LockBackend",
    "MsvcrtLockBackend",
    "ResourceLockLease",
    "ResourceLockManager",
    "canonical_resource_identity",
    "canonicalize_resources",
    "default_lock_root",
    "lock_backend_for_platform",
]
