from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from minimalist_installer import EffectContext, InvalidEffectError, Operation, PreparedEffect
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.effects import RefcountEffect, owner_key


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
        self.blobs[digest] = data
        return digest

    def read_blob(self, digest: str) -> bytes:
        return self.blobs[digest]


def _context(root: Path, safe: object, operation: Operation = Operation.INSTALL) -> EffectContext:
    return EffectContext(root, root / "state", operation, "tx", "shared", safe)


def _args(owner: str) -> dict[str, object]:
    return {
        "owners_dir": "shared/owners",
        "owner_id": owner,
        "owner_manifest_path": f"manifests/{owner}.json",
    }


def _prepare(effect: RefcountEffect, safe: object, root: Path, owner: str, previous: object = None):
    return effect.prepare(_args(owner), previous, _context(root, safe))


def _apply(effect: RefcountEffect, safe: object, root: Path, owner: str, previous: object = None):
    prepared = _prepare(effect, safe, root, owner, previous)
    writer = MemoryCheckpoints()
    effect.apply(prepared, writer)
    return prepared, writer


def _write_manifest(root: Path, owner: str, state: object) -> None:
    path = root / f"manifests/{owner}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 1,
        "engine": {"name": "minimalist-installer", "version": "0.1.0"},
        "installation": {"id": owner, "consumer": "tests", "consumer_version": "1"},
        "transaction_id": f"tx-{owner}",
        "effects": [{"id": "shared", "type": "refcount", "effect_version": 1, "before_state": state, "resources": [f"path:{(root / 'shared/owners').as_posix()}"]}],
        "installed_at": "2026-09-10T00:00:00Z",
        "updated_at": "2026-09-10T00:00:00Z",
    }
    path.write_text(json.dumps(value), "utf-8")


def _marker(root: Path, owner: str) -> Path:
    return root / "shared/owners" / owner_key(owner)


def _fixture_cases() -> list[dict[str, str]]:
    path = Path(__file__).parents[3] / "spec/conformance/refcount.json"
    return json.loads(path.read_text("utf-8"))["owner_keys"]


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: case["name"])
def test_owner_key_conformance(case: dict[str, str]) -> None:
    assert owner_key(case["owner_id"]) == case["sha256"]


def test_prepare_is_read_only_and_apply_registers_after_ready_checkpoint(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path, "a")
        assert list(tmp_path.iterdir()) == []
        writer = MemoryCheckpoints()
        effect.apply(prepared, writer)
    assert _marker(tmp_path, "a").is_file()
    assert writer.events == ["checkpoint:apply:ready", "checkpoint:apply:done"]


def test_one_of_two_reverts_keeps_only_manifest_proven_owner(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        state_a, _ = _apply(effect, safe, tmp_path, "a")
        state_b, _ = _apply(effect, safe, tmp_path, "b")
        _write_manifest(tmp_path, "a", state_a.before_state)
        _write_manifest(tmp_path, "b", state_b.before_state)
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), state_a.before_state, MemoryCheckpoints())
    assert not _marker(tmp_path, "a").exists()
    assert _marker(tmp_path, "b").is_file()


def test_orphans_are_healed_and_last_owner_directory_is_reclaimed(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        live, _ = _apply(effect, safe, tmp_path, "live")
        orphan, _ = _apply(effect, safe, tmp_path, "orphan")
        _write_manifest(tmp_path, "live", live.before_state)
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), live.before_state, MemoryCheckpoints())
    assert not _marker(tmp_path, "orphan").exists()
    assert not (tmp_path / "shared/owners").exists()
    assert not (tmp_path / "shared").exists()
    assert orphan.before_state["owns_marker"] is True


def test_existing_but_unproven_manifest_is_not_counted_as_owner(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        live, _ = _apply(effect, safe, tmp_path, "live")
        fake, _ = _apply(effect, safe, tmp_path, "fake")
        _write_manifest(tmp_path, "live", live.before_state)
        _write_manifest(tmp_path, "fake", live.before_state)
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), live.before_state, MemoryCheckpoints())
    assert not _marker(tmp_path, "fake").exists()
    assert not (tmp_path / "shared/owners").exists()
    assert fake.before_state["owner_key"] != live.before_state["owner_key"]


