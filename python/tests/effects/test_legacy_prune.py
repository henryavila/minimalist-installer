from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from minimalist_installer import EffectContext, InvalidEffectError, ModifiedContentError, Operation, PreparedEffect
from minimalist_installer.core.path_safety import PathEntryKind, SafeFilesystem
from minimalist_installer.effects import LegacyPruneEffect, read_frontmatter_name


class MemoryCheckpoints:
    def __init__(self) -> None:
        self.checkpoints: dict[str, object] = {}
        self.blobs: dict[str, bytes] = {}
        self.events: list[str] = []

    def write(self, checkpoint: str, state: object) -> None:
        self.events.append(f"checkpoint:{checkpoint}:{state['phase']}")
        self.checkpoints[checkpoint] = state

    def snapshot(self) -> Mapping[str, object]:
        return dict(self.checkpoints)

    def read(self, checkpoint: str) -> object:
        return self.checkpoints.get(checkpoint)

    def write_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.events.append(f"blob:{digest}")
        self.blobs[digest] = data
        return digest

    def read_blob(self, digest: str) -> bytes:
        return self.blobs[digest]


class DenyReadFilesystem:
    def __init__(self, safe: SafeFilesystem, denied: str) -> None:
        self._safe = safe
        self.base = safe.base
        self.denied = denied

    @property
    def closed(self) -> bool:
        return self._safe.closed

    def read_bytes(self, relative: str) -> bytes:
        if relative == self.denied:
            raise PermissionError(relative)
        return self._safe.read_bytes(relative)

    def directory_exists(self, relative: str) -> bool:
        return self._safe.directory_exists(relative)

    def list_directory(self, relative: str) -> tuple[tuple[str, PathEntryKind], ...]:
        return self._safe.list_directory(relative)

    def ensure_directory(self, relative: str) -> None:
        self._safe.ensure_directory(relative)

    def atomic_write_bytes(self, relative: str, data: bytes, *, mode: int = 0o600) -> None:
        self._safe.atomic_write_bytes(relative, data, mode=mode)

    def unlink(self, relative: str, *, missing_ok: bool = False) -> bool:
        return self._safe.unlink(relative, missing_ok=missing_ok)

    def rmdir_empty(self, relative: str, *, missing_ok: bool = False) -> bool:
        return self._safe.rmdir_empty(relative, missing_ok=missing_ok)


def _context(root: Path, safe: object, operation: Operation = Operation.INSTALL) -> EffectContext:
    return EffectContext(root, root / "state", operation, "tx", "legacy", safe)


def _args(dirs: list[str] | None = None) -> dict[str, object]:
    return {
        "legacy_namespace_dirs": dirs or [".claude/commands"],
        "namespace_name": "atomic-skills",
        "known_names": ["fix", "historical-name"],
    }


def _write(root: Path, relative: str, data: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _prepare(effect: LegacyPruneEffect, safe: object, root: Path, previous: object = None, dirs: list[str] | None = None):
    return effect.prepare(_args(dirs), previous, _context(root, safe))


def _fixture_cases() -> list[dict[str, object]]:
    path = Path(__file__).parents[3] / "spec/conformance/legacy-prune.json"
    return json.loads(path.read_text("utf-8"))["frontmatter"]


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: str(case["name"]))
def test_frontmatter_signature_conformance(case: dict[str, object]) -> None:
    assert read_frontmatter_name(str(case["content"]).encode()) == case["detected"]


def test_prepare_is_read_only_then_apply_backs_up_before_delete(tmp_path: Path) -> None:
    content = b'---\nname: "fix"\n---\n# Exact\r\n'
    target = _write(tmp_path, ".claude/commands/atomic-skills/fix.md", content)
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path)
        assert target.read_bytes() == content
        writer = MemoryCheckpoints()
        effect.apply(prepared, writer)
        assert writer.events[0].startswith("blob:")
        assert writer.events[1] == "checkpoint:apply:000000:ready"
    assert not target.exists()


def test_preserves_unknown_no_frontmatter_unreadable_and_invalid_utf8(tmp_path: Path) -> None:
    root = ".claude/commands/atomic-skills"
    unknown = _write(tmp_path, f"{root}/custom.md", b"---\nname: custom\n---\n")
    plain = _write(tmp_path, f"{root}/plain.md", b"# Mine\n")
    denied = _write(tmp_path, f"{root}/denied.md", b"---\nname: fix\n---\n")
    invalid = _write(tmp_path, f"{root}/invalid.md", b"---\nname: fix\n---\n\xff")
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, DenyReadFilesystem(safe, f"{root}/denied.md"), tmp_path)
        effect.apply(prepared, MemoryCheckpoints())
    assert unknown.exists() and plain.exists() and denied.exists() and invalid.exists()
    assert prepared.before_state["pruned"] == ()


def test_prunes_known_nested_files_and_restores_exact_bytes(tmp_path: Path) -> None:
    content = b"---\nname: fix\n---\n# Fix\n\x00exact\r\n"
    target = _write(tmp_path, ".claude/commands/atomic-skills/a/b/fix.md", content)
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path)
        effect.apply(prepared, MemoryCheckpoints())
        assert not (tmp_path / ".claude/commands/atomic-skills").exists()
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), prepared.before_state, MemoryCheckpoints())
    assert target.read_bytes() == content
    assert (tmp_path / ".claude/commands").is_dir()


