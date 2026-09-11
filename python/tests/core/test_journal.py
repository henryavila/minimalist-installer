from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from minimalist_installer import CorruptTransactionError, EffectPlan, Operation, PreparedEffect
from minimalist_installer.core.journal import (
    ACTIVE_TRANSACTION_V1_SCHEMA,
    BlobStatus,
    CleanupTombstone,
    EffectProgress,
    TRANSACTION_V1_SCHEMA,
    TransactionBlobRecord,
    TransactionPhase,
    TransactionRepository,
)
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.core.manifest import ManifestRepository


def _repository(tmp_path: Path) -> TransactionRepository:
    return TransactionRepository(
        SafeFilesystem(tmp_path),
        manifest_directory="state",
    )


def _plan(effect_id: str = "effect:a") -> EffectPlan:
    return EffectPlan(
        id=effect_id,
        type="record",
        version=1,
        args={"value": effect_id},
        resources=("kind:z", "kind:a"),
    )


def test_schema_file_exactly_matches_runtime_transaction_schema() -> None:
    schema_path = (
        Path(__file__).parents[3]
        / "spec/schemas/python-transaction-v1.schema.json"
    )

    assert json.loads(schema_path.read_text("utf-8")) == TRANSACTION_V1_SCHEMA
    active_schema_path = (
        Path(__file__).parents[3]
        / "spec/schemas/python-active-transaction-v1.schema.json"
    )
    assert json.loads(active_schema_path.read_text("utf-8")) == (
        ACTIVE_TRANSACTION_V1_SCHEMA
    )


def test_begin_persists_a_strict_active_write_ahead_journal(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    journal = repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(_plan(),),
        resources=("kind:a", "kind:z"),
    )

    assert repository.active() == journal
    assert journal.phase is TransactionPhase.PLANNED
    assert journal.planned_effect_ids == ("effect:a",)
    persisted = json.loads(
        (tmp_path / "state/transactions/tx-1/journal.json").read_text("utf-8")
    )
    assert persisted == journal.to_dict()
    assert TRANSACTION_V1_SCHEMA["properties"]["schema_version"] == {"const": 1}


def test_prepared_state_and_effect_checkpoint_are_durable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    prepared = PreparedEffect(
        before_state={"old": "bytes"},
        payload={"new": "bytes"},
        resources=("kind:a", "kind:z"),
    )

    repository.record_prepared("tx-1", plan.id, prepared)
    writer = repository.checkpoint_writer("tx-1", plan.id)
    writer.write("write:a", {"done": True})
    repository.record_applied("tx-1", plan.id, {"changed": ["a"]})
    repository.checkpoint("tx-1", "effects_applied")

    persisted = repository.read("tx-1")
    effect = persisted.effects[0]
    assert effect.prepared == prepared
    assert effect.checkpoints[0].name == "write:a"
    assert effect.checkpoints[0].state == {"done": True}
    assert effect.result == {"changed": ("a",)}
    assert persisted.operation_checkpoints == ("effects_applied",)


def test_effect_checkpoint_names_are_idempotently_replaced(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )
    writer = repository.checkpoint_writer("tx-1", plan.id)

    writer.write("step", {"attempt": 1})
    writer.write("step", {"attempt": 2})

    checkpoints = repository.read("tx-1").effects[0].checkpoints
    assert len(checkpoints) == 1
    assert checkpoints[0].state == {"attempt": 2}


def test_blob_storage_is_content_addressed_and_verified(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )

    digest = repository.write_blob("tx-1", b"original")

    assert repository.read("tx-1").blobs == (
        TransactionBlobRecord(digest=digest, status=BlobStatus.READY),
    )
    assert repository.read_blob("tx-1", digest) == b"original"
    (tmp_path / f"state/transactions/tx-1/blobs/{digest}.blob").write_bytes(b"tamper")
    with pytest.raises(CorruptTransactionError, match="digest"):
        repository.read_blob("tx-1", digest)


def test_checkpoint_writer_exposes_the_transaction_blob_store(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )
    writer = repository.checkpoint_writer("tx-1", plan.id)

    digest = writer.write_blob(b"backup bytes")

    assert writer.read_blob(digest) == b"backup bytes"
    assert repository.read("tx-1").blobs == (
        TransactionBlobRecord(digest=digest, status=BlobStatus.READY),
    )


