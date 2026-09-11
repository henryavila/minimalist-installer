from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from minimalist_installer import UnsafePathError
from minimalist_installer.skills import Scope, resolve_project_root, resolve_scope


def _git_worktree(root: Path, *, bare: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "objects").mkdir()
    (git / "refs").mkdir()
    (git / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        f"\tbare = {'true' if bare else 'false'}\n",
        encoding="utf-8",
    )
    return root


def _bare_git_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "objects").mkdir()
    (root / "refs").mkdir()
    (root / "files").mkdir()
    (root / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
        encoding="utf-8",
    )
    return root


def test_user_scope_uses_home_as_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    resolved = resolve_scope(Scope.USER, home=home)

    assert resolved.scope is Scope.USER
    assert resolved.root == home
    assert resolved.to_dict() == {"scope": "user", "root": str(home)}


def test_project_scope_resolves_git_root_from_nested_subdirectory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = _git_worktree(tmp_path / "repo")
    nested = root / "apps" / "pkg" / "src"
    nested.mkdir(parents=True)

    resolved = resolve_project_root(nested, home=home)
    scoped = resolve_scope(Scope.PROJECT, start=nested, home=home)

    assert resolved == root
    assert scoped.scope is Scope.PROJECT
    assert scoped.root == root
    assert scoped.to_dict()["root"] == str(root)


def test_project_scope_accepts_gitfile_worktree_without_following_gitdir(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "worktree"
    root.mkdir()
    (root / ".git").write_text("gitdir: /tmp/unrelated.git\n", encoding="utf-8")

    assert resolve_project_root(root, home=home) == root


def test_project_scope_does_not_execute_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = _git_worktree(tmp_path / "repo")
    nested = root / "src"
    nested.mkdir()

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("project scope must not execute git")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "execv", boom)

    assert resolve_project_root(nested, home=home) == root


def test_project_scope_refuses_filesystem_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(UnsafePathError, match="filesystem root"):
        resolve_project_root(Path("/"), home=home)


def test_missing_git_repository_reports_git_root_not_filesystem_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    nested = tmp_path / "nongit" / "nested"
    nested.mkdir(parents=True)

    with pytest.raises(UnsafePathError, match="git root") as excinfo:
        resolve_project_root(nested, home=home)

    assert "filesystem root" not in str(excinfo.value)


def test_project_scope_refuses_home_as_project(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _git_worktree(home)

    with pytest.raises(UnsafePathError, match="home"):
        resolve_project_root(home, home=home)
    with pytest.raises(UnsafePathError, match="home"):
        resolve_scope(Scope.PROJECT, start=home, home=home)


def test_project_scope_refuses_bare_repository(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bare = _bare_git_dir(tmp_path / "bare.git")

    with pytest.raises(UnsafePathError, match="bare"):
        resolve_project_root(bare, home=home)


def test_project_scope_refuses_worktree_marked_bare(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = _git_worktree(tmp_path / "repo", bare=True)

    with pytest.raises(UnsafePathError, match="bare"):
        resolve_project_root(root, home=home)


def test_project_scope_refuses_unwritable_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = _git_worktree(tmp_path / "repo")
    original = os.access

    def fake(path: object, mode: int, *args: object, **kwargs: object) -> bool:
        if mode == os.W_OK and Path(os.fspath(path)) == root:
            return False
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", fake)

    with pytest.raises(UnsafePathError, match="unwritable"):
        resolve_project_root(root, home=home)


def test_project_scope_refuses_symlink_escape(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = _git_worktree(tmp_path / "outside")
    start = tmp_path / "start"
    start.mkdir()
    link = start / "jump"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    with pytest.raises(UnsafePathError):
        resolve_project_root(link, home=home)
