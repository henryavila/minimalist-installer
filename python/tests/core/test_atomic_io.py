from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from minimalist_installer.core import path_safety
from minimalist_installer.core.path_safety import SafeFilesystem


def test_safe_read_write_and_unlink_round_trip(tmp_path: Path) -> None:
    base = tmp_path / "install"
    base.mkdir()
    filesystem = SafeFilesystem(base)

    filesystem.atomic_write_bytes("nested/value.bin", b"first")

    assert filesystem.read_bytes("nested/value.bin") == b"first"
    assert filesystem.unlink("nested/value.bin") is True
    assert filesystem.unlink("nested/value.bin", missing_ok=True) is False
    assert not (base / "nested/value.bin").exists()


def test_byte_replacement_is_atomic_and_uses_an_exclusive_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    target = base / "value.bin"
    target.write_bytes(b"old")
    filesystem = SafeFilesystem(base)
    real_open = path_safety.os.open
    real_replace = path_safety.os.replace
    temp_flags: list[int] = []
    replacements: list[tuple[Any, Any, int | None, int | None]] = []

    def recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            temp_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    def recording_replace(
        source: Any,
        destination: Any,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        replacements.append((source, destination, src_dir_fd, dst_dir_fd))
        if src_dir_fd is None or dst_dir_fd is None:
            real_replace(source, destination)
        else:
            real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

    monkeypatch.setattr(path_safety.os, "open", recording_open)
    monkeypatch.setattr(path_safety.os, "replace", recording_replace)

    filesystem.atomic_write_bytes("value.bin", b"new")

    assert target.read_bytes() == b"new"
    assert temp_flags
    assert all(flags & os.O_EXCL for flags in temp_flags)
    assert replacements
    if filesystem.uses_dir_fd:
        assert all(
            src_fd == dst_fd and src_fd is not None
            for _, _, src_fd, dst_fd in replacements
        )
    else:
        assert all(
            Path(source).parent == Path(destination).parent
            for source, destination, _, _ in replacements
        )
    assert list(base.glob(".minimalist-installer-*.tmp")) == []


def test_replace_failure_preserves_old_bytes_and_removes_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    target = base / "value.bin"
    target.write_bytes(b"old")
    filesystem = SafeFilesystem(base)

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "injected replace failure")

    monkeypatch.setattr(path_safety.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        filesystem.atomic_write_bytes("value.bin", b"new")

    assert target.read_bytes() == b"old"
    assert list(base.glob(".minimalist-installer-*.tmp")) == []


def test_json_replacement_is_utf8_deterministic_and_round_trips(tmp_path: Path) -> None:
    base = tmp_path / "install"
    base.mkdir()
    filesystem = SafeFilesystem(base)
    value = {"z": [1, True, None], "á": "ação"}

    filesystem.atomic_write_json("state/manifest.json", {"old": True})
    filesystem.atomic_write_json("state/manifest.json", value)

    expected = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    assert filesystem.read_bytes("state/manifest.json") == expected
    assert filesystem.read_json("state/manifest.json") == value


@pytest.mark.parametrize("non_finite", (float("nan"), float("inf"), float("-inf")))
def test_json_rejects_non_finite_numbers_without_replacing_existing_bytes(
    tmp_path: Path,
    non_finite: float,
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    target = base / "manifest.json"
    target.write_bytes(b'{"original":true}\n')
    filesystem = SafeFilesystem(base)

    with pytest.raises(ValueError):
        filesystem.atomic_write_json("manifest.json", {"value": non_finite})

    assert target.read_bytes() == b'{"original":true}\n'


def test_atomic_replace_flushes_file_and_directory_where_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    filesystem = SafeFilesystem(base)
    real_fsync = path_safety.os.fsync
    flushed_kinds: list[str] = []

    def recording_fsync(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        flushed_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(file_descriptor)

    monkeypatch.setattr(path_safety.os, "fsync", recording_fsync)

    filesystem.atomic_write_bytes("value.bin", b"durable")

    assert "file" in flushed_kinds
    if path_safety.directory_fsync_supported():
        assert "directory" in flushed_kinds


def test_unsupported_directory_fsync_does_not_hide_file_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "install"
    base.mkdir()
    filesystem = SafeFilesystem(base)
    real_fsync = path_safety.os.fsync
    file_was_flushed = False

    def directory_fsync_is_unsupported(file_descriptor: int) -> None:
        nonlocal file_was_flushed
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        file_was_flushed = True
        real_fsync(file_descriptor)

    monkeypatch.setattr(path_safety.os, "fsync", directory_fsync_is_unsupported)

    filesystem.atomic_write_bytes("value.bin", b"durable")

    assert file_was_flushed is True
    assert (base / "value.bin").read_bytes() == b"durable"


def test_prune_empty_parents_stops_at_base(tmp_path: Path) -> None:
    base = tmp_path / "install"
    base.mkdir()
    outside_sentinel = tmp_path / "outside.bin"
    outside_sentinel.write_bytes(b"outside-original")
    filesystem = SafeFilesystem(base)
    filesystem.atomic_write_bytes("one/two/value.bin", b"value")
    filesystem.unlink("one/two/value.bin")

    pruned = filesystem.prune_empty_parents("one/two/value.bin")

    assert pruned == (Path("one/two"), Path("one"))
    assert base.is_dir()
    assert outside_sentinel.read_bytes() == b"outside-original"


def test_prune_preserves_non_empty_parent_and_ancestors(tmp_path: Path) -> None:
    base = tmp_path / "install"
    base.mkdir()
    filesystem = SafeFilesystem(base)
    filesystem.atomic_write_bytes("one/two/value.bin", b"value")
    filesystem.atomic_write_bytes("one/keep.bin", b"keep")
    filesystem.unlink("one/two/value.bin")

    pruned = filesystem.prune_empty_parents("one/two/value.bin")

    assert pruned == (Path("one/two"),)
    assert filesystem.read_bytes("one/keep.bin") == b"keep"


def test_prune_rejects_escape_without_touching_outside_directory(tmp_path: Path) -> None:
    base = tmp_path / "install"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside-original")
    filesystem = SafeFilesystem(base)

    with pytest.raises(path_safety.UnsafePathError):
        filesystem.prune_empty_parents("../outside/sentinel.bin")

    assert outside.is_dir()
    assert sentinel.read_bytes() == b"outside-original"