def test_preserved_sibling_keeps_namespace_parents(tmp_path: Path) -> None:
    signed = _write(tmp_path, ".gemini/skills/atomic-skills/nested/fix.md", b"---\nname: fix\n---\n")
    custom = _write(tmp_path, ".gemini/skills/atomic-skills/nested/custom.md", b"---\nname: custom\n---\n")
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(LegacyPruneEffect(), safe, tmp_path, dirs=[".gemini/skills"])
        LegacyPruneEffect().apply(prepared, MemoryCheckpoints())
    assert not signed.exists()
    assert custom.is_file()
    assert custom.parent.is_dir()


def test_missing_roots_and_empty_state_are_noops(tmp_path: Path) -> None:
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path, dirs=["missing/commands"])
        writer = MemoryCheckpoints()
        effect.apply(prepared, writer)
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), prepared.before_state, writer)
    assert list(tmp_path.iterdir()) == []


def test_escape_and_symlink_are_never_followed(tmp_path: Path) -> None:
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    sentinel = _write(outside, "atomic-skills/fix.md", b"---\nname: fix\n---\noutside")
    (base / "legacy").symlink_to(outside, target_is_directory=True)
    effect = LegacyPruneEffect()
    with SafeFilesystem(base) as safe:
        with pytest.raises(Exception):
            _prepare(effect, safe, base, dirs=["../outside"])
        with pytest.raises(Exception):
            _prepare(effect, safe, base, dirs=["legacy"])
        with pytest.raises(Exception):
            effect.revert(
                _context(base, safe, Operation.UNINSTALL),
                {"version": 1, "pruned": [{"path": "../outside/fix.md", "content": "", "sha256": "0" * 64, "namespace_root": "outside"}]},
                MemoryCheckpoints(),
            )
    assert sentinel.read_bytes() == b"---\nname: fix\n---\noutside"


def test_edit_after_prepare_is_preserved_and_fails_apply(tmp_path: Path) -> None:
    target = _write(tmp_path, ".claude/commands/atomic-skills/fix.md", b"---\nname: fix\n---\nold")
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path)
        target.write_bytes(b"---\nname: fix\n---\nuser edit")
        with pytest.raises(ModifiedContentError):
            effect.apply(prepared, MemoryCheckpoints())
    assert target.read_bytes().endswith(b"user edit")


def test_interrupted_delete_and_revert_resume_with_exact_blob(tmp_path: Path) -> None:
    content = b"---\nname: fix\n---\nexact\r\n"
    target = _write(tmp_path, ".claude/commands/atomic-skills/fix.md", content)
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path)
        entry = prepared.payload["entries"][0]
        writer = MemoryCheckpoints()
        digest = writer.write_blob(content)
        writer.write("apply:000000", {**entry["checkpoint"], "phase": "ready", "blob": digest})
        safe.unlink(entry["path"])
        effect.apply(prepared, writer)
        effect.revert(_context(tmp_path, safe, Operation.UPDATE), prepared.before_state, writer)
        effect.revert(_context(tmp_path, safe, Operation.UPDATE), prepared.before_state, writer)
    assert target.read_bytes() == content
    assert writer.checkpoints["rollback:000000"]["phase"] == "done"


def test_uninstall_does_not_overwrite_third_party_collision(tmp_path: Path) -> None:
    original = b"---\nname: fix\n---\noriginal"
    target = _write(tmp_path, ".claude/commands/atomic-skills/fix.md", original)
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path)
        effect.apply(prepared, MemoryCheckpoints())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"third-party")
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), prepared.before_state, MemoryCheckpoints())
    assert target.read_bytes() == b"third-party"


def test_corrupt_prepared_and_before_state_fail_closed(tmp_path: Path) -> None:
    sentinel = _write(tmp_path, ".claude/commands/atomic-skills/fix.md", b"---\nname: fix\n---\n")
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        with pytest.raises(InvalidEffectError):
            effect.apply(PreparedEffect(before_state={}, payload={"version": 99}, filesystem=safe), MemoryCheckpoints())
        with pytest.raises((InvalidEffectError, ValueError, TypeError)):
            effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), {"version": 99}, MemoryCheckpoints())
    assert sentinel.is_file()


@pytest.mark.parametrize(
    "content",
    [
        b"---\nname: fix\nname: historical-name\n---\n",
        b"---\nname: \"fix'\n---\n",
    ],
)
def test_ambiguous_frontmatter_is_not_an_ownership_signature(content: bytes) -> None:
    assert read_frontmatter_name(content) is None


def test_prepared_delete_must_be_owned_by_persisted_before_state(tmp_path: Path) -> None:
    target = _write(tmp_path, ".claude/commands/atomic-skills/fix.md", b"---\nname: fix\n---\n")
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path)
        forged = PreparedEffect(
            before_state={"version": 1, "pruned": []},
            payload=prepared.to_dict()["payload"],
            filesystem=safe,
        )
        with pytest.raises(InvalidEffectError, match="state"):
            effect.apply(forged, MemoryCheckpoints())
    assert target.is_file()


def test_corrupt_done_restore_checkpoint_cannot_skip_restore(tmp_path: Path) -> None:
    content = b"---\nname: fix\n---\n"
    target = _write(tmp_path, ".claude/commands/atomic-skills/fix.md", content)
    effect = LegacyPruneEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path)
        effect.apply(prepared, MemoryCheckpoints())
        writer = MemoryCheckpoints()
        writer.checkpoints["uninstall:000000"] = {
            "phase": "done",
            "path": "other.md",
            "sha256": "0" * 64,
            "outcome": "restored",
        }
        with pytest.raises(InvalidEffectError, match="checkpoint"):
            effect.revert(
                _context(tmp_path, safe, Operation.UNINSTALL),
                prepared.before_state,
                writer,
            )
    assert not target.exists()
