from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from minimalist_installer import CorruptManifestError, UnsafePathError
from minimalist_installer.core.manifest import (
    MANIFEST_V1_SCHEMA,
    CommittedManifest,
    ManifestEffectRecord,
    ManifestRepository,
)
from minimalist_installer.core.path_safety import SafeFilesystem


def _effect(effect_id: str = "skills:user") -> ManifestEffectRecord:
    return ManifestEffectRecord(
        id=effect_id,
        type="reconcile_file_set",
        effect_version=1,
        before_state={"files": []},
    )


def _manifest(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "engine": {"name": "minimalist-installer", "version": "0.1.0"},
        "installation": {
            "id": "installation-1",
            "consumer": "lacuna-signer",
            "consumer_version": "1.2.3",
        },
        "transaction_id": "transaction-1",
        "effects": [
            {
                "id": "skills:user",
                "type": "reconcile_file_set",
                "effect_version": 1,
                "before_state": {"files": []},
            }
        ],
        "installed_at": "2026-09-10T12:00:00Z",
        "updated_at": "2026-09-10T12:00:00Z",
    }
    value.update(overrides)
    return value


def _manifest_with_duplicate_effect_ids() -> dict[str, object]:
    duplicate = {
        "id": "duplicate",
        "type": "effect",
        "effect_version": 1,
        "before_state": None,
    }
    return _manifest(effects=[duplicate, {**duplicate, "type": "other"}])


def _repository(tmp_path: Path, **kwargs: object) -> ManifestRepository:
    base = tmp_path / "base"
    base.mkdir()
    return ManifestRepository(
        SafeFilesystem(base),
        manifest_directory="state/owned",
        **kwargs,
    )


def test_schema_file_exactly_matches_runtime_schema() -> None:
    schema_path = (
        Path(__file__).parents[3] / "spec/schemas/python-manifest-v1.schema.json"
    )

    assert json.loads(schema_path.read_text(encoding="utf-8")) == MANIFEST_V1_SCHEMA
    assert MANIFEST_V1_SCHEMA["additionalProperties"] is False
    assert MANIFEST_V1_SCHEMA["properties"]["engine"]["additionalProperties"] is False
    assert (
        MANIFEST_V1_SCHEMA["properties"]["effects"]["items"]["additionalProperties"]
        is False
    )
    timestamp_pattern = "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
    assert MANIFEST_V1_SCHEMA["properties"]["installed_at"]["pattern"] == (
        timestamp_pattern
    )
    assert MANIFEST_V1_SCHEMA["properties"]["updated_at"]["pattern"] == (
        timestamp_pattern
    )


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-09-10 12:00:00+00:00",
        "2026-09-10T12:00:00+00:00",
        "2026-09-10T12:00:00.000Z",
        "20260910T120000Z",
        "2026-09-10T12:00:00z",
        "2026-09-10T09:00:00-03:00",
    ),
)
def test_manifest_rejects_noncanonical_timestamp_syntax(timestamp: str) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        CommittedManifest.from_dict(_manifest(updated_at=timestamp))


def test_commit_writes_exact_versioned_manifest_atomically(tmp_path: Path) -> None:
    repository = _repository(
        tmp_path,
        clock=lambda: datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    )
    calls: list[tuple[object, object]] = []
    real_write = repository.filesystem.atomic_write_json

    def recording_write(relative: object, value: object, **kwargs: object) -> None:
        calls.append((relative, value))
        real_write(relative, value, **kwargs)

    repository.filesystem.atomic_write_json = recording_write  # type: ignore[method-assign]

    committed = repository.commit(
        installation_id="installation-1",
        consumer="lacuna-signer",
        consumer_version="1.2.3",
        transaction_id="transaction-1",
        engine_version="0.1.0",
        effects=(_effect(),),
    )

    assert calls and calls[0][0] == "state/owned/manifest.json"
    assert repository.read() == committed
    assert committed.to_dict() == _manifest()


def test_update_preserves_installed_at_and_advances_updated_at(tmp_path: Path) -> None:
    times: Iterator[datetime] = iter(
        (
            datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc),
        )
    )
    repository = _repository(tmp_path, clock=lambda: next(times))

    first = repository.commit(
        installation_id="installation-1",
        consumer="lacuna-signer",
        consumer_version="1.2.3",
        transaction_id="transaction-1",
        engine_version="0.1.0",
        effects=(_effect(),),
    )
    updated = repository.commit(
        installation_id="installation-1",
        consumer="lacuna-signer",
        consumer_version="1.2.4",
        transaction_id="transaction-2",
        engine_version="0.1.0",
        effects=(_effect(),),
    )

    assert updated.installed_at == first.installed_at
    assert updated.updated_at == "2026-09-11T13:30:00Z"
    assert updated.transaction_id == "transaction-2"


def test_new_installation_identity_gets_a_new_installed_timestamp(
    tmp_path: Path,
) -> None:
    times: Iterator[datetime] = iter(
        (
            datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc),
        )
    )
    repository = _repository(tmp_path, clock=lambda: next(times))
    repository.commit(
        installation_id="installation-1",
        consumer="consumer",
        consumer_version="1",
        transaction_id="transaction-1",
        engine_version="0.1.0",
        effects=(),
    )

    replacement = repository.commit(
        installation_id="installation-2",
        consumer="consumer",
        consumer_version="1",
        transaction_id="transaction-2",
        engine_version="0.1.0",
        effects=(),
    )

    assert replacement.installed_at == "2026-09-11T13:30:00Z"