def test_idempotent_update_carries_original_marker_ownership(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        first, _ = _apply(effect, safe, tmp_path, "owner")
        second = _prepare(effect, safe, tmp_path, "owner", first.before_state)
        writer = MemoryCheckpoints()
        effect.apply(second, writer)
        assert writer.checkpoints == {}
        assert second.before_state["owns_marker"] is True
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), second.before_state, MemoryCheckpoints())
    assert not (tmp_path / "shared/owners").exists()


def test_second_unowned_registration_revert_does_not_remove_first_claim(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        first, _ = _apply(effect, safe, tmp_path, "owner")
        second, _ = _apply(effect, safe, tmp_path, "owner")
        assert second.before_state["owns_marker"] is False
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), second.before_state, MemoryCheckpoints())
        assert _marker(tmp_path, "owner").is_file()
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), first.before_state, MemoryCheckpoints())
    assert not _marker(tmp_path, "owner").exists()


def test_corrupt_unknown_entry_is_preserved_and_blocks_reclaim(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        state, _ = _apply(effect, safe, tmp_path, "owner")
        unknown = tmp_path / "shared/owners/not-a-marker"
        unknown.write_bytes(b"user data")
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), state.before_state, MemoryCheckpoints())
    assert unknown.read_bytes() == b"user data"
    assert (tmp_path / "shared/owners").is_dir()


def test_path_escape_manifest_symlink_and_owner_dir_symlink_preserve_outside(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside")
    effect = RefcountEffect()
    with SafeFilesystem(base) as safe:
        with pytest.raises(Exception):
            effect.prepare({**_args("a"), "owners_dir": "../outside"}, None, _context(base, safe))
        (base / "manifests").symlink_to(outside, target_is_directory=True)
        with pytest.raises(Exception):
            _prepare(effect, safe, base, "a")
    assert sentinel.read_bytes() == b"outside"


def test_interrupted_apply_then_rollback_is_checkpointed_and_idempotent(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepare(effect, safe, tmp_path, "owner")
        writer = MemoryCheckpoints()
        writer.write("apply", {**prepared.payload["checkpoint"], "phase": "ready"})
        safe.atomic_write_bytes(prepared.payload["marker_path"], prepared.payload["marker_bytes"].encode("latin1"))
        effect.apply(prepared, writer)
        effect.revert(_context(tmp_path, safe, Operation.UPDATE), prepared.before_state, writer)
        effect.revert(_context(tmp_path, safe, Operation.UPDATE), prepared.before_state, writer)
    assert not _marker(tmp_path, "owner").exists()
    assert writer.checkpoints["rollback"]["phase"] == "done"


def test_changed_marker_and_corrupt_states_fail_closed(tmp_path: Path) -> None:
    effect = RefcountEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared, _ = _apply(effect, safe, tmp_path, "owner")
        _marker(tmp_path, "owner").write_bytes(b"third-party")
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), prepared.before_state, MemoryCheckpoints())
        assert _marker(tmp_path, "owner").read_bytes() == b"third-party"
        with pytest.raises(InvalidEffectError):
            effect.apply(PreparedEffect(before_state={}, payload={"version": 99}, filesystem=safe), MemoryCheckpoints())
        with pytest.raises((InvalidEffectError, ValueError, TypeError)):
            effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), {"version": 99}, MemoryCheckpoints())


def test_default_installer_registers_all_four_builtins() -> None:
    class Provider:
        def plan(self, config: Mapping[str, object], context: object) -> tuple[()]:
            return ()

    from minimalist_installer import define_installer

    installer = define_installer(config={}, providers=[Provider()])
    assert installer.registry.list() == (
        ("json_merge", 1),
        ("legacy_prune", 1),
        ("reconcile_file_set", 1),
        ("refcount", 1),
    )
