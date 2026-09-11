from __future__ import annotations

import json
from pathlib import Path

import pytest

from minimalist_installer import (
    InvalidDistributionError,
    NoHostDetectedError,
    UnsafePathError,
    UnsupportedHostError,
)
from minimalist_installer.core.locks import canonical_resource_identity
from minimalist_installer.skills import (
    DISTRIBUTION_V1_SCHEMA,
    DetectionSignals,
    HostAdapter,
    HostDestinations,
    HostLayout,
    HostRegistry,
    Scope,
    SkillDistribution,
    SupportTier,
    load_distribution,
    plan_distribution,
)


def _write_tree(root: Path, files: dict[str, str | bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def _skill(
    root: Path,
    *,
    body: str = "Use {{FOO}}.\n",
    extra: dict[str, str | bytes] | None = None,
) -> Path:
    files: dict[str, str | bytes] = {
        "SKILL.md": "---\nname: demo\ndescription: A demo skill\n---\n" + body,
    }
    if extra:
        files.update(extra)
    return _write_tree(root, files)


def _host(
    host_id: str,
    *destinations: str,
    skill_file: str = "SKILL.md",
) -> HostAdapter:
    paths = destinations or (".agents/skills",)
    return HostAdapter(
        id=host_id,
        display_name=host_id,
        support_tier=SupportTier.LAYOUT_ONLY,
        destinations=HostDestinations(user=paths, project=paths),
        detection=DetectionSignals(executables=(host_id,)),
        layout=HostLayout(skill_file=skill_file),
    )


def _distribution(bundle: Path, **overrides: object) -> SkillDistribution:
    values: dict[str, object] = {
        "name": "demo",
        "version": "0.1.0",
        "bundle": bundle,
        "variables": {"FOO": "bar"},
    }
    values.update(overrides)
    return SkillDistribution(**values)


def test_toml_distribution_loads_and_resolves_bundle(tmp_path: Path) -> None:
    bundle = _skill(tmp_path / "agent_skill")
    descriptor = tmp_path / "skill.toml"
    descriptor.write_text(
        "\n".join(
            [
                'name = "lacuna-signer"',
                'version = "0.1.0"',
                'bundle = "agent_skill"',
                "",
                "[variables]",
                'LACUNA_SIGNER_BIN = "/usr/bin/lacuna-signer"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = load_distribution(descriptor)

    assert loaded.name == "lacuna-signer"
    assert loaded.version == "0.1.0"
    assert loaded.bundle == bundle
    assert loaded.variables == {"LACUNA_SIGNER_BIN": "/usr/bin/lacuna-signer"}


def test_toml_distribution_rejects_unknown_fields(tmp_path: Path) -> None:
    _skill(tmp_path / "agent_skill")
    descriptor = tmp_path / "skill.toml"
    descriptor.write_text(
        'name = "demo"\nversion = "0.1.0"\nbundle = "agent_skill"\napi_key = "nope"\n',
        encoding="utf-8",
    )

    with pytest.raises(InvalidDistributionError, match="unknown"):
        load_distribution(descriptor)


@pytest.mark.parametrize(
    "name",
    ("API_KEY", "LACUNA_SIGNER_API_KEY", "AUTH_TOKEN", "SECRET", "PASSWORD", "secret_path"),
)
def test_secret_variable_names_are_refused(tmp_path: Path, name: str) -> None:
    bundle = _skill(tmp_path / "bundle", body="plain\n")

    with pytest.raises(InvalidDistributionError, match="API_KEY|SECRET|TOKEN|PASSWORD"):
        SkillDistribution(
            name="demo",
            version="0.1.0",
            bundle=bundle,
            variables={name: "leaked"},
        )


def test_shared_agents_skills_is_one_physical_file_with_host_attribution(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bundle = _skill(tmp_path / "bundle")
    registry = HostRegistry(
        [
            _host("codex", ".agents/skills"),
            _host("grok", ".grok/skills", ".agents/skills"),
        ]
    )

    planned = plan_distribution(
        _distribution(bundle),
        hosts=("codex", "grok"),
        scope=Scope.USER,
        home=home,
        registry=registry,
    )
    by_destination = {
        plan.args["destination"]: plan for plan in planned.plans
    }
    agents_plan = by_destination[".agents/skills"]
    grok_plan = by_destination[".grok/skills"]
    agents_desired = list(agents_plan.args["desired"])
    grok_desired = list(grok_plan.args["desired"])
    agents_paths = [item["path"] for item in agents_desired]
    grok_paths = [item["path"] for item in grok_desired]
    shared = ".agents/skills/demo/SKILL.md"
    grok_only = ".grok/skills/demo/SKILL.md"
    attribution = {item.relative: item.host_ids for item in planned.files}

    assert len(planned.plans) == 2
    assert agents_plan.type == "reconcile_file_set"
    assert agents_plan.version == 1
    assert agents_paths.count("demo/SKILL.md") == 1
    assert grok_paths.count("demo/SKILL.md") == 1
    assert attribution[shared] == ("codex", "grok")
    assert attribution[grok_only] == ("grok",)
    assert "Use bar." in next(
        item["content"] for item in agents_desired if item["path"] == "demo/SKILL.md"
    )
    assert agents_plan.resources == (
        canonical_resource_identity("path", home / ".agents/skills"),
    )
    assert grok_plan.resources == (
        canonical_resource_identity("path", home / ".grok/skills"),
    )
    assert not (home / ".agents").exists()
    assert tuple(planned) == planned.plans


def test_different_bundle_files_colliding_on_one_destination_fail(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bundle = _skill(tmp_path / "bundle", extra={"notes.md": "other file\n"})
    registry = HostRegistry([_host("codex", ".agents/skills", skill_file="notes.md")])

    with pytest.raises(InvalidDistributionError, match="collid"):
        plan_distribution(
            _distribution(bundle),
            hosts=("codex",),
            scope=Scope.USER,
            home=home,
            registry=registry,
        )


def test_parent_skill_file_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bundle = _skill(tmp_path / "bundle", body="plain\n")
    registry = HostRegistry(
        [_host("codex", ".agents/skills", skill_file="../SKILL.md")]
    )

    with pytest.raises(UnsafePathError):
        plan_distribution(
            _distribution(bundle, variables={}),
            hosts=("codex",),
            scope=Scope.USER,
            home=home,
            registry=registry,
        )


def test_absolute_executable_is_injected_into_planned_skill_md(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    executable = tmp_path / "bin" / "lacuna-signer"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    absolute = str(executable.resolve())
    bundle = _skill(
        tmp_path / "bundle",
        body="Execute `{{LACUNA_SIGNER_BIN}} --help`.\n",
    )
    registry = HostRegistry([_host("codex", ".agents/skills")])

    planned = plan_distribution(
        SkillDistribution(
            name="lacuna-signer",
            version="0.1.0",
            bundle=bundle,
            variables={"LACUNA_SIGNER_BIN": absolute},
        ),
        hosts=("codex",),
        scope=Scope.USER,
        home=home,
        registry=registry,
    )
    content = planned.plans[0].args["desired"][0]["content"]

    assert Path(absolute).is_absolute()
    assert absolute in content
    assert "{{LACUNA_SIGNER_BIN}}" not in content


def test_unknown_host_id_is_unsupported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bundle = _skill(tmp_path / "bundle", body="plain\n")
    registry = HostRegistry([_host("codex", ".agents/skills")])

    with pytest.raises(UnsupportedHostError):
        plan_distribution(
            _distribution(bundle, variables={}),
            hosts=("missing-host",),
            scope=Scope.USER,
            home=home,
            registry=registry,
        )


def test_no_detected_host_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bundle = _skill(tmp_path / "bundle", body="plain\n")
    registry = HostRegistry([_host("codex", ".agents/skills")])

    with pytest.raises(NoHostDetectedError):
        plan_distribution(
            _distribution(bundle, variables={}),
            hosts=registry,
            scope=Scope.USER,
            home=home,
            registry=registry,
            environ={},
            search_path="",
        )


def test_non_utf8_asset_is_not_silently_corrupted_when_planning(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    original = b"\x89PNG\r\n\x1a\n\x00\xff{{FOO}}\xfe"
    bundle = _skill(tmp_path / "bundle", extra={"assets/icon.bin": original})
    registry = HostRegistry([_host("codex", ".agents/skills")])

    with pytest.raises(InvalidDistributionError, match="UTF-8"):
        plan_distribution(
            _distribution(bundle),
            hosts=("codex",),
            scope=Scope.USER,
            home=home,
            registry=registry,
        )


def test_distribution_schema_matches_runtime_and_rejects_unknown_fields() -> None:
    schema_path = (
        Path(__file__).parents[3] / "spec/schemas/skill-distribution-v1.schema.json"
    )

    assert json.loads(schema_path.read_text(encoding="utf-8")) == DISTRIBUTION_V1_SCHEMA
    assert DISTRIBUTION_V1_SCHEMA["additionalProperties"] is False
    assert DISTRIBUTION_V1_SCHEMA["required"] == ["name", "version", "bundle"]
    assert DISTRIBUTION_V1_SCHEMA["properties"]["variables"]["additionalProperties"] == {
        "type": "string"
    }
