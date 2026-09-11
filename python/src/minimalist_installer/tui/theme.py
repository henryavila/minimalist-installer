"""Presentation theme respecting NO_COLOR and non-Unicode terminals."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Theme:
    """Resolved glyphs and color flag for console rendering."""

    color: bool
    unicode: bool
    arrow: str
    checkmark: str
    bullet: str


def _env_no_color(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("NO_COLOR", "").strip())


def resolve_theme(
    *,
    unicode_ok: bool = True,
    color_ok: bool = True,
    force_color: bool = False,
    environ: dict[str, str] | None = None,
) -> Theme:
    """Pick glyphs and color from terminal capability and ``NO_COLOR``."""

    if force_color:
        color = bool(color_ok)
    else:
        color = bool(color_ok) and not _env_no_color(environ)

    if unicode_ok:
        return Theme(
            color=color,
            unicode=True,
            arrow="→",
            checkmark="✓",
            bullet="•",
        )
    return Theme(
        color=color,
        unicode=False,
        arrow="->",
        checkmark="[ok]",
        bullet="*",
    )


__all__ = ["Theme", "resolve_theme"]
