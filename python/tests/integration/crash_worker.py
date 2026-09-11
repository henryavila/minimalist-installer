"""Subprocess worker that pauses after a durable installer WAL boundary.

Parent tests spawn this module, wait for the ready marker, then SIGKILL.
It is not a pytest test module.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from minimalist_installer import EffectPlan, PlanContext, define_installer
from minimalist_installer.core.journal import TransactionRepository
from minimalist_installer.core.locks import canonical_resource_identity
from minimalist_installer.core.manifest import ManifestRepository
from minimalist_installer.core.path_safety import SafeFilesystem
from minimalist_installer.providers import FileSetProvider

MANIFEST_DIRECTORY = "state"
SENTINEL_NAME = "outside-sentinel.txt"
SENTINEL_BYTES = b"preserve-outside-bytes"
README_PATH = "app/README.txt"
SETTINGS_PATH = "settings.json"
FILE_SET_ID = "files:app"
JSON_ID = "json:settings"

VERSIONS: dict[str, dict[str, object]] = {
    "v1": {
        "readme": "version-1",
        "delta": {"managed": True},
    },
    "v2": {
        "readme": "version-2",
        "delta": {"managed": True, "updated": True},
    },
}

HOLD_POINTS = frozenset(
    {
        "journal",
        "prepared",
        "applied",
        "reverted",
        "effects_applied",
        "committing",
        "manifest",
        "manifest_committed",
        "manifest_removed",
        "tombstone",
        "repairing",
        "resuming",
        "effects_reverted",
        "rolled_back",
    }
)


class JsonMergeProvider:
    def __init__(self, *, path: str = SETTINGS_PATH) -> None:
        self.path = path

    def plan(self, config: object, context: PlanContext) -> tuple[EffectPlan, ...]:
        if not isinstance(config, dict):
            raise TypeError("config must be a mapping")
        delta = config["delta"]
        if not isinstance(delta, dict):
            raise TypeError("config.delta must be an object")
        resource = canonical_resource_identity(
            "path", context.base_path / self.path
        )
        return (
            EffectPlan(
                id=JSON_ID,
                type="json_merge",
                version=1,
                args={"path": self.path, "delta": delta},
                resources=(resource,),
            ),
        )


def installer_config(version: str) -> dict[str, object]:
    spec = VERSIONS[version]
    return {
        "consumer": "crash-worker",
        "consumer_version": "1",
        "manifest_dir": MANIFEST_DIRECTORY,
        "files": [{"path": "README.txt", "content": spec["readme"]}],
        "delta": spec["delta"],
    }


def make_installer(version: str):
    return define_installer(
        config=installer_config(version),
        providers=(
            FileSetProvider(effect_id=FILE_SET_ID, destination="app"),
            JsonMergeProvider(),
        ),
        manifest_directory=MANIFEST_DIRECTORY,
    )


def write_sentinel(base: Path) -> None:
    path = base / SENTINEL_NAME
    if not path.exists():
        path.write_bytes(SENTINEL_BYTES)


def _pause(hold_after: str, ready: Path, name: str) -> None:
    if name != hold_after:
        return
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text(name, encoding="utf-8")
    while True:
        time.sleep(0.05)


def install_holds(hold_after: str, ready: Path) -> None:
    original_begin = TransactionRepository.begin
    original_prepared = TransactionRepository.record_prepared
    original_applied = TransactionRepository.record_applied
    original_reverted = TransactionRepository.record_reverted
    original_checkpoint = TransactionRepository.checkpoint
    original_begin_repair = getattr(
        TransactionRepository, "begin_repair", None
    )
    original_commit = ManifestRepository.commit
    original_write_json = SafeFilesystem.atomic_write_json

    def begin(self, *args, **kwargs):
        journal = original_begin(self, *args, **kwargs)
        _pause(hold_after, ready, "journal")
        return journal

    def record_prepared(self, *args, **kwargs):
        journal = original_prepared(self, *args, **kwargs)
        _pause(hold_after, ready, "prepared")
        return journal

    def record_applied(self, *args, **kwargs):
        journal = original_applied(self, *args, **kwargs)
        _pause(hold_after, ready, "applied")
        return journal

    def record_reverted(self, *args, **kwargs):
        journal = original_reverted(self, *args, **kwargs)
        _pause(hold_after, ready, "reverted")
        return journal

    def checkpoint(self, transaction_id, name):
        journal = original_checkpoint(self, transaction_id, name)
        _pause(hold_after, ready, name)
        return journal

    def commit(self, *args, **kwargs):
        result = original_commit(self, *args, **kwargs)
        _pause(hold_after, ready, "manifest")
        return result

    def write_json(self, relative, value, **kwargs):
        original_write_json(self, relative, value, **kwargs)
        if str(relative).endswith("active.json") and isinstance(value, dict):
            if value.get("state") == "cleanup":
                _pause(hold_after, ready, "tombstone")

    TransactionRepository.begin = begin
    TransactionRepository.record_prepared = record_prepared
    TransactionRepository.record_applied = record_applied
    TransactionRepository.record_reverted = record_reverted
    TransactionRepository.checkpoint = checkpoint
    ManifestRepository.commit = commit
    SafeFilesystem.atomic_write_json = write_json
    if original_begin_repair is not None:
        def begin_repair(self, *args, **kwargs):
            journal = original_begin_repair(self, *args, **kwargs)
            hold_name = (
                "resuming"
                if "resuming" in journal.operation_checkpoints
                else "repairing"
            )
            _pause(hold_after, ready, hold_name)
            return journal

        TransactionRepository.begin_repair = begin_repair


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument(
        "--operation",
        required=True,
        choices=("install", "update", "uninstall", "repair"),
    )
    parser.add_argument("--hold-after", required=True, choices=sorted(HOLD_POINTS))
    parser.add_argument("--ready", required=True)
    parser.add_argument("--version", default="v1", choices=sorted(VERSIONS))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)

    base = Path(args.base)
    base.mkdir(parents=True, exist_ok=True)
    write_sentinel(base)
    install_holds(args.hold_after, Path(args.ready))
    installer = make_installer(args.version)
    if args.operation == "install":
        installer.install(base_path=base)
    elif args.operation == "update":
        installer.update(base_path=base)
    elif args.operation == "uninstall":
        installer.uninstall(base_path=base)
    else:
        installer.repair(base_path=base, resume=args.resume)
    return 0


if __name__ == "__main__":
    sys.exit(main())
