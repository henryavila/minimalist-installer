from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence
from unittest.mock import MagicMock

import pytest

from minimalist_installer import Operation, OperationResult, OperationStatus
from minimalist_installer.skills import (
    DetectionResult,
    DetectionSignals,
    Evidence,
    EvidenceKind,
    HostAdapter,
    HostDestinations,
    HostDetection,
    HostLayout,
    HostRegistry,
    PlannedDestination,
    Scope,
    SkillDistribution,
    SupportTier,
)


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


def _skill(root: Path, *, body: str = "Use the tool.\n") -> Path:
    return _write_tree(
        root,
        {
            "SKILL.md": (
                "---\nname: demo\ndescription: A demo skill\n---\n" + body
            ),
        },
    )


def _host(
    host_id: str,
    *destinations: str,
    confidence: int = 40,
    evidence: tuple[Evidence, ...] | None = None,
) -> HostDetection:
    paths = destinations or (".agents/skills",)
    adapter = HostAdapter(
        id=host_id,
        display_name=host_id.replace("-", " ").title(),
        support_tier=SupportTier.LAYOUT_ONLY,
        destinations=HostDestinations(user=paths, project=paths),
        detection=DetectionSignals(executables=(host_id,)),
        layout=HostLayout(skill_file="SKILL.md"),
    )
    if evidence is None:
        evidence = (Evidence(kind=EvidenceKind.EXECUTABLE, value=host_id),)
    return HostDetection(host=adapter, confidence=confidence, evidence=evidence)


def _detection(
    *hosts: HostDetection,
    scope: Scope = Scope.USER,
    root: Path | None = None,
) -> DetectionResult:
    destinations = tuple(
        PlannedDestination(
            path=(root or Path("/tmp")) / host.host.destinations.user[0],
            scope=scope,
            host_ids=(host.host.id,),
        )
        for host in hosts
    )
    return DetectionResult(scope=scope, detections=hosts, destinations=destinations)


@dataclass
class FakePrompt:
    """Scripted PromptPort used by InstallerApp tests."""

    selects: list[Any] = field(default_factory=list)
    checkboxes: list[Any] = field(default_factory=list)
    confirms: list[Any] = field(default_factory=list)
    log: list[tuple[str, object]] = field(default_factory=list)

    def select(
        self,
        message: str,
        choices: Sequence[object],
        *,
        default: str | None = None,
    ) -> str | None:
        self.log.append(("select", (message, default, list(choices))))
        if not self.selects:
            raise AssertionError(f"unexpected select: {message!r}")
        return self.selects.pop(0)

    def checkbox(
        self,
        message: str,
        choices: Sequence[object],
    ) -> list[str] | None:
        self.log.append(("checkbox", (message, list(choices))))
        if not self.checkboxes:
            raise AssertionError(f"unexpected checkbox: {message!r}")
        return self.checkboxes.pop(0)

    def confirm(self, message: str, *, default: bool = False) -> bool | None:
        self.log.append(("confirm", (message, default)))
        if not self.confirms:
            raise AssertionError(f"unexpected confirm: {message!r}")
        return self.confirms.pop(0)


@dataclass
class FakeConsole:
    lines: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)

    def print(self, message: str = "") -> None:
        self.lines.append(str(message))

    def rule(self, message: str = "") -> None:
        self.lines.append(f"=== {message} ===" if message else "===")

    @contextmanager
    def status(self, message: str) -> Iterator[None]:
        self.statuses.append(message)
        yield


@pytest.fixture
def distribution(tmp_path: Path) -> SkillDistribution:
    bundle = _skill(tmp_path / "bundle")
    return SkillDistribution(name="demo", version="1.2.3", bundle=bundle)


def test_intro_shows_package_version(distribution: SkillDistribution) -> None:
    from minimalist_installer.tui.app import InstallerApp, run_install_flow
    from minimalist_installer.tui import messages

    prompts = FakePrompt(
        selects=["user", "en"],
        checkboxes=[["codex"]],
        confirms=[False],
    )
    console = FakeConsole()
    detection = _detection(_host("codex", confidence=40))
    installer = MagicMock()

    outcome = run_install_flow(
        distribution,
        prompts=prompts,
        console=console,
        lang=None,
        detect_hosts=lambda **_: detection,
        installer_factory=lambda **_: installer,
        version="9.9.9",
    )

    joined = "\n".join(console.lines)
    assert "9.9.9" in joined
    assert outcome.cancelled is True
    installer.install.assert_not_called()
    assert messages.catalog("en")["intro"].format(version="9.9.9")


