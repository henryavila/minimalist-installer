from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from minimalist_installer import (
    EffectContext,
    EffectPlan,
    InvalidEffectError,
    Operation,
    PlanContext,
    PreparedEffect,
    define_installer,
)
from minimalist_installer.core.locks import canonical_resource_identity
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.effects import JsonMergeEffect


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
        import hashlib

        digest = hashlib.sha256(data).hexdigest()
        self.events.append(f"blob:{digest}")
        self.blobs[digest] = data
        return digest

    def read_blob(self, digest: str) -> bytes:
        return self.blobs[digest]


class InterruptingCheckpoints(MemoryCheckpoints):
    def __init__(self, checkpoint_name: str, phase: str) -> None:
        super().__init__()
        self.checkpoint_name = checkpoint_name
        self.phase = phase
        self.interrupted = False

    def write(self, checkpoint: str, state: object) -> None:
        if (
            not self.interrupted
            and checkpoint == self.checkpoint_name
            and state["phase"] == self.phase
        ):
            self.interrupted = True
            raise RuntimeError(f"interrupted:{checkpoint}:{self.phase}")
        super().write(checkpoint, state)


class RecordingFilesystem:
    def __init__(self, filesystem: SafeFilesystem, events: list[str]) -> None:
        self._filesystem = filesystem
        self.base = filesystem.base
        self.events = events

    @property
    def closed(self) -> bool:
        return self._filesystem.closed

    def read_bytes(self, relative: str) -> bytes:
        return self._filesystem.read_bytes(relative)

    def directory_exists(self, relative: str) -> bool:
        return self._filesystem.directory_exists(relative)

    def ensure_directory(self, relative: str) -> None:
        self.events.append(f"mkdir:{relative}")
        self._filesystem.ensure_directory(relative)

    def atomic_write_bytes(self, relative: str, data: bytes, *, mode: int = 0o600) -> None:
        self.events.append(f"write:{relative}")
        self._filesystem.atomic_write_bytes(relative, data, mode=mode)

    def unlink(self, relative: str, *, missing_ok: bool = False) -> bool:
        self.events.append(f"unlink:{relative}")
        return self._filesystem.unlink(relative, missing_ok=missing_ok)

    def rmdir_empty(self, relative: str, *, missing_ok: bool = False) -> bool:
        self.events.append(f"rmdir:{relative}")
        return self._filesystem.rmdir_empty(relative, missing_ok=missing_ok)


def _context(root: Path, filesystem: object, operation: Operation = Operation.INSTALL) -> EffectContext:
    return EffectContext(
        base_path=root,
        manifest_dir=root / "state",
        operation=operation,
        transaction_id="tx",
        effect_id="settings",
        filesystem=filesystem,
    )


def _prepared(
    effect: JsonMergeEffect,
    safe: object,
    root: Path,
    delta: object,
    *,
    path: str = "settings.json",
    previous: object = None,
) -> PreparedEffect:
    return effect.prepare({"path": path, "delta": delta}, previous, _context(root, safe))


def _apply(effect: JsonMergeEffect, safe: object, root: Path, delta: object, **kwargs: object):
    prepared = _prepared(effect, safe, root, delta, **kwargs)
    writer = MemoryCheckpoints()
    effect.apply(prepared, writer)
    return prepared, writer


def _fixture_cases() -> list[dict[str, object]]:
    path = Path(__file__).parents[3] / "spec/conformance/json-merge.json"
    return json.loads(path.read_text("utf-8"))["cases"]


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: str(case["name"]))
def test_conformance_additive_merge_and_array_dedupe(tmp_path: Path, case: dict[str, object]) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(case["target"]), encoding="utf-8")
    with SafeFilesystem(tmp_path) as safe:
        _apply(JsonMergeEffect(), safe, tmp_path, case["delta"])
    assert json.loads(target.read_text("utf-8")) == case["merged"]


def test_prepare_is_read_only_and_apply_checkpoints_before_write(tmp_path: Path) -> None:
    original = b'{ "existing" : true }\n'
    (tmp_path / "settings.json").write_bytes(original)
    events: list[str] = []
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        filesystem = RecordingFilesystem(safe, events)
        prepared = _prepared(effect, filesystem, tmp_path, {"added": [1]})
        assert (tmp_path / "settings.json").read_bytes() == original
        assert prepared.resources == (f"path:{(tmp_path / 'settings.json').as_posix()}",)
        writer = MemoryCheckpoints()
        writer.events = events
        effect.apply(prepared, writer)
    assert events.index("checkpoint:apply:ready") < events.index("write:settings.json")
    assert events[-1] == "checkpoint:apply:done"


