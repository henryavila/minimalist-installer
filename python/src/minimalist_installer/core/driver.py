"""Consumer-neutral transactional orchestration for providers and effects."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from .errors import (
    IncompleteTransactionError,
    InvalidEffectError,
    InvalidPlanError,
    NoInstallationError,
    PlanDriftError,
)
from .journal import TransactionRepository
from .locks import (
    ResourceLockLease,
    ResourceLockManager,
    canonical_resource_identity,
    canonicalize_resources,
)
from .manifest import CommittedManifest, ManifestEffectRecord, ManifestRepository
from .models import (
    Effect,
    EffectContext,
    EffectPlan,
    JsonObject,
    JsonValue,
    Operation,
    OperationResult,
    OperationStatus,
    PlanContext,
    PreparedEffect,
    Provider,
    StatusResult,
    _json_value,
)
from .path_safety import SafeFilesystem
from .registry import EffectRegistry

_STABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class LockManager(Protocol):
    def acquire(
        self, resources: Iterable[str], *, timeout: float
    ) -> ResourceLockLease: ...


def _generated_id() -> str:
    return str(uuid4())


def _validated_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _STABLE_IDENTIFIER.fullmatch(value) is None:
        raise InvalidPlanError(f"{label} must be a safe stable identifier")
    return value


def _manifest_fingerprint(manifest: CommittedManifest | None) -> str:
    value: JsonValue = manifest.to_dict() if manifest is not None else None
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Driver:
    """Plan fully, lock fully, then execute through a durable WAL."""

    def __init__(
        self,
        *,
        registry: EffectRegistry,
        providers: Sequence[Provider],
        config: Mapping[str, object],
        consumer: str,
        consumer_version: str,
        engine_version: str,
        manifest_directory: str = ".minimalist-installer",
        lock_manager: LockManager | None = None,
        lock_timeout: float = 10.0,
        id_factory: Callable[[], str] = _generated_id,
    ) -> None:
        if not isinstance(registry, EffectRegistry):
            raise TypeError("registry must be an EffectRegistry")
        if not providers:
            raise ValueError("at least one provider is required")
        if any(not callable(getattr(provider, "plan", None)) for provider in providers):
            raise TypeError("every provider must define a callable plan method")
        if not isinstance(config, Mapping):
            raise TypeError("config must be a mapping")
        if not isinstance(consumer, str) or not consumer:
            raise ValueError("consumer must be non-empty text")
        if not isinstance(consumer_version, str) or not consumer_version:
            raise ValueError("consumer_version must be non-empty text")
        if not isinstance(engine_version, str) or not engine_version:
            raise ValueError("engine_version must be non-empty text")
        if not isinstance(manifest_directory, str) or not manifest_directory:
            raise ValueError("manifest_directory must be non-empty text")
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, int | float):
            raise TypeError("lock_timeout must be a number")
        if lock_timeout < 0:
            raise ValueError("lock_timeout must be non-negative")
        if not callable(id_factory):
            raise TypeError("id_factory must be callable")
        self.registry = registry
        self.providers = tuple(providers)
        self.config = dict(config)
        self.consumer = consumer
        self.consumer_version = consumer_version
        self.engine_version = engine_version
        self.manifest_directory = manifest_directory.rstrip("/\\")
        self._lock_manager = lock_manager
        self.lock_timeout = float(lock_timeout)
        self._id_factory = id_factory

    def _manager(self) -> LockManager:
        if self._lock_manager is None:
            self._lock_manager = ResourceLockManager()
        return self._lock_manager

    def _plan(
        self,
        *,
        base_path: Path,
        operation: Operation,
        installation_id: str,
    ) -> tuple[EffectPlan, ...]:
        context = PlanContext(
            base_path=base_path,
            operation=operation,
            installation_id=installation_id,
        )
        emitted_plans: list[EffectPlan] = []
        for provider in self.providers:
            emitted = provider.plan(self.config, context)
            if not isinstance(emitted, Sequence) or isinstance(emitted, str | bytes):
                raise InvalidPlanError("provider.plan must return a sequence of EffectPlan")
            for plan in emitted:
                if not isinstance(plan, EffectPlan):
                    raise InvalidPlanError("provider emitted a value that is not an EffectPlan")
                emitted_plans.append(
                    replace(plan, resources=canonicalize_resources(plan.resources))
                )

        type_counts: dict[str, int] = {}
        for plan in emitted_plans:
            type_counts[plan.type] = type_counts.get(plan.type, 0) + 1

        planned: list[EffectPlan] = []
        for plan in emitted_plans:
            effect_id = plan.id.strip()
            if not effect_id:
                if type_counts[plan.type] != 1:
                    raise PlanDriftError(
                        f'effect type "{plan.type}" occurs more than once '
                        "and requires explicit ids",
                        details={
                            "effect_type": plan.type,
                            "occurrences": type_counts[plan.type],
                        },
                    )
                effect_id = plan.type
            _validated_identifier(effect_id, "effect id")
            planned.append(
                plan if effect_id == plan.id else replace(plan, id=effect_id)
            )

        identifiers = [plan.id for plan in planned]
        duplicates = sorted(
            {identifier for identifier in identifiers if identifiers.count(identifier) > 1}
        )
        if duplicates:
            duplicate_values: list[JsonValue] = list(duplicates)
            raise InvalidPlanError(
                "planned effect ids must be unique",
                details={"duplicates": duplicate_values},
            )
        # Resolve every exact implementation before lock acquisition or WAL writes.
        for plan in planned:
            self.registry.require(plan.type, plan.version)
        return tuple(planned)

    @staticmethod
    def _resources(
        base_path: Path,
        plans: Sequence[EffectPlan],
        previous: CommittedManifest | None = None,
    ) -> tuple[str, ...]:
        root = canonical_resource_identity("path", base_path)
        resources = [
            root,
            *(resource for plan in plans for resource in plan.resources),
        ]
        if previous is not None:
            resources.extend(
                resource
                for record in previous.effects
                for resource in record.resources
            )
        return canonicalize_resources(resources)

    def _validate_prior(
        self,
        previous: CommittedManifest | None,
        plans: Sequence[EffectPlan],
    ) -> dict[str, ManifestEffectRecord]:
        if previous is None:
            return {}
        prior_by_id: dict[str, ManifestEffectRecord] = {}
        for record in previous.effects:
            self.registry.require(record.type, record.effect_version)
            prior_by_id[record.id] = record

        planned_ids = {plan.id for plan in plans}
        missing = tuple(
            record.id for record in previous.effects if record.id not in planned_ids
        )
        if missing:
            raise PlanDriftError(
                "new desired plan would drop previously committed effects",
                details={"missing_effect_ids": list(missing)},
            )
        for plan in plans:
            prior = prior_by_id.get(plan.id)
            if prior is not None and (
                prior.type != plan.type or prior.effect_version != plan.version
            ):
                raise PlanDriftError(
                    f'effect id "{plan.id}" changed type or version',
                    details={
                        "effect_id": plan.id,
                        "previous_type": prior.type,
                        "previous_version": prior.effect_version,
                        "planned_type": plan.type,
                        "planned_version": plan.version,
                    },
                )
        return prior_by_id

    def _authorize_absent_manifest(
        self,
        *,
        filesystem: SafeFilesystem,
        manifests: ManifestRepository,
        transactions: TransactionRepository,
        operation: Operation,
    ) -> CommittedManifest | None:
        """Check WAL authority under the root lock before trusting absence."""

        root_resources = canonicalize_resources(
            (canonical_resource_identity("path", filesystem.base),)
        )
        with self._manager().acquire(
            root_resources, timeout=self.lock_timeout
        ):
            active = transactions.active()
            if active is not None:
                raise IncompleteTransactionError(
                    f"transaction {active.transaction_id} is incomplete",
                    operation=operation,
                    details={"transaction_id": active.transaction_id},
                )
            return manifests.read()

    @staticmethod
    def _ensure_prepared_resources_locked(
        prepared: PreparedEffect, resources: tuple[str, ...]
    ) -> None:
        prepared_resources = canonicalize_resources(prepared.resources)
        undeclared = tuple(
            resource for resource in prepared_resources if resource not in resources
        )
        if undeclared:
            raise InvalidPlanError(
                "effect prepare returned undeclared resources",
                details={"resources": list(undeclared)},
            )

    def _mutate(self, *, base_path: Path, operation: Operation) -> OperationResult:
        with SafeFilesystem(base_path) as filesystem:
            manifests = ManifestRepository(
                filesystem, manifest_directory=self.manifest_directory
            )
            transactions = TransactionRepository(
                filesystem, manifest_directory=self.manifest_directory
            )
            candidate_installation_id: str | None = None
            for _attempt in range(3):
                preliminary = manifests.read()
                if preliminary is None:
                    authoritative_absence = self._authorize_absent_manifest(
                        filesystem=filesystem,
                        manifests=manifests,
                        transactions=transactions,
                        operation=operation,
                    )
                    if authoritative_absence is not None:
                        continue
                    if operation is Operation.UPDATE:
                        raise NoInstallationError(
                            "update requires an existing installation",
                            operation=operation,
                            path=manifests.display_path,
                        )
                if preliminary is None:
                    if candidate_installation_id is None:
                        candidate_installation_id = _validated_identifier(
                            self._id_factory(), "installation id"
                        )
                    installation_id = candidate_installation_id
                else:
                    installation_id = preliminary.installation_id
                plans = self._plan(
                    base_path=filesystem.base,
                    operation=operation,
                    installation_id=installation_id,
                )
                resources = self._resources(filesystem.base, plans, preliminary)
                preliminary_fingerprint = _manifest_fingerprint(preliminary)

                with self._manager().acquire(resources, timeout=self.lock_timeout):
                    active = transactions.active()
                    if active is not None:
                        raise IncompleteTransactionError(
                            f"transaction {active.transaction_id} is incomplete",
                            operation=operation,
                            details={"transaction_id": active.transaction_id},
                        )
                    authoritative = manifests.read()
                    if _manifest_fingerprint(authoritative) != preliminary_fingerprint:
                        continue
                    prior_by_id = self._validate_prior(authoritative, plans)
                    transaction_id = _validated_identifier(
                        self._id_factory(), "transaction id"
                    )
                    transactions.begin(
                        transaction_id=transaction_id,
                        installation_id=installation_id,
                        operation=operation,
                        engine_version=self.engine_version,
                        plans=plans,
                        resources=resources,
                    )
                    records: list[ManifestEffectRecord] = []
                    applied: list[str] = []
                    for plan in plans:
                        effect = self.registry.require(plan.type, plan.version)
                        context = EffectContext(
                            base_path=filesystem.base,
                            manifest_dir=filesystem.base / self.manifest_directory,
                            operation=operation,
                            transaction_id=transaction_id,
                            effect_id=plan.id,
                            filesystem=filesystem,
                        )
                        prior = prior_by_id.get(plan.id)
                        args_value = plan.to_dict()["args"]
                        if not isinstance(args_value, dict):
                            raise InvalidPlanError(
                                "effect args did not serialize to an object"
                            )
                        prepared = effect.prepare(
                            cast(JsonObject, args_value),
                            prior.before_state if prior is not None else None,
                            context,
                        )
                        if not isinstance(prepared, PreparedEffect):
                            raise InvalidEffectError(
                                f'effect "{plan.type}" prepare must return PreparedEffect'
                            )
                        if not prepared.recoverable:
                            raise InvalidEffectError(
                                f'effect "{plan.type}" is not recoverable in durable mode'
                            )
                        prepared = replace(
                            prepared,
                            resources=canonicalize_resources(
                                prepared.resources
                            ),
                        )
                        self._ensure_prepared_resources_locked(prepared, resources)
                        transactions.record_prepared(
                            transaction_id, plan.id, prepared
                        )
                        result = effect.apply(
                            prepared,
                            transactions.checkpoint_writer(
                                transaction_id, plan.id
                            ),
                        )
                        result_value = _json_value(result)
                        transactions.record_applied(
                            transaction_id, plan.id, result_value
                        )
                        applied.append(plan.id)
                        records.append(
                            ManifestEffectRecord(
                                id=plan.id,
                                type=plan.type,
                                effect_version=plan.version,
                                before_state=prepared.before_state,
                                resources=canonicalize_resources(
                                    prepared.resources
                                ),
                            )
                        )

                    transactions.checkpoint(transaction_id, "effects_applied")
                    transactions.checkpoint(transaction_id, "committing")
                    committed = manifests.commit(
                        installation_id=installation_id,
                        consumer=self.consumer,
                        consumer_version=self.consumer_version,
                        transaction_id=transaction_id,
                        engine_version=self.engine_version,
                        effects=records,
                    )
                    transactions.checkpoint(transaction_id, "manifest_committed")
                    transactions.complete(
                        transaction_id,
                        committed_transaction_id=committed.transaction_id,
                    )

                    return OperationResult(
                        operation=operation,
                        status=OperationStatus.COMPLETED,
                        transaction_id=transaction_id,
                        installation_id=installation_id,
                        planned=tuple(plan.id for plan in plans),
                        applied=tuple(applied),
                    )
            raise PlanDriftError(
                "committed manifest changed repeatedly while acquiring authority",
                operation=operation,
                path=manifests.display_path,
            )

    def install(self, *, base_path: Path) -> OperationResult:
        """Install or reconcile an already committed installation."""

        return self._mutate(base_path=Path(base_path), operation=Operation.INSTALL)

    def update(self, *, base_path: Path) -> OperationResult:
        """Reconcile only when a committed installation already exists."""

        return self._mutate(base_path=Path(base_path), operation=Operation.UPDATE)

    def uninstall(self, *, base_path: Path) -> OperationResult:
        """Replay committed effects in reverse and remove the manifest last."""

        with SafeFilesystem(base_path) as filesystem:
            manifests = ManifestRepository(
                filesystem, manifest_directory=self.manifest_directory
            )
            transactions = TransactionRepository(
                filesystem, manifest_directory=self.manifest_directory
            )
            for _attempt in range(3):
                preliminary = manifests.read()
                if preliminary is None:
                    authoritative_absence = self._authorize_absent_manifest(
                        filesystem=filesystem,
                        manifests=manifests,
                        transactions=transactions,
                        operation=Operation.UNINSTALL,
                    )
                    if authoritative_absence is not None:
                        continue
                    return OperationResult(
                        operation=Operation.UNINSTALL,
                        status=OperationStatus.COMPLETED,
                    )

                resources = self._resources(filesystem.base, (), preliminary)
                preliminary_fingerprint = _manifest_fingerprint(preliminary)

                with self._manager().acquire(
                    resources, timeout=self.lock_timeout
                ):
                    active = transactions.active()
                    if active is not None:
                        raise IncompleteTransactionError(
                            f"transaction {active.transaction_id} is incomplete",
                            operation=Operation.UNINSTALL,
                            details={"transaction_id": active.transaction_id},
                        )
                    authoritative = manifests.read()
                    if (
                        _manifest_fingerprint(authoritative)
                        != preliminary_fingerprint
                    ):
                        continue
                    if authoritative is None:
                        continue

                    effects: dict[str, Effect] = {}
                    for record in authoritative.effects:
                        effects[record.id] = self.registry.require(
                            record.type, record.effect_version
                        )
                    transaction_id = _validated_identifier(
                        self._id_factory(), "transaction id"
                    )
                    plans = tuple(
                        EffectPlan(
                            id=record.id,
                            type=record.type,
                            version=record.effect_version,
                            args={},
                            resources=record.resources,
                        )
                        for record in authoritative.effects
                    )
                    transactions.begin(
                        transaction_id=transaction_id,
                        installation_id=authoritative.installation_id,
                        operation=Operation.UNINSTALL,
                        engine_version=self.engine_version,
                        plans=plans,
                        resources=resources,
                    )
                    reverted: list[str] = []
                    for record in reversed(authoritative.effects):
                        prepared = PreparedEffect(
                            before_state=record.before_state,
                            payload=None,
                            resources=record.resources,
                        )
                        transactions.record_prepared(
                            transaction_id, record.id, prepared
                        )
                        context = EffectContext(
                            base_path=filesystem.base,
                            manifest_dir=filesystem.base
                            / self.manifest_directory,
                            operation=Operation.UNINSTALL,
                            transaction_id=transaction_id,
                            effect_id=record.id,
                            filesystem=filesystem,
                        )
                        effects[record.id].revert(
                            context,
                            record.before_state,
                            transactions.checkpoint_writer(
                                transaction_id, record.id
                            ),
                        )
                        transactions.record_reverted(
                            transaction_id, record.id
                        )
                        reverted.append(record.id)

                    transactions.checkpoint(
                        transaction_id, "effects_reverted"
                    )
                    transactions.checkpoint(transaction_id, "committing")
                    manifests.remove()
                    transactions.checkpoint(
                        transaction_id, "manifest_removed"
                    )
                    transactions.complete(transaction_id)

                    return OperationResult(
                        operation=Operation.UNINSTALL,
                        status=OperationStatus.COMPLETED,
                        transaction_id=transaction_id,
                        installation_id=authoritative.installation_id,
                        planned=tuple(
                            record.id for record in authoritative.effects
                        ),
                        reverted=tuple(reverted),
                    )
            raise PlanDriftError(
                "committed manifest changed repeatedly while acquiring authority",
                operation=Operation.UNINSTALL,
                path=manifests.display_path,
            )

    def status(self, *, base_path: Path) -> StatusResult:
        """Return a read-only summary; recovery policy is completed in Task 7."""

        with SafeFilesystem(base_path) as filesystem:
            manifests = ManifestRepository(
                filesystem, manifest_directory=self.manifest_directory
            )
            transactions = TransactionRepository(
                filesystem, manifest_directory=self.manifest_directory
            )
            root_resources = canonicalize_resources(
                (canonical_resource_identity("path", filesystem.base),)
            )
            with self._manager().acquire(
                root_resources, timeout=self.lock_timeout
            ):
                active = transactions.active()
                manifest = manifests.read()
                return StatusResult(
                    status=(
                        OperationStatus.BLOCKED
                        if active is not None
                        else OperationStatus.COMPLETED
                    ),
                    installed=manifest is not None,
                    installation_id=(
                        manifest.installation_id if manifest is not None else None
                    ),
                    incomplete_transaction_id=(
                        active.transaction_id if active is not None else None
                    ),
                    effect_ids=(
                        tuple(record.id for record in manifest.effects)
                        if manifest is not None
                        else ()
                    ),
                )


class Installer:
    """Bound declarative config with a small Pythonic operation surface."""

    def __init__(self, driver: Driver) -> None:
        self.driver = driver
        self.registry = driver.registry

    def install(self, *, base_path: Path) -> OperationResult:
        return self.driver.install(base_path=base_path)

    def update(self, *, base_path: Path) -> OperationResult:
        return self.driver.update(base_path=base_path)

    def uninstall(self, *, base_path: Path) -> OperationResult:
        return self.driver.uninstall(base_path=base_path)

    def status(self, *, base_path: Path) -> StatusResult:
        return self.driver.status(base_path=base_path)


def define_installer(
    *,
    config: Mapping[str, object],
    providers: Sequence[Provider],
    effects: Sequence[object] = (),
    manifest_directory: str | None = None,
    lock_manager: LockManager | None = None,
    lock_timeout: float = 10.0,
    id_factory: Callable[[], str] = _generated_id,
    engine_version: str = "0.1.0",
) -> Installer:
    """Build an installer from declarative config plus code extensions."""

    consumer = config.get("consumer", "consumer")
    consumer_version = config.get("consumer_version", "0+unknown")
    configured_manifest = config.get("manifest_dir", ".minimalist-installer")
    if not isinstance(consumer, str) or not consumer:
        raise ValueError("config.consumer must be non-empty text")
    if not isinstance(consumer_version, str) or not consumer_version:
        raise ValueError("config.consumer_version must be non-empty text")
    if manifest_directory is None:
        if not isinstance(configured_manifest, str) or not configured_manifest:
            raise ValueError("config.manifest_dir must be non-empty text")
        manifest_directory = configured_manifest
    from ..effects import ReconcileFileSetEffect

    registry = EffectRegistry((ReconcileFileSetEffect(), *effects))
    return Installer(
        Driver(
            registry=registry,
            providers=providers,
            config=config,
            consumer=consumer,
            consumer_version=consumer_version,
            engine_version=engine_version,
            manifest_directory=manifest_directory,
            lock_manager=lock_manager,
            lock_timeout=lock_timeout,
            id_factory=id_factory,
        )
    )


__all__ = ["Driver", "Installer", "LockManager", "define_installer"]
