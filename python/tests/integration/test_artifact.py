"""Black-box packaging gates: wheel install, console script, bundled hosts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PYTHON_PROJECT = _REPO / "python"
_HOST_COUNT = 7


def _venv_python(env_dir: Path) -> Path:
    if sys.platform == "win32":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _venv_script(env_dir: Path, name: str) -> Path:
    if sys.platform == "win32":
        return env_dir / "Scripts" / f"{name}.exe"
    return env_dir / "bin" / name


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    outdir = tmp_path_factory.mktemp("mi-build")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--outdir",
            str(outdir),
            str(_PYTHON_PROJECT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(outdir.glob("*.whl"))
    sdist = next(outdir.glob("*.tar.gz"))
    return {"outdir": outdir, "wheel": wheel, "sdist": sdist}


def test_build_emits_wheel_and_sdist(built_artifacts: dict[str, Path]) -> None:
    assert built_artifacts["wheel"].is_file()
    assert built_artifacts["sdist"].is_file()
    assert "minimalist_installer" in built_artifacts["wheel"].name
    assert "minimalist_installer" in built_artifacts["sdist"].name.replace("-", "_")


def test_wheel_installs_console_entry_and_bundled_hosts(
    built_artifacts: dict[str, Path],
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
    python = _venv_python(env_dir)
    script = _venv_script(env_dir, "minimalist-installer")

    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(built_artifacts["wheel"])],
        check=True,
        capture_output=True,
        text=True,
    )

    help_result = subprocess.run(
        [str(script), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "install" in help_result.stdout
    assert "detect" in help_result.stdout

    detect_result = subprocess.run(
        [str(script), "detect", "--json", "--search-path", ""],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path / "home")},
    )
    payload = json.loads(detect_result.stdout)
    assert payload["schema_version"] == 1
    assert "detections" in payload

    host_probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from importlib.resources import files\n"
                "from minimalist_installer.skills import HostRegistry\n"
                "hosts = HostRegistry.bundled(load_entry_points=False)\n"
                "root = files('minimalist_installer.skills') / 'hosts'\n"
                "tomls = [path.name for path in root.iterdir() if path.name.endswith('.toml')]\n"
                "print(len(hosts.hosts), len(tomls))\n"
                "print(','.join(sorted(host.id for host in hosts.hosts)))\n"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    counts, ids = host_probe.stdout.strip().splitlines()
    host_count, toml_count = (int(part) for part in counts.split())
    assert host_count == _HOST_COUNT
    assert toml_count == _HOST_COUNT
    assert "claude-code" in ids
    assert "github-copilot" in ids