def test_reconstructed_checkpoint_writer_loads_an_immutable_wal_snapshot(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.UNINSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )
    first = repository.checkpoint_writer("tx-1", plan.id)
    first.write("removed:a", {"paths": ["a"]})
    digest = first.write_blob(b"backup")

    reconstructed = repository.checkpoint_writer("tx-1", plan.id)

    assert reconstructed.read("removed:a") == {"paths": ("a",)}
    assert reconstructed.read("missing") is None
    snapshot = reconstructed.snapshot()
    assert snapshot == {"removed:a": {"paths": ("a",)}}
    with pytest.raises(TypeError):
        snapshot["new"] = True
    with pytest.raises(AttributeError):
        snapshot["removed:a"]["paths"].append("mutate")
    assert reconstructed.read_blob(digest) == b"backup"
    reconstructed.write("removed:b", {"done": True})
    assert repository.checkpoint_writer("tx-1", plan.id).snapshot() == {
        "removed:a": {"paths": ("a",)},
        "removed:b": {"done": True},
    }


def test_blob_descriptor_is_write_ahead_of_bytes_and_marked_ready_last(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )
    events: list[str] = []
    real_write = repository._write
    real_bytes = repository.filesystem.atomic_write_bytes

    def record_journal(transaction_id: str, journal: object) -> None:
        status = journal.blobs[0].status.value
        events.append(f"journal:{status}")
        real_write(transaction_id, journal)

    def record_bytes(path: object, data: bytes, **kwargs: object) -> None:
        if str(path).endswith(".blob"):
            events.append("blob:bytes")
        real_bytes(path, data, **kwargs)

    repository._write = record_journal  # type: ignore[method-assign]
    repository.filesystem.atomic_write_bytes = record_bytes  # type: ignore[method-assign]

    repository.write_blob("tx-1", b"backup")

    assert events == ["journal:pending", "blob:bytes", "journal:ready"]


@pytest.mark.parametrize("failure_boundary", ["pending", "bytes", "ready"])
def test_blob_write_failures_remain_wal_discoverable(
    tmp_path: Path,
    failure_boundary: str,
) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )
    digest = __import__("hashlib").sha256(b"backup").hexdigest()
    real_write = repository._write
    real_bytes = repository.filesystem.atomic_write_bytes

    def failing_journal(transaction_id: str, journal: object) -> None:
        status = journal.blobs[0].status.value
        if status == failure_boundary:
            raise OSError(f"fail {status}")
        real_write(transaction_id, journal)

    def failing_bytes(path: object, data: bytes, **kwargs: object) -> None:
        if failure_boundary == "bytes" and str(path).endswith(".blob"):
            raise OSError("fail bytes")
        real_bytes(path, data, **kwargs)

    repository._write = failing_journal  # type: ignore[method-assign]
    repository.filesystem.atomic_write_bytes = failing_bytes  # type: ignore[method-assign]

    with pytest.raises(OSError):
        repository.write_blob("tx-1", b"backup")

    active = repository.active()
    assert active is not None
    blob_path = tmp_path / f"state/transactions/tx-1/blobs/{digest}.blob"
    if failure_boundary == "pending":
        assert active.blobs == ()
        assert not blob_path.exists()
    else:
        assert active.blobs == (
            TransactionBlobRecord(digest=digest, status=BlobStatus.PENDING),
        )
        assert blob_path.exists() is (failure_boundary == "ready")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda journal: replace(journal, phase=TransactionPhase.COMMITTED),
        lambda journal: replace(journal, phase=TransactionPhase.APPLYING),
        lambda journal: replace(
            journal,
            phase=TransactionPhase.APPLYING,
            effects=(
                replace(
                    journal.effects[0],
                    status=EffectProgress.APPLIED,
                    prepared=None,
                ),
            ),
        ),
        lambda journal: replace(
            journal,
            effects=(
                replace(
                    journal.effects[0],
                    prepared=PreparedEffect(
                        before_state=None,
                        payload=None,
                        resources=("kind:outside",),
                    ),
                    status=EffectProgress.PREPARED,
                ),
            ),
            phase=TransactionPhase.APPLYING,
        ),
    ],
)
def test_impossible_wal_state_combinations_are_rejected(
    tmp_path: Path,
    mutation: object,
) -> None:
    repository = _repository(tmp_path)
    journal = repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(_plan(),),
        resources=("kind:a", "kind:z"),
    )

    with pytest.raises(ValueError):
        mutation(journal)


