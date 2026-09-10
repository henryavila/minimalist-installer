from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from minimalist_installer import UnsafePathError
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
