"""Generic console entry point for skill distribution commands."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .core.errors import (
    IncompleteTransactionError,
    InstallerError,
    NoHostDetectedError,
    NonInteractiveInputRequiredError,
    RecoveryBlockedError,
)
from .core.models import Operation, OperationResult, StatusResult
from .skills import (
    HostRegistry,
    Scope,
    SkillDistribution,
    detect_hosts,
    load_distribution,
    plan_distribution,
    resolve_scope,
)
from .tui.app import (
    RecordingConsole,
    build_installer,
    create_questionary_prompts,
    create_rich_console,
    require_interactive_tty,
    require_tui_dependencies,
    run_install_flow,
    run_uninstall_flow,
    should_preselect,
)
from .tui.messages import catalog, normalize_lang
from .tui.theme import detect_unicode_ok, resolve_theme

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_RECOVERY = 3
JSON_SCHEMA_VERSION = 1


def _parse_hosts(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    parts = tuple(item.strip() for item in value.split(",") if item.strip())
    return parts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minimalist-installer",
        description="Reversible userland installer for agent skill distributions",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(cmd: argparse.ArgumentParser, *, distribution: bool = True) -> None:
        if distribution:
            cmd.add_argument("distribution", type=Path, help="Path to distribution.toml")
        cmd.add_argument(
            "--scope",
            choices=("user", "project"),
            default=None,
            help="Install scope (default: prompt or user for --yes)",
        )
        cmd.add_argument(
            "--hosts",
            default=None,
            help="Comma-separated host ids (required with --yes when undetected)",
        )
        cmd.add_argument("--lang", choices=("en", "pt"), default=None)
        cmd.add_argument(
            "--yes",
            action="store_true",
            help="Accept a fully determined plan without prompting (not --force)",
        )
        cmd.add_argument("--json", action="store_true", help="Emit versioned JSON on stdout")
        cmd.add_argument("--project", type=Path, default=None, help="Project path for project scope")
        cmd.add_argument("--home", type=Path, default=None, help="Override home directory")
        cmd.add_argument(
            "--search-path",
            default=None,
            help="PATH used for executable detection (empty string disables)",
        )

    install = sub.add_parser("install", help="Install a skill distribution")
    add_common(install)

    update = sub.add_parser("update", help="Update an installed skill distribution")
    add_common(update)

    detect = sub.add_parser("detect", help="Detect available hosts")
    detect.add_argument("--json", action="store_true")
    detect.add_argument("--scope", choices=("user", "project"), default="user")
    detect.add_argument("--project", type=Path, default=None)
    detect.add_argument("--home", type=Path, default=None)
    detect.add_argument("--search-path", default=None)
    detect.add_argument("--lang", choices=("en", "pt"), default="en")

    status = sub.add_parser("status", help="Show installation status")
    add_common(status)

    repair = sub.add_parser("repair", help="Repair an incomplete transaction")
    add_common(repair)
    repair.add_argument(
        "--resume",
        action="store_true",
        help="Resume a resumable incomplete transaction",
    )

    uninstall = sub.add_parser("uninstall", help="Uninstall a skill distribution")
    add_common(uninstall)

    return parser


def _human(stream: TextIO, message: str) -> None:
    print(message, file=stream)


def _emit_json(payload: Mapping[str, object], *, stdout: TextIO) -> None:
    json.dump(payload, stdout, indent=2, sort_keys=True)
    stdout.write("\n")


def _exit_for_error(error: BaseException) -> int:
    if isinstance(error, NonInteractiveInputRequiredError):
        return EXIT_USAGE
    if isinstance(error, (RecoveryBlockedError, IncompleteTransactionError)):
        return EXIT_RECOVERY
    if isinstance(error, InstallerError):
        return EXIT_ERROR
    if isinstance(error, (argparse.ArgumentError, SystemExit)):
        return EXIT_USAGE
    return EXIT_ERROR


def _is_tty() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(
        getattr(sys.stdout, "isatty", lambda: False)()
    )


def _resolve_search_path(value: str | None) -> str | None:
    if value is None:
        return None
    return value


def _default_scope(args: argparse.Namespace) -> Scope:
    if args.scope:
        return Scope(args.scope)
    return Scope.USER


def _status_payload(status: StatusResult) -> dict[str, object]:
    data = status.to_dict()
    data["schema_version"] = JSON_SCHEMA_VERSION
    return data


def _result_payload(result: OperationResult) -> dict[str, object]:
    data = result.to_dict()
    data["schema_version"] = JSON_SCHEMA_VERSION
    data["cancelled"] = False
    return data


def _detection_payload(result: object) -> dict[str, object]:
    data = result.to_dict()  # type: ignore[attr-defined]
    data["schema_version"] = JSON_SCHEMA_VERSION
    return data


def _cancelled_payload(*, operation: str) -> dict[str, object]:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "cancelled": True,
        "operation": operation,
    }


def _error_payload(error: InstallerError) -> dict[str, object]:
    data = error.to_dict()
    data["schema_version"] = JSON_SCHEMA_VERSION
    return data


def _human_stream(*, json_mode: bool, stdout: TextIO, stderr: TextIO) -> TextIO:
    return stderr if json_mode else stdout


def _console_for(
    *,
    json_mode: bool,
    stdout: TextIO,
    stderr: TextIO,
    interactive: bool,
):
    human = _human_stream(json_mode=json_mode, stdout=stdout, stderr=stderr)
    theme = resolve_theme(unicode_ok=detect_unicode_ok(stream=human))
    if interactive:
        return create_rich_console(theme=theme, file=human)
    return RecordingConsole(stream=human)


def _load_distribution(path: Path) -> SkillDistribution:
    return load_distribution(path)


def _installer_for_distribution(
    distribution: SkillDistribution,
    *,
    scope: Scope,
    hosts: Sequence[str],
    home: Path | None,
    project: Path | None,
    registry: HostRegistry | None,
    environ: Mapping[str, str] | None = None,
    search_path: str | None = None,
):
    planned = plan_distribution(
        distribution,
        hosts=hosts,
        scope=scope,
        home=home,
        project=project,
        registry=registry,
        environ=environ,
        search_path=search_path,
    )
    return build_installer(distribution, planned), planned


def _cmd_detect(
    args: argparse.Namespace,
    *,
    registry: HostRegistry,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    text = catalog(normalize_lang(args.lang))
    result = detect_hosts(
        scope=Scope(args.scope),
        registry=registry,
        home=args.home,
        project=args.project,
        search_path=_resolve_search_path(args.search_path),
    )
    if args.json:
        _emit_json(_detection_payload(result), stdout=stdout)
        if not result.detections:
            _human(stderr, text["no_hosts"])
            return EXIT_ERROR
        return EXIT_OK
    if not result.detections:
        _human(stderr, text["no_hosts"])
        return EXIT_ERROR
    for item in result.detections:
        evidence = ", ".join(f"{e.kind.value}={e.value}" for e in item.evidence)
        _human(
            stdout,
            f"{item.host.id}\t{item.confidence}\t{evidence}",
        )
    return EXIT_OK


def _cmd_status(
    args: argparse.Namespace,
    *,
    registry: HostRegistry,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    distribution = _load_distribution(args.distribution)
    scope = _default_scope(args)
    resolved = resolve_scope(scope, start=args.project, home=args.home)
    # Status does not need host selection; bind an empty provider set via a
    # throwaway plan only when hosts are known. Otherwise probe the root.
    hosts = _parse_hosts(args.hosts)
    if hosts:
        installer, _planned = _installer_for_distribution(
            distribution,
            scope=scope,
            hosts=hosts,
            home=args.home,
            project=args.project,
            registry=registry,
            search_path=_resolve_search_path(args.search_path),
        )
    else:
        from .tui.app import FixedPlansProvider
        from .core.driver import define_installer

        installer = define_installer(
            config={
                "consumer": distribution.name,
                "consumer_version": distribution.version,
                "manifest_dir": ".minimalist-installer",
            },
            providers=(FixedPlansProvider(()),),
        )
    status = installer.status(base_path=resolved.root)
    if args.json:
        if not args.yes:
            _human(stderr, f"status for {distribution.name} @ {resolved.root}")
        _emit_json(_status_payload(status), stdout=stdout)
        return EXIT_OK
    _human(stdout, f"installed={status.installed} status={status.status.value}")
    if status.installation_id:
        _human(stdout, f"installation_id={status.installation_id}")
    if status.incomplete_transaction_id:
        _human(stdout, f"incomplete={status.incomplete_transaction_id}")
    return EXIT_OK


def _cmd_repair(
    args: argparse.Namespace,
    *,
    registry: HostRegistry,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    distribution = _load_distribution(args.distribution)
    scope = _default_scope(args)
    resolved = resolve_scope(scope, start=args.project, home=args.home)
    from .core.driver import define_installer
    from .tui.app import FixedPlansProvider

    installer = define_installer(
        config={
            "consumer": distribution.name,
            "consumer_version": distribution.version,
            "manifest_dir": ".minimalist-installer",
        },
        providers=(FixedPlansProvider(()),),
    )
    result = installer.repair(base_path=resolved.root, resume=bool(args.resume))
    text = catalog(normalize_lang(args.lang))
    if args.json:
        _human(stderr, text["repairing"])
        _emit_json(_result_payload(result), stdout=stdout)
    else:
        _human(stdout, f"repair status={result.status.value}")
    return EXIT_OK


def _determine_hosts(
    args: argparse.Namespace,
    *,
    registry: HostRegistry,
    interactive: bool,
) -> tuple[str, ...]:
    explicit = _parse_hosts(args.hosts)
    if explicit is not None:
        if not explicit:
            raise NoHostDetectedError("no hosts selected")
        return explicit

    scope = _default_scope(args)
    detection = detect_hosts(
        scope=scope,
        registry=registry,
        home=args.home,
        project=args.project,
        search_path=_resolve_search_path(args.search_path),
    )
    if args.yes:
        selected = tuple(
            item.host.id for item in detection.detections if should_preselect(item)
        )
        if not selected:
            raise NoHostDetectedError("no host detected")
        return selected

    if not interactive:
        raise NonInteractiveInputRequiredError(
            "non-interactive install requires --yes and a determined host set"
        )
    return ()  # interactive flow selects later


def _cmd_mutate(
    args: argparse.Namespace,
    *,
    operation: Operation,
    registry: HostRegistry,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    distribution = _load_distribution(args.distribution)
    lang = normalize_lang(args.lang)
    text = catalog(lang)
    interactive = _is_tty() and not args.yes
    human = _human_stream(json_mode=args.json, stdout=stdout, stderr=stderr)

    if interactive:
        require_tui_dependencies()
        require_interactive_tty(is_tty=True)
        prompts = create_questionary_prompts()
        console = _console_for(
            json_mode=args.json,
            stdout=stdout,
            stderr=stderr,
            interactive=True,
        )
        theme = resolve_theme(unicode_ok=detect_unicode_ok(stream=human))
        outcome = run_install_flow(
            distribution,
            prompts=prompts,
            console=console,
            operation=operation,
            scope=Scope(args.scope) if args.scope else None,
            hosts=_parse_hosts(args.hosts),
            lang=args.lang,
            yes=False,
            home=args.home,
            project=args.project,
            registry=registry,
            search_path=_resolve_search_path(args.search_path),
            theme=theme,
        )
        if outcome.cancelled:
            _human(human, text["cancelled"])
            if args.json:
                _emit_json(
                    _cancelled_payload(operation=operation.value),
                    stdout=stdout,
                )
            return EXIT_OK
        if args.json and outcome.result is not None:
            _emit_json(_result_payload(outcome.result), stdout=stdout)
        return EXIT_OK

    # Non-interactive path
    if not args.yes and operation in {Operation.INSTALL, Operation.UPDATE}:
        raise NonInteractiveInputRequiredError(
            "non-interactive install requires --yes and a determined host set"
        )

    hosts = _determine_hosts(args, registry=registry, interactive=False)
    scope = _default_scope(args)
    console = _console_for(
        json_mode=args.json,
        stdout=stdout,
        stderr=stderr,
        interactive=False,
    )
    theme = resolve_theme(unicode_ok=detect_unicode_ok(stream=human))
    # Use run_install_flow with yes=True and scripted-free ports.
    from dataclasses import dataclass, field

    @dataclass
    class _RejectPrompts:
        log: list[str] = field(default_factory=list)

        def select(self, message: str, choices, *, default=None):
            raise NonInteractiveInputRequiredError("unexpected interactive select")

        def checkbox(self, message: str, choices):
            raise NonInteractiveInputRequiredError("unexpected interactive checkbox")

        def confirm(self, message: str, *, default: bool = False):
            raise NonInteractiveInputRequiredError("unexpected interactive confirm")

    outcome = run_install_flow(
        distribution,
        prompts=_RejectPrompts(),
        console=console,
        operation=operation,
        scope=scope,
        hosts=hosts,
        lang=lang,
        yes=True,
        home=args.home,
        project=args.project,
        registry=registry,
        search_path=_resolve_search_path(args.search_path),
        theme=theme,
    )
    if outcome.result is None:
        return EXIT_ERROR
    if args.json:
        _emit_json(_result_payload(outcome.result), stdout=stdout)
    return EXIT_OK


def _cmd_uninstall(
    args: argparse.Namespace,
    *,
    registry: HostRegistry,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    distribution = _load_distribution(args.distribution)
    lang = normalize_lang(args.lang)
    text = catalog(lang)
    interactive = _is_tty() and not args.yes
    explicit_hosts = _parse_hosts(args.hosts)
    scope = _default_scope(args)
    human = _human_stream(json_mode=args.json, stdout=stdout, stderr=stderr)

    if interactive and not args.yes:
        require_tui_dependencies()
        require_interactive_tty(is_tty=True)
        prompts = create_questionary_prompts()
        console = _console_for(
            json_mode=args.json,
            stdout=stdout,
            stderr=stderr,
            interactive=True,
        )
        outcome = run_uninstall_flow(
            distribution,
            prompts=prompts,
            console=console,
            scope=scope,
            hosts=explicit_hosts,
            lang=lang,
            yes=False,
            home=args.home,
            project=args.project,
            registry=registry,
            search_path=_resolve_search_path(args.search_path),
        )
        if outcome.cancelled:
            _human(human, text["cancelled"])
            if args.json:
                _emit_json(
                    _cancelled_payload(operation=Operation.UNINSTALL.value),
                    stdout=stdout,
                )
            return EXIT_OK
        if args.json and outcome.result is not None:
            _emit_json(_result_payload(outcome.result), stdout=stdout)
        return EXIT_OK

    if not args.yes:
        raise NonInteractiveInputRequiredError(
            "non-interactive uninstall requires --yes"
        )
    hosts = _determine_hosts(args, registry=registry, interactive=False)
    if not hosts:
        raise NoHostDetectedError(text["no_hosts"])

    installer, planned = _installer_for_distribution(
        distribution,
        scope=scope,
        hosts=hosts,
        home=args.home,
        project=args.project,
        registry=registry,
        search_path=_resolve_search_path(args.search_path),
    )
    result = installer.uninstall(base_path=planned.scope.root)
    if args.json:
        _human(stderr, text["uninstalling"])
        _emit_json(_result_payload(result), stdout=stdout)
    else:
        _human(stdout, text["done"])
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    registry: HostRegistry | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """CLI entry. Returns an exit code; never calls ``sys.exit`` itself."""

    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as error:
        code = error.code
        return int(code) if isinstance(code, int) else EXIT_USAGE

    active_registry = registry if registry is not None else HostRegistry.bundled()
    json_mode = bool(getattr(args, "json", False))
    try:
        if args.command == "detect":
            return _cmd_detect(args, registry=active_registry, stdout=out, stderr=err)
        if args.command == "status":
            return _cmd_status(args, registry=active_registry, stdout=out, stderr=err)
        if args.command == "repair":
            return _cmd_repair(args, registry=active_registry, stdout=out, stderr=err)
        if args.command == "install":
            return _cmd_mutate(
                args,
                operation=Operation.INSTALL,
                registry=active_registry,
                stdout=out,
                stderr=err,
            )
        if args.command == "update":
            return _cmd_mutate(
                args,
                operation=Operation.UPDATE,
                registry=active_registry,
                stdout=out,
                stderr=err,
            )
        if args.command == "uninstall":
            return _cmd_uninstall(
                args, registry=active_registry, stdout=out, stderr=err
            )
        parser.error(f"unknown command {args.command!r}")
        return EXIT_USAGE
    except BrokenPipeError:
        return EXIT_ERROR
    except InstallerError as error:
        message = str(error) or type(error).__name__
        _human(err, message)
        if json_mode:
            _emit_json(_error_payload(error), stdout=out)
        return _exit_for_error(error)
    except Exception as error:
        message = str(error) or type(error).__name__
        _human(err, message)
        return _exit_for_error(error)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