def test_transaction_resources_must_cover_every_planned_effect(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ValueError, match="resource union"):
        repository.begin(
            transaction_id="tx-1",
            installation_id="install-1",
            operation=Operation.INSTALL,
            engine_version="0.1.0",
            plans=(_plan(),),
            resources=("kind:a",),
        )


def test_effect_progress_cannot_transition_back_from_applied_to_prepared(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    prepared = PreparedEffect(before_state=None, payload=None)
    repository.record_prepared("tx-1", plan.id, prepared)
    repository.record_applied("tx-1", plan.id, None)

    with pytest.raises(CorruptTransactionError, match="transition"):
        repository.record_prepared("tx-1", plan.id, prepared)


def test_complete_removes_only_the_named_transaction_tree(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(),
        resources=("kind:a",),
    )
    repository.checkpoint("tx-1", "effects_applied")
    repository.checkpoint("tx-1", "committing")
    repository.checkpoint("tx-1", "manifest_committed")
    ManifestRepository(
        repository.filesystem, manifest_directory="state"
    ).commit(
        installation_id="install-1",
        consumer="tests",
        consumer_version="1",
        transaction_id="tx-1",
        engine_version="0.1.0",
        effects=(),
    )
    removals: list[object] = []
    real_unlink = repository.filesystem.unlink

    def recording_unlink(path: object, **kwargs: object) -> bool:
        removals.append(path)
        return real_unlink(path, **kwargs)

    repository.filesystem.unlink = recording_unlink  # type: ignore[method-assign]

    repository.complete("tx-1")

    assert repository.active() is None
    assert not (tmp_path / "state/transactions/tx-1").exists()
    assert removals[-1] == "state/transactions/active.json"


def test_complete_requires_actual_committed_manifest_transaction_proof(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(),
        resources=("kind:a",),
    )
    repository.checkpoint("tx-1", "effects_applied")
    repository.checkpoint("tx-1", "committing")
    repository.checkpoint("tx-1", "manifest_committed")

    with pytest.raises(CorruptTransactionError, match="proof"):
        repository.complete("tx-1", committed_transaction_id="tx-1")

    ManifestRepository(
        repository.filesystem, manifest_directory="state"
    ).commit(
        installation_id="install-1",
        consumer="tests",
        consumer_version="1",
        transaction_id="other-tx",
        engine_version="0.1.0",
        effects=(),
    )
    with pytest.raises(CorruptTransactionError, match="proof"):
        repository.complete("tx-1", committed_transaction_id="tx-1")

    assert repository.active() is not None


def test_cleanup_failure_never_removes_active_authority_first(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(),
        resources=("kind:a",),
    )
    repository.checkpoint("tx-1", "effects_applied")
    repository.checkpoint("tx-1", "committing")
    repository.checkpoint("tx-1", "manifest_committed")
    ManifestRepository(
        repository.filesystem, manifest_directory="state"
    ).commit(
        installation_id="install-1",
        consumer="tests",
        consumer_version="1",
        transaction_id="tx-1",
        engine_version="0.1.0",
        effects=(),
    )
    real_unlink = repository.filesystem.unlink

    def fail_journal(path: object, **kwargs: object) -> bool:
        if str(path).endswith("journal.json"):
            raise OSError("cleanup interrupted")
        return real_unlink(path, **kwargs)

    repository.filesystem.unlink = fail_journal  # type: ignore[method-assign]

    with pytest.raises(OSError, match="cleanup interrupted"):
        repository.complete("tx-1")

    assert (tmp_path / "state/transactions/active.json").is_file()
    assert repository.active() is not None


@pytest.mark.parametrize(
    "boundary",
    ["tombstone", "blob", "blob-dir", "journal", "transaction-dir", "active"],
)
def test_cleanup_restarts_from_strict_active_tombstone_after_every_boundary(
    tmp_path: Path,
    boundary: str,
) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(plan,),
        resources=("kind:a", "kind:z"),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )
    digest = repository.write_blob("tx-1", b"backup")
    repository.record_applied("tx-1", plan.id, None)
    repository.checkpoint("tx-1", "effects_applied")
    repository.checkpoint("tx-1", "committing")
    repository.checkpoint("tx-1", "manifest_committed")
    ManifestRepository(
        repository.filesystem, manifest_directory="state"
    ).commit(
        installation_id="install-1",
        consumer="tests",
        consumer_version="1",
        transaction_id="tx-1",
        engine_version="0.1.0",
        effects=(),
    )
    real_json = repository.filesystem.atomic_write_json
    real_unlink = repository.filesystem.unlink
    real_rmdir = repository.filesystem.rmdir_empty
    failed = False

    def fail_once(label: str) -> None:
        nonlocal failed
        if not failed and boundary == label:
            failed = True
            raise OSError(f"fail {label}")

    def write_json(path: object, value: object, **kwargs: object) -> None:
        if path == repository.active_path and isinstance(value, dict):
            if value.get("state") == "cleanup":
                fail_once("tombstone")
        real_json(path, value, **kwargs)

    def unlink(path: object, **kwargs: object) -> bool:
        text = str(path)
        if text.endswith(".blob"):
            fail_once("blob")
        elif text.endswith("journal.json"):
            fail_once("journal")
        elif path == repository.active_path:
            fail_once("active")
        return real_unlink(path, **kwargs)

    def rmdir(path: object, **kwargs: object) -> bool:
        text = str(path)
        if text.endswith("/blobs"):
            fail_once("blob-dir")
        elif text.endswith("/tx-1"):
            fail_once("transaction-dir")
        return real_rmdir(path, **kwargs)

    repository.filesystem.atomic_write_json = write_json  # type: ignore[method-assign]
    repository.filesystem.unlink = unlink  # type: ignore[method-assign]
    repository.filesystem.rmdir_empty = rmdir  # type: ignore[method-assign]

    with pytest.raises(OSError, match=f"fail {boundary}"):
        repository.complete("tx-1")

    active = repository.active()
    assert active is not None
    if boundary == "tombstone":
        assert not isinstance(active, CleanupTombstone)
    else:
        assert isinstance(active, CleanupTombstone)
        assert active.blobs == (digest,)

    repository.filesystem.atomic_write_json = real_json  # type: ignore[method-assign]
    repository.filesystem.unlink = real_unlink  # type: ignore[method-assign]
    repository.filesystem.rmdir_empty = real_rmdir  # type: ignore[method-assign]
    repository.complete("tx-1")

    assert repository.active() is None
    assert not (tmp_path / f"state/transactions/tx-1/blobs/{digest}.blob").exists()


