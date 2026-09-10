from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from minimalist_installer import LockTimeoutError
from minimalist_installer.core import locks
from minimalist_installer.core.locks import (
    FcntlLockBackend,
    MsvcrtLockBackend,
    ResourceLockLease,
    ResourceLockManager,
    canonical_resource_identity,
    canonicalize_resources,
    default_lock_root,
    lock_backend_for_platform,
)


def test_path_resource_identity_is_absolute_lexical_and_stable(tmp_path: Path) -> None:
    direct = canonical_resource_identity("path", tmp_path / "target")
    aliased = canonical_resource_identity(
        "PATH", "nested/../target", base_path=tmp_path
    )

    assert direct == aliased == f"path:{(tmp_path / 'target').as_posix()}"


def test_non_path_resource_identity_preserves_opaque_utf8_value() -> None:
    assert canonical_resource_identity("json-key", "config:á") == "json-key:config:á"


@pytest.mark.parametrize(
    ("namespace", "value"),
    (("", "value"), ("bad namespace", "value"), ("kind", ""), ("kind", "a\0b")),
)
def test_invalid_resource_identity_is_rejected(namespace: str, value: str) -> None:
    with pytest.raises(ValueError):
        canonical_resource_identity(namespace, value)


def test_path_resource_identity_rejects_nul_in_base_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="NUL"):
        canonical_resource_identity(
            "path",
            "target",
            base_path=f"{tmp_path}\0escape",
        )


def test_opaque_resource_identity_still_rejects_nul_in_supplied_base() -> None:
    with pytest.raises(ValueError, match="NUL"):
        canonical_resource_identity("kind", "value", base_path="ignored\0base")


def test_input_resource_identity_rejects_nul() -> None:
    with pytest.raises(ValueError, match="NUL"):
        canonicalize_resources(("kind:value\0suffix",))


