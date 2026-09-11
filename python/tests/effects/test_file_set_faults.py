from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from minimalist_installer import EffectContext, Operation
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.effects import ReconcileFileSetEffect


class InjectedFailure(RuntimeError):
    pass


class FaultController:
    def __init__(self, fail_at: int | None) -> None:
        self.fail_at = fail_at
        self.boundaries: list[str] = []

    def call(self, name: str, operation: Callable[[], Any]) -> Any:
        self.boundaries.append(name)
        if self.fail_at == len(self.boundaries):
            raise InjectedFailure(name)
        return operation()


class FaultingFilesystem:
    def __init__(self, filesystem: SafeFilesystem, controller: FaultController) -> None:
        self._filesystem = filesystem
        self.controller = controller
        self.base = filesystem.base

    @property
    def closed(self) -> bool:
        return self._filesystem.closed

    def read_bytes(self, relative: str) -> bytes:
        return self.controller.call(
            f"read:{relative}", lambda: self._filesystem.read_bytes(relative)
        )

    def directory_exists(self, relative: str) -> bool:
        return self.controller.call(
            f"directory-exists:{relative}",
            lambda: self._filesystem.directory_exists(relative),
        )

    def atomic_write_bytes(
        self, relative: str, data: bytes, *, mode: int = 0o600
    ) -> None:
        self.controller.call(
            f"write:{relative}",
            lambda: self._filesystem.atomic_write_bytes(relative, data, mode=mode),
        )

    def unlink(self, relative: str, *, missing_ok: bool = False) -> bool:
        return self.controller.call(
            f"unlink:{relative}",
            lambda: self._filesystem.unlink(relative, missing_ok=missing_ok),
        )

    def rmdir_empty(self, relative: str, *, missing_ok: bool = False) -> bool:
        return self.controller.call(
            f"rmdir:{relative}",
            lambda: self._filesystem.rmdir_empty(
                relative, missing_ok=missing_ok
            ),
        )


class DirectoryCreationFailureFilesystem(FaultingFilesystem):
    def __init__(self, filesystem: SafeFilesystem) -> None:
        super().__init__(filesystem, FaultController(None))
        self.failed = False

    def atomic_write_bytes(
        self, relative: str, data: bytes, *, mode: int = 0o600
    ) -> None:
        if not self.failed:
            self.failed = True
            marker = f"{relative.rsplit('/', 1)[0]}/.directory-created"
            self._filesystem.atomic_write_bytes(marker, b"")
            self._filesystem.unlink(marker)
            raise InjectedFailure("after parent creation")
        self._filesystem.atomic_write_bytes(relative, data, mode=mode)


class DurableMemoryWriter:
    def __init__(
        self,
        filesystem: object,
        controller: FaultController,
        *,
        checkpoints: dict[str, object] | None = None,
        blobs: dict[str, bytes] | None = None,
    ) -> None:
        self.filesystem = filesystem
        self.controller = controller
        self.checkpoints = checkpoints if checkpoints is not None else {}
        self.blobs = blobs if blobs is not None else {}

    def write(self, checkpoint: str, state: object) -> None:
        def persist() -> None:
            self.checkpoints[checkpoint] = state

        self.controller.call(f"checkpoint:{checkpoint}:{state['phase']}", persist)

    def snapshot(self) -> Mapping[str, object]:
        return dict(self.checkpoints)

    def read(self, checkpoint: str) -> object:
        return self.checkpoints.get(checkpoint)

    def write_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()

        def persist() -> str:
            self.blobs[digest] = data
            return digest

        return self.controller.call(f"blob:{digest}", persist)

    def read_blob(self, digest: str) -> bytes:
        return self.controller.call(
            f"read-blob:{digest}", lambda: self.blobs[digest]
        )


