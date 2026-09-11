"""Whole-tree skill round trips and concurrent overlapping Driver installs."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from minimalist_installer import (
    FileSetProvider,
    OperationStatus,
    define_installer,
)
from minimalist_installer.core.path_safety import (
    SafeFilesystemBackendStatus,
    safe_filesystem_backend_status,
)
from minimalist_installer.skills import (
    DetectionSignals,
    HostAdapter,
    HostDestinations,
    HostLayout,
    HostRegistry,
    Scope,
    SkillDistribution,
    SupportTier,
    plan_distribution,
)
from minimalist_installer.tui.app import build_installer

requires_safe_fs = pytest.mark.skipif(
    safe_filesystem_backend_status() is SafeFilesystemBackendStatus.UNAVAILABLE,
    reason="safe filesystem backend unavailable on this platform",
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


def _skill_bundle(root: Path, *, body: str = "Use {{BIN}}.\n") -> Path:
    return _write_tree(
        root,
        {
            "SKILL.md": (
                "---\nname: demo\ndescription: Demo skill\n---\n" + body
            ),
            "references/notes.md": "notes for {{BIN}}\n",
        },
    )


def _host(host_id: str, *destinations: str) -> HostAdapter:
    paths = destinations or (".agents/skills",)
    return HostAdapter(
        id=host_id,
        display_name=host_id,
        support_tier=SupportTier.LAYOUT_ONLY,
        destinations=HostDestinations(user=paths, project=paths),
        detection=DetectionSignals(executables=(host_id,)),
        layout=HostLayout(skill_file="SKILL.md"),
    )


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


@requires_safe_fs
def test_skill_distribution_install_update_uninstall_round_trip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    before = _snapshot_tree(home)
    bundle = _skill_bundle(tmp_path / "bundle", body="Call {{BIN}} v1.\n")
    registry = HostRegistry(
        [
            _host("codex", ".agents/skills"),
            _host("grok", ".grok/skills", ".agents/skills"),
        ]
    )
    distribution = SkillDistribution(
        name="demo",
        version="0.1.0",
        bundle=bundle,
        variables={"BIN": "/usr/bin/demo"},
    )
    planned = plan_distribution(
        distribution,
        hosts=("codex", "grok"),
        scope=Scope.USER,
        home=home,
        registry=registry,
    )
    installer = build_installer(distribution, planned)

    installed = installer.install(base_path=home)
    assert installed.status is OperationStatus.COMPLETED
    agents_skill = home / ".agents" / "skills" / "demo" / "SKILL.md"
    grok_skill = home / ".grok" / "skills" / "demo" / "SKILL.md"
    assert agents_skill.is_file()
    assert grok_skill.is_file()
    assert "Call /usr/bin/demo v1." in agents_skill.read_text(encoding="utf-8")
    assert (home / ".agents" / "skills" / "demo" / "references" / "notes.md").is_file()

    updated_bundle = _skill_bundle(tmp_path / "bundle-v2", body="Call {{BIN}} v2.\n")
    updated = SkillDistribution(
        name="demo",
        version="0.2.0",
        bundle=updated_bundle,
        variables={"BIN": "/usr/bin/demo"},
    )
    planned_update = plan_distribution(
        updated,
        hosts=("codex", "grok"),
        scope=Scope.USER,
        home=home,
        registry=registry,
    )
    updater = build_installer(updated, planned_update)
    update_result = updater.update(base_path=home)
    assert update_result.status is OperationStatus.COMPLETED
    assert "Call /usr/bin/demo v2." in agents_skill.read_text(encoding="utf-8")
    assert "Call /usr/bin/demo v2." in grok_skill.read_text(encoding="utf-8")

    removed = updater.uninstall(base_path=home)
    assert removed.status is OperationStatus.COMPLETED
    assert not agents_skill.exists()
    assert not grok_skill.exists()
    assert not (home / ".minimalist-installer" / "manifest.json").exists()
    assert _snapshot_tree(home) == before


@requires_safe_fs
def test_concurrent_overlapping_installs_do_not_corrupt_manifests(tmp_path: Path) -> None:
    base = tmp_path / "shared"
    base.mkdir()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=10)

    def _install(consumer: str, relative: str, content: str) -> Path:
        barrier.wait()
        installer = define_installer(
            config={
                "consumer": consumer,
                "consumer_version": "1",
                "files": [{"path": relative, "content": content}],
            },
            providers=(FileSetProvider(effect_id=f"files:{consumer}"),),
            manifest_directory=f".mi-{consumer}",
            lock_timeout=30.0,
        )
        result = installer.install(base_path=base)
        assert result.status is OperationStatus.COMPLETED
        return base / f".mi-{consumer}" / "manifest.json"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_install, "alpha", "shared/alpha.txt", "alpha-body\n"),
            pool.submit(_install, "beta", "shared/beta.txt", "beta-body\n"),
        ]
        manifests: list[Path] = []
        for future in as_completed(futures):
            try:
                manifests.append(future.result())
            except BaseException as error:  # noqa: BLE001 - gather for assertion
                errors.append(error)

    assert errors == []
    assert (base / "shared" / "alpha.txt").read_text(encoding="utf-8") == "alpha-body\n"
    assert (base / "shared" / "beta.txt").read_text(encoding="utf-8") == "beta-body\n"
    for manifest_path in manifests:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert payload["effects"]
        installation = payload["installation"]
        assert isinstance(installation["id"], str) and installation["id"]
        assert installation["consumer"] in {"alpha", "beta"}
        json.dumps(payload)  # round-trip proves the document stayed well-formed
