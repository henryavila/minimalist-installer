"""Validated registry for versioned reversible effects."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from .errors import (
    InvalidEffectError,
    UnknownEffectError,
    UnsupportedEffectVersionError,
)
from .models import Effect, JsonValue


class EffectRegistry:
    """Resolve exact effect versions and reject malformed extensions early."""

    def __init__(self, effects: Iterable[object] = ()) -> None:
        self._effects: dict[tuple[str, int], Effect] = {}
        for effect in effects:
            self.register(effect)

    @staticmethod
    def _contract(effect: object) -> tuple[str, int]:
        effect_type = getattr(effect, "type", None)
        version = getattr(effect, "version", None)
        if not isinstance(effect_type, str) or not effect_type.strip():
            raise InvalidEffectError("effect type must be a non-empty string")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise InvalidEffectError(
                f'effect type "{effect_type}" must declare a positive integer version'
            )
        for method in ("prepare", "apply", "revert"):
            if not callable(getattr(effect, method, None)):
                raise InvalidEffectError(
                    f'effect type "{effect_type}" must define a callable {method}'
                )
        return effect_type, version

    def register(self, effect: object) -> None:
        """Validate and add one exact type/version implementation."""

        key = self._contract(effect)
        if key in self._effects:
            raise InvalidEffectError(
                f'effect type "{key[0]}" version {key[1]} is already registered'
            )
        self._effects[key] = cast(Effect, effect)

    def get(self, effect_type: str, version: int) -> Effect | None:
        return self._effects.get((effect_type, version))

    def require(self, effect_type: str, version: int) -> Effect:
        """Resolve exactly, distinguishing a foreign type from a foreign version."""

        effect = self.get(effect_type, version)
        if effect is not None:
            return effect
        versions = sorted(
            registered_version
            for registered_type, registered_version in self._effects
            if registered_type == effect_type
        )
        if versions:
            supported: list[JsonValue] = list(versions)
            raise UnsupportedEffectVersionError(
                f'unsupported version {version} for effect type "{effect_type}"',
                details={
                    "effect_type": effect_type,
                    "version": version,
                    "supported": supported,
                },
            )
        raise UnknownEffectError(
            f'unknown effect type "{effect_type}"',
            details={"effect_type": effect_type, "version": version},
        )

    def has(self, effect_type: str, version: int) -> bool:
        return (effect_type, version) in self._effects

    def list(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            sorted(self._effects, key=lambda key: (key[0].encode("utf-8"), key[1]))
        )


__all__ = ["EffectRegistry"]