def test_final_canonical_resource_identity_rejects_nul(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(locks.ntpath, "normcase", lambda value: f"{value}\0suffix")

    with pytest.raises(ValueError, match="NUL"):
        canonical_resource_identity("path", r"C:\safe", platform_name="win32")


def test_resources_are_deduplicated_and_sorted_by_raw_utf8_bytes() -> None:
    resources = ("kind:é", "kind:z", "kind:a", "kind:z")

    assert canonicalize_resources(resources) == ("kind:a", "kind:z", "kind:é")


def test_windows_path_identities_apply_normcase_and_deduplicate(tmp_path: Path) -> None:
    first = canonical_resource_identity("path", r"C:\Foo\Skills", platform_name="win32")
    second = canonical_resource_identity("path", "c:/foo/skills", platform_name="win32")

    assert first == second == "path:c:/foo/skills"
    assert canonicalize_resources(
        ("path:C:\\Foo\\Skills", "path:c:/foo/skills"),
        platform_name="win32",
    ) == ("path:c:/foo/skills",)


def test_manager_uses_platform_path_semantics_before_locking(tmp_path: Path) -> None:
    backend = _RecordingBackend()
    manager = ResourceLockManager(
        tmp_path / "locks",
        backend=backend,
        platform_name="win32",
    )

    with manager.acquire(
        ("path:C:\\Foo\\Skills", "path:c:/foo/skills"), timeout=0
    ) as lease:
        assert lease.resources == ("path:c:/foo/skills",)

    assert len(backend.acquired) == 1


def test_default_lock_root_is_deterministic_and_user_scoped(tmp_path: Path) -> None:
    first = default_lock_root(
        platform_name="linux",
        temporary_directory=tmp_path,
        user_id=123,
    )
    second = default_lock_root(
        platform_name="linux",
        temporary_directory=tmp_path,
        user_id=123,
    )

    assert first == second == tmp_path / "minimalist-installer-123" / "locks"
    assert (
        default_lock_root(
            platform_name="win32",
            temporary_directory=tmp_path,
            environment={"LOCALAPPDATA": str(tmp_path / "local")},
        )
        == tmp_path / "local" / "minimalist-installer" / "locks"
    )


@pytest.mark.parametrize(
    "poll_interval",
    (True, False, 0, -0.1, float("nan"), float("inf"), float("-inf"), "0.1", None),
)
def test_poll_interval_must_be_a_positive_finite_real(
    tmp_path: Path, poll_interval: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ResourceLockManager(tmp_path / "locks", poll_interval=poll_interval)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "timeout",
    (True, False, -0.1, float("nan"), float("inf"), float("-inf"), "0.1", None),
)
def test_timeout_must_be_a_nonnegative_finite_real(
    tmp_path: Path, timeout: object
) -> None:
    root = tmp_path / "locks"
    manager = ResourceLockManager(root)

    with pytest.raises((TypeError, ValueError)):
        manager.acquire(("kind:value",), timeout=timeout)  # type: ignore[arg-type]

    assert not root.exists()


class _RecordingBackend:
    def __init__(self) -> None:
        self.acquired: list[int] = []
        self.released: list[int] = []

    def try_acquire(self, file: BinaryIO) -> bool:
        self.acquired.append(file.fileno())
        return True

    def release(self, file: BinaryIO) -> None:
        self.released.append(file.fileno())


def test_manager_acquires_sorted_unique_resources_and_releases_in_reverse(
    tmp_path: Path,
) -> None:
    backend = _RecordingBackend()
    manager = ResourceLockManager(tmp_path / "locks", backend=backend)

    lease = manager.acquire(("kind:é", "kind:a", "kind:a", "kind:z"), timeout=0)
    assert lease.resources == ("kind:a", "kind:z", "kind:é")
    acquired = list(backend.acquired)

    lease.release()

    assert backend.released == list(reversed(acquired))
    lease.release()
    assert backend.released == list(reversed(acquired))


def test_failure_after_os_acquisition_releases_the_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _RecordingBackend()
    manager = ResourceLockManager(tmp_path / "locks", backend=backend)

    def fail_metadata(file: BinaryIO, resource: str) -> None:
        raise RuntimeError("injected metadata failure")

    monkeypatch.setattr(manager, "_write_diagnostic_metadata", fail_metadata)

    with pytest.raises(RuntimeError, match="metadata failure"):
        manager.acquire(("kind:value",), timeout=0)

    assert backend.released == backend.acquired


def test_contention_times_out_and_reports_safe_diagnostics(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    first = ResourceLockManager(root, poll_interval=0.005)
    second = ResourceLockManager(root, poll_interval=0.005)
    held = first.acquire(("path:/shared",), timeout=0.1)
    started = time.monotonic()

    try:
        with pytest.raises(LockTimeoutError) as raised:
            second.acquire(("path:/shared",), timeout=0.03)
    finally:
        held.release()

    assert time.monotonic() - started >= 0.02
    assert raised.value.resource == "path:/shared"
    assert raised.value.details["timeout_seconds"] == 0.03


def test_stale_or_forged_metadata_never_grants_or_denies_a_lock(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    root.mkdir()
    resource = "kind:shared"
    manager = ResourceLockManager(root)
    path = manager.lock_path(resource)
    path.write_text('{"pid":1,"resource":"forged"}\n', encoding="utf-8")

    with manager.acquire((resource,), timeout=0.1):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata["resource"] == resource
        assert metadata["pid"] == os.getpid()


def test_lock_is_released_when_subprocess_exits_without_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    script = textwrap.dedent("""
        import os
        import sys
        from pathlib import Path
        from minimalist_installer.core.locks import ResourceLockManager

        lease = ResourceLockManager(Path(sys.argv[1])).acquire(("kind:process",), timeout=1)
        print("ready", flush=True)
        sys.stdin.readline()
        os._exit(0)
    """)
    environment = dict(os.environ)
    source = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    contender = ResourceLockManager(root, poll_interval=0.005)

    with pytest.raises(LockTimeoutError):
        contender.acquire(("kind:process",), timeout=0.03)

    assert process.stdin is not None
    process.stdin.write("exit\n")
    process.stdin.flush()
    process.wait(timeout=5)

    with contender.acquire(("kind:process",), timeout=0.5):
        pass


def test_threads_contend_and_can_acquire_after_release(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    manager = ResourceLockManager(root, poll_interval=0.005)
    held = manager.acquire(("kind:thread",), timeout=0.1)
    outcomes: list[str] = []

    def contend() -> None:
        try:
            with manager.acquire(("kind:thread",), timeout=0.03):
                outcomes.append("acquired")
        except LockTimeoutError:
            outcomes.append("timeout")

    thread = threading.Thread(target=contend)
    thread.start()
    thread.join(timeout=2)
    held.release()

    assert outcomes == ["timeout"]
    with manager.acquire(("kind:thread",), timeout=0.2):
        pass


class _FakeFcntl:
    LOCK_EX = 1
    LOCK_NB = 2
    LOCK_UN = 4

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.block = False

    def flock(self, descriptor: int, flags: int) -> None:
        self.calls.append((descriptor, flags))
        if self.block and flags == self.LOCK_EX | self.LOCK_NB:
            raise BlockingIOError(errno.EAGAIN, "busy")


def test_fcntl_backend_uses_nonblocking_advisory_lock(tmp_path: Path) -> None:
    module = _FakeFcntl()
    backend = FcntlLockBackend(module=module)

    with (tmp_path / "lock").open("w+b") as file:
        assert backend.try_acquire(file) is True
        module.block = True
        assert backend.try_acquire(file) is False
        backend.release(file)

    assert module.calls[-1][1] == module.LOCK_UN


class _FakeMsvcrt:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.block = False

    def locking(self, descriptor: int, mode: int, count: int) -> None:
        self.calls.append((descriptor, mode, count))
        if self.block and mode == self.LK_NBLCK:
            raise OSError(errno.EACCES, "busy")


def test_msvcrt_backend_uses_nonblocking_one_byte_lock(tmp_path: Path) -> None:
    module = _FakeMsvcrt()
    backend = MsvcrtLockBackend(module=module)

    with (tmp_path / "lock").open("w+b") as file:
        assert backend.try_acquire(file) is True
        module.block = True
        assert backend.try_acquire(file) is False
        backend.release(file)

    assert [call[1:] for call in module.calls] == [
        (module.LK_NBLCK, 1),
        (module.LK_NBLCK, 1),
        (module.LK_UNLCK, 1),
    ]


class _DeterministicWindowsBackend:
    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self.holder_descriptor: int | None = None

    def try_acquire(self, file: BinaryIO) -> bool:
        with self._mutex:
            if self.holder_descriptor is None:
                self.holder_descriptor = file.fileno()
                return True
            return self.holder_descriptor == file.fileno()

    def release(self, file: BinaryIO) -> None:
        with self._mutex:
            if self.holder_descriptor == file.fileno():
                self.holder_descriptor = None


class _WindowsRaceFile:
    def __init__(self, file: BinaryIO, backend: _DeterministicWindowsBackend) -> None:
        self._file = file
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        return getattr(self._file, name)

    def write(self, data: bytes) -> int:
        holder = self._backend.holder_descriptor
        if holder is not None and holder != self.fileno():
            raise OSError(errno.EACCES, "byte zero is locked by holder")
        return self._file.write(data)


def test_windows_contender_retries_when_holder_pauses_after_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "locks"
    backend = _DeterministicWindowsBackend()
    holder = ResourceLockManager(
        root,
        backend=backend,
        platform_name="win32",
        poll_interval=0.005,
    )
    contender = ResourceLockManager(
        root,
        backend=backend,
        platform_name="win32",
        poll_interval=0.005,
    )
    real_fdopen = locks.os.fdopen
    original_metadata_writer = holder._write_diagnostic_metadata
    truncated = threading.Event()
    resume_holder = threading.Event()
    holder_leases: list[ResourceLockLease] = []
    holder_errors: list[Exception] = []

    def wrapping_fdopen(*args: Any, **kwargs: Any) -> _WindowsRaceFile:
        return _WindowsRaceFile(real_fdopen(*args, **kwargs), backend)

    def paused_metadata(file: BinaryIO, resource: str) -> None:
        file.seek(0)
        file.truncate()
        truncated.set()
        if not resume_holder.wait(timeout=2):
            raise TimeoutError("test did not resume metadata writer")
        original_metadata_writer(file, resource)

    def acquire_holder() -> None:
        try:
            holder_leases.append(holder.acquire(("kind:windows-race",), timeout=1))
        except Exception as error:  # noqa: BLE001 - report failures from the thread
            holder_errors.append(error)

    monkeypatch.setattr(locks.os, "name", "nt")
    monkeypatch.setattr(locks.os, "fdopen", wrapping_fdopen)
    monkeypatch.setattr(holder, "_write_diagnostic_metadata", paused_metadata)
    thread = threading.Thread(target=acquire_holder)
    thread.start()
    assert truncated.wait(timeout=2)

    try:
        with pytest.raises(LockTimeoutError):
            contender.acquire(("kind:windows-race",), timeout=0.03)
    finally:
        resume_holder.set()
        thread.join(timeout=2)
        for lease in holder_leases:
            lease.release()

    assert not thread.is_alive()
    assert holder_errors == []


def test_platform_backend_selection_is_explicit_and_testable() -> None:
    fake_fcntl = _FakeFcntl()
    fake_msvcrt = _FakeMsvcrt()

    assert isinstance(
        lock_backend_for_platform("linux", fcntl_module=fake_fcntl),
        FcntlLockBackend,
    )
    assert isinstance(
        lock_backend_for_platform("win32", msvcrt_module=fake_msvcrt),
        MsvcrtLockBackend,
    )
    with pytest.raises(RuntimeError, match="unsupported"):
        lock_backend_for_platform("plan9")