def test_revert_preserves_preexisting_and_later_third_party_entries(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"hooks": [{"command": "before"}]}), "utf-8")
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared, _ = _apply(effect, safe, tmp_path, {"hooks": [{"command": "ours"}], "managed": True})
        value = json.loads(target.read_text("utf-8"))
        value["hooks"].append({"command": "after"})
        value["managed"] = "user-edited"
        value["unrelated"] = {"keep": True}
        target.write_text(json.dumps(value), "utf-8")
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), prepared.before_state, MemoryCheckpoints())
    assert json.loads(target.read_text("utf-8")) == {
        "hooks": [{"command": "before"}, {"command": "after"}],
        "managed": "user-edited",
        "unrelated": {"keep": True},
    }


def test_exact_original_bytes_return_when_no_third_party_edit_occurred(tmp_path: Path) -> None:
    original = b'{\n\t"hooks": [],\n\t"keep": true\n}\n'
    target = tmp_path / "settings.json"
    target.write_bytes(original)
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared, _ = _apply(effect, safe, tmp_path, {"hooks": [{"command": "ours"}]})
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), prepared.before_state, MemoryCheckpoints())
    assert target.read_bytes() == original


def test_created_file_and_only_owned_parents_are_removed(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared, _ = _apply(
            effect,
            safe,
            tmp_path,
            {"hooks": {"start": ["ours"]}},
            path="nested/created/settings.json",
        )
        effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), prepared.before_state, MemoryCheckpoints())
    assert not (tmp_path / "nested/created").exists()
    assert (tmp_path / "nested").is_dir()


def test_empty_delta_and_repeat_delta_preserve_bytes_and_do_not_duplicate(tmp_path: Path) -> None:
    original = b'{ "hooks": [] }\n'
    target = tmp_path / "settings.json"
    target.write_bytes(original)
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        empty, empty_writer = _apply(effect, safe, tmp_path, {})
        assert target.read_bytes() == original
        assert empty_writer.checkpoints == {}
        first, _ = _apply(effect, safe, tmp_path, {"hooks": [{"command": "ours"}]})
        second = _prepared(
            effect,
            safe,
            tmp_path,
            {"hooks": [{"command": "ours"}]},
            previous=first.before_state,
        )
        second_writer = MemoryCheckpoints()
        effect.apply(second, second_writer)
        assert second_writer.checkpoints == {}
    assert json.loads(target.read_text("utf-8"))["hooks"] == [{"command": "ours"}]
    assert empty.before_state["owned"] == ()


