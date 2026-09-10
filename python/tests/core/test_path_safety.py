from __future__ import annotations

import gc
import os
import stat
import subprocess
import sys
import textwrap
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from minimalist_installer import UnsafePathError
from minimalist_installer.core import path_safety
from minimalist_installer.core.path_safety import (
    PathEntryKind,
    SafeFilesystem,
    classify_entry,
)


def _symlink(target: Path, link: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")


@pytest.mark.parametrize(
    "relative",
    (
        "../outside/sentinel.bin",
        "nested/../../outside/sentinel.bin",
    ),
)
def test_mutation_rejects_parent_traversal_without_touching_sentinel(
    tmp_path: Path,
    relative: str,
) -> None:
    base = tmp_path / "install"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside-original")

    filesystem = SafeFilesystem(base)

    with pytest.raises(UnsafePathError):
        filesystem.atomic_write_bytes(relative, b"attacker-data")

    assert sentinel.read_bytes() == b"outside-original"


def test_mutation_rejects_an_absolute_path_without_touching_sentinel(
    tmp_path: Path,
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"outside-original")

    filesystem = SafeFilesystem(base)

    with pytest.raises(UnsafePathError):
        filesystem.atomic_write_bytes(sentinel, b"attacker-data")

    assert sentinel.read_bytes() == b"outside-original"


def test_sibling_prefix_is_not_treated_as_contained(tmp_path: Path) -> None:
    base = tmp_path / "app"
    sibling = tmp_path / "app-escape"
    base.mkdir()
    sibling.mkdir()
    sentinel = sibling / "sentinel.bin"
    sentinel.write_bytes(b"outside-original")

    filesystem = SafeFilesystem(base)

    with pytest.raises(UnsafePathError):
        filesystem.atomic_write_bytes("../app-escape/sentinel.bin", b"attacker-data")

    assert sentinel.read_bytes() == b"outside-original"


@pytest.mark.parametrize(
    "operation",
    (
        lambda filesystem, path: filesystem.read_bytes(path),
        lambda filesystem, path: filesystem.atomic_write_bytes(path, b"attacker-data"),
        lambda filesystem, path: filesystem.unlink(path),
    ),
)
def test_symlinked_intermediate_is_rejected_without_touching_sentinel(
    tmp_path: Path,
    operation: Callable[[SafeFilesystem, str], object],
) -> None:
    base = tmp_path / "install"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside-original")
    _symlink(outside, base / "linked", target_is_directory=True)

    filesystem = SafeFilesystem(base)

    with pytest.raises(UnsafePathError):
        operation(filesystem, "linked/sentinel.bin")

    assert sentinel.read_bytes() == b"outside-original"


@pytest.mark.parametrize(
    "operation",
    (
        lambda filesystem, path: filesystem.read_bytes(path),
        lambda filesystem, path: filesystem.atomic_write_bytes(path, b"attacker-data"),
        lambda filesystem, path: filesystem.unlink(path),
    ),
)
def test_symlink_leaf_is_rejected_without_touching_sentinel(
    tmp_path: Path,
    operation: Callable[[SafeFilesystem, str], object],
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"outside-original")
    _symlink(sentinel, base / "linked.bin", target_is_directory=False)

    filesystem = SafeFilesystem(base)

    with pytest.raises(UnsafePathError):
        operation(filesystem, "linked.bin")

    assert sentinel.read_bytes() == b"outside-original"
    assert (base / "linked.bin").is_symlink()


def test_windows_reparse_points_have_a_platform_classification_seam() -> None:
    reparse = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=0x0400,
    )
    ordinary = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=0,
    )

    assert classify_entry(reparse, platform_name="win32") is PathEntryKind.REPARSE
    assert classify_entry(ordinary, platform_name="win32") is PathEntryKind.DIRECTORY
    assert classify_entry(reparse, platform_name="linux") is PathEntryKind.DIRECTORY


def test_unknown_platform_fails_closed_when_safe_backend_is_unavailable(
    tmp_path: Path,
) -> None:
    base = tmp_path / "install"
    base.mkdir()

    with pytest.raises(UnsafePathError, match="safe filesystem backend"):
        SafeFilesystem(base, platform_name="unsupported-test-platform")


def test_windows_fails_closed_without_handle_relative_mutations(
    tmp_path: Path,
) -> None:
    base = tmp_path / "install"
    base.mkdir()

    with pytest.raises(UnsafePathError, match="safe filesystem backend"):
        SafeFilesystem(base, platform_name="win32")