def test_corrupt_or_foreign_transaction_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    journal_path = tmp_path / "state/transactions/tx-1/journal.json"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text('{"schema_version":2}', encoding="utf-8")

    with pytest.raises(CorruptTransactionError):
        repository.read("tx-1")


def test_requested_transaction_id_must_match_the_journal_and_cannot_redirect_writes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    journal = repository.begin(
        transaction_id="tx-a",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(),
        resources=("kind:a",),
    )
    forged = journal.to_dict()
    forged["transaction_id"] = "tx-b"
    repository.filesystem.atomic_write_json(
        "state/transactions/tx-a/journal.json", forged
    )
    redirected = tmp_path / "state/transactions/tx-b/journal.json"
    redirected.parent.mkdir(parents=True)
    redirected.write_bytes(b"do-not-touch")

    with pytest.raises(CorruptTransactionError, match="does not match"):
        repository.read("tx-a")
    with pytest.raises(CorruptTransactionError, match="does not match"):
        repository.checkpoint("tx-a", "must-not-write")

    assert redirected.read_bytes() == b"do-not-touch"


def test_transaction_and_effect_identifiers_cannot_escape_wal_directory(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ValueError):
        repository.begin(
            transaction_id="../escape",
            installation_id="install-1",
            operation=Operation.INSTALL,
            engine_version="0.1.0",
            plans=(),
            resources=("kind:a",),
        )
