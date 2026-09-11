"""Installer TUI controller with injectable prompt/console ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator, Protocol

from .. import __version__ as _package_version
from ..core.errors import (
    NoHostDetectedError,
    NonInteractiveInputRequiredError,
)
from ..core.models import EffectPlan, Operation, OperationResult, PlanContext
from ..core.driver import Installer, define_installer
from ..skills import (
    DetectionResult,
    HostDetection,
    HostRegistry,
    Scope,
    SkillDistribution,
    SkillDistributionPlan,
    detect_hosts as _detect_hosts,
    plan_distribution,
)
from . import messages
from .theme import Theme, resolve_theme

# Preselect when confidence >= 30 (environment or better). One executable
# alone scores 40 and therefore also preselects. Users still confirm.
PRESELECT_MIN_CONFIDENCE = 30


def should_preselect(detection: HostDetection) -> bool:
    """Return whether a detection clears the documented preselect threshold."""

    return detection.confidence >= PRESELECT_MIN_CONFIDENCE


@dataclass(frozen=True, slots=True)
class Choice:
    """One selectable option for prompt ports."""

    value: str
    label: str
    checked: bool = False


class PromptPort(Protocol):
    def select(
        self,
        message: str,
        choices: Sequence[Choice],
        *,
        default: str | None = None,
    ) -> str | None: ...

    def checkbox(
        self,
        message: str,
        choices: Sequence[Choice],
    ) -> Sequence[str] | None: ...

    def confirm(self, message: str, *, default: bool = False) -> bool | None: ...


class ConsolePort(Protocol):
    def print(self, message: str = "") -> None: ...

    def rule(self, message: str = "") -> None: ...

    @contextmanager
    def status(self, message: str) -> Iterator[None]: ...


@dataclass
class RecordingConsole:
    """Simple console used by non-interactive CLI human output."""

    lines: list[str] = field(default_factory=list)
    stream: object | None = None

    def print(self, message: str = "") -> None:
        self.lines.append(str(message))
        if self.stream is not None:
            print(message, file=self.stream)

    def rule(self, message: str = "") -> None:
        self.print(f"=== {message} ===" if message else "===")

    @contextmanager
    def status(self, message: str) -> Iterator[None]:
        self.print(message)
        yield


@dataclass(frozen=True, slots=True)
class FlowOutcome:
    """Structured result of an install/update flow."""

    cancelled: bool = False
    result: OperationResult | None = None
    detection: DetectionResult | None = None
    selected_hosts: tuple[str, ...] = ()
    scope: Scope | None = None
    lang: str = "en"
    planned: SkillDistributionPlan | None = None


class FixedPlansProvider:
    """Provider that re-emits a previously planned effect set."""

    def __init__(self, plans: Sequence[EffectPlan]) -> None:
        self._plans = tuple(plans)

    def plan(
        self, config: Mapping[str, object], context: PlanContext
    ) -> tuple[EffectPlan, ...]:
        return self._plans


def build_installer(
    distribution: SkillDistribution,
    planned: SkillDistributionPlan,
) -> Installer:
    """Bind distribution metadata and planned file-set effects to an Installer."""

    return define_installer(
        config={
            "consumer": distribution.name,
            "consumer_version": distribution.version,
            "manifest_dir": ".minimalist-installer",
        },
        providers=(FixedPlansProvider(planned.plans),),
    )


def require_tui_dependencies(*, available: bool | None = None) -> None:
    """Fail closed when interactive adapters need Rich/Questionary."""

    if available is False:
        raise RuntimeError(messages.catalog("en")["missing_tui"])
    if available is True:
        return
    try:
        import questionary  # noqa: F401
        import rich  # noqa: F401
    except ImportError as error:
        raise RuntimeError(messages.catalog("en")["missing_tui"]) from error


def require_interactive_tty(*, is_tty: bool) -> None:
    if not is_tty:
        raise NonInteractiveInputRequiredError(messages.catalog("en")["no_tty"])


def _format_evidence(detection: HostDetection) -> str:
    parts = [f"{item.kind.value}={item.value}" for item in detection.evidence]
    return ", ".join(parts) if parts else "(none)"


def _existing_conflicts(planned: SkillDistributionPlan) -> tuple[Path, ...]:
    return tuple(item.path for item in planned.files if item.path.exists())


def run_install_flow(
    distribution: SkillDistribution,
    *,
    prompts: PromptPort,
    console: ConsolePort,
    operation: Operation = Operation.INSTALL,
    scope: Scope | str | None = None,
    hosts: Sequence[str] | None = None,
    lang: str | None = None,
    yes: bool = False,
    home: Path | None = None,
    project: Path | None = None,
    registry: HostRegistry | None = None,
    environ: Mapping[str, str] | None = None,
    search_path: str | None = None,
    version: str | None = None,
    detect_hosts: Callable[..., DetectionResult] = _detect_hosts,
    installer_factory: Callable[..., Installer] | None = None,
    theme: Theme | None = None,
) -> FlowOutcome:
    """Drive the Atomic-style install/update flow using injectable ports.

    Mutations happen only through ``Installer`` / ``Driver``. Cancellation
    before confirmation leaves no transaction.
    """

    ui_lang = messages.normalize_lang(lang)
    text = messages.catalog(ui_lang)
    pkg_version = version if version is not None else _package_version
    active_theme = theme or resolve_theme()
    console.rule()
    console.print(text["intro"].format(version=pkg_version))

    resolved_scope: Scope
    if scope is None:
        choice = prompts.select(
            text["select_scope"],
            [
                Choice("user", text["scope_user"]),
                Choice("project", text["scope_project"]),
            ],
            default="user",
        )
        if choice is None:
            console.print(text["cancelled"])
            return FlowOutcome(cancelled=True, lang=ui_lang)
        resolved_scope = Scope(choice)
    else:
        resolved_scope = scope if isinstance(scope, Scope) else Scope(scope)

    detection = detect_hosts(
        scope=resolved_scope,
        registry=registry,
        home=home,
        project=project,
        environ=environ,
        search_path=search_path,
    )

    console.print(text["detected_header"])
    for item in detection.detections:
        console.print(
            text["evidence_line"].format(
                name=item.host.display_name,
                confidence=item.confidence,
                evidence=_format_evidence(item),
            )
        )

    selected: tuple[str, ...]
    if hosts is not None:
        selected = tuple(dict.fromkeys(hosts))
    elif yes:
        selected = tuple(
            item.host.id
            for item in detection.detections
            if should_preselect(item)
        )
    else:
        choices = [
            Choice(
                value=item.host.id,
                label=f"{item.host.display_name} ({item.confidence})",
                checked=should_preselect(item),
            )
            for item in detection.detections
        ]
        if not choices:
            # Offer nothing detected — still an error path after empty pick.
            console.print(text["no_hosts"])
            return FlowOutcome(
                cancelled=True,
                detection=detection,
                scope=resolved_scope,
                lang=ui_lang,
            )
        picked = prompts.checkbox(text["select_hosts"], choices)
        if picked is None:
            console.print(text["cancelled"])
            return FlowOutcome(
                cancelled=True,
                detection=detection,
                scope=resolved_scope,
                lang=ui_lang,
            )
        selected = tuple(dict.fromkeys(picked))

    if not selected:
        console.print(text["no_hosts"])
        raise NoHostDetectedError(text["no_hosts"])

    if lang is None and not yes:
        lang_choice = prompts.select(
            text["select_lang"],
            [
                Choice("en", text["lang_en"]),
                Choice("pt", text["lang_pt"]),
            ],
            default=ui_lang,
        )
        if lang_choice is None:
            console.print(text["cancelled"])
            return FlowOutcome(
                cancelled=True,
                detection=detection,
                selected_hosts=selected,
                scope=resolved_scope,
                lang=ui_lang,
            )
        ui_lang = messages.normalize_lang(lang_choice)
        text = messages.catalog(ui_lang)

    planned = plan_distribution(
        distribution,
        hosts=selected,
        scope=resolved_scope,
        home=home,
        project=project,
        registry=registry,
        environ=environ,
        search_path=search_path,
    )

    console.print(text["planned_header"])
    for item in planned.files:
        console.print(
            text["planned_file"].format(
                path=item.path,
                hosts=",".join(item.host_ids),
            )
        )
    conflicts = _existing_conflicts(planned)
    if conflicts:
        console.print(text["conflicts_header"])
        for path in conflicts:
            console.print(text["conflict_line"].format(path=path))

    if not yes:
        confirm_key = (
            "confirm_update"
            if operation is Operation.UPDATE
            else "confirm_install"
        )
        answer = prompts.confirm(text[confirm_key], default=False)
        if answer is not True:
            console.print(text["cancelled"])
            return FlowOutcome(
                cancelled=True,
                detection=detection,
                selected_hosts=selected,
                scope=resolved_scope,
                lang=ui_lang,
                planned=planned,
            )

    factory = installer_factory or (
        lambda **_: build_installer(distribution, planned)
    )
    installer = factory(
        distribution=distribution,
        planned=planned,
        selected_hosts=selected,
        scope=resolved_scope,
    )
    progress = (
        text["updating"] if operation is Operation.UPDATE else text["installing"]
    )
    with console.status(progress):
        if operation is Operation.UPDATE:
            raw = installer.update(base_path=planned.scope.root)
        else:
            raw = installer.install(base_path=planned.scope.root)

    result = replace(
        raw,
        selected_hosts=selected,
        resolved_destinations=tuple(sorted({item.path.parent for item in planned.files})),
    )
    console.print(text["done"])
    console.print(text["summary_header"])
    for host_id in selected:
        console.print(
            text["summary_host"].format(host=host_id)
            + f" {active_theme.checkmark}"
        )
    console.print(text["next_steps"])
    console.print(f"  {active_theme.arrow} {text['next_restart']}")

    return FlowOutcome(
        cancelled=False,
        result=result,
        detection=detection,
        selected_hosts=selected,
        scope=resolved_scope,
        lang=ui_lang,
        planned=planned,
    )


def run_uninstall_flow(
    distribution: SkillDistribution,
    *,
    prompts: PromptPort,
    console: ConsolePort,
    scope: Scope | str,
    hosts: Sequence[str],
    lang: str = "en",
    yes: bool = False,
    home: Path | None = None,
    project: Path | None = None,
    registry: HostRegistry | None = None,
    installer_factory: Callable[..., Installer] | None = None,
) -> FlowOutcome:
    """Plan then uninstall using the same presentation ports."""

    ui_lang = messages.normalize_lang(lang)
    text = messages.catalog(ui_lang)
    resolved_scope = scope if isinstance(scope, Scope) else Scope(scope)
    selected = tuple(dict.fromkeys(hosts))
    if not selected:
        raise NoHostDetectedError(text["no_hosts"])

    planned = plan_distribution(
        distribution,
        hosts=selected,
        scope=resolved_scope,
        home=home,
        project=project,
        registry=registry,
    )
    if not yes:
        answer = prompts.confirm(text["confirm_uninstall"], default=False)
        if answer is not True:
            console.print(text["cancelled"])
            return FlowOutcome(
                cancelled=True,
                selected_hosts=selected,
                scope=resolved_scope,
                lang=ui_lang,
                planned=planned,
            )

    factory = installer_factory or (
        lambda **_: build_installer(distribution, planned)
    )
    installer = factory(
        distribution=distribution,
        planned=planned,
        selected_hosts=selected,
        scope=resolved_scope,
    )
    with console.status(text["uninstalling"]):
        raw = installer.uninstall(base_path=planned.scope.root)
    result = replace(raw, selected_hosts=selected)
    console.print(text["done"])
    return FlowOutcome(
        cancelled=False,
        result=result,
        selected_hosts=selected,
        scope=resolved_scope,
        lang=ui_lang,
        planned=planned,
    )


class InstallerApp:
    """Object-oriented wrapper around :func:`run_install_flow`."""

    def __init__(
        self,
        *,
        prompts: PromptPort,
        console: ConsolePort,
        version: str | None = None,
        detect_hosts: Callable[..., DetectionResult] = _detect_hosts,
        installer_factory: Callable[..., Installer] | None = None,
    ) -> None:
        self.prompts = prompts
        self.console = console
        self.version = version
        self.detect_hosts = detect_hosts
        self.installer_factory = installer_factory

    def run_install(
        self, distribution: SkillDistribution, **kwargs: object
    ) -> FlowOutcome:
        return run_install_flow(
            distribution,
            prompts=self.prompts,
            console=self.console,
            version=self.version,
            detect_hosts=self.detect_hosts,
            installer_factory=self.installer_factory,
            **kwargs,  # type: ignore[arg-type]
        )


def create_questionary_prompts() -> PromptPort:
    """Lazy-import Questionary and return a real prompt adapter."""

    require_tui_dependencies()
    import questionary
    from questionary import Choice as QChoice

    class QuestionaryPrompts:
        def select(
            self,
            message: str,
            choices: Sequence[Choice],
            *,
            default: str | None = None,
        ) -> str | None:
            q_choices = [
                QChoice(title=item.label, value=item.value)
                for item in choices
            ]
            result = questionary.select(
                message, choices=q_choices, default=default
            ).ask()
            return result

        def checkbox(
            self, message: str, choices: Sequence[Choice]
        ) -> Sequence[str] | None:
            q_choices = [
                QChoice(
                    title=item.label,
                    value=item.value,
                    checked=item.checked,
                )
                for item in choices
            ]
            result = questionary.checkbox(message, choices=q_choices).ask()
            return result

        def confirm(self, message: str, *, default: bool = False) -> bool | None:
            return questionary.confirm(message, default=default).ask()

    return QuestionaryPrompts()


def create_rich_console(*, theme: Theme | None = None) -> ConsolePort:
    """Lazy-import Rich and return a console adapter."""

    require_tui_dependencies()
    from rich.console import Console

    active = theme or resolve_theme()
    rich_console = Console(
        no_color=not active.color,
        emoji=active.unicode,
        highlight=False,
    )

    class RichConsoleAdapter:
        def print(self, message: str = "") -> None:
            rich_console.print(message)

        def rule(self, message: str = "") -> None:
            rich_console.rule(message)

        @contextmanager
        def status(self, message: str) -> Iterator[None]:
            with rich_console.status(message):
                yield

    return RichConsoleAdapter()


__all__ = [
    "PRESELECT_MIN_CONFIDENCE",
    "Choice",
    "ConsolePort",
    "FixedPlansProvider",
    "FlowOutcome",
    "InstallerApp",
    "PromptPort",
    "RecordingConsole",
    "build_installer",
    "create_questionary_prompts",
    "create_rich_console",
    "require_interactive_tty",
    "require_tui_dependencies",
    "run_install_flow",
    "run_uninstall_flow",
    "should_preselect",
]
