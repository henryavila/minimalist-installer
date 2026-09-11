from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from minimalist_installer import IncompleteTransactionError, OperationStatus

_WORKER_PATH = Path(__file__).with_name("crash_worker.py")
_SPEC = importlib.util.spec_from_file_location("crash_worker", _WORKER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
crash_worker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(crash_worker)

FILE_SET_ID = crash_worker.FILE_SET_ID
HOLD_POINTS = crash_worker.HOLD_POINTS
JSON_ID = crash_worker.JSON_ID
README_PATH = crash_worker.README_PATH
SENTINEL_BYTES = crash_worker.SENTINEL_BYTES
SENTINEL_NAME = crash_worker.SENTINEL_NAME
SETTINGS_PATH = crash_worker.SETTINGS_PATH
make_installer = crash_worker.make_installer
write_sentinel = crash_worker.write_sentinel

WORKER = Path(__file__).with_name("crash_worker.py")

INSTALL_BOUNDARIES = (
    "journal",
    "prepared",
    "applied",
    "effects_applied",
    "committing",
    "manifest",
    "tombstone",
)
UPDATE_BOUNDARIES = ("applied", "committing", "manifest")
UNINSTALL_BOUNDARIES = ("reverted", "effects_reverted", "manifest_removed", "tombstone")
REPAIR_BOUNDARIES = ("repairing", "reverted", "rolled_back", "tombstone")
COMMITTED_HOLD_POINTS = frozenset({"manifest", "manifest_committed", "tombstone"})


def _run_worker(
    base: Path,
    operation: str,
    hold_after: str,
    *,
    version: str = "v1",
) -> subprocess.Popen[str]:
    ready = base / ".crash-ready"
    if ready.exists():
        ready.unlink()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(WORKER),
            "--base",
            str(base),
            "--operation",
            operation,
            "--hold-after",
            hold_after,
            "--ready",
            str(ready),
            "--version",
            version,
        ],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 15
    while not ready.is_file():
        result = process.poll()
        if result is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            stdout = process.stdout.read() if process.stdout is not None else ""
            raise RuntimeError(
                f"worker exited {result} before {hold_after}: {stderr}\n{stdout}"
            )
        if time.time() > deadline:
            process.kill()
            raise TimeoutError(f"worker did not reach {hold_after}")
        time.sleep(0.01)
    assert ready.read_text("utf-8") == hold_after
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    return process


def _snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.name == ".crash-ready":
            continue
        if path.is_file() and not path.is_symlink():
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _active(base: Path) -> dict[str, object] | None:
    path = base / "state/transactions/active.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text("utf-8"))


def _seed_install(base: Path, version: str = "v1") -> None:
    write_sentinel(base)
    make_installer(version).install(base_path=base)


def _seed_incomplete_install(base: Path) -> None:
    write_sentinel(base)
    _run_worker(base, "install", "applied", version="v1")


@pytest.mark.skipif(sys.platform == "win32", reason="Linux crash matrix is verified")
@pytest.mark.parametrize(
    ("operation", "hold_after", "seed"),
    [
        * [("install", boundary, None) for boundary in INSTALL_BOUNDARIES],
        * [("update", boundary, "install") for boundary in UPDATE_BOUNDARIES],
        * [("uninstall", boundary, "install") for boundary in UNINSTALL_BOUNDARIES],
        * [("repair", boundary, "incomplete") for boundary in REPAIR_BOUNDARIES],
    ],
)
def test_crash_at_durable_boundary_is_inspectable_and_repairable(
    tmp_path: Path,
    operation: str,
    hold_after: str,
    seed: str | None,
) -> None:
    assert hold_after in HOLD_POINTS
    write_sentinel(tmp_path)
    if seed == "install":
        _seed_install(tmp_path, "v1")
    elif seed == "incomplete":
        _seed_incomplete_install(tmp_path)
    seed_manifest = tmp_path / "state/manifest.json"
    seed_transaction = None
    if seed_manifest.is_file():
        seed_transaction = json.loads(seed_manifest.read_text("utf-8"))["transaction_id"]

    version = "v2" if operation == "update" else "v1"
    _run_worker(tmp_path, operation, hold_after, version=version)

    assert (tmp_path / SENTINEL_NAME).read_bytes() == SENTINEL_BYTES
    crashed = _snapshot(tmp_path)
    installer = make_installer(version)
    report = installer.inspect_recovery(base_path=tmp_path)
    status = installer.status(base_path=tmp_path)
    assert _snapshot(tmp_path) == crashed
    assert report.reason is not None or report.transaction_id is not None or report.cleanup
    assert status.incomplete_transaction_id is not None or report.cleanup or report.remnants
    with pytest.raises(IncompleteTransactionError):
        installer.install(base_path=tmp_path)
    assert _snapshot(tmp_path) == crashed

    result = installer.repair(base_path=tmp_path)
    assert result.status is OperationStatus.COMPLETED
    assert (tmp_path / SENTINEL_NAME).read_bytes() == SENTINEL_BYTES
    assert _active(tmp_path) is None
    final_status = installer.status(base_path=tmp_path)
    assert final_status.incomplete_transaction_id is None
    assert final_status.status is OperationStatus.COMPLETED

    manifest_path = tmp_path / "state/manifest.json"
    if operation == "uninstall":
        assert not manifest_path.exists()
        assert not (tmp_path / README_PATH).exists()
        return
    if operation == "repair":
        assert not manifest_path.exists()
        assert (tmp_path / SENTINEL_NAME).read_bytes() == SENTINEL_BYTES
        return

    committed_during_crash = False
    if manifest_path.is_file():
        current_id = json.loads(manifest_path.read_text("utf-8"))["transaction_id"]
        committed_during_crash = current_id != seed_transaction
    if hold_after in COMMITTED_HOLD_POINTS or committed_during_crash:
        assert manifest_path.is_file()
        assert (tmp_path / README_PATH).is_file()
        assert (tmp_path / SETTINGS_PATH).is_file()
        expected_readme = b"version-2" if operation == "update" else b"version-1"
        assert (tmp_path / README_PATH).read_bytes() == expected_readme
    else:
        if seed_transaction is None:
            assert not manifest_path.exists()
            assert not (tmp_path / README_PATH).exists()
        else:
            assert json.loads(manifest_path.read_text("utf-8"))["transaction_id"] == (
                seed_transaction
            )
            assert (tmp_path / README_PATH).read_bytes() == b"version-1"


@pytest.mark.skipif(sys.platform == "win32", reason="Linux crash matrix is verified")
def test_crash_matrix_covers_real_file_set_and_json_merge_effects(tmp_path: Path) -> None:
    _seed_install(tmp_path, "v1")
    _run_worker(tmp_path, "update", "applied", version="v2")
    installer = make_installer("v2")
    report = installer.inspect_recovery(base_path=tmp_path)
    assert FILE_SET_ID in report.applied + report.prepared + report.planned
    assert JSON_ID in report.applied + report.prepared + report.planned
    installer.repair(base_path=tmp_path)
    assert (tmp_path / README_PATH).read_bytes() == b"version-1"
    assert b"managed" in (tmp_path / SETTINGS_PATH).read_bytes()
    assert (tmp_path / SENTINEL_NAME).read_bytes() == SENTINEL_BYTES
    assert _active(tmp_path) is None