def test_missing_manifest_reads_as_none_and_remove_is_idempotent(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    assert repository.read() is None
    assert repository.remove() is False


def test_remove_prunes_an_orphaned_empty_owned_directory_only(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    owned = repository.filesystem.base / "state/owned"
    owned.mkdir(parents=True)

    assert repository.remove() is False
    assert not owned.exists()
    assert (repository.filesystem.base / "state").is_dir()


def test_remove_deletes_manifest_and_only_the_owned_empty_directory(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.write(CommittedManifest.from_dict(_manifest()))
    shared = repository.filesystem.base / "state"

    assert repository.remove() is True
    assert not (shared / "owned").exists()
    assert shared.is_dir()


def test_remove_preserves_nonempty_owned_directory(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.write(CommittedManifest.from_dict(_manifest()))
    owned = repository.filesystem.base / "state/owned"
    (owned / "consumer-data.txt").write_text("keep", encoding="utf-8")

    assert repository.remove() is True
    assert owned.is_dir()
    assert (owned / "consumer-data.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "payload",
    (
        json.dumps(_manifest(schema_version=2)).encode(),
        json.dumps(_manifest(engine={"name": "foreign", "version": "1"})).encode(),
        b"{not-json",
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        json.dumps(_manifest_with_duplicate_effect_ids()).encode(),
        json.dumps(_manifest(updated_at="2026-09-10T12:00:00+00:00")).encode(),
    ),
)
def test_remove_refuses_invalid_manifest_without_changing_its_bytes(
    tmp_path: Path,
    payload: bytes,
) -> None:
    repository = _repository(tmp_path)
    target = repository.filesystem.base / "state/owned/manifest.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    with pytest.raises(CorruptManifestError):
        repository.remove()

    assert target.read_bytes() == payload


@pytest.mark.parametrize(
    ("name", "replacement"),
    (
        ("unknown schema", {"schema_version": 2}),
        ("foreign engine", {"engine": {"name": "other", "version": "1"}}),
        ("missing field", {"effects": None}),
        ("extra field", {"unexpected": True}),
        ("invalid timestamp", {"updated_at": "yesterday"}),
        (
            "duplicate effect id",
            {
                "effects": [
                    {
                        "id": "same",
                        "type": "one",
                        "effect_version": 1,
                        "before_state": None,
                    },
                    {
                        "id": "same",
                        "type": "two",
                        "effect_version": 1,
                        "before_state": None,
                    },
                ]
            },
        ),
    ),
)
def test_read_refuses_unknown_foreign_and_corrupt_manifests(
    tmp_path: Path,
    name: str,
    replacement: dict[str, object],
) -> None:
    repository = _repository(tmp_path)
    payload = _manifest(**replacement)
    target = repository.filesystem.base / "state/owned/manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CorruptManifestError, match="manifest"):
        repository.read()


@pytest.mark.parametrize(
    "payload",
    (
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":NaN}',
        b"{not-json",
        b"\xff",
    ),
)
def test_read_wraps_strict_json_failures_as_corrupt_manifest(
    tmp_path: Path,
    payload: bytes,
) -> None:
    repository = _repository(tmp_path)
    target = repository.filesystem.base / "state/owned/manifest.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    with pytest.raises(CorruptManifestError):
        repository.read()


def test_non_finite_before_state_is_rejected_before_existing_manifest_changes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    original = CommittedManifest.from_dict(_manifest())
    repository.write(original)
    target = repository.filesystem.base / "state/owned/manifest.json"
    original_bytes = target.read_bytes()

    with pytest.raises(ValueError):
        ManifestEffectRecord(
            id="bad",
            type="effect",
            effect_version=1,
            before_state={"number": float("nan")},
        )

    assert target.read_bytes() == original_bytes


@pytest.mark.parametrize("operation", ("read", "write", "remove"))
def test_manifest_operations_refuse_symlinked_directory_and_preserve_sentinel(
    tmp_path: Path,
    operation: str,
) -> None:
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    sentinel = outside / "manifest.json"
    sentinel.write_bytes(b"outside-original")
    try:
        (base / "state").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks unavailable: {error}")
    repository = ManifestRepository(
        SafeFilesystem(base),
        manifest_directory="state",
    )

    with pytest.raises(UnsafePathError):
        if operation == "read":
            repository.read()
        elif operation == "write":
            repository.write(CommittedManifest.from_dict(_manifest()))
        else:
            repository.remove()

    assert sentinel.read_bytes() == b"outside-original"


@pytest.mark.parametrize("operation", ("read", "write", "remove"))
def test_manifest_operations_refuse_symlink_leaf_and_preserve_sentinel(
    tmp_path: Path,
    operation: str,
) -> None:
    base = tmp_path / "base"
    owned = base / "state/owned"
    owned.mkdir(parents=True)
    sentinel = tmp_path / "outside-manifest.json"
    sentinel.write_bytes(b"outside-original")
    try:
        (owned / "manifest.json").symlink_to(sentinel)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks unavailable: {error}")
    repository = ManifestRepository(
        SafeFilesystem(base),
        manifest_directory="state/owned",
    )

    with pytest.raises(UnsafePathError):
        if operation == "read":
            repository.read()
        elif operation == "write":
            repository.write(CommittedManifest.from_dict(_manifest()))
        else:
            repository.remove()

    assert sentinel.read_bytes() == b"outside-original"


def test_manifest_directory_must_be_a_safe_relative_path(tmp_path: Path) -> None:
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    sentinel = outside / "manifest.json"
    sentinel.write_bytes(b"outside-original")
    repository = ManifestRepository(
        SafeFilesystem(base),
        manifest_directory="../outside",
    )

    with pytest.raises(UnsafePathError):
        repository.write(CommittedManifest.from_dict(_manifest()))

    assert sentinel.read_bytes() == b"outside-original"