def test_backend_status_exposes_windows_release_blocker() -> None:
    status_type = getattr(path_safety, "SafeFilesystemBackendStatus", None)
    status_function = getattr(path_safety, "safe_filesystem_backend_status", None)

    assert status_type is not None
    assert status_function is not None
    assert status_function(platform_name=sys.platform) is status_type.AVAILABLE
    assert status_function(platform_name="win32") is status_type.UNAVAILABLE
    assert status_function(platform_name="unsupported-test-platform") is status_type.UNAVAILABLE


def test_base_must_be_an_existing_real_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(UnsafePathError):
        SafeFilesystem(missing)

    regular_file = tmp_path / "file"
    regular_file.write_bytes(b"not-a-directory")
    with pytest.raises(UnsafePathError):
        SafeFilesystem(regular_file)


def test_empty_and_nul_bases_fail_with_unsafe_path_error(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        SafeFilesystem("")

    with pytest.raises(UnsafePathError):
        SafeFilesystem(f"{tmp_path}\x00invalid")


def test_trusted_base_is_canonicalized_before_descendant_access(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "install").mkdir(parents=True)
    (second / "install").mkdir(parents=True)
    alias = tmp_path / "alias"
    _symlink(first, alias, target_is_directory=True)

    filesystem = SafeFilesystem(alias / "install")
    alias.unlink()
    _symlink(second, alias, target_is_directory=True)

    filesystem.atomic_write_bytes("value.bin", b"anchored")

    assert (first / "install/value.bin").read_bytes() == b"anchored"
    assert not (second / "install/value.bin").exists()


@pytest.mark.parametrize("operation", ("read", "write", "unlink", "prune"))
def test_ancestor_retarget_cannot_redirect_an_operation_outside_the_held_base(
    tmp_path: Path,
    operation: str,
) -> None:
    trusted_parent = tmp_path / "trusted-parent"
    trusted_base = trusted_parent / "base"
    external_parent = tmp_path / "external-parent"
    external_base = external_parent / "base"
    trusted_base.mkdir(parents=True)
    external_base.mkdir(parents=True)
    trusted_sentinel = trusted_base / "sentinel.bin"
    external_sentinel = external_base / "sentinel.bin"
    trusted_sentinel.write_bytes(b"trusted-original")
    external_sentinel.write_bytes(b"external-original")
    filesystem = SafeFilesystem(trusted_base)

    if operation == "prune":
        filesystem.atomic_write_bytes("nested/empty/value.bin", b"value")
        filesystem.unlink("nested/empty/value.bin")
        (external_base / "nested/empty").mkdir(parents=True)

    held_parent = tmp_path / "trusted-parent-held"
    trusted_parent.rename(held_parent)
    _symlink(external_parent, trusted_parent, target_is_directory=True)

    if operation == "read":
        assert filesystem.read_bytes("sentinel.bin") == b"trusted-original"
    elif operation == "write":
        filesystem.atomic_write_bytes("sentinel.bin", b"trusted-updated")
        assert (held_parent / "base/sentinel.bin").read_bytes() == b"trusted-updated"
    elif operation == "unlink":
        assert filesystem.unlink("sentinel.bin") is True
        assert not (held_parent / "base/sentinel.bin").exists()
    else:
        assert filesystem.prune_empty_parents("nested/empty/value.bin") == (
            Path("nested/empty"),
            Path("nested"),
        )
        assert not (held_parent / "base/nested").exists()
        assert (external_base / "nested/empty").is_dir()

    assert external_sentinel.read_bytes() == b"external-original"
    filesystem.close()


def test_construction_rejects_resolved_path_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base"
    other = tmp_path / "other"
    base.mkdir()
    other.mkdir()
    real_close = path_safety.os.close
    closed: list[int] = []

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(
        path_safety,
        "_stat_path_identity",
        lambda target: os.stat(other, follow_symlinks=False),
        raising=False,
    )
    monkeypatch.setattr(path_safety.os, "close", recording_close)

    with pytest.raises(UnsafePathError, match="identity changed"):
        SafeFilesystem(base)

    assert len(closed) == 1


def test_construction_validates_opened_base_descriptor_is_a_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base"
    regular_file = tmp_path / "regular-file"
    base.mkdir()
    regular_file.write_bytes(b"not a directory")
    regular_stat = os.stat(regular_file)

    monkeypatch.setattr(path_safety.os, "fstat", lambda descriptor: regular_stat)

    with pytest.raises(UnsafePathError, match="opened descriptor is not a directory"):
        SafeFilesystem(base)


def test_close_and_context_manager_release_base_and_fail_closed(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()

    with SafeFilesystem(base) as filesystem:
        assert filesystem.closed is False
        filesystem.atomic_write_bytes("value.bin", b"value")
        assert filesystem.read_bytes("value.bin") == b"value"

    assert filesystem.closed is True
    filesystem.close()
    operations: tuple[Callable[[], object], ...] = (
        lambda: filesystem.read_bytes("value.bin"),
        lambda: filesystem.atomic_write_bytes("value.bin", b"changed"),
        lambda: filesystem.unlink("value.bin"),
        lambda: filesystem.prune_empty_parents("nested/value.bin"),
    )
    for operation in operations:
        with pytest.raises(UnsafePathError, match="closed"):
            operation()
    assert (base / "value.bin").read_bytes() == b"value"


def test_forgotten_filesystem_finalizer_closes_held_base_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    real_close = path_safety.os.close
    closed: list[int] = []

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(path_safety.os, "close", recording_close)
    filesystem = SafeFilesystem(base)
    reference = weakref.ref(filesystem)

    del filesystem
    gc.collect()

    assert reference() is None
    assert len(closed) == 1


def test_each_operation_closes_its_duplicate_of_the_held_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    filesystem = SafeFilesystem(base)
    filesystem.atomic_write_bytes("value.bin", b"value")
    real_dup = path_safety.os.dup
    real_close = path_safety.os.close
    duplicated: list[int] = []
    closed: list[int] = []

    def recording_dup(descriptor: int) -> int:
        duplicate = real_dup(descriptor)
        duplicated.append(duplicate)
        return duplicate

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(path_safety.os, "dup", recording_dup)
    monkeypatch.setattr(path_safety.os, "close", recording_close)

    for _ in range(10):
        assert filesystem.read_bytes("value.bin") == b"value"

    assert duplicated
    assert all(descriptor in closed for descriptor in duplicated)
    filesystem.close()


@pytest.mark.skipif(
    os.name != "posix"
    or not hasattr(os, "mkfifo")
    or os.mkfifo not in os.supports_dir_fd,
    reason="race regression requires POSIX mkfifo with dir_fd",
)
def test_read_race_to_fifo_fails_without_blocking_or_touching_external_sentinel(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "value.bin").write_bytes(b"regular-file")
    sentinel = tmp_path / "outside-sentinel.bin"
    sentinel.write_bytes(b"outside-original")
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        from minimalist_installer import UnsafePathError
        from minimalist_installer.core import path_safety
        from minimalist_installer.core.path_safety import SafeFilesystem

        base = Path(sys.argv[1])
        sentinel = Path(sys.argv[2])
        filesystem = SafeFilesystem(base)
        real_open = path_safety.os.open
        swapped = False

        def racing_open(path, flags, *args, **kwargs):
            global swapped
            if path == "value.bin" and not swapped and kwargs.get("dir_fd") is not None:
                swapped = True
                parent_fd = kwargs["dir_fd"]
                os.unlink(path, dir_fd=parent_fd)
                os.mkfifo(path, mode=0o600, dir_fd=parent_fd)
            return real_open(path, flags, *args, **kwargs)

        path_safety.os.open = racing_open
        try:
            filesystem.read_bytes("value.bin")
        except UnsafePathError:
            filesystem.close()
            if sentinel.read_bytes() != b"outside-original":
                raise SystemExit(4)
            raise SystemExit(0)
        raise SystemExit(5)
        """
    )

    try:
        completed = subprocess.run(
            [sys.executable, "-c", script, os.fspath(base), os.fspath(sentinel)],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("safe read blocked after a regular file was replaced by a FIFO")

    assert completed.returncode == 0, completed.stderr
    assert sentinel.read_bytes() == b"outside-original"


def test_empty_or_base_target_is_rejected(tmp_path: Path) -> None:
    base = tmp_path / "install"
    base.mkdir()
    filesystem = SafeFilesystem(base)

    for relative in ("", "."):
        with pytest.raises(UnsafePathError):
            filesystem.atomic_write_bytes(relative, b"data")


def test_nul_windows_absolute_and_noncanonical_forms_are_rejected(
    tmp_path: Path,
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    filesystem = SafeFilesystem(base)

    rejected = (
        "name\x00.bin",
        "C:\\outside\\sentinel.bin",
        "\\\\host\\share\\file",
        "file.",
        "file ",
        "file:stream",
        "NUL.txt",
        "nested/COM1.log",
        "nested/C:/file",
        "question?.txt",
    )
    for relative in rejected:
        with pytest.raises(UnsafePathError):
            filesystem.atomic_write_bytes(relative, b"data")


def test_posix_symlink_classification_is_explicit(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"data")
    link = tmp_path / "link"
    _symlink(target, link, target_is_directory=False)

    assert classify_entry(os.lstat(link), platform_name="linux") is PathEntryKind.SYMLINK
