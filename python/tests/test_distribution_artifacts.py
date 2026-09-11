from __future__ import annotations

import subprocess
import sys
import tarfile
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def test_wheel_and_sdist_include_typing_marker_and_root_license(tmp_path: Path) -> None:
    repository = Path(__file__).parents[2]
    python_project = repository / "python"
    root_license = (repository / "LICENSE").read_bytes()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--outdir",
            str(tmp_path),
            str(python_project),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    wheel = next(tmp_path.glob("*.whl"))
    sdist = next(tmp_path.glob("*.tar.gz"))

    with ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        license_name = next(
            name for name in names if name.endswith(".dist-info/licenses/LICENSE")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_name))

        assert "minimalist_installer/py.typed" in names
        assert archive.read(license_name) == root_license
        assert metadata.get_all("License-File") == ["LICENSE"]
        host_descriptors = [
            name
            for name in names
            if name.startswith("minimalist_installer/skills/hosts/")
            and name.endswith(".toml")
        ]
        assert len(host_descriptors) == 7

    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        root = next(name.split("/", 1)[0] for name in names)

        assert f"{root}/src/minimalist_installer/py.typed" in names
        license_member = archive.extractfile(f"{root}/LICENSE")
        assert license_member is not None
        assert license_member.read() == root_license
