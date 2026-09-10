from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimalist_installer import CorruptTransactionError, EffectPlan, Operation, PreparedEffect
from minimalist_installer.core.journal import (
    TRANSACTION_V1_SCHEMA,
    TransactionPhase,
    TransactionRepository,
)
from minimalist_installer.core.path_safety import SafeFilesystem


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
        resources=("kind:a",),
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
        resources=("kind:a",),
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
    repository.begin(
        transaction_id="tx-1",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(),
        resources=("kind:a",),
    )

    digest = repository.write_blob("tx-1", b"original")

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
        resources=("kind:a",),
    )
    repository.record_prepared(
        "tx-1", plan.id, PreparedEffect(before_state=None, payload=None)
    )
    writer = repository.checkpoint_writer("tx-1", plan.id)

    digest = writer.write_blob(b"backup bytes")

    assert writer.read_blob(digest) == b"backup bytes"
    assert repository.read("tx-1").blobs == (digest,)


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
    repository.write_blob("tx-1", b"original")

    repository.complete("tx-1")

    assert repository.active() is None
    assert not (tmp_path / "state/transactions/tx-1").exists()


def test_corrupt_or_foreign_transaction_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    journal_path = tmp_path / "state/transactions/tx-1/journal.json"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text('{"schema_version":2}', encoding="utf-8")

    with pytest.raises(CorruptTransactionError):
        repository.read("tx-1")


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
