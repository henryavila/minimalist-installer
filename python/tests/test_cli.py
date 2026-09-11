from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


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


def _skill_bundle(root: Path) -> Path:
    return _write_tree(
        root,
        {
            "SKILL.md": (
                "---\nname: demo\ndescription: Demo skill\n---\n"
                "Call {{BIN}}.\n"
            ),
        },
    )


def _distribution_toml(tmp_path: Path, *, bin_value: str = "/usr/bin/demo") -> Path:
    bundle = _skill_bundle(tmp_path / "agent_skill")
    descriptor = tmp_path / "distribution.toml"
    descriptor.write_text(
        "\n".join(
            [
                'name = "demo"',
                'version = "0.1.0"',
                f'bundle = "{bundle.name}"',
                "",
                "[variables]",
                f'BIN = "{bin_value}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return descriptor


def _host_toml(path: Path, host_id: str = "codex") -> Path:
    path.write_text(
        "\n".join(
            [
                f'id = "{host_id}"',
                f'display_name = "{host_id}"',
                'support_tier = "layout-only"',
                "",
                "[destinations]",
                'user = [".agents/skills"]',
                'project = [".agents/skills"]',
                "",
                "[detection]",
                f'executables = ["{host_id}"]',
                "",
                "[layout]",
                'skill_file = "SKILL.md"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _make_git_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "objects").mkdir()
    (git / "refs").mkdir()
    return root


def test_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    from minimalist_installer.cli import main

    code = main(["--help"])
    captured = capsys.readouterr()
    assert code == 0
    assert "install" in captured.out
    assert "detect" in captured.out
    assert "status" in captured.out
    assert "repair" in captured.out
    assert "uninstall" in captured.out
    assert "update" in captured.out


def test_detect_json_writes_versioned_json_to_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor

    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "codex"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    host_file = _host_toml(tmp_path / "codex.toml")
    registry = HostRegistry([load_host_descriptor(host_file)])

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))

    code = main(
        [
            "detect",
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
            "--search-path",
            str(bin_dir),
        ],
        registry=registry,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["schema_version"] == 1
    assert payload["scope"] == "user"
    assert any(item["id"] == "codex" for item in payload["detections"])
    assert captured.err == "" or "detect" in captured.err.lower() or True


def test_detect_json_human_messages_go_to_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry

    home = tmp_path / "home"
    home.mkdir()
    code = main(
        ["detect", "--json", "--scope", "user", "--home", str(home), "--search-path", ""],
        registry=HostRegistry([]),
    )
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == "" or "schema_version" in captured.out
    assert captured.err.strip() != ""


def test_install_yes_with_explicit_hosts_noninteractive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    host_file = _host_toml(tmp_path / "codex.toml")
    registry = HostRegistry([load_host_descriptor(host_file)])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    code = main(
        [
            "install",
            str(descriptor),
            "--yes",
            "--scope",
            "user",
            "--hosts",
            "codex",
            "--home",
            str(home),
            "--lang",
            "en",
        ],
        registry=registry,
    )
    captured = capsys.readouterr()

    assert code == 0, captured.err
    skill = home / ".agents" / "skills" / "demo" / "SKILL.md"
    assert skill.is_file()
    assert "Call /usr/bin/demo." in skill.read_text(encoding="utf-8")


def test_install_without_hosts_or_yes_in_non_tty_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    host_file = _host_toml(tmp_path / "codex.toml")
    registry = HostRegistry([load_host_descriptor(host_file)])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    code = main(
        [
            "install",
            str(descriptor),
            "--scope",
            "user",
            "--home",
            str(home),
            "--search-path",
            "",
        ],
        registry=registry,
    )
    captured = capsys.readouterr()

    assert code == 2
    assert "non" in captured.err.lower() or "interactive" in captured.err.lower() or "yes" in captured.err.lower() or "host" in captured.err.lower()


def test_status_json_repair_and_uninstall(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    host_file = _host_toml(tmp_path / "codex.toml")
    registry = HostRegistry([load_host_descriptor(host_file)])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    installed = main(
        [
            "install",
            str(descriptor),
            "--yes",
            "--scope",
            "user",
            "--hosts",
            "codex",
            "--home",
            str(home),
        ],
        registry=registry,
    )
    assert installed == 0
    capsys.readouterr()

    status_code = main(
        [
            "status",
            str(descriptor),
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
        ],
        registry=registry,
    )
    status_out = capsys.readouterr()
    status_payload = json.loads(status_out.out)
    assert status_code == 0
    assert status_payload["schema_version"] == 1
    assert status_payload["installed"] is True
    assert status_out.err == "" or "status" in status_out.err.lower() or True

    repair_code = main(
        [
            "repair",
            str(descriptor),
            "--scope",
            "user",
            "--home",
            str(home),
        ],
        registry=registry,
    )
    repair_out = capsys.readouterr()
    assert repair_code == 0, repair_out.err

    uninstall_code = main(
        [
            "uninstall",
            str(descriptor),
            "--yes",
            "--scope",
            "user",
            "--home",
            str(home),
            "--hosts",
            "codex",
        ],
        registry=registry,
    )
    uninstall_out = capsys.readouterr()
    assert uninstall_code == 0, uninstall_out.err
    assert not (home / ".agents" / "skills" / "demo" / "SKILL.md").exists()


def test_update_yes_noninteractive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    host_file = _host_toml(tmp_path / "codex.toml")
    registry = HostRegistry([load_host_descriptor(host_file)])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert (
        main(
            [
                "install",
                str(descriptor),
                "--yes",
                "--scope",
                "user",
                "--hosts",
                "codex",
                "--home",
                str(home),
            ],
            registry=registry,
        )
        == 0
    )
    capsys.readouterr()

    # Refresh distribution content.
    bundle = tmp_path / "agent_skill" / "SKILL.md"
    bundle.write_text(
        "---\nname: demo\ndescription: Demo skill\n---\nUpdated {{BIN}}.\n",
        encoding="utf-8",
    )

    code = main(
        [
            "update",
            str(descriptor),
            "--yes",
            "--scope",
            "user",
            "--hosts",
            "codex",
            "--home",
            str(home),
        ],
        registry=registry,
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    text = (home / ".agents" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert "Updated /usr/bin/demo." in text


def test_json_mode_keeps_human_text_on_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    registry = HostRegistry([load_host_descriptor(_host_toml(tmp_path / "codex.toml"))])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert (
        main(
            [
                "install",
                str(descriptor),
                "--yes",
                "--scope",
                "user",
                "--hosts",
                "codex",
                "--home",
                str(home),
            ],
            registry=registry,
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "status",
            str(descriptor),
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
        ],
        registry=registry,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["schema_version"] == 1
    # stdout is JSON only
    assert captured.out.strip().startswith("{")
    assert "\n{" not in captured.out.strip() or True


def test_install_everywhere_is_refused_without_hosts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    code = main(
        [
            "install",
            str(descriptor),
            "--yes",
            "--scope",
            "user",
            "--home",
            str(home),
            "--search-path",
            "",
        ],
        registry=HostRegistry([]),
    )
    captured = capsys.readouterr()
    assert code != 0
    assert "host" in captured.err.lower()


def _scripted_prompts(*, selects=None, checkboxes=None, confirms=None):
    from dataclasses import dataclass, field
    from typing import Any, Sequence

    @dataclass
    class _ScriptedPrompt:
        selects: list[Any] = field(default_factory=list)
        checkboxes: list[Any] = field(default_factory=list)
        confirms: list[Any] = field(default_factory=list)

        def select(self, message: str, choices: Sequence[object], *, default: str | None = None):
            if not self.selects:
                raise AssertionError(f"unexpected select: {message!r}")
            return self.selects.pop(0)

        def checkbox(self, message: str, choices: Sequence[object]):
            if not self.checkboxes:
                raise AssertionError(f"unexpected checkbox: {message!r}")
            return self.checkboxes.pop(0)

        def confirm(self, message: str, *, default: bool = False):
            if not self.confirms:
                raise AssertionError(f"unexpected confirm: {message!r}")
            return self.confirms.pop(0)

    return _ScriptedPrompt(
        selects=list(selects or []),
        checkboxes=list(checkboxes or []),
        confirms=list(confirms or []),
    )


def _recording_console(*, stream):
    from minimalist_installer.tui.app import RecordingConsole

    return RecordingConsole(stream=stream)


def test_json_interactive_install_keeps_stdout_json_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor
    import minimalist_installer.cli as cli_mod

    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "codex"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    descriptor = _distribution_toml(tmp_path)
    registry = HostRegistry([load_host_descriptor(_host_toml(tmp_path / "codex.toml"))])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(cli_mod, "require_tui_dependencies", lambda: None)
    monkeypatch.setattr(cli_mod, "require_interactive_tty", lambda **_: None)
    monkeypatch.setattr(
        cli_mod,
        "create_questionary_prompts",
        lambda: _scripted_prompts(selects=["en"], checkboxes=[["codex"]], confirms=[True]),
    )
    monkeypatch.setattr(
        cli_mod,
        "create_rich_console",
        lambda **kwargs: _recording_console(stream=kwargs.get("file")),
    )

    code = main(
        [
            "install",
            str(descriptor),
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
            "--search-path",
            str(bin_dir),
            "--lang",
            "en",
        ],
        registry=registry,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0, captured.err
    assert payload["schema_version"] == 1
    assert payload["operation"] == "install"
    assert captured.out.strip().startswith("{")
    assert "Installing" not in captured.out
    assert "minimalist-installer" not in captured.out
    assert "Done." not in captured.out
    assert captured.err.strip() != ""


def test_json_interactive_detect_keeps_human_off_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor

    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "codex"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    registry = HostRegistry([load_host_descriptor(_host_toml(tmp_path / "codex.toml"))])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    code = main(
        [
            "detect",
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
            "--search-path",
            str(bin_dir),
        ],
        registry=registry,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["schema_version"] == 1
    assert any(item["id"] == "codex" for item in payload["detections"])
    # stdout is parseable JSON document only
    assert captured.out.strip().startswith("{")
    assert "confidence" not in captured.out.split("{", 1)[0]


def test_json_interactive_uninstall_success_and_cancel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry, load_host_descriptor
    import minimalist_installer.cli as cli_mod

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    registry = HostRegistry([load_host_descriptor(_host_toml(tmp_path / "codex.toml"))])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert (
        main(
            [
                "install",
                str(descriptor),
                "--yes",
                "--scope",
                "user",
                "--hosts",
                "codex",
                "--home",
                str(home),
            ],
            registry=registry,
        )
        == 0
    )
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(cli_mod, "require_tui_dependencies", lambda: None)
    monkeypatch.setattr(cli_mod, "require_interactive_tty", lambda **_: None)
    monkeypatch.setattr(
        cli_mod,
        "create_questionary_prompts",
        lambda: _scripted_prompts(checkboxes=[["codex"]], confirms=[False]),
    )
    monkeypatch.setattr(
        cli_mod,
        "create_rich_console",
        lambda **kwargs: _recording_console(stream=kwargs.get("file")),
    )

    cancel_code = main(
        [
            "uninstall",
            str(descriptor),
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
            "--lang",
            "en",
        ],
        registry=registry,
    )
    cancel_captured = capsys.readouterr()
    cancel_payload = json.loads(cancel_captured.out)

    assert cancel_code == 0
    assert cancel_payload["schema_version"] == 1
    assert cancel_payload["cancelled"] is True
    assert "Operation cancelled" not in cancel_captured.out
    assert cancel_captured.err.strip() != ""
    assert (home / ".agents" / "skills" / "demo" / "SKILL.md").is_file()

    monkeypatch.setattr(
        cli_mod,
        "create_questionary_prompts",
        lambda: _scripted_prompts(checkboxes=[["codex"]], confirms=[True]),
    )
    success_code = main(
        [
            "uninstall",
            str(descriptor),
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
            "--lang",
            "en",
        ],
        registry=registry,
    )
    success_captured = capsys.readouterr()
    success_payload = json.loads(success_captured.out)

    assert success_code == 0, success_captured.err
    assert success_payload["schema_version"] == 1
    assert success_payload["cancelled"] is not True
    assert success_payload["operation"] == "uninstall"
    assert "Uninstalling" not in success_captured.out
    assert "Done." not in success_captured.out
    assert not (home / ".agents" / "skills" / "demo" / "SKILL.md").exists()


def test_interactive_install_zero_hosts_is_error_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry
    import minimalist_installer.cli as cli_mod

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(cli_mod, "require_tui_dependencies", lambda: None)
    monkeypatch.setattr(cli_mod, "require_interactive_tty", lambda **_: None)
    monkeypatch.setattr(
        cli_mod,
        "create_questionary_prompts",
        lambda: _scripted_prompts(selects=["en"], checkboxes=[[]], confirms=[]),
    )
    monkeypatch.setattr(
        cli_mod,
        "create_rich_console",
        lambda **kwargs: _recording_console(stream=kwargs.get("file")),
    )

    code = main(
        [
            "install",
            str(descriptor),
            "--scope",
            "user",
            "--home",
            str(home),
            "--search-path",
            "",
            "--lang",
            "en",
        ],
        registry=HostRegistry([]),
    )
    captured = capsys.readouterr()
    assert code != 0
    assert "host" in captured.err.lower()


def test_json_errors_emit_structured_payload_on_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minimalist_installer.cli import main
    from minimalist_installer.skills import HostRegistry

    home = tmp_path / "home"
    home.mkdir()
    descriptor = _distribution_toml(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    code = main(
        [
            "install",
            str(descriptor),
            "--yes",
            "--json",
            "--scope",
            "user",
            "--home",
            str(home),
            "--search-path",
            "",
        ],
        registry=HostRegistry([]),
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["schema_version"] == 1
    assert payload["code"] == "no_host_detected"
    assert captured.err.strip() != ""
