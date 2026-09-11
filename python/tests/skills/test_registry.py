from __future__ import annotations

import importlib.metadata
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from minimalist_installer import InvalidDistributionError
from minimalist_installer.core import path_safety
from minimalist_installer.skills import (
    DetectionSignals,
    HostAdapter,
    HostDestinations,
    HostRegistry,
    SupportTier,
    load_host_descriptor,
)

_BUNDLED_IDS = (
    "claude-code",
    "cursor",
    "codex",
    "gemini",
    "grok",
    "opencode",
    "github-copilot",
)

_VALID_EXTERNAL = """
id = "extra-host"
display_name = "Extra Host"
support_tier = "external"

[destinations]
user = [".extra/skills"]
project = [".extra/skills"]

[detection]
executables = ["extra-host"]
environment = []
config_dirs = [".extra"]
"""


class _FakeEntryPoint:
    def __init__(self, loaded: object, *, name: str = "extra-host") -> None:
        self.name = name
        self.group = "minimalist_installer.hosts"
        self.value = name
        self._loaded = loaded

    def load(self) -> object:
        return self._loaded


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, loaded: object) -> None:
    def fake(*, group: str | None = None, **_kwargs: object) -> tuple[object, ...]:
        if group == "minimalist_installer.hosts":
            return (_FakeEntryPoint(loaded),)
        return ()

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)
    registry_module = pytest.importorskip("minimalist_installer.skills.registry")
    if hasattr(registry_module, "entry_points"):
        monkeypatch.setattr(registry_module, "entry_points", fake)


def _bundled() -> HostRegistry:
    return HostRegistry.bundled(load_entry_points=False)


def test_bundled_toml_loads_all_seven_hosts_with_ids_tiers_and_paths() -> None:
    registry = _bundled()
    hosts = {host.id: host for host in registry.hosts}

    assert tuple(sorted(hosts)) == tuple(sorted(_BUNDLED_IDS))

    claude = hosts["claude-code"]
    assert claude.display_name == "Claude Code"
    assert claude.support_tier is SupportTier.VERIFIED
    assert claude.destinations.user == (".claude/skills",)
    assert claude.destinations.project == (".claude/skills",)
    assert "commands" not in " ".join(claude.destinations.user + claude.destinations.project)
    assert claude.layout.skill_file == "SKILL.md"

    cursor = hosts["cursor"]
    assert cursor.display_name == "Cursor"
    assert cursor.support_tier is SupportTier.VERIFIED
    assert cursor.destinations.user == (".cursor/skills",)
    assert cursor.destinations.project == (".cursor/skills",)

    codex = hosts["codex"]
    assert codex.display_name == "Codex"
    assert codex.support_tier is SupportTier.VERIFIED
    assert codex.destinations.user == (".agents/skills",)
    assert codex.destinations.project == (".agents/skills",)

    gemini = hosts["gemini"]
    assert gemini.display_name == "Gemini CLI"
    assert gemini.support_tier is SupportTier.LAYOUT_ONLY
    assert gemini.destinations.user == (".gemini/skills", ".agents/skills")
    assert gemini.destinations.project == (".gemini/skills", ".agents/skills")

    grok = hosts["grok"]
    assert grok.display_name == "Grok Build"
    assert grok.support_tier is SupportTier.VERIFIED
    assert grok.destinations.user == (".grok/skills", ".agents/skills")
    assert grok.destinations.project == (".grok/skills", ".agents/skills")
    assert "plugins" not in " ".join(grok.destinations.user + grok.destinations.project)
    assert "atomic-skills" not in " ".join(grok.destinations.user + grok.destinations.project)

    opencode = hosts["opencode"]
    assert opencode.display_name == "OpenCode"
    assert opencode.support_tier is SupportTier.LAYOUT_ONLY
    assert opencode.destinations.user == (".config/opencode/skills",)
    assert opencode.destinations.project == (".opencode/skills",)

    copilot = hosts["github-copilot"]
    assert copilot.display_name == "GitHub Copilot"
    assert copilot.support_tier is SupportTier.LAYOUT_ONLY
    assert copilot.destinations.user == (".copilot/skills",)
    assert copilot.destinations.project == (".github/skills",)
    assert copilot.detection.config_dirs == (".github/skills",)


def test_support_tiers_are_explicit_verified_layout_only_or_external() -> None:
    registry = _bundled()
    verified = {
        host.id
        for host in registry.hosts
        if host.support_tier is SupportTier.VERIFIED
    }
    layout_only = {
        host.id
        for host in registry.hosts
        if host.support_tier is SupportTier.LAYOUT_ONLY
    }

    assert verified == {"claude-code", "cursor", "codex", "grok"}
    assert layout_only == {"gemini", "opencode", "github-copilot"}
    assert SupportTier.EXTERNAL.value == "external"
    assert SupportTier.LAYOUT_ONLY.value == "layout-only"


