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
    NoInstallationError,
    Operation,
    OperationStatus,
    PlanContext,
    PreparedEffect,
    UnknownEffectError,
    UnsupportedEffectVersionError,
    define_installer,
)
from minimalist_installer.core.driver import Driver
from minimalist_installer.core.locks import canonical_resource_identity, canonicalize_resources
from minimalist_installer.core.manifest import ManifestRepository
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.core.registry import EffectRegistry


class _Lease:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.released = False

    def __enter__(self) -> Self:
        self.events.append("locks:entered")
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


class _Provider:
    def __init__(
        self,
        events: list[str],
        plans: Callable[[Mapping[str, object], PlanContext], Sequence[EffectPlan]],
    ) -> None:
        self.events = events
        self._plans = plans

    def plan(
        self, config: Mapping[str, object], context: PlanContext
    ) -> Sequence[EffectPlan]:
        self.events.append("provider:plan")
        return self._plans(config, context)


class _Effect:
    type = "record"
    version = 1

    def __init__(
        self,
        events: list[str],
        *,
        fail_apply: bool = False,
        journal_path: Path | None = None,
    ) -> None:
        self.events = events
        self.fail_apply = fail_apply
        self.journal_path = journal_path
        self.previous: list[object] = []
        self.reverted: list[str] = []

    def prepare(
        self,
        args: dict[str, Any],
        previous: object,
        context: EffectContext,
    ) -> PreparedEffect:
        self.events.append(f"prepare:{context.effect_id}")
        self.previous.append(previous)
        return PreparedEffect(
            before_state={"prior": args["value"]},
            payload={"effect_id": context.effect_id},
            resources=tuple(args.get("prepared_resources", ())),
        )

    def apply(self, prepared: PreparedEffect, checkpoint: Any) -> object:
        effect_id = prepared.payload["effect_id"]
        assert isinstance(effect_id, str)
        if self.journal_path is not None:
            journal = json.loads(self.journal_path.read_text("utf-8"))
            entry = next(item for item in journal["effects"] if item["id"] == effect_id)
            assert entry["status"] == "prepared"
            assert entry["prepared"]["before_state"] is not None
        self.events.append(f"apply:{effect_id}")
        checkpoint.write("inside", {"effect_id": effect_id})
        if self.journal_path is not None:
            journal = json.loads(self.journal_path.read_text("utf-8"))
            entry = next(item for item in journal["effects"] if item["id"] == effect_id)
            assert entry["checkpoints"] == [
                {"name": "inside", "state": {"effect_id": effect_id}}
            ]
        if self.fail_apply:
            raise RuntimeError("apply failed")
        return {"applied": effect_id}

    def revert(self, context: EffectContext, before_state: object) -> None:
        self.events.append(f"revert:{context.effect_id}")
        self.reverted.append(context.effect_id)


def _plan(
    effect_id: str,
    *,
    effect_type: str = "record",
    version: int = 1,
    resources: tuple[str, ...] = ("kind:z", "kind:a"),
) -> EffectPlan:
    return EffectPlan(
        id=effect_id,
        type=effect_type,
        version=version,
        args={"value": effect_id},
        resources=resources,
    )


def _driver(
    tmp_path: Path,
    provider: _Provider,
    effect: _Effect,
    lock_manager: _LockManager,
    ids: Iterable[str],
) -> Driver:
    identifiers = iter(ids)
    return Driver(
        registry=EffectRegistry([effect]),
        providers=(provider,),
        config={},
        consumer="tests",
        consumer_version="1",
        engine_version="0.1.0",
        manifest_directory="state",
        lock_manager=lock_manager,
        id_factory=lambda: next(identifiers),
    )


def test_driver_plans_and_validates_every_effect_before_locking(tmp_path: Path) -> None:
    events: list[str] = []
    provider = _Provider(
        events,
        lambda _config, _context: (_plan("good"), _plan("bad", effect_type="missing")),
    )
    locks = _LockManager(events)
    effect = _Effect(events)
    driver = _driver(tmp_path, provider, effect, locks, ("install-1", "tx-1"))

    with pytest.raises(UnknownEffectError):
        driver.install(base_path=tmp_path)

    assert events == ["provider:plan"]
    assert locks.acquisitions == []
    assert list(tmp_path.iterdir()) == []


