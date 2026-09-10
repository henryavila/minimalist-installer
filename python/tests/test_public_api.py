from __future__ import annotations

import json
import importlib.metadata
import inspect
from importlib import import_module, reload
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


def test_effect_plan_snapshots_and_deeply_freezes_caller_json() -> None:
    api = _api()
    source = {
        "files": [
            {"path": "SKILL.md", "metadata": {"owners": ["installer"]}},
        ],
    }
    plan = api.EffectPlan(
        id="skills:user",
        type="reconcile_file_set",
        version=1,
        args=source,
    )

    source["files"][0]["path"] = "MUTATED.md"
    source["files"][0]["metadata"]["owners"].append("caller")
    source["files"].append({"path": "EXTRA.md"})

    assert plan.to_dict()["args"] == {
        "files": [
            {"path": "SKILL.md", "metadata": {"owners": ["installer"]}},
        ],
    }
    with pytest.raises(TypeError):
        plan.args["extra"] = "blocked"
    with pytest.raises(TypeError):
        plan.args["files"][0]["path"] = "blocked"


def test_prepared_effect_snapshots_and_deeply_freezes_caller_json() -> None:
    api = _api()
    before_state = {"files": [{"path": "SKILL.md", "existed": False}]}
    payload = {"steps": [{"writes": ["SKILL.md"]}]}
    prepared = api.PreparedEffect(before_state=before_state, payload=payload)

    before_state["files"][0]["existed"] = True
    payload["steps"][0]["writes"].append("EXTRA.md")

    assert prepared.to_dict() == {
        "before_state": {
            "files": [{"path": "SKILL.md", "existed": False}],
        },
        "payload": {"steps": [{"writes": ["SKILL.md"]}]},
        "resources": [],
        "recoverable": True,
    }
    with pytest.raises(TypeError):
        prepared.before_state["files"][0]["path"] = "blocked"
    with pytest.raises(AttributeError):
        prepared.payload["steps"][0]["writes"].append("blocked")


@pytest.mark.parametrize("invalid_number", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("value_type", ["EffectPlan", "PreparedEffect"])
def test_public_json_values_reject_non_finite_numbers(
    value_type: str, invalid_number: float
) -> None:
    api = _api()

    with pytest.raises(ValueError, match="finite"):
        if value_type == "EffectPlan":
            api.EffectPlan(
                id="effect",
                type="test",
                version=1,
                args={"nested": [invalid_number]},
            )
        else:
            api.PreparedEffect(
                before_state={"nested": [invalid_number]},
                payload=None,
            )


@pytest.mark.parametrize("value_type", ["EffectPlan", "PreparedEffect"])
def test_public_json_values_reject_non_string_mapping_keys(value_type: str) -> None:
    api = _api()
    invalid = {1: "numeric", "1": "text"}

    with pytest.raises(TypeError, match="keys must be strings"):
        if value_type == "EffectPlan":
            api.EffectPlan(id="effect", type="test", version=1, args=invalid)
        else:
            api.PreparedEffect(before_state=invalid, payload=None)


def test_error_json_rejects_invalid_detail_keys_without_stringifying_them() -> None:
    api = _api()
    error = api.InstallerError("invalid details", details={1: "numeric", "1": "text"})

    with pytest.raises(TypeError, match="keys must be strings"):
        error.to_dict()


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

    class ProviderImplementation:
        def plan(self, config: object, context: object) -> tuple[object, ...]:
            return ()

    class CheckpointImplementation:
        def write(self, checkpoint: str, state: object) -> None:
            return None

    class EffectImplementation:
        type = "example"
        version = 1

        def prepare(self, args: object, previous: object, context: object) -> object:
            return object()

        def apply(self, prepared: object, checkpoint: object) -> object:
            return None

        def revert(self, context: object, before_state: object) -> None:
            return None

    assert isinstance(ProviderImplementation(), api.Provider)
    assert isinstance(CheckpointImplementation(), api.CheckpointWriter)
    assert isinstance(EffectImplementation(), api.Effect)
    assert not isinstance(object(), api.Provider)
    assert list(inspect.signature(api.Provider.plan).parameters) == [
        "self",
        "config",
        "context",
    ]
    assert list(inspect.signature(api.CheckpointWriter.write).parameters) == [
        "self",
        "checkpoint",
        "state",
    ]
    assert list(inspect.signature(api.Effect.prepare).parameters) == [
        "self",
        "args",
        "previous",
        "context",
    ]
    assert list(inspect.signature(api.Effect.apply).parameters) == [
        "self",
        "prepared",
        "checkpoint",
    ]
    assert list(inspect.signature(api.Effect.revert).parameters) == [
        "self",
        "context",
        "before_state",
    ]


def test_package_version_comes_from_installed_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api()

    with monkeypatch.context() as context:
        context.setattr(importlib.metadata, "version", lambda name: "9.8.7")
        assert reload(api).__version__ == "9.8.7"

    assert reload(api).__version__ == importlib.metadata.version("minimalist-installer")