def test_scope_choice_user_or_project(
    distribution: SkillDistribution, tmp_path: Path
) -> None:
    from minimalist_installer.tui.app import run_install_flow

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / ".git" / "objects").mkdir()
    (project / ".git" / "refs").mkdir()

    prompts = FakePrompt(
        selects=["project", "en"],
        checkboxes=[["codex"]],
        confirms=[False],
    )
    console = FakeConsole()
    detection = _detection(_host("codex"), scope=Scope.PROJECT, root=project)

    outcome = run_install_flow(
        distribution,
        prompts=prompts,
        console=console,
        home=home,
        project=project,
        detect_hosts=lambda **kwargs: detection,
        installer_factory=lambda **_: MagicMock(),
        version="0.1.0",
    )

    assert outcome.scope is Scope.PROJECT
    select_messages = [entry[1][0] for entry in prompts.log if entry[0] == "select"]
    assert any("scope" in message.lower() or "onde" in message.lower() or "where" in message.lower() for message in select_messages)


def test_shows_evidence_and_preselects_high_confidence_hosts(
    distribution: SkillDistribution, tmp_path: Path
) -> None:
    from minimalist_installer.tui.app import (
        PRESELECT_MIN_CONFIDENCE,
        run_install_flow,
        should_preselect,
    )

    assert PRESELECT_MIN_CONFIDENCE == 30
    high = _host(
        "codex",
        confidence=40,
        evidence=(Evidence(kind=EvidenceKind.EXECUTABLE, value="codex"),),
    )
    mid = _host(
        "gemini",
        confidence=30,
        evidence=(Evidence(kind=EvidenceKind.ENVIRONMENT, value="GEMINI_CLI"),),
    )
    low = _host(
        "cursor",
        confidence=10,
        evidence=(Evidence(kind=EvidenceKind.CONFIG_DIRECTORY, value=".cursor"),),
    )
    assert should_preselect(high) is True
    assert should_preselect(mid) is True
    assert should_preselect(low) is False

    prompts = FakePrompt(
        selects=["user", "en"],
        checkboxes=[["codex", "gemini"]],
        confirms=[False],
    )
    console = FakeConsole()
    home = tmp_path / "home"
    home.mkdir()

    run_install_flow(
        distribution,
        prompts=prompts,
        console=console,
        home=home,
        detect_hosts=lambda **_: _detection(high, mid, low, root=home),
        installer_factory=lambda **_: MagicMock(),
        version="0.1.0",
    )

    joined = "\n".join(console.lines)
    assert "codex" in joined.lower()
    assert "executable" in joined.lower() or "codex" in joined
    checkbox = next(entry for entry in prompts.log if entry[0] == "checkbox")
    choices = checkbox[1][1]
    checked = {
        getattr(choice, "value", choice[0] if isinstance(choice, tuple) else choice): (
            getattr(choice, "checked", False)
            if not isinstance(choice, tuple)
            else (choice[2] if len(choice) > 2 else False)
        )
        for choice in choices
    }
    assert checked["codex"] is True
    assert checked["gemini"] is True
    assert checked.get("cursor") in (False, None)


def test_user_can_customize_host_selection(
    distribution: SkillDistribution, tmp_path: Path
) -> None:
    from minimalist_installer.tui.app import run_install_flow

    prompts = FakePrompt(
        selects=["user", "en"],
        checkboxes=[["cursor"]],
        confirms=[False],
    )
    console = FakeConsole()
    home = tmp_path / "home"
    home.mkdir()
    detection = _detection(
        _host("codex", confidence=40),
        _host(
            "cursor",
            ".cursor/skills",
            confidence=10,
            evidence=(Evidence(kind=EvidenceKind.CONFIG_DIRECTORY, value=".cursor"),),
        ),
        root=home,
    )

    outcome = run_install_flow(
        distribution,
        prompts=prompts,
        console=console,
        home=home,
        detect_hosts=lambda **_: detection,
        installer_factory=lambda **_: MagicMock(),
        version="0.1.0",
    )

    assert outcome.selected_hosts == ("cursor",)


def test_cancel_before_confirm_does_not_call_install(
    distribution: SkillDistribution, tmp_path: Path
) -> None:
    from minimalist_installer.tui.app import run_install_flow

    installer = MagicMock()
    prompts = FakePrompt(
        selects=["user", "en"],
        checkboxes=[["codex"]],
        confirms=[False],
    )
    home = tmp_path / "home"
    home.mkdir()

    outcome = run_install_flow(
        distribution,
        prompts=prompts,
        console=FakeConsole(),
        home=home,
        detect_hosts=lambda **_: _detection(_host("codex"), root=home),
        installer_factory=lambda **_: installer,
        version="0.1.0",
    )

    assert outcome.cancelled is True
    installer.install.assert_not_called()
    installer.update.assert_not_called()