def _context(
    root: Path,
    filesystem: object,
    *,
    operation: Operation = Operation.UPDATE,
) -> EffectContext:
    return EffectContext(
        base_path=root,
        manifest_dir=root / "state",
        operation=operation,
        transaction_id="tx",
        effect_id="files",
        filesystem=filesystem,
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _previous() -> dict[str, object]:
    return {
        "version": 1,
        "files": [
            {"path": "create-parent/replace.txt", "installed_hash": _digest(b"v1")},
            {"path": "orphan.txt", "installed_hash": _digest(b"orphan-v1")},
            {"path": "stable.txt", "installed_hash": _digest(b"stable")},
        ],
    }


def _seed(root: Path) -> Path:
    (root / "create-parent").mkdir()
    (root / "create-parent/replace.txt").write_bytes(b"v1")
    (root / "orphan.txt").write_bytes(b"orphan-v1")
    (root / "stable.txt").write_bytes(b"stable")
    (root / "empty-sentinel").mkdir()
    sentinel = root.parent / f"{root.name}-sentinel.txt"
    sentinel.write_bytes(b"outside")
    return sentinel


def _prepare_update(
    effect: ReconcileFileSetEffect,
    root: Path,
    filesystem: object,
):
    return effect.prepare(
        {
            "desired": [
                {"path": "create-parent/replace.txt", "content": "v2"},
                {"path": "new-parent/new.txt", "content": "new"},
                {"path": "stable.txt", "content": "stable"},
                {"path": "empty-sentinel/new-owned.txt", "content": "new"},
            ]
        },
        _previous(),
        _context(root, filesystem),
    )


def _assert_prior(root: Path, sentinel: Path) -> None:
    assert (root / "create-parent/replace.txt").read_bytes() == b"v1"
    assert (root / "orphan.txt").read_bytes() == b"orphan-v1"
    assert not (root / "new-parent/new.txt").exists()
    assert (root / "stable.txt").read_bytes() == b"stable"
    assert (root / "empty-sentinel").is_dir()
    assert list((root / "empty-sentinel").iterdir()) == []
    assert sentinel.read_bytes() == b"outside"


def _apply_boundaries(tmp_path: Path) -> list[str]:
    root = tmp_path / "baseline"
    root.mkdir()
    _seed(root)
    controller = FaultController(None)
    effect = ReconcileFileSetEffect()
    with SafeFilesystem(root) as safe:
        filesystem = FaultingFilesystem(safe, controller)
        prepared = _prepare_update(effect, root, filesystem)
        controller.boundaries.clear()
        effect.apply(prepared, DurableMemoryWriter(filesystem, controller))
    return controller.boundaries


def test_failure_at_every_apply_operation_can_be_reverted_exactly(
    tmp_path: Path,
) -> None:
    boundaries = _apply_boundaries(tmp_path)
    assert boundaries

    for fail_at, boundary in enumerate(boundaries, start=1):
        root = tmp_path / f"apply-{fail_at}"
        root.mkdir()
        sentinel = _seed(root)
        controller = FaultController(None)
        checkpoints: dict[str, object] = {}
        blobs: dict[str, bytes] = {}
        effect = ReconcileFileSetEffect()

        with SafeFilesystem(root) as safe:
            filesystem = FaultingFilesystem(safe, controller)
            prepared = _prepare_update(effect, root, filesystem)
            controller.boundaries.clear()
            controller.fail_at = fail_at
            writer = DurableMemoryWriter(
                filesystem,
                controller,
                checkpoints=checkpoints,
                blobs=blobs,
            )
            with pytest.raises(InjectedFailure, match=boundary):
                effect.apply(prepared, writer)

            recovered_controller = FaultController(None)
            recovered_filesystem = FaultingFilesystem(safe, recovered_controller)
            recovered_writer = DurableMemoryWriter(
                recovered_filesystem,
                recovered_controller,
                checkpoints=checkpoints,
                blobs=blobs,
            )
            recovery_context = _context(root, recovered_filesystem)
            effect.revert(recovery_context, prepared.before_state, recovered_writer)
            effect.revert(recovery_context, prepared.before_state, recovered_writer)

        _assert_prior(root, sentinel)


def _revert_boundaries(tmp_path: Path) -> list[str]:
    root = tmp_path / "revert-baseline"
    root.mkdir()
    _seed(root)
    effect = ReconcileFileSetEffect()
    setup_controller = FaultController(None)
    checkpoints: dict[str, object] = {}
    blobs: dict[str, bytes] = {}
    with SafeFilesystem(root) as safe:
        filesystem = FaultingFilesystem(safe, setup_controller)
        prepared = _prepare_update(effect, root, filesystem)
        effect.apply(
            prepared,
            DurableMemoryWriter(
                filesystem,
                setup_controller,
                checkpoints=checkpoints,
                blobs=blobs,
            ),
        )
        controller = FaultController(None)
        recovery_filesystem = FaultingFilesystem(safe, controller)
        effect.revert(
            _context(root, recovery_filesystem),
            prepared.before_state,
            DurableMemoryWriter(
                recovery_filesystem,
                controller,
                checkpoints=checkpoints,
                blobs=blobs,
            ),
        )
    return controller.boundaries


def test_interrupted_revert_resumes_from_durable_checkpoint_snapshot(
    tmp_path: Path,
) -> None:
    boundaries = _revert_boundaries(tmp_path)
    assert boundaries

    for fail_at, boundary in enumerate(boundaries, start=1):
        root = tmp_path / f"revert-{fail_at}"
        root.mkdir()
        sentinel = _seed(root)
        effect = ReconcileFileSetEffect()
        setup_controller = FaultController(None)
        checkpoints: dict[str, object] = {}
        blobs: dict[str, bytes] = {}

        with SafeFilesystem(root) as safe:
            setup_filesystem = FaultingFilesystem(safe, setup_controller)
            prepared = _prepare_update(effect, root, setup_filesystem)
            effect.apply(
                prepared,
                DurableMemoryWriter(
                    setup_filesystem,
                    setup_controller,
                    checkpoints=checkpoints,
                    blobs=blobs,
                ),
            )
            controller = FaultController(fail_at)
            filesystem = FaultingFilesystem(safe, controller)
            writer = DurableMemoryWriter(
                filesystem,
                controller,
                checkpoints=checkpoints,
                blobs=blobs,
            )
            with pytest.raises(InjectedFailure, match=boundary):
                effect.revert(_context(root, filesystem), prepared.before_state, writer)

            resumed_controller = FaultController(None)
            resumed_filesystem = FaultingFilesystem(safe, resumed_controller)
            resumed_writer = DurableMemoryWriter(
                resumed_filesystem,
                resumed_controller,
                checkpoints=checkpoints,
                blobs=blobs,
            )
            effect.revert(
                _context(root, resumed_filesystem),
                prepared.before_state,
                resumed_writer,
            )
            assert all(
                state["phase"] == "done"
                for name, state in checkpoints.items()
                if name.startswith("rollback:")
            )

        _assert_prior(root, sentinel)


def test_rollback_cleans_owned_directories_after_file_write_fails(
    tmp_path: Path,
) -> None:
    effect = ReconcileFileSetEffect()
    checkpoints: dict[str, object] = {}
    blobs: dict[str, bytes] = {}
    with SafeFilesystem(tmp_path) as safe:
        filesystem = DirectoryCreationFailureFilesystem(safe)
        prepared = effect.prepare(
            {
                "desired": [
                    {"path": "created/deep/owned.txt", "content": "owned"}
                ]
            },
            None,
            _context(tmp_path, filesystem),
        )
        writer = DurableMemoryWriter(
            filesystem,
            FaultController(None),
            checkpoints=checkpoints,
            blobs=blobs,
        )

        with pytest.raises(InjectedFailure, match="parent creation"):
            effect.apply(prepared, writer)
        assert (tmp_path / "created/deep").is_dir()

        effect.revert(_context(tmp_path, filesystem), prepared.before_state, writer)

    assert not (tmp_path / "created").exists()


def _installed_for_uninstall(
    root: Path,
    safe: SafeFilesystem,
    effect: ReconcileFileSetEffect,
) -> tuple[object, dict[str, bytes]]:
    (root / "preexisting-empty").mkdir()
    filesystem = FaultingFilesystem(safe, FaultController(None))
    prepared = effect.prepare(
        {
            "desired": [
                {"path": "preexisting-empty/owned.txt", "content": "one"},
                {"path": "created/deep/owned.txt", "content": "two"},
            ]
        },
        None,
        _context(root, filesystem),
    )
    blobs: dict[str, bytes] = {}
    effect.apply(
        prepared,
        DurableMemoryWriter(
            filesystem,
            FaultController(None),
            blobs=blobs,
        ),
    )
    return prepared.before_state, blobs


def _uninstall_boundaries(tmp_path: Path) -> list[str]:
    root = tmp_path / "uninstall-baseline"
    root.mkdir()
    effect = ReconcileFileSetEffect()
    with SafeFilesystem(root) as safe:
        before_state, blobs = _installed_for_uninstall(root, safe, effect)
        controller = FaultController(None)
        filesystem = FaultingFilesystem(safe, controller)
        effect.revert(
            _context(root, filesystem, operation=Operation.UNINSTALL),
            before_state,
            DurableMemoryWriter(filesystem, controller, blobs=blobs),
        )
    return controller.boundaries


def test_interrupted_uninstall_never_removes_a_preexisting_empty_parent(
    tmp_path: Path,
) -> None:
    boundaries = _uninstall_boundaries(tmp_path)
    assert boundaries

    for fail_at, boundary in enumerate(boundaries, start=1):
        root = tmp_path / f"uninstall-{fail_at}"
        root.mkdir()
        outside = root.parent / f"{root.name}-outside.txt"
        outside.write_bytes(b"outside")
        effect = ReconcileFileSetEffect()
        checkpoints: dict[str, object] = {}
        with SafeFilesystem(root) as safe:
            before_state, blobs = _installed_for_uninstall(root, safe, effect)
            controller = FaultController(fail_at)
            filesystem = FaultingFilesystem(safe, controller)
            writer = DurableMemoryWriter(
                filesystem,
                controller,
                checkpoints=checkpoints,
                blobs=blobs,
            )
            with pytest.raises(InjectedFailure, match=boundary):
                effect.revert(
                    _context(root, filesystem, operation=Operation.UNINSTALL),
                    before_state,
                    writer,
                )

            resumed_controller = FaultController(None)
            resumed_filesystem = FaultingFilesystem(safe, resumed_controller)
            effect.revert(
                _context(
                    root,
                    resumed_filesystem,
                    operation=Operation.UNINSTALL,
                ),
                before_state,
                DurableMemoryWriter(
                    resumed_filesystem,
                    resumed_controller,
                    checkpoints=checkpoints,
                    blobs=blobs,
                ),
            )

        assert (root / "preexisting-empty").is_dir()
        assert list((root / "preexisting-empty").iterdir()) == []
        assert not (root / "created").exists()
        assert outside.read_bytes() == b"outside"
