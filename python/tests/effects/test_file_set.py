from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from minimalist_installer import (
    EffectContext,
    FileSetProvider,
    GreenfieldConflictError,
    Operation,
    PreparedEffect,
    define_installer,
)
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.effects import (
    FileDecision,
    ReconcileFileSetEffect,
    classify_file,
    sha256_bytes,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RecordingCheckpointWriter:
    def __init__(
        self,
        filesystem: SafeFilesystem,
        *,
        checkpoints: Mapping[str, object] | None = None,
        blobs: Mapping[str, bytes] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.filesystem = filesystem
        self.checkpoints = dict(checkpoints or {})
        self.blobs = dict(blobs or {})
        self.events = events if events is not None else []

    def write(self, checkpoint: str, state: object) -> None:
        self.events.append(f"checkpoint:{checkpoint}:{state['phase']}")
        self.checkpoints[checkpoint] = state

    def snapshot(self) -> Mapping[str, object]:
        return dict(self.checkpoints)

    def read(self, checkpoint: str) -> object:
        return self.checkpoints.get(checkpoint)

    def write_blob(self, data: bytes) -> str:
        digest = _digest(data)
        self.events.append(f"blob:{digest}")
        self.blobs[digest] = data
        return digest

    def read_blob(self, digest: str) -> bytes:
        self.events.append(f"read-blob:{digest}")
        return self.blobs[digest]


class RecordingFilesystem:
    def __init__(self, filesystem: SafeFilesystem, events: list[str]) -> None:
        self._filesystem = filesystem
        self.events = events
        self.base = filesystem.base

    @property
    def closed(self) -> bool:
        return self._filesystem.closed

    def read_bytes(self, relative: str) -> bytes:
        return self._filesystem.read_bytes(relative)

    def directory_exists(self, relative: str) -> bool:
        return self._filesystem.directory_exists(relative)

    def ensure_directory(self, relative: str) -> None:
        self.events.append(f"mkdir:{relative}")
        self._filesystem.ensure_directory(relative)

    def atomic_write_bytes(
        self, relative: str, data: bytes, *, mode: int = 0o600
    ) -> None:
        self.events.append(f"write:{relative}")
        self._filesystem.atomic_write_bytes(relative, data, mode=mode)

    def unlink(self, relative: str, *, missing_ok: bool = False) -> bool:
        self.events.append(f"unlink:{relative}")
        return self._filesystem.unlink(relative, missing_ok=missing_ok)

    def rmdir_empty(self, relative: str, *, missing_ok: bool = False) -> bool:
        self.events.append(f"rmdir:{relative}")
        return self._filesystem.rmdir_empty(relative, missing_ok=missing_ok)


def _context(
    root: Path,
    filesystem: object,
    *,
    operation: Operation = Operation.INSTALL,
) -> EffectContext:
    return EffectContext(
        base_path=root,
        manifest_dir=root / ".minimalist-installer",
        operation=operation,
        transaction_id="tx-1",
        effect_id="files",
        filesystem=filesystem,
    )


def _prepare(
    effect: ReconcileFileSetEffect,
    filesystem: object,
    root: Path,
    desired: list[dict[str, object]],
    previous: object = None,
    **args: object,
) -> PreparedEffect:
    return effect.prepare(
        {"desired": desired, **args},
        previous,
        _context(root, filesystem),
    )


def _conformance_cases() -> list[dict[str, object]]:
    fixture = (
        Path(__file__).parents[3] / "spec/conformance/file-set.json"
    )
    return json.loads(fixture.read_text(encoding="utf-8"))["classification"]


@pytest.mark.parametrize(
    "case",
    _conformance_cases(),
    ids=lambda case: str(case["name"]),
)
def test_three_hash_classification_matches_conformance(
    case: dict[str, object],
) -> None:
    hashes = {"desired", "installed", "disk"}
    encoded = {
        name: _digest(str(case[name]).encode()) if case.get(name) is not None else None
        for name in hashes
    }

    decision = classify_file(
        desired_hash=encoded["desired"],
        installed_hash=encoded["installed"],
        disk_hash=encoded["disk"],
        adopt_identical=bool(case.get("adopt_identical", False)),
    )

    assert decision is FileDecision(str(case["decision"]))


def test_sha256_hashes_exact_bytes_not_decoded_text() -> None:
    assert sha256_bytes(b"\xff\x00\r\n") == _digest(b"\xff\x00\r\n")
    assert sha256_bytes(b"\r\n") != sha256_bytes(b"\n")
    with pytest.raises(TypeError, match="bytes"):
        sha256_bytes("not bytes")  # type: ignore[arg-type]


def test_greenfield_prepare_is_read_only_and_apply_writes_exact_utf8_bytes(
    tmp_path: Path,
) -> None:
    effect = ReconcileFileSetEffect()
    events: list[str] = []
    with SafeFilesystem(tmp_path) as safe:
        filesystem = RecordingFilesystem(safe, events)
        prepared = _prepare(
            effect,
            filesystem,
            tmp_path,
            [
                {"path": r"nested\second.txt", "content": "olá\r\n"},
                {"path": "first.bin", "content": "\u0000\ud7ff"},
            ],
        )

        assert list(tmp_path.iterdir()) == []
        assert prepared.resources == (f"path:{tmp_path.as_posix()}",)
        assert prepared.before_state == {
            "version": 1,
            "files": (
                {
                    "path": "first.bin",
                    "installed_hash": _digest("\u0000\ud7ff".encode("utf-8")),
                },
                {
                    "path": "nested/second.txt",
                    "installed_hash": _digest("olá\r\n".encode("utf-8")),
                },
            ),
            "created_parents": ("nested",),
        }
        assert [item["decision"] for item in prepared.payload["decisions"]] == [
            "write",
            "write",
        ]

        writer = RecordingCheckpointWriter(filesystem, events=events)
        result = effect.apply(prepared, writer)

        assert safe.read_bytes("first.bin") == "\u0000\ud7ff".encode("utf-8")
        assert safe.read_bytes("nested/second.txt") == "olá\r\n".encode("utf-8")
        serialized = prepared.to_dict()
        assert result == {
            "files": serialized["before_state"]["files"],
            "decisions": serialized["payload"]["decisions"],
        }
        assert tuple(writer.checkpoints) == ("apply:000000", "apply:000001")
        assert all(state["phase"] == "done" for state in writer.checkpoints.values())


def test_greenfield_collision_refuses_even_identical_bytes_without_explicit_adoption(
    tmp_path: Path,
) -> None:
    target = tmp_path / "exists.txt"
    target.write_bytes(b"same")
    effect = ReconcileFileSetEffect()
    with SafeFilesystem(tmp_path) as safe:
        with pytest.raises(GreenfieldConflictError) as raised:
            _prepare(
                effect,
                safe,
                tmp_path,
                [{"path": "exists.txt", "content": "same"}],
            )

    assert raised.value.path == target
    assert target.read_bytes() == b"same"
    assert not (tmp_path / ".minimalist-installer").exists()


def test_greenfield_explicit_adoption_requires_identical_bytes_and_never_rewrites(
    tmp_path: Path,
) -> None:
    target = tmp_path / "exists.txt"
    target.write_bytes(b"same")
    effect = ReconcileFileSetEffect()
    events: list[str] = []
    with SafeFilesystem(tmp_path) as safe:
        filesystem = RecordingFilesystem(safe, events)
        prepared = _prepare(
            effect,
            filesystem,
            tmp_path,
            [{"path": "exists.txt", "content": "same"}],
            adopt_identical=True,
        )
        writer = RecordingCheckpointWriter(filesystem, events=events)
        effect.apply(prepared, writer)

        assert prepared.payload["decisions"][0]["decision"] == "adopt"
        assert events == []
        assert writer.checkpoints == {}

        with pytest.raises(GreenfieldConflictError):
            _prepare(
                effect,
                filesystem,
                tmp_path,
                [{"path": "exists.txt", "content": "different"}],
                adopt_identical=True,
            )

    assert target.read_bytes() == b"same"


def test_update_reconciles_owned_modified_orphan_and_missing_files(
    tmp_path: Path,
) -> None:
    previous_bytes = {
        "owned.txt": b"owned-v1",
        "modified.txt": b"modified-v1",
        "drop.txt": b"drop-v1",
        "keep-orphan.txt": b"orphan-v1",
        "missing-desired.txt": b"missing-v1",
        "missing-orphan.txt": b"gone-v1",
        "already-desired.txt": b"old-v1",
    }
    for path, data in previous_bytes.items():
        if not path.startswith("missing-"):
            (tmp_path / path).write_bytes(data)
    (tmp_path / "modified.txt").write_bytes(b"USER desired")
    (tmp_path / "keep-orphan.txt").write_bytes(b"USER orphan")
    (tmp_path / "already-desired.txt").write_bytes(b"already-v2")
    previous = {
        "version": 1,
        "files": [
            {"path": path, "installed_hash": _digest(data)}
            for path, data in reversed(tuple(previous_bytes.items()))
        ],
    }
    desired = [
        {"path": "owned.txt", "content": "owned-v2"},
        {"path": "modified.txt", "content": "modified-v2"},
        {"path": "missing-desired.txt", "content": "missing-v2"},
        {"path": "already-desired.txt", "content": "already-v2"},
        {"path": "new.txt", "content": "new"},
    ]
    effect = ReconcileFileSetEffect()
    events: list[str] = []

    with SafeFilesystem(tmp_path) as safe:
        filesystem = RecordingFilesystem(safe, events)
        prepared = _prepare(effect, filesystem, tmp_path, desired, previous)
        writer = RecordingCheckpointWriter(filesystem, events=events)
        result = effect.apply(prepared, writer)

        assert safe.read_bytes("owned.txt") == b"owned-v2"
        assert safe.read_bytes("modified.txt") == b"USER desired"
        assert safe.read_bytes("missing-desired.txt") == b"missing-v2"
        assert safe.read_bytes("already-desired.txt") == b"already-v2"
        assert safe.read_bytes("new.txt") == b"new"
        with pytest.raises(FileNotFoundError):
            safe.read_bytes("drop.txt")
        assert safe.read_bytes("keep-orphan.txt") == b"USER orphan"

    decisions = {
        entry["path"]: entry["decision"] for entry in result["decisions"]
    }
    assert decisions == {
        "already-desired.txt": "already_desired",
        "drop.txt": "delete",
        "keep-orphan.txt": "preserve_orphan",
        "missing-desired.txt": "write_missing",
        "missing-orphan.txt": "missing",
        "modified.txt": "conflict",
        "new.txt": "write",
        "owned.txt": "replace",
    }
    tracked = {entry["path"]: entry["installed_hash"] for entry in result["files"]}
    assert tracked == {
        "already-desired.txt": _digest(b"already-v2"),
        "missing-desired.txt": _digest(b"missing-v2"),
        "modified.txt": _digest(b"modified-v1"),
        "new.txt": _digest(b"new"),
        "owned.txt": _digest(b"owned-v2"),
    }
    assert tuple(entry["path"] for entry in result["files"]) == tuple(sorted(tracked))


@pytest.mark.parametrize(
    ("desired", "args", "message"),
    [
        ([{"path": "a", "content": b"bytes"}], {}, "content"),
        ([{"path": "a", "content": 1}], {}, "content"),
        ([{"path": "a"}], {}, "content"),
        ([{"path": "a", "content": "x", "extra": True}], {}, "keys"),
        ([{"path": "a", "content": "x"}], {"unknown": True}, "keys"),
        ([{"path": "a/b", "content": "x"}, {"path": r"a\b", "content": "y"}], {}, "duplicate"),
        ([{"path": "A.txt", "content": "x"}, {"path": "a.TXT", "content": "y"}], {}, "colliding"),
        ([{"path": "a", "content": "x"}, {"path": "a/b", "content": "y"}], {}, "colliding"),
    ],
)
def test_prepare_rejects_ambiguous_or_non_json_file_sets_without_mutation(
    tmp_path: Path,
    desired: list[dict[str, object]],
    args: dict[str, object],
    message: str,
) -> None:
    with SafeFilesystem(tmp_path) as safe:
        with pytest.raises((TypeError, ValueError), match=message):
            _prepare(
                ReconcileFileSetEffect(), safe, tmp_path, desired, **args
            )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "previous",
    [
        [],
        {"version": 2, "files": []},
        {"version": 1, "files": [{"path": "a", "installed_hash": "bad"}]},
        {
            "version": 1,
            "files": [
                {"path": "a/b", "installed_hash": "0" * 64},
                {"path": r"a\b", "installed_hash": "0" * 64},
            ],
        },
    ],
)
def test_prepare_rejects_invalid_previous_state_without_mutation(
    tmp_path: Path, previous: object
) -> None:
    with SafeFilesystem(tmp_path) as safe:
        with pytest.raises((TypeError, ValueError)):
            _prepare(
                ReconcileFileSetEffect(),
                safe,
                tmp_path,
                [{"path": "valid", "content": "data"}],
                previous,
            )
    assert list(tmp_path.iterdir()) == []


def test_apply_and_uninstall_revert_are_idempotent_and_preserve_modifications(
    tmp_path: Path,
) -> None:
    effect = ReconcileFileSetEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(
            effect,
            safe,
            tmp_path,
            [
                {"path": "remove.txt", "content": "owned"},
                {"path": "modified.txt", "content": "owned"},
                {"path": "gone.txt", "content": "owned"},
            ],
        )
        apply_writer = RecordingCheckpointWriter(safe)
        first = effect.apply(prepared, apply_writer)
        second = effect.apply(prepared, apply_writer)
        assert first == second

        safe.atomic_write_bytes("modified.txt", b"USER")
        safe.unlink("gone.txt")
        uninstall_writer = RecordingCheckpointWriter(safe)
        context = _context(tmp_path, safe, operation=Operation.UNINSTALL)
        effect.revert(context, prepared.before_state, uninstall_writer)
        effect.revert(context, prepared.before_state, uninstall_writer)

        with pytest.raises(FileNotFoundError):
            safe.read_bytes("remove.txt")
        with pytest.raises(FileNotFoundError):
            safe.read_bytes("gone.txt")
        assert safe.read_bytes("modified.txt") == b"USER"
        assert all(
            state["phase"] == "done"
            for name, state in uninstall_writer.checkpoints.items()
            if name.startswith("uninstall:")
        )


def test_uninstall_removes_only_installer_created_empty_parents(
    tmp_path: Path,
) -> None:
    preexisting = tmp_path / "preexisting"
    preexisting.mkdir()
    effect = ReconcileFileSetEffect()

    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(
            effect,
            safe,
            tmp_path,
            [
                {
                    "path": "preexisting/created/owned.txt",
                    "content": "owned",
                }
            ],
        )
        assert prepared.before_state["created_parents"] == (
            "preexisting/created",
        )
        effect.apply(prepared, RecordingCheckpointWriter(safe))
        effect.revert(
            _context(tmp_path, safe, operation=Operation.UNINSTALL),
            prepared.before_state,
            RecordingCheckpointWriter(safe),
        )

    assert preexisting.is_dir()
    assert list(preexisting.iterdir()) == []
    assert not (preexisting / "created").exists()


def test_apply_validates_manifest_state_before_the_first_mutation(
    tmp_path: Path,
) -> None:
    effect = ReconcileFileSetEffect()
    with SafeFilesystem(tmp_path) as safe:
        valid = _prepare(
            effect,
            safe,
            tmp_path,
            [{"path": "created.txt", "content": "owned"}],
        )
        invalid = PreparedEffect(
            before_state={"version": 1, "files": []},
            payload=valid.payload,
            resources=valid.resources,
            filesystem=safe,
        )

        with pytest.raises(ValueError, match="tracking state"):
            effect.apply(invalid, RecordingCheckpointWriter(safe))

    assert not (tmp_path / "created.txt").exists()


def test_replace_and_delete_back_up_old_bytes_before_each_mutation(
    tmp_path: Path,
) -> None:
    (tmp_path / "replace.txt").write_bytes(b"old replace")
    (tmp_path / "delete.txt").write_bytes(b"old delete")
    previous = {
        "version": 1,
        "files": [
            {"path": "replace.txt", "installed_hash": _digest(b"old replace")},
            {"path": "delete.txt", "installed_hash": _digest(b"old delete")},
        ],
    }
    events: list[str] = []
    effect = ReconcileFileSetEffect()
    with SafeFilesystem(tmp_path) as safe:
        filesystem = RecordingFilesystem(safe, events)
        prepared = _prepare(
            effect,
            filesystem,
            tmp_path,
            [{"path": "replace.txt", "content": "new replace"}],
            previous,
        )
        writer = RecordingCheckpointWriter(filesystem, events=events)
        effect.apply(prepared, writer)

    replace_blob = f"blob:{_digest(b'old replace')}"
    delete_blob = f"blob:{_digest(b'old delete')}"
    assert events.index(delete_blob) < events.index("unlink:delete.txt")
    assert events.index(replace_blob) < events.index("write:replace.txt")
    assert events.index("checkpoint:apply:000000:ready") < events.index(
        "unlink:delete.txt"
    )
    assert events.index("checkpoint:apply:000001:ready") < events.index(
        "write:replace.txt"
    )
    assert writer.blobs == {
        _digest(b"old delete"): b"old delete",
        _digest(b"old replace"): b"old replace",
    }


def test_effect_uses_held_filesystem_authority_without_reopening_root(
    tmp_path: Path,
) -> None:
    original = tmp_path / "install"
    original.mkdir()
    held = tmp_path / "held"
    effect = ReconcileFileSetEffect()

    with SafeFilesystem(original) as safe:
        prepared = _prepare(
            effect,
            safe,
            original,
            [{"path": "skill.txt", "content": "installed"}],
        )
        original.rename(held)
        original.mkdir()
        (original / "sentinel.txt").write_bytes(b"replacement-root")

        effect.apply(prepared, RecordingCheckpointWriter(safe))

    assert (held / "skill.txt").read_bytes() == b"installed"
    assert (original / "sentinel.txt").read_bytes() == b"replacement-root"
    assert not (original / "skill.txt").exists()


def test_define_installer_registers_only_the_file_set_builtin_and_round_trips(
    tmp_path: Path,
) -> None:
    ids = iter(("install-1", "tx-1", "tx-2"))
    installer = define_installer(
        config={
            "consumer": "tests",
            "consumer_version": "1",
            "files": [{"path": r"nested\file.txt", "content": "content"}],
        },
        providers=(FileSetProvider(),),
        manifest_directory="state",
        id_factory=lambda: next(ids),
    )

    installed = installer.install(base_path=tmp_path)
    assert len(installed.applied) == 1
    assert installed.applied[0].startswith("reconcile_file_set:")
    assert (tmp_path / "nested/file.txt").read_bytes() == b"content"
    manifest = json.loads((tmp_path / "state/manifest.json").read_text("utf-8"))
    assert manifest["effects"][0]["before_state"]["files"][0]["path"] == (
        "nested/file.txt"
    )

    installer.uninstall(base_path=tmp_path)
    assert not (tmp_path / "nested/file.txt").exists()


def test_update_cleans_missing_orphan_parents_without_reclaiming_other_paths(
    tmp_path: Path,
) -> None:
    preexisting = tmp_path / "preexisting-empty"
    preexisting.mkdir()
    files = [
        {"path": "owned-dropped/deep/file.txt", "content": "drop"},
        {"path": "owned-modified/deep/file.txt", "content": "modify"},
        {"path": "preexisting-empty/file.txt", "content": "preexisting"},
        {"path": "owned-desired/deep/file.txt", "content": "desired-v1"},
    ]
    ids = iter(("install-1", "tx-1", "tx-2"))
    installer = define_installer(
        config={
            "consumer": "tests",
            "consumer_version": "1",
            "files": files,
        },
        providers=(FileSetProvider(),),
        manifest_directory="state",
        id_factory=lambda: next(ids),
    )
    installer.install(base_path=tmp_path)

    (tmp_path / "owned-dropped/deep/file.txt").unlink()
    (tmp_path / "preexisting-empty/file.txt").unlink()
    (tmp_path / "owned-modified/deep/file.txt").write_bytes(b"USER")
    (tmp_path / "owned-desired/deep/file.txt").unlink()
    (tmp_path / "owned-desired/deep").rmdir()
    (tmp_path / "owned-desired").rmdir()
    files[:] = [
        {"path": "owned-desired/deep/file.txt", "content": "desired-v2"}
    ]

    installer.update(base_path=tmp_path)

    assert not (tmp_path / "owned-dropped").exists()
    assert (tmp_path / "owned-modified/deep/file.txt").read_bytes() == b"USER"
    assert preexisting.is_dir()
    assert list(preexisting.iterdir()) == []
    assert (tmp_path / "owned-desired/deep/file.txt").read_bytes() == b"desired-v2"
