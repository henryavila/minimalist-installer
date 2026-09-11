from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

from minimalist_installer import (
    EffectContext,
    EffectPlan,
    IncompleteTransactionError,
    Operation,
    OperationStatus,
    PlanContext,
    PlanDriftError,
    PreparedEffect,
    RecoveryBlockedError,
    UnknownEffectError,
    UnsupportedEffectVersionError,
    define_installer,
)
from minimalist_installer.core.errors import CorruptTransactionError
from minimalist_installer.core.journal import (
    CleanupProofKind,
    CleanupTombstone,
    TransactionPhase,
)
from minimalist_installer.core.locks import canonical_resource_identity
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.providers import FileSetProvider


class _Lease:
    def __init__(
        self,
        events: list[str],
        on_enter: Callable[[], None] | None = None,
    ) -> None:
        self.events = events
        self.on_enter = on_enter
        self.released = False

    def __enter__(self) -> Self:
        self.events.append("locks:entered")
        if self.on_enter is not None:
            self.on_enter()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.released = True
        self.events.append("locks:released")


class _LockManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.acquisitions: list[tuple[str, ...]] = []
        self.leases: list[_Lease] = []

    def acquire(self, resources: Iterable[str], *, timeout: float) -> _Lease:
        ordered = tuple(resources)
        self.events.append("locks:acquire")
        self.acquisitions.append(ordered)
        lease = _Lease(self.events)
        self.leases.append(lease)
        return lease


class _StaticProvider:
    def __init__(self, plans: Sequence[EffectPlan] | Callable[[], Sequence[EffectPlan]]) -> None:
        self._plans = plans
        self.calls = 0

    def plan(
        self, config: Mapping[str, object], context: PlanContext
    ) -> Sequence[EffectPlan]:
        self.calls += 1
        if callable(self._plans):
            return self._plans()
        return self._plans


class _PathEffect:
    """Toy reversible effect that writes one relative file through the WAL filesystem."""

    type = "record"
    version = 1

    def __init__(
        self,
        *,
        fail_apply_of: str | None = None,
        fail_revert_of: str | None = None,
    ) -> None:
        self.fail_apply_of = fail_apply_of
        self.fail_revert_of = fail_revert_of
        self._revert_interrupted = False
        self.applied: list[str] = []
        self.reverted: list[str] = []

    def prepare(
        self,
        args: dict[str, Any],
        previous: object,
        context: EffectContext,
    ) -> PreparedEffect:
        relative = str(args["path"])
        filesystem = context.filesystem
        existed = False
        before: str | None = None
        try:
            before_bytes = filesystem.read_bytes(relative)
        except FileNotFoundError:
            before_bytes = None
        else:
            existed = True
            before = before_bytes.decode("latin1")
        return PreparedEffect(
            before_state={
                "path": relative,
                "existed": existed,
                "before": before,
            },
            payload={
                "path": relative,
                "content": str(args["content"]),
                "effect_id": context.effect_id,
            },
            filesystem=filesystem,
        )

    def apply(self, prepared: PreparedEffect, checkpoint: Any) -> object:
        effect_id = str(prepared.payload["effect_id"])
        relative = str(prepared.payload["path"])
        content = str(prepared.payload["content"]).encode("utf-8")
        checkpoint.write("apply", {"phase": "ready", "path": relative})
        if effect_id == self.fail_apply_of:
            raise RuntimeError(f"apply interrupted:{effect_id}")
        prepared.filesystem.atomic_write_bytes(relative, content)
        checkpoint.write("apply", {"phase": "done", "path": relative})
        self.applied.append(effect_id)
        return {"applied": effect_id}

    def revert(
        self,
        context: EffectContext,
        before_state: object,
        checkpoint: Any,
    ) -> None:
        existing = checkpoint.read("revert")
        if isinstance(existing, Mapping) and existing.get("phase") == "done":
            self.reverted.append(context.effect_id)
            return
        checkpoint.write(
            "revert",
            {"phase": "ready", "effect_id": context.effect_id},
        )
        if (
            context.effect_id == self.fail_revert_of
            and not self._revert_interrupted
        ):
            self._revert_interrupted = True
            raise RuntimeError(f"revert interrupted:{context.effect_id}")
        assert isinstance(before_state, Mapping)
        relative = str(before_state["path"])
        filesystem = context.filesystem
        if before_state["existed"]:
            filesystem.atomic_write_bytes(
                relative, str(before_state["before"]).encode("latin1")
            )
        else:
            filesystem.unlink(relative, missing_ok=True)
        checkpoint.write(
            "revert",
            {"phase": "done", "effect_id": context.effect_id},
        )
        self.reverted.append(context.effect_id)