def test_confirm_calls_install_once(
    distribution: SkillDistribution, tmp_path: Path
) -> None:
    from minimalist_installer.tui.app import run_install_flow

    home = tmp_path / "home"
    home.mkdir()
    result = OperationResult(
        operation=Operation.INSTALL,
        status=OperationStatus.COMPLETED,
        transaction_id="tx-1",
        installation_id="install-1",
        planned=("skills:demo:user:agents:skills",),
        applied=("skills:demo:user:agents:skills",),
        selected_hosts=("codex",),
    )
    installer = MagicMock()
    installer.install.return_value = result
    prompts = FakePrompt(
        selects=["user", "en"],
        checkboxes=[["codex"]],
        confirms=[True],
    )
    console = FakeConsole()

    outcome = run_install_flow(
        distribution,
        prompts=prompts,
        console=console,
        home=home,
        detect_hosts=lambda **_: _detection(_host("codex"), root=home),
        installer_factory=lambda **kwargs: installer,
        version="0.1.0",
    )

    assert outcome.cancelled is False
    assert outcome.result is not None
    assert outcome.result.status is OperationStatus.COMPLETED
    installer.install.assert_called_once()
    assert console.statuses, "progress status should be shown during install"
    joined = "\n".join(console.lines).lower()
    assert "codex" in joined
    assert "next" in joined or "próxim" in joined or "done" in joined or "conclu" in joined


def test_pt_and_en_message_catalogs() -> None:
    from minimalist_installer.tui import messages

    en = messages.catalog("en")
    pt = messages.catalog("pt")
    for key in (
        "intro",
        "select_scope",
        "select_hosts",
        "select_lang",
        "confirm_install",
        "cancelled",
        "no_hosts",
        "next_steps",
    ):
        assert key in en
        assert key in pt
        assert en[key] != pt[key]


def test_no_color_and_ascii_theme_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from minimalist_installer.tui import theme

    monkeypatch.setenv("NO_COLOR", "1")
    plain = theme.resolve_theme(unicode_ok=True, color_ok=True)
    assert plain.color is False

    ascii_theme = theme.resolve_theme(unicode_ok=False, color_ok=True)
    assert ascii_theme.unicode is False
    assert "->" in ascii_theme.arrow or ascii_theme.arrow.isascii()
    assert ascii_theme.checkmark.isascii()

    fancy = theme.resolve_theme(unicode_ok=True, color_ok=True, force_color=True)
    assert fancy.unicode is True


def test_missing_tui_extra_and_no_tty_fail_clearly(
    monkeypatch: pytest.MonkeyPatch, distribution: SkillDistribution
) -> None:
    from minimalist_installer import NonInteractiveInputRequiredError
    from minimalist_installer.tui import app as tui_app

    with pytest.raises((ImportError, RuntimeError, NonInteractiveInputRequiredError)) as missing:
        tui_app.require_tui_dependencies(available=False)
    assert "tui" in str(missing.value).lower() or "rich" in str(missing.value).lower()

    with pytest.raises(NonInteractiveInputRequiredError) as no_tty:
        tui_app.require_interactive_tty(is_tty=False)
    assert no_tty.value.code.value == "non_interactive_input_required"


def test_conflict_review_lists_existing_destinations(
    distribution: SkillDistribution, tmp_path: Path
) -> None:
    from minimalist_installer.tui.app import run_install_flow

    home = tmp_path / "home"
    home.mkdir()
    existing = home / ".agents" / "skills" / "demo" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("local\n", encoding="utf-8")

    registry = HostRegistry([_host("codex").host])
    prompts = FakePrompt(
        selects=["user", "en"],
        checkboxes=[["codex"]],
        confirms=[False],
    )
    console = FakeConsole()

    run_install_flow(
        distribution,
        prompts=prompts,
        console=console,
        home=home,
        registry=registry,
        detect_hosts=lambda **_: _detection(_host("codex"), root=home),
        installer_factory=lambda **_: MagicMock(),
        version="0.1.0",
    )

    joined = "\n".join(console.lines)
    assert "conflict" in joined.lower() or "exist" in joined.lower() or "SKILL.md" in joined


def test_keyboard_cancel_from_prompt_aborts(
    distribution: SkillDistribution, tmp_path: Path
) -> None:
    from minimalist_installer.tui.app import run_install_flow

    installer = MagicMock()
    prompts = FakePrompt(selects=[None])
    home = tmp_path / "home"
    home.mkdir()

    outcome = run_install_flow(
        distribution,
        prompts=prompts,
        console=FakeConsole(),
        home=home,
        detect_hosts=lambda **_: _detection(_host("codex"), root=home),
        installer_factory=lambda **_: installer,
        version="0.1.0",
    )

    assert outcome.cancelled is True
    installer.install.assert_not_called()