def test_driver_acquires_complete_sorted_resources_before_prepare_and_apply(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    plans = (_plan("one", resources=("kind:z",)), _plan("two", resources=("kind:a", "kind:z")))
    provider = _Provider(events, lambda _config, _context: plans)
    locks = _LockManager(events)
    effect = _Effect(
        events,
        journal_path=tmp_path / "state/transactions/tx-1/journal.json",
    )
    driver = _driver(tmp_path, provider, effect, locks, ("install-1", "tx-1"))

    result = driver.install(base_path=tmp_path)

    root_resource = canonical_resource_identity("path", tmp_path)
    assert locks.acquisitions == [
        canonicalize_resources((root_resource, "kind:z", "kind:a", "kind:z"))
    ]
    assert events == [
        "provider:plan",
        "locks:acquire",
        "locks:entered",
        "prepare:one",
        "apply:one",
        "prepare:two",
        "apply:two",
        "locks:released",
    ]
    assert result.status is OperationStatus.COMPLETED
    assert result.planned == ("one", "two")
    assert result.applied == ("one", "two")
    assert locks.leases[0].released


def test_driver_rejects_resources_discovered_only_during_prepare(tmp_path: Path) -> None:
    events: list[str] = []
    plan = EffectPlan(
        id="one",
        type="record",
        version=1,
        args={"value": "one", "prepared_resources": ["kind:late"]},
        resources=("kind:declared",),
    )
    provider = _Provider(events, lambda _config, _context: (plan,))
    locks = _LockManager(events)
    driver = _driver(tmp_path, provider, _Effect(events), locks, ("install-1", "tx-1"))

    with pytest.raises(ValueError, match="undeclared resources"):
        driver.install(base_path=tmp_path)

    assert "apply:one" not in events
    assert locks.leases[0].released


def test_failed_apply_keeps_previous_manifest_and_wal_and_releases_locks(
    tmp_path: Path,
) -> None:
    first_events: list[str] = []
    first_provider = _Provider(first_events, lambda _config, _context: (_plan("one"),))
    first_locks = _LockManager(first_events)
    _driver(
        tmp_path,
        first_provider,
        _Effect(first_events),
        first_locks,
        ("install-1", "tx-1"),
    ).install(base_path=tmp_path)
    committed_path = tmp_path / "state/manifest.json"
    committed_before = committed_path.read_bytes()

    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan("one"),))
    locks = _LockManager(events)
    driver = _driver(
        tmp_path,
        provider,
        _Effect(events, fail_apply=True),
        locks,
        ("tx-2",),
    )

    with pytest.raises(RuntimeError, match="apply failed"):
        driver.install(base_path=tmp_path)

    assert committed_path.read_bytes() == committed_before
    assert (tmp_path / "state/transactions/tx-2/journal.json").is_file()
    assert locks.leases[0].released
    with pytest.raises(IncompleteTransactionError):
        driver.install(base_path=tmp_path)


def test_reinstall_is_idempotent_and_matches_previous_state_by_effect_id(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    order = [(_plan("a"), _plan("b")), (_plan("b"), _plan("a"))]
    provider = _Provider(events, lambda _config, _context: order.pop(0))
    locks = _LockManager(events)
    effect = _Effect(events)
    driver = _driver(
        tmp_path, provider, effect, locks, ("install-1", "tx-1", "tx-2")
    )

    first = driver.install(base_path=tmp_path)
    second = driver.install(base_path=tmp_path)

    assert first.installation_id == second.installation_id == "install-1"
    assert effect.previous == [None, None, {"prior": "b"}, {"prior": "a"}]
    manifest = ManifestRepository(
        SafeFilesystem(tmp_path), manifest_directory="state"
    ).read()
    assert manifest is not None
    assert tuple(record.id for record in manifest.effects) == ("b", "a")


def test_empty_effect_id_gets_a_deterministic_derived_fallback(tmp_path: Path) -> None:
    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan(""),))
    locks = _LockManager(events)
    effect = _Effect(events)
    driver = _driver(
        tmp_path, provider, effect, locks, ("install-1", "tx-1", "tx-2")
    )

    first = driver.install(base_path=tmp_path)
    second = driver.install(base_path=tmp_path)

    assert first.planned == second.planned
    assert first.planned[0].startswith("record:")


def test_update_rejects_an_absent_installation_without_locks_or_writes(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan("one"),))
    locks = _LockManager(events)
    driver = _driver(tmp_path, provider, _Effect(events), locks, ("unused",))

    with pytest.raises(NoInstallationError):
        driver.update(base_path=tmp_path)

    assert events == []
    assert list(tmp_path.iterdir()) == []


def test_uninstall_validates_all_versions_then_replays_in_reverse_order(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan("a"), _plan("b")))
    locks = _LockManager(events)
    effect = _Effect(events)
    driver = _driver(
        tmp_path, provider, effect, locks, ("install-1", "tx-1", "tx-2")
    )
    driver.install(base_path=tmp_path)
    events.clear()

    result = driver.uninstall(base_path=tmp_path)

    assert events == [
        "locks:acquire",
        "locks:entered",
        "revert:b",
        "revert:a",
        "locks:released",
    ]
    assert result.reverted == ("b", "a")
    assert not (tmp_path / "state/manifest.json").exists()


def test_uninstall_refuses_unknown_effect_version_before_locks_or_mutation(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan("a"),))
    locks = _LockManager(events)
    effect = _Effect(events)
    driver = _driver(tmp_path, provider, effect, locks, ("install-1", "tx-1"))
    driver.install(base_path=tmp_path)
    manifest_path = tmp_path / "state/manifest.json"
    value = json.loads(manifest_path.read_text("utf-8"))
    value["effects"][0]["effect_version"] = 2
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    events.clear()
    locks.acquisitions.clear()

    with pytest.raises(UnsupportedEffectVersionError):
        driver.uninstall(base_path=tmp_path)

    assert locks.acquisitions == []
    assert manifest_path.is_file()


def test_define_installer_exposes_structured_library_operations(tmp_path: Path) -> None:
    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan("one"),))
    effect = _Effect(events)
    installer = define_installer(
        config={"consumer": "tests", "consumer_version": "1"},
        providers=(provider,),
        effects=(effect,),
        manifest_directory="state",
        lock_manager=_LockManager(events),
        id_factory=iter(("install-1", "tx-1", "tx-2")).__next__,
    )

    installed = installer.install(base_path=tmp_path)
    removed = installer.uninstall(base_path=tmp_path)

    assert installed.operation is Operation.INSTALL
    assert removed.operation is Operation.UNINSTALL
    assert removed.status is OperationStatus.COMPLETED