def _plan(effect_id: str, relative: str, content: str) -> EffectPlan:
    return EffectPlan(
        id=effect_id,
        type="record",
        version=1,
        args={"path": relative, "content": content},
    )


def _installer(
    tmp_path: Path,
    provider: _StaticProvider,
    effect: _PathEffect,
    ids: Iterable[str],
    *,
    lock_manager: _LockManager | None = None,
    extra_providers: Sequence[object] = (),
    extra_effects: Sequence[object] = (),
):
    identifiers = iter(ids)
    return define_installer(
        config={"consumer": "tests", "consumer_version": "1"},
        providers=(provider, *extra_providers),
        effects=(effect, *extra_effects),
        manifest_directory="state",
        lock_manager=lock_manager,
        id_factory=lambda: next(identifiers),
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _journal(tmp_path: Path, transaction_id: str) -> dict[str, Any]:
    path = tmp_path / "state/transactions" / transaction_id / "journal.json"
    return json.loads(path.read_text("utf-8"))


def _active(tmp_path: Path) -> dict[str, Any] | None:
    path = tmp_path / "state/transactions/active.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text("utf-8"))


def _interrupt_second_effect(tmp_path: Path) -> tuple[Any, _PathEffect, str]:
    effect = _PathEffect(fail_apply_of="b")
    installer = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A"), _plan("b", "files/b.txt", "B"))),
        effect,
        ("install-1", "tx-1"),
    )
    with pytest.raises(RuntimeError, match="apply interrupted:b"):
        installer.install(base_path=tmp_path)
    return installer, effect, "tx-1"


def test_recovery_report_is_public_frozen_and_json_serializable() -> None:
    from minimalist_installer import RecoveryReport

    report = RecoveryReport(
        status=OperationStatus.BLOCKED,
        trusted=True,
        resumable=True,
        rollback_supported=True,
        operation=Operation.INSTALL,
        transaction_id="tx-1",
        installation_id="install-1",
        phase=TransactionPhase.APPLYING.value,
        applied=("a",),
        prepared=("b",),
        planned=("c",),
        missing_blobs=("deadbeef",),
        remnants=("old-tx",),
        warnings=("partial apply",),
        reason="interrupted apply",
    )

    encoded = json.loads(json.dumps(report.to_dict()))
    assert encoded["status"] == "blocked"
    assert encoded["trusted"] is True
    assert encoded["resumable"] is True
    assert encoded["rollback_supported"] is True
    assert encoded["operation"] == "install"
    assert encoded["applied"] == ["a"]
    assert encoded["missing_blobs"] == ["deadbeef"]
    with pytest.raises(AttributeError):
        report.trusted = False  # type: ignore[misc]


def test_inspect_and_status_never_write(tmp_path: Path) -> None:
    installer, _effect, _tx_id = _interrupt_second_effect(tmp_path)
    before = _snapshot(tmp_path)

    status = installer.status(base_path=tmp_path)
    report = installer.inspect_recovery(base_path=tmp_path)

    assert status.status is OperationStatus.BLOCKED
    assert status.incomplete_transaction_id == "tx-1"
    from minimalist_installer import RecoveryReport

    assert isinstance(report, RecoveryReport)
    assert report.trusted is True
    assert report.transaction_id == "tx-1"
    assert _snapshot(tmp_path) == before


def test_incomplete_wal_blocks_install_update_and_uninstall(tmp_path: Path) -> None:
    installer, _effect, tx_id = _interrupt_second_effect(tmp_path)

    for operation in ("install", "update", "uninstall"):
        with pytest.raises(IncompleteTransactionError, match=tx_id):
            getattr(installer, operation)(base_path=tmp_path)

    assert (tmp_path / "files/a.txt").read_bytes() == b"A"
    assert not (tmp_path / "files/b.txt").exists()
    assert not (tmp_path / "state/manifest.json").exists()


