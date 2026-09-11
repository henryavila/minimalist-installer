from __future__ import annotations

import sys
from pathlib import Path

import pytest

from minimalist_installer import InvalidDistributionError, UnsafePathError
from minimalist_installer.skills import load_bundle, render_bundle


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
    body: str = "Use the skill.\n",
    frontmatter: str | None = "name: demo\ndescription: A demo skill\n",
    extra: dict[str, str | bytes] | None = None,
) -> Path:
    skill = f"---\n{frontmatter}---\n{body}" if frontmatter is not None else body
    files: dict[str, str | bytes] = {"SKILL.md": skill}
    if extra:
        files.update(extra)
    return _write_tree(root, files)


def test_missing_skill_md_fails(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "skill.md").write_text("wrong case\n", encoding="utf-8")

    with pytest.raises(InvalidDistributionError, match="SKILL.md"):
        load_bundle(root)


def test_frontmatter_name_extracted_without_a_yaml_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel", None)
    root = _skill(tmp_path / "bundle")

    loaded = load_bundle(root)

    assert loaded.frontmatter.name == "demo"
    assert loaded.frontmatter.description == "A demo skill"
    bundle_source = (
        Path(__file__).parents[2]
        / "src"
        / "minimalist_installer"
        / "skills"
        / "bundle.py"
    ).read_text(encoding="utf-8").lower()
    assert "yaml" not in bundle_source
    assert "pyyaml" not in bundle_source


def test_frontmatter_may_be_omitted(tmp_path: Path) -> None:
    root = _skill(tmp_path / "bundle", frontmatter=None, body="No fence.\n")

    loaded = load_bundle(root)

    assert loaded.frontmatter.name is None
    assert loaded.frontmatter.description is None
    assert any(item.path == "SKILL.md" for item in loaded.files)


def test_invalid_frontmatter_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_tree(root, {"SKILL.md": "---\nname: demo\n"})

    with pytest.raises(InvalidDistributionError, match="frontmatter"):
        load_bundle(root)


def test_empty_frontmatter_name_fails_closed(tmp_path: Path) -> None:
    root = _skill(tmp_path / "bundle", frontmatter="name:\n")

    with pytest.raises(InvalidDistributionError, match="name"):
        load_bundle(root)


def test_recursive_regular_files_are_included_deterministically(tmp_path: Path) -> None:
    root = _skill(
        tmp_path / "bundle",
        extra={
            "references/guide.md": "See the guide.\n",
            "scripts/run.sh": "#!/bin/sh\nexit 0\n",
            "assets/note.txt": "asset\n",
        },
    )

    loaded = load_bundle(root)
    paths = [item.path for item in loaded.files]
    second = [item.path for item in load_bundle(root).files]

    assert paths == [
        "SKILL.md",
        "assets/note.txt",
        "references/guide.md",
        "scripts/run.sh",
    ]
    assert paths == second
    by_path = {item.path: item.data for item in loaded.files}
    assert by_path["references/guide.md"] == b"See the guide.\n"
    assert by_path["scripts/run.sh"].startswith(b"#!/bin/sh")


def test_symlink_to_outside_is_refused_and_not_copied(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "secret.bin"
    sentinel.write_bytes(b"outside-original")
    root = _skill(tmp_path / "bundle")
    link = root / "assets" / "leak.bin"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(sentinel)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    with pytest.raises(UnsafePathError):
        load_bundle(root)

    assert sentinel.read_bytes() == b"outside-original"


def test_bundle_root_inventory_uses_held_base_after_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.skills import bundle as bundle_mod

    trusted_parent = tmp_path / "trusted-parent"
    external_parent = tmp_path / "external-parent"
    root = _skill(trusted_parent / "bundle", body="trusted body\n")
    evil = _skill(external_parent / "bundle", body="evil body\n", extra={"planted.txt": "nope\n"})
    real_inventory = bundle_mod._inventory

    def inventory_after_retarget(filesystem: object) -> object:
        held_parent = tmp_path / "trusted-parent-held"
        trusted_parent.rename(held_parent)
        try:
            (tmp_path / "trusted-parent").symlink_to(
                external_parent, target_is_directory=True
            )
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"symlinks are unavailable in this environment: {error}")
        return real_inventory(filesystem)

    monkeypatch.setattr(bundle_mod, "_inventory", inventory_after_retarget)

    loaded = load_bundle(root)
    paths = [item.path for item in loaded.files]
    by_path = {item.path: item.data for item in loaded.files}

    assert paths == ["SKILL.md"]
    assert b"trusted body" in by_path["SKILL.md"]
    assert b"evil body" not in by_path["SKILL.md"]
    assert "planted.txt" not in paths
    assert (evil / "planted.txt").read_text(encoding="utf-8") == "nope\n"


