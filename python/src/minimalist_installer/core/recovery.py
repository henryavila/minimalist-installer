"""Read-only recovery inspection and checkpointed repair of an active WAL."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence, cast

from .errors import (
    CorruptTransactionError,
    RecoveryBlockedError,
    UnknownEffectError,
    UnsupportedEffectVersionError,
)
from .journal import (
    BlobStatus,
    CleanupTombstone,
    EffectProgress,
    TransactionJournal,
    TransactionRepository,
)
from .locks import canonicalize_resources
from .manifest import ManifestEffectRecord, ManifestRepository
from .models import (
    EffectContext,
    EffectPlan,
    JsonObject,
    JsonValue,
    Operation,
    OperationResult,
    OperationStatus,
    PreparedEffect,
    _json_value,
)
from .path_safety import SafeFilesystem
from .registry import EffectRegistry


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Read-only diagnostic for an incomplete or residual installer transaction."""

    status: OperationStatus
    trusted: bool
    resumable: bool
    rollback_supported: bool
    operation: Operation | None = None
    transaction_id: str | None = None
    installation_id: str | None = None
    phase: str | None = None
    applied: tuple[str, ...] = ()
    prepared: tuple[str, ...] = ()
    planned: tuple[str, ...] = ()
    reverted: tuple[str, ...] = ()
    missing_blobs: tuple[str, ...] = ()
    remnants: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str | None = None
    cleanup: bool = False

    def to_dict(self) -> JsonObject:
        """Return the stable JSON representation used by presentation layers."""

        return {
            "status": self.status.value,
            "trusted": self.trusted,
            "resumable": self.resumable,
            "rollback_supported": self.rollback_supported,
            "operation": (
                self.operation.value if self.operation is not None else None
            ),
            "transaction_id": self.transaction_id,
            "installation_id": self.installation_id,
            "phase": self.phase,
            "applied": list(self.applied),
            "prepared": list(self.prepared),
            "planned": list(self.planned),
            "reverted": list(self.reverted),
            "missing_blobs": list(self.missing_blobs),
            "remnants": list(self.remnants),
            "warnings": list(self.warnings),
            "reason": self.reason,
            "cleanup": self.cleanup,
        }