def test_trusted_rollback_restores_applied_files_and_keeps_previous_manifest(
    tmp_path: Path,
) -> None:
    first = _PathEffect()
    installed = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A1"),)),
        first,
        ("install-1", "tx-0"),
    )
    committed = installed.install(base_path=tmp_path)
    manifest_before = (tmp_path / "state/manifest.json").read_bytes()
    assert (tmp_path / "files/a.txt").read_bytes() == b"A1"

    effect = _PathEffect(fail_apply_of="b")
    installer = _installer(
        tmp_path,
        _StaticProvider(
            (_plan("a", "files/a.txt", "A2"), _plan("b", "files/b.txt", "B"))
        ),
        effect,
        ("tx-1",),
    )
    with pytest.raises(RuntimeError, match="apply interrupted:b"):
        installer.install(base_path=tmp_path)
    assert (tmp_path / "files/a.txt").read_bytes() == b"A2"

    result = installer.repair(base_path=tmp_path)

    assert result.operation is Operation.REPAIR
    assert result.status is OperationStatus.COMPLETED
    assert result.transaction_id == "tx-1"
    assert result.reverted == ("b", "a") or "a" in result.reverted
    assert (tmp_path / "files/a.txt").read_bytes() == b"A1"
    assert not (tmp_path / "files/b.txt").exists()
    assert (tmp_path / "state/manifest.json").read_bytes() == manifest_before
    assert json.loads(manifest_before)["transaction_id"] == committed.transaction_id
    assert _active(tmp_path) is None
    assert installer.status(base_path=tmp_path).incomplete_transaction_id is None


def test_repair_does_not_begin_a_nested_transaction(tmp_path: Path) -> None:
    installer, _effect, tx_id = _interrupt_second_effect(tmp_path)

    result = installer.repair(base_path=tmp_path)

    assert result.transaction_id == tx_id
    assert _active(tmp_path) is None
    assert not (tmp_path / "files/a.txt").exists()
    assert not (tmp_path / "state/manifest.json").exists()


def test_repair_interrupted_mid_revert_is_resumable(tmp_path: Path) -> None:
    effect = _PathEffect(fail_apply_of="b", fail_revert_of="b")
    installer = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A"), _plan("b", "files/b.txt", "B"))),
        effect,
        ("install-1", "tx-1"),
    )
    with pytest.raises(RuntimeError, match="apply interrupted:b"):
        installer.install(base_path=tmp_path)

    with pytest.raises(RuntimeError, match="revert interrupted:b"):
        installer.repair(base_path=tmp_path)

    journal = _journal(tmp_path, "tx-1")
    assert journal["phase"] == TransactionPhase.REVERTING.value
    assert "repairing" in journal["operation_checkpoints"]
    assert (tmp_path / "files/a.txt").is_file()
    assert _active(tmp_path) is not None

    result = installer.repair(base_path=tmp_path)

    assert result.status is OperationStatus.COMPLETED
    assert effect.reverted.count("b") >= 1
    assert "a" in effect.reverted
    assert not (tmp_path / "files/a.txt").exists()
    assert not (tmp_path / "files/b.txt").exists()
    assert _active(tmp_path) is None


def test_missing_blob_fails_closed_without_skipping_rollback(tmp_path: Path) -> None:
    seed = define_installer(
        config={
            "consumer": "tests",
            "consumer_version": "1",
            "files": [{"path": "owned.txt", "content": "original-bytes"}],
        },
        providers=(FileSetProvider(),),
        manifest_directory="state",
        id_factory=iter(("install-1", "tx-0")).__next__,
    )
    seed.install(base_path=tmp_path)
    effect = _PathEffect(fail_apply_of="boom")
    installer = define_installer(
        config={
            "consumer": "tests",
            "consumer_version": "1",
            "files": [{"path": "owned.txt", "content": "replacement"}],
        },
        providers=(
            FileSetProvider(),
            _StaticProvider((_plan("boom", "other.txt", "nope"),)),
        ),
        effects=(effect,),
        manifest_directory="state",
        id_factory=iter(("tx-1",)).__next__,
    )
    with pytest.raises(RuntimeError, match="apply interrupted:boom"):
        installer.install(base_path=tmp_path)

    blobs_dir = tmp_path / "state/transactions/tx-1/blobs"
    blob_files = list(blobs_dir.glob("*.blob"))
    assert blob_files
    for blob in blob_files:
        blob.unlink()

    before = _snapshot(tmp_path)
    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.trusted is True
    assert report.missing_blobs
    assert report.rollback_supported is False

    with pytest.raises((RecoveryBlockedError, CorruptTransactionError)):
        installer.repair(base_path=tmp_path)

    assert _snapshot(tmp_path) == before
    with pytest.raises(IncompleteTransactionError):
        installer.install(base_path=tmp_path)


