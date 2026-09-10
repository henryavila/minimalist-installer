from __future__ import annotations

import json
from importlib import import_module
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType

import pytest


def _api() -> ModuleType:
    return import_module("minimalist_installer")


def _planning_values(api: ModuleType) -> dict[str, object]:
    return {
        "EffectPlan": api.EffectPlan(
            id="skills:user",
            type="reconcile_file_set",
            version=1,
            args={
                "destination": "skills",
                "files": [{"path": "SKILL.md", "content": "instructions"}],
            },
            resources=("path:/tmp/skills",),
        ),
        "PlanContext": api.PlanContext(
            base_path=Path("/tmp/install"),
            operation=api.Operation.INSTALL,
            installation_id="installation-1",
        ),
        "EffectContext": api.EffectContext(
            base_path=Path("/tmp/install"),
            manifest_dir=Path("/tmp/state"),
            operation=api.Operation.INSTALL,
            transaction_id="tx-1",
            effect_id="skills:user",
        ),
        "PreparedEffect": api.PreparedEffect(
            before_state={"files": [{"path": "SKILL.md", "existed": False}]},
            payload={"writes": [{"path": "SKILL.md", "content": "instructions"}]},
            resources=("path:/tmp/install/SKILL.md",),
        ),
    }


@pytest.mark.parametrize(
    ("value", "attribute", "replacement"),
    [
        (
            "EffectPlan",
            "id",
            "changed",
        ),
        (
            "PlanContext",
            "operation",
            "update",
        ),
        (
            "EffectContext",
            "effect_id",
            "changed",
        ),
        (
            "PreparedEffect",
            "resources",
            (),
        ),
    ],
)
def test_public_planning_values_are_immutable(
    value: str, attribute: str, replacement: object
) -> None:
    api = _api()
    values = _planning_values(api)
    if replacement == "update":
        replacement = api.Operation.UPDATE

    with pytest.raises(FrozenInstanceError):
        setattr(values[value], attribute, replacement)


@pytest.mark.parametrize(
    ("value_name", "expected"),
    [
        (
            "EffectPlan",
            {
                "id": "skills:user",
                "type": "reconcile_file_set",
                "version": 1,
                "args": {
                    "destination": "skills",
                    "files": [
                        {"path": "SKILL.md", "content": "instructions"},
                    ],
                },
                "resources": ["path:/tmp/skills"],
            },
        ),
        (
            "PlanContext",
            {
                "base_path": "/tmp/install",
                "operation": "install",
                "installation_id": "installation-1",
            },
        ),
        (
            "EffectContext",
            {
                "base_path": "/tmp/install",
                "manifest_dir": "/tmp/state",
                "operation": "install",
                "transaction_id": "tx-1",
                "effect_id": "skills:user",
            },
        ),
        (
            "PreparedEffect",
            {
                "before_state": {
                    "files": [{"path": "SKILL.md", "existed": False}],
                },
                "payload": {
                    "writes": [
                        {
                            "path": "SKILL.md",
                            "content": "instructions",
                        },
                    ],
                },
                "resources": ["path:/tmp/install/SKILL.md"],
                "recoverable": True,
            },
        ),
    ],
)
def test_public_planning_values_have_stable_json_representation(
    value_name: str, expected: dict[str, object]
) -> None:
    api = _api()
    value = _planning_values(api)[value_name]

    assert json.loads(json.dumps(value.to_dict())) == expected


def test_operation_results_are_immutable_and_json_serializable() -> None:
    api = _api()
    result = api.OperationResult(
        operation=api.Operation.INSTALL,
        status=api.OperationStatus.COMPLETED,
        transaction_id="tx-1",
        installation_id="installation-1",
        planned=("skills:user",),
        applied=("skills:user",),
        preserved=("user-edited-file",),
        conflicts=("existing-file",),
        stale=("old-file",),
        missing=("missing-file",),
        reverted=("rolled-back-effect",),
        selected_hosts=("codex",),
        resolved_destinations=(Path("/tmp/install"),),
        warnings=("preserved user content",),
    )

    encoded = json.dumps(result.to_dict(), sort_keys=True)
    decoded = json.loads(encoded)

    assert decoded["operation"] == "install"
    assert decoded["status"] == "completed"
    assert decoded["resolved_destinations"] == ["/tmp/install"]
    assert decoded["conflicts"] == ["existing-file"]
    with pytest.raises(FrozenInstanceError):
        result.status = api.OperationStatus.FAILED

    assert api.InstallResult is api.OperationResult
    assert api.UpdateResult is api.OperationResult
    assert api.RepairResult is api.OperationResult
    assert api.UninstallResult is api.OperationResult

    status = api.StatusResult(
        status=api.OperationStatus.COMPLETED,
        installed=True,
        installation_id="installation-1",
        incomplete_transaction_id=None,
        effect_ids=("skills:user",),
        warnings=(),
    )
    assert json.loads(json.dumps(status.to_dict()))["installed"] is True
    with pytest.raises(FrozenInstanceError):
        status.installed = False


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        ("UnsafePathError", "UNSAFE_PATH"),
        ("GreenfieldConflictError", "GREENFIELD_CONFLICT"),
        ("ModifiedContentError", "MODIFIED_CONTENT"),
        ("LockTimeoutError", "LOCK_TIMEOUT"),
        ("IncompleteTransactionError", "INCOMPLETE_TRANSACTION"),
        ("CorruptManifestError", "CORRUPT_MANIFEST"),
        ("UnknownEffectError", "UNKNOWN_EFFECT"),
        ("UnsupportedEffectVersionError", "UNSUPPORTED_EFFECT_VERSION"),
        ("RecoveryBlockedError", "RECOVERY_BLOCKED"),
        ("NoHostDetectedError", "NO_HOST_DETECTED"),
        ("UnsupportedHostError", "UNSUPPORTED_HOST"),
        ("InvalidDistributionError", "INVALID_DISTRIBUTION"),
        (
            "NonInteractiveInputRequiredError",
            "NON_INTERACTIVE_INPUT_REQUIRED",
        ),
    ],
)
def test_typed_errors_keep_stable_codes_when_serialized(
    error_type: str, code: str
) -> None:
    api = _api()
    error_class = getattr(api, error_type)
    error_code = getattr(api.ErrorCode, code)
    error = error_class(
        "operation could not continue",
        operation=api.Operation.INSTALL,
        path=Path("/tmp/install"),
        resource="path:/tmp/install",
        details={"reason": "test"},
    )

    assert isinstance(error, api.InstallerError)
    assert error.code is error_code
    assert json.loads(json.dumps(error.to_dict())) == {
        "code": error_code.value,
        "details": {"reason": "test"},
        "message": "operation could not continue",
        "operation": "install",
        "path": "/tmp/install",
        "resource": "path:/tmp/install",
    }


def test_provider_and_effect_contracts_are_public_protocols() -> None:
    api = _api()
    assert api.Provider.__name__ == "Provider"
    assert api.Effect.__name__ == "Effect"
    assert api.CheckpointWriter.__name__ == "CheckpointWriter"