def test_scalar_conflict_and_container_type_conflict_never_clobber(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    original = b'{"enabled":true,"nested":3}\n'
    target.write_bytes(original)
    with SafeFilesystem(tmp_path) as safe:
        effect = JsonMergeEffect()
        with pytest.raises(ValueError, match="overwrite existing scalar"):
            _prepared(effect, safe, tmp_path, {"enabled": False})
        with pytest.raises(ValueError, match="object into existing non-object"):
            _prepared(effect, safe, tmp_path, {"nested": {"x": 1}})
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "invalid",
    [b"{not json", b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff'],
)
def test_strict_json_input_is_rejected_without_clobber(tmp_path: Path, invalid: bytes) -> None:
    target = tmp_path / "settings.json"
    target.write_bytes(invalid)
    with SafeFilesystem(tmp_path) as safe:
        with pytest.raises((UnicodeDecodeError, ValueError)):
            _prepared(JsonMergeEffect(), safe, tmp_path, {"ok": True})
    assert target.read_bytes() == invalid


def test_path_escape_and_symlink_leave_outside_sentinel_unchanged(tmp_path: Path) -> None:
    base = tmp_path / "base"
    outside = tmp_path / "outside.json"
    base.mkdir()
    outside.write_bytes(b'{"sentinel":true}\n')
    (base / "link.json").symlink_to(outside)
    effect = JsonMergeEffect()
    with SafeFilesystem(base) as safe:
        with pytest.raises(Exception):
            _prepared(effect, safe, base, {"x": 1}, path="../outside.json")
        with pytest.raises(Exception):
            _prepared(effect, safe, base, {"x": 1}, path="link.json")
        with pytest.raises(Exception):
            effect.revert(
                _context(base, safe, Operation.UNINSTALL),
                {"version": 1, "path": "../outside.json", "file_created": True, "original": None, "installed_hash": "0" * 64, "owned": [], "created_containers": [], "created_parents": []},
                MemoryCheckpoints(),
            )
    assert outside.read_bytes() == b'{"sentinel":true}\n'


def test_interrupted_apply_and_rollback_resume_idempotently(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    original = b'{"before":true}\n'
    target.write_bytes(original)
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepared(effect, safe, tmp_path, {"ours": True})
        writer = MemoryCheckpoints()
        blob = writer.write_blob(original)
        writer.write(
            "apply",
            {**prepared.payload["checkpoint"], "phase": "ready", "blob": blob},
        )
        safe.atomic_write_bytes("settings.json", prepared.payload["after_bytes"].encode("latin1"))
        effect.apply(prepared, writer)
        effect.apply(prepared, writer)
        effect.revert(_context(tmp_path, safe, Operation.UPDATE), prepared.before_state, writer)
        effect.revert(_context(tmp_path, safe, Operation.UPDATE), prepared.before_state, writer)
    assert target.read_bytes() == original
    assert writer.checkpoints["rollback"]["phase"] == "done"


def test_corrupt_prepared_and_before_state_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_bytes(b"{}\n")
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        bad = PreparedEffect(before_state={}, payload={"version": 99}, filesystem=safe)
        with pytest.raises(InvalidEffectError):
            effect.apply(bad, MemoryCheckpoints())
        with pytest.raises((InvalidEffectError, ValueError, TypeError)):
            effect.revert(_context(tmp_path, safe, Operation.UNINSTALL), {"version": 99}, MemoryCheckpoints())
    assert target.read_bytes() == b"{}\n"


def test_nonfinite_exponent_and_inconsistent_owned_state_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_bytes(b'{"huge":1e999}\n')
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        with pytest.raises(ValueError, match="finite"):
            _prepared(effect, safe, tmp_path, {"added": True})

        target.write_bytes(b"{}\n")
        prepared = _prepared(effect, safe, tmp_path, {"added": True})
        corrupt = dict(prepared.to_dict()["before_state"])
        corrupt["path"] = "other.json"
        forged = PreparedEffect(
            before_state=corrupt,
            payload=prepared.to_dict()["payload"],
            filesystem=safe,
        )
        with pytest.raises(InvalidEffectError, match="state"):
            effect.apply(forged, MemoryCheckpoints())
    assert target.read_bytes() == b"{}\n"


def test_corrupt_done_uninstall_checkpoint_cannot_skip_revert(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_bytes(b"{}\n")
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared, _ = _apply(effect, safe, tmp_path, {"ours": True})
        writer = MemoryCheckpoints()
        writer.checkpoints["uninstall"] = {"phase": "done", "path": "other.json"}
        with pytest.raises(InvalidEffectError, match="checkpoint"):
            effect.revert(
                _context(tmp_path, safe, Operation.UNINSTALL),
                prepared.before_state,
                writer,
            )
    assert json.loads(target.read_text("utf-8"))["ours"] is True


def test_driver_update_then_uninstall_retains_merge_ownership(tmp_path: Path) -> None:
    class MergeProvider:
        def plan(
            self,
            config: Mapping[str, object],
            context: PlanContext,
        ) -> tuple[EffectPlan, ...]:
            path = ".claude/settings.json"
            return (
                EffectPlan(
                    id="settings",
                    type="json_merge",
                    version=1,
                    args={"path": path, "delta": config["delta"]},
                    resources=(
                        canonical_resource_identity(
                            "path", context.base_path / path
                        ),
                    ),
                ),
            )

    target = tmp_path / ".claude/settings.json"
    target.parent.mkdir()
    original = b'{\n  "third_party": true\n}\n'
    target.write_bytes(original)
    installer = define_installer(
        config={
            "consumer": "tests",
            "consumer_version": "1",
            "manifest_dir": "state",
            "delta": {"ours": [1]},
        },
        providers=[MergeProvider()],
        id_factory=iter(("install", "tx-1", "tx-2", "tx-3")).__next__,
    )
    installer.install(base_path=tmp_path)
    installer.install(base_path=tmp_path)
    installer.uninstall(base_path=tmp_path)
    assert target.read_bytes() == original


def test_rollback_rejects_checkpoint_for_unrelated_in_base_file(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"unrelated")
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepared(effect, safe, tmp_path, {"ours": True})
        writer = MemoryCheckpoints()
        writer.checkpoints["apply"] = {
            "phase": "done",
            "action": "write",
            "path": "victim.txt",
            "before_hash": None,
            "after_hash": hashlib.sha256(b"unrelated").hexdigest(),
            "blob": None,
            "created_parents": [],
        }
        with pytest.raises(InvalidEffectError, match="authorized state"):
            effect.revert(
                _context(tmp_path, safe, Operation.UPDATE),
                prepared.before_state,
                writer,
            )
    assert victim.read_bytes() == b"unrelated"


def test_rollback_prunes_only_apply_created_parents_with_checkpoints(tmp_path: Path) -> None:
    (tmp_path / "existing").mkdir()
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared = _prepared(
            effect,
            safe,
            tmp_path,
            {"ours": True},
            path="existing/created/settings.json",
        )
        assert prepared.before_state["created_parents"] == ("existing/created",)
        writer = MemoryCheckpoints()
        effect.apply(prepared, writer)
        effect.revert(
            _context(tmp_path, safe, Operation.UPDATE),
            prepared.before_state,
            writer,
        )
    assert (tmp_path / "existing").is_dir()
    assert not (tmp_path / "existing/created").exists()
    assert writer.checkpoints["rollback-dir:000000"]["phase"] == "done"


def test_update_rollback_preserves_parents_owned_by_prior_install(tmp_path: Path) -> None:
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        first, _ = _apply(
            effect,
            safe,
            tmp_path,
            {"ours": True},
            path="created/settings.json",
        )
        safe.unlink("created/settings.json")
        update = _prepared(
            effect,
            safe,
            tmp_path,
            {"ours": True},
            path="created/settings.json",
            previous=first.before_state,
        )
        writer = MemoryCheckpoints()
        effect.apply(update, writer)
        effect.revert(
            _context(tmp_path, safe, Operation.UPDATE),
            update.before_state,
            writer,
        )
    assert (tmp_path / "created").is_dir()
    assert not (tmp_path / "created/settings.json").exists()


def test_uninstall_resume_reclaims_parents_when_leaf_was_removed_after_ready(
    tmp_path: Path,
) -> None:
    (tmp_path / "preexisting").mkdir()
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared, _ = _apply(
            effect,
            safe,
            tmp_path,
            {"ours": True},
            path="preexisting/owned/deep/settings.json",
        )
        writer = MemoryCheckpoints()
        writer.write(
            "uninstall",
            {
                "phase": "ready",
                "path": "preexisting/owned/deep/settings.json",
            },
        )
        safe.unlink("preexisting/owned/deep/settings.json")
        effect.revert(
            _context(tmp_path, safe, Operation.UNINSTALL),
            prepared.before_state,
            writer,
        )
    assert (tmp_path / "preexisting").is_dir()
    assert not (tmp_path / "preexisting/owned").exists()
    assert writer.checkpoints["uninstall"]["phase"] == "done"
    assert writer.checkpoints["uninstall-dir:000000"]["phase"] == "done"
    assert writer.checkpoints["uninstall-dir:000001"]["phase"] == "done"


@pytest.mark.parametrize(
    ("checkpoint_name", "phase"),
    [
        ("uninstall-dir:000000", "ready"),
        ("uninstall-dir:000000", "done"),
        ("uninstall-dir:000001", "ready"),
        ("uninstall-dir:000001", "done"),
    ],
)
def test_uninstall_parent_cleanup_resumes_after_each_durable_boundary(
    tmp_path: Path,
    checkpoint_name: str,
    phase: str,
) -> None:
    (tmp_path / "preexisting").mkdir()
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        prepared, _ = _apply(
            effect,
            safe,
            tmp_path,
            {"ours": True},
            path="preexisting/owned/deep/settings.json",
        )
        writer = InterruptingCheckpoints(checkpoint_name, phase)
        with pytest.raises(RuntimeError, match="interrupted:uninstall-dir"):
            effect.revert(
                _context(tmp_path, safe, Operation.UNINSTALL),
                prepared.before_state,
                writer,
            )
        effect.revert(
            _context(tmp_path, safe, Operation.UNINSTALL),
            prepared.before_state,
            writer,
        )
    assert (tmp_path / "preexisting").is_dir()
    assert not (tmp_path / "preexisting/owned").exists()
    assert writer.checkpoints["uninstall-dir:000000"]["phase"] == "done"
    assert writer.checkpoints["uninstall-dir:000001"]["phase"] == "done"