def test_corrupt_journal_json_fails_closed(tmp_path: Path) -> None:
    installer, _effect, tx_id = _interrupt_second_effect(tmp_path)
    journal_path = tmp_path / "state/transactions" / tx_id / "journal.json"
    journal_path.write_bytes(b"{not-json")
    before = _snapshot(tmp_path)

    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.trusted is False
    assert report.reason is not None
    assert _snapshot(tmp_path) == before

    with pytest.raises((CorruptTransactionError, RecoveryBlockedError)):
        installer.repair(base_path=tmp_path)
    assert _snapshot(tmp_path) == before
    assert journal_path.read_bytes() == b"{not-json"


def test_unknown_effect_type_in_wal_fails_closed(tmp_path: Path) -> None:
    installer, _effect, tx_id = _interrupt_second_effect(tmp_path)
    journal_path = tmp_path / "state/transactions" / tx_id / "journal.json"
    payload = json.loads(journal_path.read_text("utf-8"))
    payload["effects"][0]["type"] = "not-registered"
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot(tmp_path)

    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.trusted is False
    assert report.rollback_supported is False
    assert report.resumable is False

    with pytest.raises((UnknownEffectError, RecoveryBlockedError)):
        installer.repair(base_path=tmp_path)
    assert _snapshot(tmp_path) == before


def test_unknown_effect_version_in_wal_fails_closed(tmp_path: Path) -> None:
    installer, _effect, tx_id = _interrupt_second_effect(tmp_path)
    journal_path = tmp_path / "state/transactions" / tx_id / "journal.json"
    payload = json.loads(journal_path.read_text("utf-8"))
    payload["effects"][0]["effect_version"] = 99
    payload["effects"][0]["prepared"]["recoverable"] = True
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot(tmp_path)

    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.trusted is False

    with pytest.raises((UnsupportedEffectVersionError, RecoveryBlockedError)):
        installer.repair(base_path=tmp_path)
    assert _snapshot(tmp_path) == before


def test_leftover_completed_transaction_dir_is_gc_only_with_commit_proof(
    tmp_path: Path,
) -> None:
    effect = _PathEffect()
    installer = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A"),)),
        effect,
        ("install-1", "tx-1", "tx-gc"),
    )
    result = installer.install(base_path=tmp_path)
    assert result.transaction_id == "tx-1"
    root = canonical_resource_identity("path", tmp_path)
    leftover = tmp_path / "state/transactions/tx-1"
    leftover.mkdir(parents=True)
    (leftover / "journal.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": {"name": "minimalist-installer", "version": "0.1.0"},
                "transaction_id": "tx-1",
                "installation_id": "install-1",
                "operation": "install",
                "phase": "committed",
                "planned_effect_ids": [],
                "resources": [root],
                "effects": [],
                "operation_checkpoints": [
                    "effects_applied",
                    "committing",
                    "manifest_committed",
                ],
                "blobs": [],
            }
        ),
        encoding="utf-8",
    )
    foreign = tmp_path / "state/transactions/foreign-tx"
    foreign.mkdir()
    (foreign / "journal.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": {"name": "minimalist-installer", "version": "0.1.0"},
                "transaction_id": "foreign-tx",
                "installation_id": "install-1",
                "operation": "install",
                "phase": "committed",
                "planned_effect_ids": [],
                "resources": [root],
                "effects": [],
                "operation_checkpoints": [
                    "effects_applied",
                    "committing",
                    "manifest_committed",
                ],
                "blobs": [],
            }
        ),
        encoding="utf-8",
    )

    report = installer.inspect_recovery(base_path=tmp_path)
    assert "tx-1" in report.remnants
    assert "foreign-tx" in report.remnants

    installer.repair(base_path=tmp_path)

    assert not leftover.exists()
    assert foreign.exists()
    assert (tmp_path / "files/a.txt").read_bytes() == b"A"
    assert json.loads((tmp_path / "state/manifest.json").read_text("utf-8"))[
        "transaction_id"
    ] == "tx-1"


