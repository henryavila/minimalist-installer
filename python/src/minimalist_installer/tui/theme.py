"""Presentation theme respecting NO_COLOR and non-Unicode terminals."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Mapping, TextIO


@dataclass(frozen=True, slots=True)
class Theme:
    """Resolved glyphs and color flag for console rendering."""

    color: bool
    unicode: bool
    arrow: str
    checkmark: str
    bullet: str


_UNICODE_SAMPLE = "…→✓─•"


def _env_no_color(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get("NO_COLOR", "").strip())


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def detect_unicode_ok(
    *,
    stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether Unicode presentation glyphs are safe to emit."""

    env = os.environ if environ is None else environ
    if _env_truthy(env.get("NO_UNICODE")):
        return False
    target = sys.stdout if stream is None else stream
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        _UNICODE_SAMPLE.encode(encoding)
    except UnicodeEncodeError:
        return False
    return True


def resolve_theme(
    *,
    unicode_ok: bool = True,
    color_ok: bool = True,
    force_color: bool = False,
    environ: Mapping[str, str] | None = None,
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


__all__ = ["Theme", "detect_unicode_ok", "resolve_theme"]