def test_host_adapter_is_frozen_and_json_serializable() -> None:
    host = _bundled().get("claude-code")
    assert host is not None
    encoded = json.loads(json.dumps(host.to_dict()))

    assert encoded["id"] == "claude-code"
    assert encoded["support_tier"] == "verified"
    assert encoded["destinations"]["user"] == [".claude/skills"]
    with pytest.raises(FrozenInstanceError):
        host.display_name = "mutated"


@pytest.mark.parametrize(
    "toml_text",
    (
        _VALID_EXTERNAL + "\nunexpected_key = true\n",
        """
id = "extra-host"
display_name = "Extra Host"
support_tier = "external"
mystery = 1

[destinations]
user = [".extra/skills"]
project = [".extra/skills"]

[detection]
executables = ["extra-host"]
environment = []
config_dirs = [".extra"]
""",
        """
id = "extra-host"
display_name = "Extra Host"
support_tier = "external"

[destinations]
user = [".extra/skills"]
project = [".extra/skills"]
alias = "nope"

[detection]
executables = ["extra-host"]
environment = []
config_dirs = [".extra"]
""",
        """
id = "extra-host"
display_name = "Extra Host"
support_tier = "external"

[destinations]
user = [".extra/skills"]
project = [".extra/skills"]

[detection]
executables = ["extra-host"]
environment = []
config_dirs = [".extra"]
probe = "run-binary"
""",
    ),
)
def test_unknown_descriptor_keys_fail_closed(toml_text: str) -> None:
    with pytest.raises(InvalidDistributionError, match="unknown"):
        load_host_descriptor(toml_text)


def test_unknown_support_tier_fails_closed() -> None:
    with pytest.raises(InvalidDistributionError, match="support_tier"):
        load_host_descriptor(
            _VALID_EXTERNAL.replace('support_tier = "external"', 'support_tier = "experimental"')
        )


def test_entry_point_toml_path_appears_in_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = tmp_path / "extra-host.toml"
    descriptor.write_text(_VALID_EXTERNAL, encoding="utf-8")
    _patch_entry_points(monkeypatch, descriptor)

    registry = HostRegistry.bundled(load_entry_points=True)
    extra = registry.get("extra-host")

    assert extra is not None
    assert extra.display_name == "Extra Host"
    assert extra.support_tier is SupportTier.EXTERNAL
    assert extra.destinations.user == (".extra/skills",)
    assert registry.get("claude-code") is not None


def test_entry_point_adapter_object_appears_in_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = HostAdapter(
        id="object-host",
        display_name="Object Host",
        support_tier=SupportTier.EXTERNAL,
        destinations=HostDestinations(
            user=(".object/skills",),
            project=(".object/skills",),
        ),
        detection=DetectionSignals(executables=("object-host",)),
    )
    _patch_entry_points(monkeypatch, adapter)

    registry = HostRegistry.bundled(load_entry_points=True)
    loaded = registry.get("object-host")

    assert loaded is not None
    assert loaded.display_name == "Object Host"
    assert loaded.support_tier is SupportTier.EXTERNAL


def test_core_modules_do_not_contain_host_id_literals() -> None:
    root = Path(path_safety.__file__).parent
    forbidden = (
        "claude-code",
        "cursor",
        "codex",
        "gemini",
        "grok",
        "opencode",
        "github-copilot",
    )
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} must not contain {token!r}"


def test_relative_destination_rejects_dot_as_scope_root() -> None:
    with pytest.raises(ValueError, match="relative"):
        HostDestinations(user=(".",), project=(".skills",))
    with pytest.raises(ValueError, match="relative"):
        HostDestinations(user=(".skills",), project=(".",))


@pytest.mark.parametrize(
    "kwargs",
    (
        {"config_dirs": ("/etc",)},
        {"config_dirs": ("C:\\Windows",)},
        {"config_dirs": (".",)},
        {"config_dirs": ("..",)},
        {"config_dirs": ("foo/../bar",)},
        {"config_dirs": ("foo/./bar",)},
        {"config_dirs": ("foo//bar",)},
        {"environment": ("*",)},
        {"executables": ("/usr/bin/true",)},
        {"executables": ("bin/true",)},
    ),
)
def test_detection_signals_reject_unsafe_values(kwargs: dict[str, tuple[str, ...]]) -> None:
    with pytest.raises(ValueError):
        DetectionSignals(**kwargs)


def test_descriptor_rejects_unsafe_detection_signals() -> None:
    with pytest.raises(InvalidDistributionError):
        load_host_descriptor(
            _VALID_EXTERNAL.replace('config_dirs = [".extra"]', 'config_dirs = ["/etc"]')
        )
    with pytest.raises(InvalidDistributionError):
        load_host_descriptor(
            _VALID_EXTERNAL.replace('executables = ["extra-host"]', 'executables = ["/usr/bin/true"]')
        )
    with pytest.raises(InvalidDistributionError):
        load_host_descriptor(
            _VALID_EXTERNAL.replace("environment = []", 'environment = ["*"]')
        )
