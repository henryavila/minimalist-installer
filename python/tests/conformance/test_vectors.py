"""Shared conformance vectors exercised against the Python effects."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from minimalist_installer import (
    EffectContext,
    FileDecision,
    JsonMergeEffect,
    Operation,
    PreparedEffect,
    classify_file,
    owner_key,
    read_frontmatter_name,
    sha256_bytes,
)
from minimalist_installer.core.path_safety import (
    SafeFilesystem,
    SafeFilesystemBackendStatus,
    safe_filesystem_backend_status,
)

_REPO = Path(__file__).resolve().parents[3]
_CONFORMANCE = _REPO / "spec" / "conformance"
_CAPABILITY_MATRIX = _REPO / "spec" / "capability-matrix.json"
_ALLOWED_STATUSES = frozenset(
    {"equivalent", "python-extension", "node-extension", "not-applicable"}
)

requires_safe_fs = pytest.mark.skipif(
    safe_filesystem_backend_status() is SafeFilesystemBackendStatus.UNAVAILABLE,
    reason="safe filesystem backend unavailable on this platform",
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(name: str) -> dict[str, object]:
    return json.loads((_CONFORMANCE / name).read_text(encoding="utf-8"))


def _conformance_files() -> list[Path]:
    return sorted(_CONFORMANCE.glob("*.json"))


class _MemoryCheckpoints:
    def __init__(self) -> None:
        self.checkpoints: dict[str, object] = {}
        self.blobs: dict[str, bytes] = {}

    def write(self, checkpoint: str, state: object) -> None:
        self.checkpoints[checkpoint] = state

    def snapshot(self) -> dict[str, object]:
        return dict(self.checkpoints)

    def read(self, checkpoint: str) -> object:
        return self.checkpoints.get(checkpoint)

    def write_blob(self, data: bytes) -> str:
        digest = _digest(data)
        self.blobs[digest] = data
        return digest

    def read_blob(self, digest: str) -> bytes:
        return self.blobs[digest]


def test_every_shared_conformance_vector_file_is_present() -> None:
    names = {path.name for path in _conformance_files()}
    assert names == {
        "file-set.json",
        "json-merge.json",
        "legacy-prune.json",
        "refcount.json",
    }


def test_capability_matrix_covers_shared_vectors_and_statuses() -> None:
    assert _CAPABILITY_MATRIX.is_file(), "spec/capability-matrix.json is required"
    matrix = json.loads(_CAPABILITY_MATRIX.read_text(encoding="utf-8"))
    assert matrix.get("schema_version") == 1
    features = matrix.get("features")
    assert isinstance(features, list) and features

    statuses = {feature["status"] for feature in features}
    assert statuses <= _ALLOWED_STATUSES

    referenced = {
        feature.get("conformance")
        for feature in features
        if isinstance(feature.get("conformance"), str)
    }
    for path in _conformance_files():
        relative = f"spec/conformance/{path.name}"
        assert relative in referenced, f"{relative} missing from capability matrix"

    by_id = {feature["id"]: feature for feature in features}
    for vector_id in ("reconcile-file-set", "json-merge", "refcount", "legacy-prune"):
        assert by_id[vector_id]["status"] == "equivalent"

    node_only = [
        feature for feature in features if feature["status"] == "node-extension"
    ]
    assert node_only, "Node-only capabilities must be marked node-extension"


@pytest.mark.parametrize(
    "case",
    _load_json("file-set.json")["classification"],
    ids=lambda case: str(case["name"]),
)
def test_file_set_classification_vectors(case: dict[str, object]) -> None:
    encoded = {
        name: _digest(str(case[name]).encode()) if case.get(name) is not None else None
        for name in ("desired", "installed", "disk")
    }
    decision = classify_file(
        desired_hash=encoded["desired"],
        installed_hash=encoded["installed"],
        disk_hash=encoded["disk"],
        adopt_identical=bool(case.get("adopt_identical", False)),
    )
    assert decision is FileDecision(str(case["decision"]))
    assert sha256_bytes(b"vector") == _digest(b"vector")


@pytest.mark.parametrize(
    "case",
    _load_json("json-merge.json")["cases"],
    ids=lambda case: str(case["name"]),
)
@requires_safe_fs
def test_json_merge_vectors(tmp_path: Path, case: dict[str, object]) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps(case["target"]), encoding="utf-8")
    effect = JsonMergeEffect()
    with SafeFilesystem(tmp_path) as safe:
        context = EffectContext(
            base_path=tmp_path,
            manifest_dir=tmp_path / ".minimalist-installer",
            operation=Operation.INSTALL,
            transaction_id="tx-conformance",
            effect_id="json-merge",
            filesystem=safe,
        )
        prepared = effect.prepare(
            {
                "path": "settings.json",
                "delta": deepcopy(case["delta"]),
            },
            None,
            context,
        )
        assert isinstance(prepared, PreparedEffect)
        effect.apply(prepared, _MemoryCheckpoints())
    assert json.loads(target.read_text(encoding="utf-8")) == case["merged"]


@pytest.mark.parametrize(
    "case",
    _load_json("refcount.json")["owner_keys"],
    ids=lambda case: str(case["name"]),
)
def test_refcount_owner_key_vectors(case: dict[str, str]) -> None:
    assert owner_key(case["owner_id"]) == case["sha256"]


@pytest.mark.parametrize(
    "case",
    _load_json("legacy-prune.json")["frontmatter"],
    ids=lambda case: str(case["name"]),
)
def test_legacy_prune_frontmatter_vectors(case: dict[str, object]) -> None:
    assert read_frontmatter_name(str(case["content"]).encode()) == case["detected"]