def test_aborted_install_cleanup_uses_rolled_back_proof_not_committed_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer, _effect, tx_id = _interrupt_second_effect(tmp_path)
    real_unlink = SafeFilesystem.unlink

    def fail_journal(
        self: SafeFilesystem,
        relative: str,
        *,
        missing_ok: bool = False,
    ) -> bool:
        if str(relative).endswith("journal.json"):
            raise OSError("cleanup interrupted")
        return real_unlink(self, relative, missing_ok=missing_ok)

    monkeypatch.setattr(SafeFilesystem, "unlink", fail_journal)
    with pytest.raises(OSError, match="cleanup interrupted"):
        installer.repair(base_path=tmp_path)
    monkeypatch.setattr(SafeFilesystem, "unlink", real_unlink)

    active = _active(tmp_path)
    assert active is not None
    assert active["state"] == "cleanup"
    assert active["proof"]["kind"] == CleanupProofKind.ROLLED_BACK.value
    assert active["proof"]["transaction_id"] == tx_id
    assert not (tmp_path / "state/manifest.json").exists()
    tombstone = CleanupTombstone.from_dict(active)
    assert tombstone.proof_kind is CleanupProofKind.ROLLED_BACK

    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.trusted is True
    assert report.cleanup is True
    assert report.rollback_supported is False
    assert report.phase == "cleanup"

    installer.repair(base_path=tmp_path)
    assert _active(tmp_path) is None
    assert not (tmp_path / "state/transactions" / tx_id).exists()
    assert not (tmp_path / "state/manifest.json").exists()


def test_aborted_remnant_gc_does_not_require_aborted_id_in_manifest(
    tmp_path: Path,
) -> None:
    effect = _PathEffect()
    installer = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A"),)),
        effect,
        ("install-1", "tx-keep"),
    )
    installer.install(base_path=tmp_path)
    committed_id = json.loads((tmp_path / "state/manifest.json").read_text("utf-8"))[
        "transaction_id"
    ]
    assert committed_id == "tx-keep"
    root = canonical_resource_identity("path", tmp_path)
    remnant_id = "tx-aborted"
    remnant = tmp_path / "state/transactions" / remnant_id
    remnant.mkdir(parents=True)
    (remnant / "journal.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": {"name": "minimalist-installer", "version": "0.1.0"},
                "transaction_id": remnant_id,
                "installation_id": "install-1",
                "operation": "install",
                "phase": "committing",
                "planned_effect_ids": [],
                "resources": [root],
                "effects": [],
                "operation_checkpoints": [
                    "repairing",
                    "effects_reverted",
                    "rolled_back",
                ],
                "blobs": [],
            }
        ),
        encoding="utf-8",
    )

    installer.repair(base_path=tmp_path)

    assert not remnant.exists()
    assert json.loads((tmp_path / "state/manifest.json").read_text("utf-8"))[
        "transaction_id"
    ] == "tx-keep"
    assert (tmp_path / "files/a.txt").read_bytes() == b"A"


def test_resume_incompatible_plan_is_rejected_without_mutation(tmp_path: Path) -> None:
    plans = iter(
        (
            (_plan("a", "files/a.txt", "A"), _plan("b", "files/b.txt", "B")),
            (_plan("other", "files/other.txt", "X"),),
        )
    )
    effect = _PathEffect(fail_apply_of="b")
    installer = _installer(
        tmp_path,
        _StaticProvider(lambda: next(plans)),
        effect,
        ("install-1", "tx-1"),
    )
    with pytest.raises(RuntimeError, match="apply interrupted:b"):
        installer.install(base_path=tmp_path)
    before = _snapshot(tmp_path)

    with pytest.raises((PlanDriftError, RecoveryBlockedError)):
        installer.repair(base_path=tmp_path, resume=True)

    assert _snapshot(tmp_path) == before
    assert (tmp_path / "files/a.txt").read_bytes() == b"A"
    assert not (tmp_path / "files/b.txt").exists()
    assert _active(tmp_path) is not None


def test_resume_compatible_plan_continues_from_wal_progress(tmp_path: Path) -> None:
    effect = _PathEffect(fail_apply_of="b")
    installer = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A"), _plan("b", "files/b.txt", "B"))),
        effect,
        ("install-1", "tx-1"),
    )
    with pytest.raises(RuntimeError, match="apply interrupted:b"):
        installer.install(base_path=tmp_path)

    result = installer.repair(base_path=tmp_path, resume=True)

    assert result.operation is Operation.REPAIR
    assert result.status is OperationStatus.COMPLETED
    assert result.transaction_id == "tx-1"
    assert "a" not in effect.reverted
    assert (tmp_path / "files/a.txt").read_bytes() == b"A"
    assert (tmp_path / "files/b.txt").read_bytes() == b"B"
    manifest = json.loads((tmp_path / "state/manifest.json").read_text("utf-8"))
    assert manifest["transaction_id"] == "tx-1"
    assert [entry["id"] for entry in manifest["effects"]] == ["a", "b"]
    assert _active(tmp_path) is None


