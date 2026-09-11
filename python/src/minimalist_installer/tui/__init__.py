"""Optional interactive presentation layer for skill installs."""

from .app import (
    PRESELECT_MIN_CONFIDENCE,
    Choice,
    ConsolePort,
    FixedPlansProvider,
    FlowOutcome,
    InstallerApp,
    PromptPort,
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
from .messages import catalog, normalize_lang
from .theme import Theme, resolve_theme

__all__ = [
    "PRESELECT_MIN_CONFIDENCE",
    "Choice",
    "ConsolePort",
    "FixedPlansProvider",
    "FlowOutcome",
    "InstallerApp",
    "PromptPort",
    "RecordingConsole",
    "Theme",
    "build_installer",
    "catalog",
    "create_questionary_prompts",
    "create_rich_console",
    "normalize_lang",
    "require_interactive_tty",
    "require_tui_dependencies",
    "resolve_theme",
    "run_install_flow",
    "run_uninstall_flow",
    "should_preselect",
]