class RecoveryCoordinator:
    """Inspect and repair the discoverable WAL without beginning a nested transaction."""

    def __init__(
        self,
        *,
        registry: EffectRegistry,
        filesystem: SafeFilesystem,
        manifests: ManifestRepository,
        transactions: TransactionRepository,
        engine_version: str,
        consumer: str,
        consumer_version: str,
        manifest_directory: str,
    ) -> None:
        self.registry = registry
        self.filesystem = filesystem
        self.manifests = manifests
        self.transactions = transactions
        self.engine_version = engine_version
        self.consumer = consumer
        self.consumer_version = consumer_version
        self.manifest_directory = manifest_directory

    def _remnants(self, active_id: str | None) -> tuple[str, ...]:
        return tuple(
            transaction_id
            for transaction_id in self.transactions.transaction_ids()
            if transaction_id != active_id
        )

    def _missing_blobs(self, journal: TransactionJournal) -> tuple[str, ...]:
        missing: list[str] = []
        for blob in journal.blobs:
            if blob.status is not BlobStatus.READY:
                missing.append(blob.digest)
                continue
            try:
                self.transactions.read_blob(journal.transaction_id, blob.digest)
            except (CorruptTransactionError, FileNotFoundError):
                missing.append(blob.digest)
        return tuple(missing)

    def _progress(
        self, journal: TransactionJournal
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        applied = tuple(
            effect.id
            for effect in journal.effects
            if effect.status is EffectProgress.APPLIED
        )
        prepared = tuple(
            effect.id
            for effect in journal.effects
            if effect.status is EffectProgress.PREPARED
        )
        planned = tuple(
            effect.id
            for effect in journal.effects
            if effect.status is EffectProgress.PLANNED
        )
        reverted = tuple(
            effect.id
            for effect in journal.effects
            if effect.status is EffectProgress.REVERTED
        )
        return applied, prepared, planned, reverted

    def _effect_error(
        self, journal: TransactionJournal
    ) -> UnknownEffectError | UnsupportedEffectVersionError | None:
        for effect in journal.effects:
            try:
                self.registry.require(effect.type, effect.effect_version)
            except (UnknownEffectError, UnsupportedEffectVersionError) as error:
                return error
        return None

    def inspect(self) -> RecoveryReport:
        """Describe recovery state without writing WAL, manifests, or user files."""

        committed = self.manifests.read()
        installation_id = (
            committed.installation_id if committed is not None else None
        )
        try:
            active = self.transactions.active()
        except CorruptTransactionError as error:
            return RecoveryReport(
                status=OperationStatus.BLOCKED,
                trusted=False,
                resumable=False,
                rollback_supported=False,
                installation_id=installation_id,
                remnants=self._remnants(None),
                reason=str(error),
            )

        remnants = self._remnants(
            active.transaction_id if active is not None else None
        )
        warnings = (("residual transaction directories",) if remnants else ())
        if active is None:
            return RecoveryReport(
                status=OperationStatus.COMPLETED,
                trusted=True,
                resumable=False,
                rollback_supported=False,
                installation_id=installation_id,
                remnants=remnants,
                warnings=warnings,
            )

        if isinstance(active, CleanupTombstone):
            return RecoveryReport(
                status=OperationStatus.BLOCKED,
                trusted=True,
                resumable=False,
                rollback_supported=False,
                operation=active.operation,
                transaction_id=active.transaction_id,
                installation_id=installation_id,
                phase="cleanup",
                remnants=remnants,
                warnings=warnings,
                reason="trusted cleanup awaiting completion",
                cleanup=True,
            )

        applied, prepared, planned, reverted = self._progress(active)
        missing_blobs = self._missing_blobs(active)
        effect_error = self._effect_error(active)
        committed_this = (
            committed is not None
            and committed.transaction_id == active.transaction_id
        )
        removed_this = "manifest_removed" in active.operation_checkpoints
        cleanup = committed_this or removed_this
        trusted = effect_error is None
        repairing = active.repairing()
        resuming = active.resuming()
        uninstall = active.operation is Operation.UNINSTALL
        uninstall_reverted = uninstall and bool(reverted)
        rollback_supported = (
            trusted
            and not cleanup
            and not missing_blobs
            and not uninstall_reverted
            and not resuming
            and all(
                effect.prepared is None or effect.prepared.recoverable
                for effect in active.effects
            )
        )
        resumable = (
            trusted
            and not cleanup
            and not repairing
            and active.operation
            in {Operation.INSTALL, Operation.UPDATE, Operation.UNINSTALL}
            and (not reverted or uninstall)
        )
        reason = None
        if effect_error is not None:
            reason = str(effect_error)
        elif missing_blobs:
            reason = "transaction blob is missing"
        elif cleanup:
            reason = "committed transaction awaiting cleanup"
        elif resuming:
            reason = "repair resume in progress"
        elif repairing:
            reason = "repair rollback in progress"
        elif uninstall_reverted:
            reason = "interrupted uninstall cannot restore the previous installation"
        else:
            reason = "incomplete transaction"
        return RecoveryReport(
            status=OperationStatus.BLOCKED,
            trusted=trusted,
            resumable=resumable,
            rollback_supported=rollback_supported,
            operation=active.operation,
            transaction_id=active.transaction_id,
            installation_id=active.installation_id,
            phase=active.phase.value,
            applied=applied,
            prepared=prepared,
            planned=planned,
            reverted=reverted,
            missing_blobs=missing_blobs,
            remnants=remnants,
            warnings=warnings,
            reason=reason,
            cleanup=cleanup,
        )

    def _gc_remnants(self, active_id: str | None) -> None:
        for transaction_id in self._remnants(active_id):
            self.transactions.gc_remnant(transaction_id)

    def _context(
        self, journal: TransactionJournal, effect_id: str, operation: Operation
    ) -> EffectContext:
        return EffectContext(
            base_path=self.filesystem.base,
            manifest_dir=self.filesystem.base / self.manifest_directory,
            operation=operation,
            transaction_id=journal.transaction_id,
            effect_id=effect_id,
            filesystem=self.filesystem,
        )

    @staticmethod
    def _record(journal: TransactionJournal, effect_id: str):
        matches = [effect for effect in journal.effects if effect.id == effect_id]
        if len(matches) != 1:
            raise CorruptTransactionError(
                "transaction does not contain exactly one effect",
                details={
                    "transaction_id": journal.transaction_id,
                    "effect_id": effect_id,
                },
            )
        return matches[0]

    @staticmethod
    def _manifest_record(record) -> ManifestEffectRecord:
        prepared = record.prepared
        if prepared is None:
            raise CorruptTransactionError(
                "applied effect is missing prepared state",
                details={"effect_id": record.id},
            )
        return ManifestEffectRecord(
            id=record.id,
            type=record.type,
            effect_version=record.effect_version,
            before_state=prepared.before_state,
            resources=canonicalize_resources(prepared.resources),
        )

    def _complete_committed(self, journal: TransactionJournal) -> OperationResult:
        committed = self.manifests.read()
        if (
            committed is not None
            and committed.transaction_id == journal.transaction_id
            and "manifest_committed" not in journal.operation_checkpoints
        ):
            self.transactions.checkpoint(journal.transaction_id, "manifest_committed")
        if (
            committed is None
            and journal.operation is Operation.UNINSTALL
            and "manifest_removed" not in journal.operation_checkpoints
        ):
            self.transactions.checkpoint(journal.transaction_id, "manifest_removed")
        committed_id = (
            committed.transaction_id
            if committed is not None
            and committed.transaction_id == journal.transaction_id
            else None
        )
        self.transactions.complete(
            journal.transaction_id,
            committed_transaction_id=committed_id,
        )
        self._gc_remnants(None)
        return OperationResult(
            operation=Operation.REPAIR,
            status=OperationStatus.COMPLETED,
            transaction_id=journal.transaction_id,
            installation_id=journal.installation_id,
            planned=journal.planned_effect_ids,
        )

    def _rollback(self, journal: TransactionJournal) -> OperationResult:
        missing = self._missing_blobs(journal)
        if missing:
            raise RecoveryBlockedError(
                "transaction blob is missing",
                operation=Operation.REPAIR,
                details={
                    "transaction_id": journal.transaction_id,
                    "missing_blobs": list(missing),
                },
            )
        error = self._effect_error(journal)
        if error is not None:
            raise error
        journal = self.transactions.begin_repair(journal.transaction_id)
        reverted: list[str] = []
        for record in reversed(journal.effects):
            journal = self.transactions.read(journal.transaction_id)
            current = self._record(journal, record.id)
            if current.status is EffectProgress.PLANNED:
                continue
            if current.status is EffectProgress.REVERTED:
                reverted.append(current.id)
                continue
            if current.status is EffectProgress.APPLIED:
                journal = self.transactions.record_reverting(
                    journal.transaction_id, current.id
                )
                current = self._record(journal, current.id)
            prepared = current.prepared
            if prepared is None:
                raise CorruptTransactionError(
                    "prepared rollback state is missing",
                    details={"effect_id": current.id},
                )
            if not prepared.recoverable:
                raise RecoveryBlockedError(
                    f'effect "{current.type}" is not recoverable',
                    operation=Operation.REPAIR,
                    details={"effect_id": current.id},
                )
            effect = self.registry.require(current.type, current.effect_version)
            effect.revert(
                self._context(journal, current.id, Operation.REPAIR),
                prepared.before_state,
                self.transactions.checkpoint_writer(
                    journal.transaction_id, current.id
                ),
            )
            self.transactions.record_reverted(journal.transaction_id, current.id)
            reverted.append(current.id)
        journal = self.transactions.read(journal.transaction_id)
        if "effects_reverted" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "effects_reverted"
            )
        if "rolled_back" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "rolled_back"
            )
        self.transactions.complete(journal.transaction_id)
        self._gc_remnants(None)
        committed = self.manifests.read()
        return OperationResult(
            operation=Operation.REPAIR,
            status=OperationStatus.COMPLETED,
            transaction_id=journal.transaction_id,
            installation_id=(
                committed.installation_id
                if committed is not None
                else journal.installation_id
            ),
            planned=journal.planned_effect_ids,
            reverted=tuple(reverted),
        )

    @staticmethod
    def _has_reverted(journal: TransactionJournal) -> bool:
        return any(
            effect.status is EffectProgress.REVERTED for effect in journal.effects
        )

    def _abort_unmutated_uninstall(
        self, journal: TransactionJournal
    ) -> OperationResult:
        error = self._effect_error(journal)
        if error is not None:
            raise error
        if self._has_reverted(journal):
            raise RecoveryBlockedError(
                "interrupted uninstall cannot restore the previous installation",
                operation=Operation.REPAIR,
                details={
                    "transaction_id": journal.transaction_id,
                    "rollback_supported": False,
                },
            )
        journal = self.transactions.begin_repair(journal.transaction_id)
        if "rolled_back" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "rolled_back"
            )
        self.transactions.complete(journal.transaction_id)
        self._gc_remnants(None)
        committed = self.manifests.read()
        return OperationResult(
            operation=Operation.REPAIR,
            status=OperationStatus.COMPLETED,
            transaction_id=journal.transaction_id,
            installation_id=(
                committed.installation_id
                if committed is not None
                else journal.installation_id
            ),
            planned=journal.planned_effect_ids,
        )

    def _continue_uninstall(self, journal: TransactionJournal) -> OperationResult:
        error = self._effect_error(journal)
        if error is not None:
            raise error
        journal = self.transactions.begin_repair(
            journal.transaction_id, resume=True
        )
        committed = self.manifests.read()
        prior = (
            {record.id: record for record in committed.effects}
            if committed is not None
            else {}
        )
        reverted: list[str] = []
        for record in reversed(journal.effects):
            journal = self.transactions.read(journal.transaction_id)
            current = self._record(journal, record.id)
            if current.status is EffectProgress.REVERTED:
                reverted.append(current.id)
                continue
            if current.status is EffectProgress.PLANNED:
                before_state: JsonValue = None
                resources = current.resources
                if current.id in prior:
                    before_state = prior[current.id].before_state
                    resources = prior[current.id].resources
                prepared = PreparedEffect(
                    before_state=before_state,
                    payload=None,
                    resources=resources,
                )
                journal = self.transactions.record_prepared(
                    journal.transaction_id, current.id, prepared
                )
                current = self._record(journal, current.id)
            effect = self.registry.require(current.type, current.effect_version)
            before = (
                current.prepared.before_state
                if current.prepared is not None
                else None
            )
            effect.revert(
                self._context(journal, current.id, Operation.UNINSTALL),
                before,
                self.transactions.checkpoint_writer(
                    journal.transaction_id, current.id
                ),
            )
            self.transactions.record_reverted(journal.transaction_id, current.id)
            reverted.append(current.id)
        journal = self.transactions.read(journal.transaction_id)
        if "effects_reverted" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "effects_reverted"
            )
        if "committing" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "committing"
            )
        if self.manifests.read() is not None:
            self.manifests.remove()
        if "manifest_removed" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "manifest_removed"
            )
        self.transactions.complete(journal.transaction_id)
        self._gc_remnants(None)
        return OperationResult(
            operation=Operation.REPAIR,
            status=OperationStatus.COMPLETED,
            transaction_id=journal.transaction_id,
            installation_id=journal.installation_id,
            planned=journal.planned_effect_ids,
            reverted=tuple(reverted),
        )

    @staticmethod
    def _compatible(journal: TransactionJournal, plans: Sequence[EffectPlan]) -> bool:
        if tuple(plan.id for plan in plans) != journal.planned_effect_ids:
            return False
        for plan, record in zip(plans, journal.effects, strict=True):
            if plan.type != record.type or plan.version != record.effect_version:
                return False
        return True

    def _resume_forward(self, journal: TransactionJournal) -> OperationResult:
        error = self._effect_error(journal)
        if error is not None:
            raise error
        committed = self.manifests.read()
        prior = (
            {record.id: record for record in committed.effects}
            if committed is not None
            else {}
        )
        applied: list[str] = []
        records: list[ManifestEffectRecord] = []
        for record in journal.effects:
            journal = self.transactions.read(journal.transaction_id)
            current = self._record(journal, record.id)
            if current.status is EffectProgress.REVERTED:
                raise RecoveryBlockedError(
                    "cannot resume a rollback in progress",
                    operation=Operation.REPAIR,
                    details={"transaction_id": journal.transaction_id},
                )
            if current.status is EffectProgress.APPLIED:
                applied.append(current.id)
                records.append(self._manifest_record(current))
                continue
            effect = self.registry.require(current.type, current.effect_version)
            context = self._context(journal, current.id, journal.operation)
            if current.status is EffectProgress.PLANNED:
                args_value = _json_value(current.args)
                if not isinstance(args_value, dict):
                    raise RecoveryBlockedError(
                        "effect args did not serialize to an object",
                        operation=Operation.REPAIR,
                    )
                previous = prior.get(current.id)
                prepared = effect.prepare(
                    cast(JsonObject, args_value),
                    previous.before_state if previous is not None else None,
                    context,
                )
                if not isinstance(prepared, PreparedEffect):
                    raise RecoveryBlockedError(
                        f'effect "{current.type}" prepare must return PreparedEffect',
                        operation=Operation.REPAIR,
                    )
                if not prepared.recoverable:
                    raise RecoveryBlockedError(
                        f'effect "{current.type}" is not recoverable in durable mode',
                        operation=Operation.REPAIR,
                    )
                prepared = replace(
                    prepared,
                    resources=canonicalize_resources(prepared.resources),
                    filesystem=self.filesystem,
                )
                journal = self.transactions.record_prepared(
                    journal.transaction_id, current.id, prepared
                )
                current = self._record(journal, current.id)
            prepared_state = current.prepared
            if prepared_state is None:
                raise CorruptTransactionError(
                    "prepared state is missing",
                    details={"effect_id": current.id},
                )
            prepared_state = replace(prepared_state, filesystem=self.filesystem)
            result = effect.apply(
                prepared_state,
                self.transactions.checkpoint_writer(
                    journal.transaction_id, current.id
                ),
            )
            self.transactions.record_applied(
                journal.transaction_id, current.id, _json_value(result)
            )
            journal = self.transactions.read(journal.transaction_id)
            current = self._record(journal, current.id)
            applied.append(current.id)
            records.append(self._manifest_record(current))
        journal = self.transactions.read(journal.transaction_id)
        if "effects_applied" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "effects_applied"
            )
        if "committing" not in journal.operation_checkpoints:
            journal = self.transactions.checkpoint(
                journal.transaction_id, "committing"
            )
        committed = self.manifests.commit(
            installation_id=journal.installation_id,
            consumer=self.consumer,
            consumer_version=self.consumer_version,
            transaction_id=journal.transaction_id,
            engine_version=self.engine_version,
            effects=records,
        )
        if "manifest_committed" not in journal.operation_checkpoints:
            self.transactions.checkpoint(
                journal.transaction_id, "manifest_committed"
            )
        self.transactions.complete(
            journal.transaction_id,
            committed_transaction_id=committed.transaction_id,
        )
        self._gc_remnants(None)
        return OperationResult(
            operation=Operation.REPAIR,
            status=OperationStatus.COMPLETED,
            transaction_id=journal.transaction_id,
            installation_id=journal.installation_id,
            planned=journal.planned_effect_ids,
            applied=tuple(applied),
        )

    def repair(
        self,
        *,
        resume: bool = False,
        plans: Sequence[EffectPlan] | None = None,
    ) -> OperationResult:
        """Roll back, resume, or finish cleanup of the active transaction."""

        try:
            active = self.transactions.active()
        except CorruptTransactionError:
            raise

        if active is None:
            self._gc_remnants(None)
            committed = self.manifests.read()
            return OperationResult(
                operation=Operation.REPAIR,
                status=OperationStatus.COMPLETED,
                installation_id=(
                    committed.installation_id if committed is not None else None
                ),
            )

        if isinstance(active, CleanupTombstone):
            self.transactions.complete(
                active.transaction_id,
                committed_transaction_id=(
                    active.proof_transaction_id
                    if active.proof_kind.value == "committed_manifest"
                    else None
                ),
            )
            self._gc_remnants(None)
            committed = self.manifests.read()
            return OperationResult(
                operation=Operation.REPAIR,
                status=OperationStatus.COMPLETED,
                transaction_id=active.transaction_id,
                installation_id=(
                    committed.installation_id if committed is not None else None
                ),
            )

        journal = active
        report = self.inspect()
        if not report.trusted:
            error = self._effect_error(journal)
            if error is not None:
                raise error
            raise RecoveryBlockedError(
                report.reason or "recovery is blocked",
                operation=Operation.REPAIR,
                details={"transaction_id": journal.transaction_id},
            )

        committed = self.manifests.read()
        if (
            committed is not None
            and committed.transaction_id == journal.transaction_id
        ) or "manifest_removed" in journal.operation_checkpoints:
            return self._complete_committed(journal)

        if journal.resuming():
            if journal.operation is Operation.UNINSTALL:
                return self._continue_uninstall(journal)
            return self._resume_forward(journal)

        if journal.repairing():
            if journal.operation is Operation.UNINSTALL:
                if self._has_reverted(journal):
                    raise RecoveryBlockedError(
                        "interrupted uninstall cannot restore the previous installation",
                        operation=Operation.REPAIR,
                        details={
                            "transaction_id": journal.transaction_id,
                            "rollback_supported": False,
                        },
                    )
                return self._abort_unmutated_uninstall(journal)
            return self._rollback(journal)

        if resume:
            if plans is None or not self._compatible(journal, plans):
                raise RecoveryBlockedError(
                    "resume plan is incompatible with the write-ahead journal",
                    operation=Operation.REPAIR,
                    details={"transaction_id": journal.transaction_id},
                )
            if journal.operation is Operation.UNINSTALL:
                return self._continue_uninstall(journal)
            return self._resume_forward(journal)

        if journal.operation is Operation.UNINSTALL:
            if self._has_reverted(journal):
                raise RecoveryBlockedError(
                    "interrupted uninstall cannot restore the previous installation",
                    operation=Operation.REPAIR,
                    details={
                        "transaction_id": journal.transaction_id,
                        "rollback_supported": False,
                    },
                )
            return self._abort_unmutated_uninstall(journal)
        return self._rollback(journal)


__all__ = ["RecoveryCoordinator", "RecoveryReport"]
