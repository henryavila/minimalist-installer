from __future__ import annotations

from pathlib import Path

import pytest

from minimalist_installer.skills import (
    DetectionSignals,
    EvidenceKind,
    HostAdapter,
    HostDestinations,
    HostRegistry,
    Scope,
    SupportTier,
    detect_hosts,
)


def _bundled() -> HostRegistry:
    return HostRegistry.bundled(load_entry_points=False)


def _executable(directory: Path, name: str, sentinel: Path | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if sentinel is None:
        target.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    else:
        target.write_text(
            f"#!/bin/sh\nprintf ran > '{sentinel}'\n",
            encoding="utf-8",
        )
    target.chmod(0o755)
    return target


def _detect(
    tmp_path: Path,
    *,
    scope: Scope = Scope.USER,
    search_path: str = "",
    environ: dict[str, str] | None = None,
    home: Path | None = None,
    project: Path | None = None,
    registry: HostRegistry | None = None,
):
    if home is None:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
    return detect_hosts(
        scope=scope,
        registry=registry or _bundled(),
        home=home,
        project=project,
        environ={} if environ is None else environ,
        search_path=search_path,
    )


def _git_worktree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "objects").mkdir()
    (git / "refs").mkdir()
    (git / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
        encoding="utf-8",
    )
    return root


def _adapter(
    host_id: str,
    *,
    executables: tuple[str, ...] = (),
    environment: tuple[str, ...] = (),
    config_dirs: tuple[str, ...] = (),
    user: str = "skills",
) -> HostAdapter:
    return HostAdapter(
        id=host_id,
        display_name=host_id,
        support_tier=SupportTier.LAYOUT_ONLY,
        destinations=HostDestinations(user=(user,), project=(user,)),
        detection=DetectionSignals(
            executables=executables,
            environment=environment,
            config_dirs=config_dirs,
        ),
    )


def test_path_hit_detects_host_without_executing_the_binary(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    sentinel = tmp_path / "ran"
    _executable(bin_dir, "claude", sentinel)

    result = _detect(tmp_path, home=home, search_path=str(bin_dir))
    detection = next(item for item in result.detections if item.host.id == "claude-code")

    assert sentinel.exists() is False
    assert any(
        item.kind is EvidenceKind.EXECUTABLE and item.value == "claude"
        for item in detection.evidence
    )
    assert [item.host.id for item in result.detections] == ["claude-code"]


def test_environment_hit_detects_host(tmp_path: Path) -> None:
    result = _detect(tmp_path, environ={"CLAUDECODE": "1"})
    detection = next(item for item in result.detections if item.host.id == "claude-code")

    assert any(
        item.kind is EvidenceKind.ENVIRONMENT and item.value == "CLAUDECODE"
        for item in detection.evidence
    )
    assert [item.host.id for item in result.detections] == ["claude-code"]


def test_environment_prefix_hit_detects_cursor(tmp_path: Path) -> None:
    result = _detect(tmp_path, environ={"CURSOR_TRACE_ID": "1"})

    assert [item.host.id for item in result.detections] == ["cursor"]
    assert any(
        item.kind is EvidenceKind.ENVIRONMENT and item.value == "CURSOR_TRACE_ID"
        for item in result.detections[0].evidence
    )


def test_config_directory_hit_detects_host(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    result = _detect(tmp_path, home=home)
    detection = next(item for item in result.detections if item.host.id == "claude-code")

    assert any(
        item.kind is EvidenceKind.CONFIG_DIRECTORY and item.value == ".claude"
        for item in detection.evidence
    )
    assert [item.host.id for item in result.detections] == ["claude-code"]


def test_manifest_under_destination_is_evidence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = home / ".agents" / "skills"
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text("{}", encoding="utf-8")

    result = _detect(tmp_path, home=home)
    ids = {item.host.id for item in result.detections}

    assert "codex" in ids
    assert any(
        item.kind is EvidenceKind.MANIFEST
        for detection in result.detections
        if detection.host.id == "codex"
        for item in detection.evidence
    )


def test_no_host_returns_empty_detections_not_an_error(tmp_path: Path) -> None:
    result = _detect(tmp_path)

    assert result.detections == ()
    assert result.destinations == ()
    assert result.to_dict()["detections"] == []


def test_confidence_orders_stronger_evidence_higher(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".config-host").mkdir()
    bin_dir = tmp_path / "bin"
    _executable(bin_dir, "path-host-bin")
    registry = HostRegistry(
        [
            _adapter("config-host", config_dirs=(".config-host",), user="config-skills"),
            _adapter("env-host", environment=("ENV_HOST",), user="env-skills"),
            _adapter("path-host", executables=("path-host-bin",), user="path-skills"),
            _adapter(
                "combined-host",
                executables=("path-host-bin",),
                environment=("ENV_HOST",),
                user="combined-skills",
            ),
        ]
    )

    result = _detect(
        tmp_path,
        home=home,
        search_path=str(bin_dir),
        environ={"ENV_HOST": "1"},
        registry=registry,
    )
    ranked = [item.host.id for item in result.detections]
    confidences = {item.host.id: item.confidence for item in result.detections}

    assert ranked[0] == "combined-host"
    assert confidences["path-host"] > confidences["env-host"] > confidences["config-host"]
    assert confidences["combined-host"] > confidences["path-host"]


def test_shared_agents_skills_physical_path_is_deduplicated(tmp_path: Path) -> None:
    result = _detect(
        tmp_path,
        environ={
            "CODEX_HOME": "1",
            "GEMINI_API_KEY": "x",
            "GROK_API_KEY": "y",
        },
    )
    home = tmp_path / "home"
    shared = [
        item
        for item in result.destinations
        if item.path == (home / ".agents" / "skills")
    ]

    assert {item.host.id for item in result.detections} == {"codex", "gemini", "grok"}
    assert len(shared) == 1
    assert tuple(shared[0].host_ids) == ("codex", "gemini", "grok")
    paths = {item.path for item in result.destinations}
    assert home / ".gemini" / "skills" in paths
    assert home / ".grok" / "skills" in paths
    assert result.destinations[0].scope is Scope.USER


def test_user_and_project_destinations_use_different_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    project = _git_worktree(tmp_path / "project")
    (project / ".claude").mkdir()

    user_result = _detect(tmp_path, home=home, project=project, scope=Scope.USER)
    project_result = _detect(tmp_path, home=home, project=project, scope=Scope.PROJECT)

    user_paths = {item.path for item in user_result.destinations}
    project_paths = {item.path for item in project_result.destinations}

    assert user_paths == {home / ".claude" / "skills"}
    assert project_paths == {project / ".claude" / "skills"}
    assert user_result.scope is Scope.USER
    assert project_result.scope is Scope.PROJECT


def test_project_config_directory_is_evidence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = _git_worktree(tmp_path / "project")
    (project / ".cursor").mkdir()

    result = _detect(tmp_path, home=home, project=project, scope=Scope.PROJECT)

    assert [item.host.id for item in result.detections] == ["cursor"]
    assert any(
        item.kind is EvidenceKind.CONFIG_DIRECTORY
        for item in result.detections[0].evidence
    )
