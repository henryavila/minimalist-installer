"""Deterministic, process-scoped advisory locks for installer resources."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, Self

from .errors import LockTimeoutError

_RESOURCE_NAMESPACE = re.compile(r"^[a-z][a-z0-9._-]*$")
_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN}
_POSIX_PLATFORMS = ("linux", "darwin", "freebsd", "openbsd", "netbsd", "aix", "cygwin")


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

    if canonical_namespace == "path":
        if base_path is not None:
            base = os.fspath(base_path)
            if not isinstance(base, str):
                raise TypeError("byte base paths are not accepted")
            raw_value = os.path.join(base, raw_value)
        canonical_value = Path(os.path.abspath(os.path.normpath(raw_value))).as_posix()
    else:
        canonical_value = raw_value

    identity = f"{canonical_namespace}:{canonical_value}"
    try:
        identity.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("resource identity must be valid UTF-8") from error
    return identity


def _canonical_existing_identity(identity: str) -> str:
    if not isinstance(identity, str):
        raise TypeError("resource identities must be strings")
    namespace, separator, value = identity.partition(":")
    if not separator:
        raise ValueError("resource identity must contain a namespace")
    return canonical_resource_identity(namespace, value)


def canonicalize_resources(resources: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize, deduplicate, and total-sort identities by raw UTF-8."""

    canonical = {_canonical_existing_identity(resource) for resource in resources}
    return tuple(sorted(canonical, key=lambda resource: resource.encode("utf-8")))


def default_lock_root(
    *,
    platform_name: str | None = None,
    temporary_directory: os.PathLike[str] | str | None = None,
    environment: Mapping[str, str] | None = None,
    user_id: int | None = None,
) -> Path:
    """Return a deterministic per-user directory for cross-process locks."""

    platform = platform_name or sys.platform
    temporary = Path(temporary_directory or tempfile.gettempdir())
    if platform == "win32":
        variables = environment if environment is not None else os.environ
        local_data = variables.get("LOCALAPPDATA")
        root = Path(local_data) if local_data else temporary
        return root / "minimalist-installer" / "locks"
    identifier = user_id
    if identifier is None:
        getuid = getattr(os, "getuid", None)
        identifier = int(getuid()) if getuid is not None else 0
    return temporary / f"minimalist-installer-{identifier}" / "locks"


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
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.root = Path(root) if root is not None else default_lock_root()
        self.backend = backend if backend is not None else lock_backend_for_platform()
        self.poll_interval = poll_interval

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        entry = self.root.lstat()
        if not self.root.is_dir() or self.root.is_symlink():
            raise RuntimeError(f"lock root is not a real directory: {self.root}")
        if os.name == "posix" and entry.st_uid != os.getuid():
            raise RuntimeError(
                f"lock root is not owned by the current user: {self.root}"
            )

    def lock_path(self, resource: str) -> Path:
        canonical = canonicalize_resources((resource,))[0]
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.lock"

    def _open_lock_file(self, resource: str) -> BinaryIO:
        path = self.lock_path(resource)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(path, flags, 0o600)
        file = os.fdopen(descriptor, "r+b", buffering=0)
        if os.name == "nt":
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
        return file

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

        if isinstance(timeout, bool) or timeout < 0:
            raise ValueError("timeout must be a non-negative number")
        ordered = canonicalize_resources(resources)
        self._ensure_root()
        deadline = time.monotonic() + timeout
        held: list[_HeldLock] = []
        try:
            for resource in ordered:
                file = self._open_lock_file(resource)
                acquired = False
                try:
                    while True:
                        if self.backend.try_acquire(file):
                            acquired = True
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise LockTimeoutError(
                                f"timed out acquiring resource lock: {resource}",
                                resource=resource,
                                details={
                                    "lock_path": str(self.lock_path(resource)),
                                    "timeout_seconds": timeout,
                                },
                            )
                        time.sleep(min(self.poll_interval, remaining))
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
