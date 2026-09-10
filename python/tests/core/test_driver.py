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
    PlanDriftError,
    PlanContext,
    PreparedEffect,
    UnknownEffectError,
    UnsupportedEffectVersionError,
    define_installer,
)
from minimalist_installer.core.driver import Driver
from minimalist_installer.core.journal import TransactionRepository
from minimalist_installer.core.locks import canonical_resource_identity, canonicalize_resources
from minimalist_installer.core.manifest import ManifestRepository
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.core.registry import EffectRegistry


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
    def __init__(
        self,
        events: list[str],
        *,
        on_enters: Iterable[Callable[[], None] | None] = (),
    ) -> None:
        self.events = events
        self.on_enters = iter(on_enters)
        self.acquisitions: list[tuple[str, ...]] = []
        self.leases: list[_Lease] = []

    def acquire(self, resources: Iterable[str], *, timeout: float) -> _Lease:
        ordered = tuple(resources)
        self.events.append("locks:acquire")
        self.acquisitions.append(ordered)
        lease = _Lease(self.events, next(self.on_enters, None))
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
        args={"value": effect_id, "prepared_resources": list(resources)},
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


def test_driver_validates_effects_after_wal_authority_and_before_effect_locks(
    tmp_path: Path,
) -> None:
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

    root_resource = canonical_resource_identity("path", tmp_path)
    assert events == [
        "locks:acquire",
        "locks:entered",
        "locks:released",
        "provider:plan",
    ]
    assert locks.acquisitions == [(root_resource,)]
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
        (root_resource,),
        canonicalize_resources((root_resource, "kind:z", "kind:a", "kind:z")),
    ]
    assert events == [
        "locks:acquire",
        "locks:entered",
        "locks:released",
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


def test_single_missing_effect_id_falls_back_to_type_and_matches_across_updates(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    plans = iter(
        (
            (
                EffectPlan(
                    id="", type="record", version=1, args={"value": "v1"}
                ),
            ),
            (
                EffectPlan(
                    id="", type="record", version=1, args={"value": "v2"}
                ),
            ),
        )
    )
    provider = _Provider(events, lambda _config, _context: next(plans))
    locks = _LockManager(events)
    effect = _Effect(events)
    driver = _driver(
        tmp_path, provider, effect, locks, ("install-1", "tx-1", "tx-2")
    )

    first = driver.install(base_path=tmp_path)
    second = driver.install(base_path=tmp_path)

    assert first.planned == second.planned == ("record",)
    assert effect.previous == [None, {"prior": "v1"}]


def test_multiple_missing_ids_for_the_same_type_are_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    provider = _Provider(
        events,
        lambda _config, _context: (
            EffectPlan(id="", type="record", version=1, args={"value": "a"}),
            EffectPlan(id="", type="record", version=1, args={"value": "b"}),
        ),
    )
    locks = _LockManager(events)
    driver = _driver(tmp_path, provider, _Effect(events), locks, ("install-1",))

    with pytest.raises(PlanDriftError, match="explicit ids"):
        driver.install(base_path=tmp_path)

    assert locks.acquisitions == [
        (canonical_resource_identity("path", tmp_path),)
    ]
    assert list(tmp_path.iterdir()) == []


def test_reinstall_rejects_dropped_effect_ids_and_preserves_committed_state(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    plans = iter(((_plan("a"), _plan("b")), (_plan("a"),)))
    provider = _Provider(events, lambda _config, _context: next(plans))
    locks = _LockManager(events)
    driver = _driver(
        tmp_path, provider, _Effect(events), locks, ("install-1", "tx-1")
    )
    driver.install(base_path=tmp_path)
    manifest_path = tmp_path / "state/manifest.json"
    committed = manifest_path.read_bytes()
    acquisitions = len(locks.acquisitions)

    with pytest.raises(PlanDriftError, match="would drop"):
        driver.install(base_path=tmp_path)

    assert len(locks.acquisitions) == acquisitions + 1
    assert locks.leases[-1].released
    assert manifest_path.read_bytes() == committed
    assert not (tmp_path / "state/transactions").exists()


def test_reinstall_validates_every_prior_effect_before_plan_drift_check(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    plans = iter(((_plan("a"), _plan("b")), (_plan("a"),)))
    provider = _Provider(events, lambda _config, _context: next(plans))
    locks = _LockManager(events)
    driver = _driver(
        tmp_path, provider, _Effect(events), locks, ("install-1", "tx-1")
    )
    driver.install(base_path=tmp_path)
    manifest_path = tmp_path / "state/manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["effects"][1]["type"] = "removed-extension"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    acquisitions = len(locks.acquisitions)

    with pytest.raises(UnknownEffectError, match="removed-extension"):
        driver.install(base_path=tmp_path)

    assert len(locks.acquisitions) == acquisitions + 1
    assert locks.leases[-1].released


def test_authoritative_incomplete_wal_takes_precedence_over_plan_drift(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    plans = iter(((_plan("a"), _plan("b")), (_plan("a"),)))
    provider = _Provider(events, lambda _config, _context: next(plans))
    locks = _LockManager(events)
    driver = _driver(
        tmp_path, provider, _Effect(events), locks, ("install-1", "tx-1")
    )
    driver.install(base_path=tmp_path)
    repository = TransactionRepository(
        SafeFilesystem(tmp_path), manifest_directory="state"
    )
    repository.begin(
        transaction_id="active-tx",
        installation_id="install-1",
        operation=Operation.UPDATE,
        engine_version="0.1.0",
        plans=(),
        resources=("kind:a",),
    )

    with pytest.raises(IncompleteTransactionError, match="active-tx"):
        driver.install(base_path=tmp_path)


@pytest.mark.parametrize("operation", ["install", "update", "uninstall"])
def test_active_wal_precedes_absent_manifest_results(
    tmp_path: Path,
    operation: str,
) -> None:
    filesystem = SafeFilesystem(tmp_path)
    repository = TransactionRepository(filesystem, manifest_directory="state")
    repository.begin(
        transaction_id="active-tx",
        installation_id="install-1",
        operation=Operation.INSTALL,
        engine_version="0.1.0",
        plans=(),
        resources=(canonical_resource_identity("path", tmp_path),),
    )
    filesystem.close()
    sentinel = tmp_path / "partially-mutated.txt"
    sentinel.write_bytes(b"preserve incomplete state")
    events: list[str] = []
    locks = _LockManager(events)
    driver = _driver(
        tmp_path,
        _Provider(events, lambda _config, _context: (_plan("one"),)),
        _Effect(events),
        locks,
        ("candidate-installation",),
    )

    with pytest.raises(IncompleteTransactionError, match="active-tx"):
        getattr(driver, operation)(base_path=tmp_path)

    root = canonical_resource_identity("path", tmp_path)
    assert locks.acquisitions == [(root,)]
    assert "provider:plan" not in events
    assert sentinel.read_bytes() == b"preserve incomplete state"
    assert not (tmp_path / "state/manifest.json").exists()


def test_install_retries_planning_when_manifest_changes_before_lock_authority(
    tmp_path: Path,
) -> None:
    setup_events: list[str] = []
    setup = _driver(
        tmp_path,
        _Provider(setup_events, lambda _config, _context: (_plan("one"),)),
        _Effect(setup_events),
        _LockManager(setup_events),
        ("install-1", "tx-1"),
    )
    setup.install(base_path=tmp_path)
    manifest_path = tmp_path / "state/manifest.json"

    def change_manifest() -> None:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["transaction_id"] = "external-update"
        manifest["effects"][0]["before_state"] = {"prior": "authoritative"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan("one"),))
    locks = _LockManager(events, on_enters=(change_manifest, None))
    effect = _Effect(events)
    driver = _driver(tmp_path, provider, effect, locks, ("tx-2",))

    driver.install(base_path=tmp_path)

    assert len(locks.acquisitions) == 2
    assert effect.previous == [{"prior": "authoritative"}]
    assert events.count("provider:plan") == 2


def test_uninstall_retries_and_relocks_if_manifest_resources_change(
    tmp_path: Path,
) -> None:
    setup_events: list[str] = []
    setup = _driver(
        tmp_path,
        _Provider(
            setup_events,
            lambda _config, _context: (
                _plan("one", resources=("kind:old",)),
            ),
        ),
        _Effect(setup_events),
        _LockManager(setup_events),
        ("install-1", "tx-1"),
    )
    setup.install(base_path=tmp_path)
    manifest_path = tmp_path / "state/manifest.json"

    def change_manifest() -> None:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["transaction_id"] = "external-update"
        manifest["effects"][0]["resources"] = ["kind:new"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    events: list[str] = []
    locks = _LockManager(events, on_enters=(change_manifest, None))
    effect = _Effect(events)
    driver = _driver(
        tmp_path,
        _Provider(events, lambda _config, _context: (_plan("unused"),)),
        effect,
        locks,
        ("tx-2",),
    )

    driver.uninstall(base_path=tmp_path)

    assert "kind:old" in locks.acquisitions[0]
    assert "kind:new" in locks.acquisitions[1]
    assert effect.reverted == ["one"]


def test_effect_resources_persist_and_serialize_shared_external_locks(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    locks = _LockManager(events)
    shared = "external:shared-destination"
    roots = (tmp_path / "one", tmp_path / "two")
    for root in roots:
        root.mkdir()
    drivers = tuple(
        _driver(
            root,
            _Provider(
                events,
                lambda _config, _context: (
                    _plan("one", resources=(shared,)),
                ),
            ),
            _Effect(events),
            locks,
            (f"install-{index}", f"tx-install-{index}", f"tx-remove-{index}"),
        )
        for index, root in enumerate(roots, start=1)
    )

    for driver, root in zip(drivers, roots, strict=True):
        driver.install(base_path=root)
        manifest = ManifestRepository(
            SafeFilesystem(root), manifest_directory="state"
        ).read()
        assert manifest is not None
        assert manifest.effects[0].resources == (shared,)

    for driver, root in zip(drivers, roots, strict=True):
        driver.uninstall(base_path=root)

    assert len(locks.acquisitions) == 6
    assert all(
        shared in locks.acquisitions[index] for index in (1, 3, 4, 5)
    )
    assert all(
        shared not in locks.acquisitions[index] for index in (0, 2)
    )


def test_update_persists_prepared_revert_resources_for_uninstall_locking(
    tmp_path: Path,
) -> None:
    old_resource = "external:old-authority"
    new_resource = "external:new-plan"
    plans = iter(
        (
            (_plan("one", resources=(old_resource,)),),
            (
                EffectPlan(
                    id="one",
                    type="record",
                    version=1,
                    args={
                        "value": "v2",
                        "prepared_resources": [old_resource],
                    },
                    resources=(new_resource,),
                ),
            ),
        )
    )
    events: list[str] = []
    locks = _LockManager(events)
    driver = _driver(
        tmp_path,
        _Provider(events, lambda _config, _context: next(plans)),
        _Effect(events),
        locks,
        ("install-1", "tx-1", "tx-2", "tx-3"),
    )

    driver.install(base_path=tmp_path)
    driver.update(base_path=tmp_path)
    manifest = ManifestRepository(
        SafeFilesystem(tmp_path), manifest_directory="state"
    ).read()
    assert manifest is not None
    assert manifest.effects[0].resources == (old_resource,)
    driver.uninstall(base_path=tmp_path)

    assert old_resource in locks.acquisitions[2]
    assert new_resource in locks.acquisitions[2]
    assert old_resource in locks.acquisitions[3]
    assert new_resource not in locks.acquisitions[3]


def test_update_rejects_an_absent_installation_after_root_authority_without_writes(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    provider = _Provider(events, lambda _config, _context: (_plan("one"),))
    locks = _LockManager(events)
    driver = _driver(tmp_path, provider, _Effect(events), locks, ("unused",))

    with pytest.raises(NoInstallationError):
        driver.update(base_path=tmp_path)

    assert events == ["locks:acquire", "locks:entered", "locks:released"]
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


def test_uninstall_refuses_unknown_effect_version_before_revert_or_mutation(
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

    assert len(locks.acquisitions) == 1
    assert locks.leases[-1].released
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