def test_cleanup_tombstone_inspect_is_trusted_cleanup_and_repair_only_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = _PathEffect()
    events: list[str] = []
    locks = _LockManager(events)
    installer = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A"),)),
        effect,
        ("install-1", "tx-1"),
        lock_manager=locks,
    )
    real_unlink = SafeFilesystem.unlink

    def fail_journal(
        self: SafeFilesystem,
        relative: str,
        *,
        missing_ok: bool = False,
    ) -> bool:
        if str(relative).endswith("journal.json"):
            raise OSError("cleanup interrupted")
        return real_unlink(self, relative, missing_ok=missing_ok)

    monkeypatch.setattr(SafeFilesystem, "unlink", fail_journal)
    with pytest.raises(OSError, match="cleanup interrupted"):
        installer.install(base_path=tmp_path)
    monkeypatch.setattr(SafeFilesystem, "unlink", real_unlink)

    assert (tmp_path / "files/a.txt").read_bytes() == b"A"
    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.trusted is True
    assert report.cleanup is True
    assert report.rollback_supported is False
    assert report.resumable is False
    assert report.phase == "cleanup"
    assert report.operation is Operation.INSTALL

    with pytest.raises(IncompleteTransactionError):
        installer.uninstall(base_path=tmp_path)

    events.clear()
    locks.acquisitions.clear()
    result = installer.repair(base_path=tmp_path)
    assert result.status is OperationStatus.COMPLETED
    assert _active(tmp_path) is None
    assert (tmp_path / "files/a.txt").read_bytes() == b"A"
    assert json.loads((tmp_path / "state/manifest.json").read_text("utf-8"))[
        "transaction_id"
    ] == "tx-1"


def test_inspect_uses_root_lock_and_repair_uses_journal_resources(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    locks = _LockManager(events)
    effect = _PathEffect(fail_apply_of="b")
    installer = _installer(
        tmp_path,
        _StaticProvider((_plan("a", "files/a.txt", "A"), _plan("b", "files/b.txt", "B"))),
        effect,
        ("install-1", "tx-1"),
        lock_manager=locks,
    )
    with pytest.raises(RuntimeError, match="apply interrupted:b"):
        installer.install(base_path=tmp_path)
    journal_resources = tuple(_journal(tmp_path, "tx-1")["resources"])
    root = (canonical_resource_identity("path", tmp_path),)

    locks.acquisitions.clear()
    installer.inspect_recovery(base_path=tmp_path)
    installer.status(base_path=tmp_path)
    assert locks.acquisitions == [root, root]

    locks.acquisitions.clear()
    installer.repair(base_path=tmp_path)
    assert journal_resources in locks.acquisitions or locks.acquisitions[0] == journal_resources


def test_driver_and_installer_share_repair_and_inspect_surface(tmp_path: Path) -> None:
    effect = _PathEffect()
    provider = _StaticProvider((_plan("a", "files/a.txt", "A"),))
    installer = _installer(
        tmp_path, provider, effect, ("install-1", "tx-1")
    )
    assert callable(installer.inspect_recovery)
    assert callable(installer.repair)
    assert callable(installer.driver.inspect_recovery)
    assert callable(installer.driver.repair)
    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.status is OperationStatus.COMPLETED
    assert report.trusted is True
    assert report.transaction_id is None
    result = installer.repair(base_path=tmp_path)
    assert result.operation is Operation.REPAIR
    assert result.status is OperationStatus.COMPLETED


def test_untrusted_foreign_engine_journal_is_not_silently_recovered(
    tmp_path: Path,
) -> None:
    installer, _effect, tx_id = _interrupt_second_effect(tmp_path)
    journal_path = tmp_path / "state/transactions" / tx_id / "journal.json"
    payload = json.loads(journal_path.read_text("utf-8"))
    payload["engine"]["name"] = "legacy-installer"
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot(tmp_path)

    report = installer.inspect_recovery(base_path=tmp_path)
    assert report.trusted is False
    with pytest.raises((CorruptTransactionError, RecoveryBlockedError)):
        installer.repair(base_path=tmp_path)
    assert _snapshot(tmp_path) == before