def test_parent_path_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "secret.md"
    sentinel.write_text("outside-original\n", encoding="utf-8")
    root = _skill(tmp_path / "bundle")
    nested = root / "references"
    nested.mkdir()
    link = nested / "escape.md"
    try:
        link.symlink_to(Path("..") / ".." / "outside" / "secret.md")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable in this environment: {error}")

    with pytest.raises(UnsafePathError):
        load_bundle(root)

    assert sentinel.read_text(encoding="utf-8") == "outside-original\n"


def test_declared_variable_is_rendered(tmp_path: Path) -> None:
    root = _skill(tmp_path / "bundle", body="Run {{FOO}} now.\n")

    rendered = render_bundle(load_bundle(root), {"FOO": "/usr/bin/demo"})
    skill = next(item for item in rendered if item.path == "SKILL.md")

    assert b"Run /usr/bin/demo now.\n" in skill.data
    assert b"{{FOO}}" not in skill.data


def test_unknown_render_token_fails_closed(tmp_path: Path) -> None:
    root = _skill(tmp_path / "bundle", body="Run {{BAR}} now.\n")

    with pytest.raises(InvalidDistributionError, match="BAR"):
        render_bundle(load_bundle(root), {"FOO": "x"})


def test_undeclared_leftover_token_fails_closed(tmp_path: Path) -> None:
    root = _skill(tmp_path / "bundle", body="Run {{FOO}} now.\n")

    with pytest.raises(InvalidDistributionError, match="token"):
        render_bundle(load_bundle(root), {"FOO": "{{NESTED}}"})


def test_absolute_executable_is_injected_into_skill_md(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "lacuna-signer"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    root = _skill(
        tmp_path / "bundle",
        body="Execute `{{LACUNA_SIGNER_BIN}} --help`.\n",
    )

    rendered = render_bundle(
        load_bundle(root),
        {"LACUNA_SIGNER_BIN": str(executable.resolve())},
    )
    skill = next(item for item in rendered if item.path == "SKILL.md")
    injected = executable.resolve().as_posix().encode("utf-8")

    assert executable.resolve().is_absolute()
    assert injected in skill.data
    assert b"{{LACUNA_SIGNER_BIN}}" not in skill.data


def test_non_utf8_skill_md_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    _write_tree(root, {"SKILL.md": b"\xff\xfe not utf-8"})

    with pytest.raises(InvalidDistributionError, match="UTF-8"):
        load_bundle(root)


def test_binary_asset_is_copied_unrendered_and_not_silently_corrupted(
    tmp_path: Path,
) -> None:
    original = b"\x89PNG\r\n\x1a\n\x00\xff{{FOO}}\xfe"
    root = _skill(tmp_path / "bundle", extra={"assets/icon.bin": original})

    loaded = load_bundle(root)
    rendered = render_bundle(loaded, {"FOO": "replaced"})
    asset = next(item for item in rendered if item.path == "assets/icon.bin")
    source = next(item for item in loaded.files if item.path == "assets/icon.bin")

    assert source.data == original
    assert asset.data == original
    assert b"replaced" not in asset.data
