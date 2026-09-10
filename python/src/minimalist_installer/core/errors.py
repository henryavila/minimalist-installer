"""Typed installer errors with stable, serializable codes."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Mapping

from .models import JsonObject, JsonValue, Operation, _json_value


class ErrorCode(StrEnum):
    """Machine-readable error codes forming part of the public API."""

    INSTALLER_ERROR = "installer_error"
    UNSAFE_PATH = "unsafe_path"
    GREENFIELD_CONFLICT = "greenfield_conflict"
    MODIFIED_CONTENT = "modified_content"
    LOCK_TIMEOUT = "lock_timeout"
    INCOMPLETE_TRANSACTION = "incomplete_transaction"
    CORRUPT_MANIFEST = "corrupt_manifest"
    CORRUPT_TRANSACTION = "corrupt_transaction"
    INVALID_EFFECT = "invalid_effect"
    INVALID_PLAN = "invalid_plan"
    NO_INSTALLATION = "no_installation"
    UNKNOWN_EFFECT = "unknown_effect"
    UNSUPPORTED_EFFECT_VERSION = "unsupported_effect_version"
    RECOVERY_BLOCKED = "recovery_blocked"
    NO_HOST_DETECTED = "no_host_detected"
    UNSUPPORTED_HOST = "unsupported_host"
    INVALID_DISTRIBUTION = "invalid_distribution"
    NON_INTERACTIVE_INPUT_REQUIRED = "non_interactive_input_required"


class InstallerError(Exception):
    """Base library error carrying safe structured diagnostic context."""

    code = ErrorCode.INSTALLER_ERROR

    def __init__(
        self,
        message: str,
        *,
        operation: Operation | None = None,
        path: Path | None = None,
        resource: str | None = None,
        details: Mapping[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.operation = operation
        self.path = path
        self.resource = resource
        self.details = dict(details or {})

    def to_dict(self) -> JsonObject:
        """Return a JSON-safe diagnostic without changing the stable code."""

        return {
            "code": self.code.value,
            "message": self.message,
            "operation": self.operation.value if self.operation is not None else None,
            "path": str(self.path) if self.path is not None else None,
            "resource": self.resource,
            "details": _json_value(self.details),
        }


class UnsafePathError(InstallerError):
    code = ErrorCode.UNSAFE_PATH


class GreenfieldConflictError(InstallerError):
    code = ErrorCode.GREENFIELD_CONFLICT


class ModifiedContentError(InstallerError):
    code = ErrorCode.MODIFIED_CONTENT


class LockTimeoutError(InstallerError):
    code = ErrorCode.LOCK_TIMEOUT


class IncompleteTransactionError(InstallerError):
    code = ErrorCode.INCOMPLETE_TRANSACTION


class CorruptManifestError(InstallerError):
    code = ErrorCode.CORRUPT_MANIFEST


class CorruptTransactionError(InstallerError):
    code = ErrorCode.CORRUPT_TRANSACTION


class InvalidEffectError(InstallerError, ValueError):
    code = ErrorCode.INVALID_EFFECT


class InvalidPlanError(InstallerError, ValueError):
    code = ErrorCode.INVALID_PLAN


class NoInstallationError(InstallerError):
    code = ErrorCode.NO_INSTALLATION


class UnknownEffectError(InstallerError):
    code = ErrorCode.UNKNOWN_EFFECT


class UnsupportedEffectVersionError(InstallerError):
    code = ErrorCode.UNSUPPORTED_EFFECT_VERSION


class RecoveryBlockedError(InstallerError):
    code = ErrorCode.RECOVERY_BLOCKED


class NoHostDetectedError(InstallerError):
    code = ErrorCode.NO_HOST_DETECTED


class UnsupportedHostError(InstallerError):
    code = ErrorCode.UNSUPPORTED_HOST


class InvalidDistributionError(InstallerError):
    code = ErrorCode.INVALID_DISTRIBUTION


class NonInteractiveInputRequiredError(InstallerError):
    code = ErrorCode.NON_INTERACTIVE_INPUT_REQUIRED
