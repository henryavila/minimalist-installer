"""Portuguese and English message catalogs for the installer TUI/CLI."""

from __future__ import annotations

from typing import Mapping

_EN: dict[str, str] = {
    "intro": "minimalist-installer v{version}",
    "select_scope": "Where should the skill be installed?",
    "scope_user": "User — home directory",
    "scope_project": "Project — current repository",
    "select_hosts": "Which hosts should receive the skill?",
    "select_lang": "Which language should installer messages use?",
    "lang_en": "English",
    "lang_pt": "Portuguese",
    "confirm_install": "Proceed with installation?",
    "confirm_update": "Proceed with update?",
    "confirm_uninstall": "Proceed with uninstall?",
    "cancelled": "Operation cancelled.",
    "no_hosts": "No hosts detected or selected.",
    "detected_header": "Detected hosts",
    "evidence_line": "  {name} (confidence {confidence}): {evidence}",
    "planned_header": "Planned changes",
    "planned_file": "  {path} [{hosts}]",
    "conflicts_header": "Existing paths (conflicts preserved unless adopted)",
    "conflict_line": "  conflict: {path}",
    "installing": "Installing…",
    "updating": "Updating…",
    "uninstalling": "Uninstalling…",
    "repairing": "Repairing…",
    "done": "Done.",
    "summary_header": "Summary",
    "summary_host": "  {host}: installed",
    "next_steps": "Next steps",
    "next_restart": "Restart the host or start a new conversation.",
    "missing_tui": "Interactive mode requires the optional tui extra: pip install 'minimalist-installer[tui]'",
    "no_tty": "Interactive prompts require a TTY. Re-run with --yes and --hosts, or use a terminal.",
    "progress": "Applying changes…",
}

_PT: dict[str, str] = {
    "intro": "minimalist-installer v{version} — instalador de skills",
    "select_scope": "Onde o skill deve ser instalado?",
    "scope_user": "Usuário — diretório home",
    "scope_project": "Projeto — repositório atual",
    "select_hosts": "Quais hosts devem receber o skill?",
    "select_lang": "Em qual idioma as mensagens do instalador devem aparecer?",
    "lang_en": "Inglês",
    "lang_pt": "Português",
    "confirm_install": "Prosseguir com a instalação?",
    "confirm_update": "Prosseguir com a atualização?",
    "confirm_uninstall": "Prosseguir com a desinstalação?",
    "cancelled": "Operação cancelada.",
    "no_hosts": "Nenhum host detectado ou selecionado.",
    "detected_header": "Hosts detectados",
    "evidence_line": "  {name} (confiança {confidence}): {evidence}",
    "planned_header": "Alterações planejadas",
    "planned_file": "  {path} [{hosts}]",
    "conflicts_header": "Caminhos existentes (conflitos preservados salvo adoção)",
    "conflict_line": "  conflito: {path}",
    "installing": "Instalando…",
    "updating": "Atualizando…",
    "uninstalling": "Desinstalando…",
    "repairing": "Reparando…",
    "done": "Concluído.",
    "summary_header": "Resumo",
    "summary_host": "  {host}: instalado",
    "next_steps": "Próximos passos",
    "next_restart": "Reinicie o host ou inicie uma nova conversa.",
    "missing_tui": "O modo interativo exige o extra opcional tui: pip install 'minimalist-installer[tui]'",
    "no_tty": "Prompts interativos exigem um TTY. Use --yes e --hosts, ou um terminal.",
    "progress": "Aplicando alterações…",
}

_CATALOGS: dict[str, dict[str, str]] = {"en": _EN, "pt": _PT}


def catalog(lang: str) -> Mapping[str, str]:
    """Return the message catalog for ``lang``, defaulting to English."""

    return _CATALOGS.get(lang, _EN)


def normalize_lang(value: str | None) -> str:
    if value is None:
        return "en"
    lowered = value.strip().lower()
    if lowered in _CATALOGS:
        return lowered
    return "en"


__all__ = ["catalog", "normalize_lang"]
