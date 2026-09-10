from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from minimalist_installer import (
    EffectContext,
    EffectPlan,
    InvalidEffectError,
    Operation,
    PlanContext,
    PreparedEffect,
    UnknownEffectError,
    UnsupportedEffectVersionError,
)
from minimalist_installer.core.registry import EffectRegistry
from minimalist_installer.providers import FileSetProvider


class _Effect:
    type = "record"
    version = 1

    def prepare(
        self,
        args: dict[str, Any],
        previous: object,
        context: EffectContext,
    ) -> PreparedEffect:
        return PreparedEffect(before_state=None, payload=args)

    def apply(self, prepared: PreparedEffect, checkpoint: object) -> object:
        return None

    def revert(
        self,
        context: EffectContext,
        before_state: object,
        checkpoint: object,
    ) -> None:
        return None


def test_registry_registers_and_resolves_an_exact_effect_version() -> None:
    effect = _Effect()
    registry = EffectRegistry([effect])

    assert registry.get("record", 1) is effect
    assert registry.has("record", 1)
    assert registry.list() == (("record", 1),)


def test_registry_rejects_duplicate_type_and_version() -> None:
    with pytest.raises(InvalidEffectError, match="already registered"):
        EffectRegistry([_Effect(), _Effect()])


@pytest.mark.parametrize(
    "effect",
    [
        object(),
        type("EmptyType", (), {"type": "", "version": 1})(),
        type("BooleanVersion", (), {"type": "x", "version": True})(),
        type("ZeroVersion", (), {"type": "x", "version": 0})(),
        type(
            "MissingPrepare",
            (),
            {"type": "x", "version": 1, "apply": lambda *_: None, "revert": lambda *_: None},
        )(),
        type(
            "MissingApply",
            (),
            {"type": "x", "version": 1, "prepare": lambda *_: None, "revert": lambda *_: None},
        )(),
        type(
            "MissingRevert",
            (),
            {"type": "x", "version": 1, "prepare": lambda *_: None, "apply": lambda *_: None},
        )(),
    ],
)
def test_registry_rejects_invalid_effect_contracts(effect: object) -> None:
    with pytest.raises(InvalidEffectError):
        EffectRegistry([effect])


def test_registry_distinguishes_unknown_type_from_unsupported_version() -> None:
    registry = EffectRegistry([_Effect()])

    with pytest.raises(UnknownEffectError):
        registry.require("missing", 1)
    with pytest.raises(UnsupportedEffectVersionError):
        registry.require("record", 2)


def test_file_set_provider_is_a_pure_planner_with_a_stable_explicit_id(
    tmp_path: Path,
) -> None:
    provider = FileSetProvider(effect_id="skills:user")
    config: Mapping[str, object] = {
        "files": [{"path": "SKILL.md", "content": "instructions"}],
    }
    context = PlanContext(base_path=tmp_path, operation=Operation.INSTALL)

    first = provider.plan(config, context)
    second = provider.plan(config, context)

    assert first == second
    assert first == (
        EffectPlan(
            id="skills:user",
            type="reconcile_file_set",
            version=1,
            args={
                "desired": [
                    {"path": "SKILL.md", "content": "instructions"},
                ]
            },
            resources=(f"path:{tmp_path.as_posix()}",),
        ),
    )
    assert list(tmp_path.iterdir()) == []


def test_file_set_provider_derives_an_id_that_survives_content_updates(
    tmp_path: Path,
) -> None:
    provider = FileSetProvider(destination="skills")
    context = PlanContext(base_path=tmp_path, operation=Operation.INSTALL)

    first = provider.plan(
        {"files": [{"path": "SKILL.md", "content": "v1"}]}, context
    )[0]
    updated = provider.plan(
        {"files": [{"path": "SKILL.md", "content": "v2"}]}, context
    )[0]

    assert first.id == updated.id
    assert first.id.startswith("reconcile_file_set:")
    assert first.args["destination"] == "skills"


@pytest.mark.parametrize("files", ["bad", ["bad"], [{"path": "only"}]])
def test_file_set_provider_rejects_invalid_config(files: object, tmp_path: Path) -> None:
    with pytest.raises((TypeError, ValueError)):
        FileSetProvider().plan(
            {"files": files},
            PlanContext(base_path=tmp_path, operation=Operation.INSTALL),
        )
