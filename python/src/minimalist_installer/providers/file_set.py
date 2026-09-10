"""Pure planner for a generic desired file set."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from ..core.locks import canonical_resource_identity
from ..core.models import EffectPlan, JsonValue, PlanContext, _json_value


class FileSetProvider:
    """Map one config array to a versioned ``reconcile_file_set`` plan."""

    def __init__(
        self,
        *,
        effect_id: str | None = None,
        config_key: str = "files",
        destination: str = ".",
    ) -> None:
        if effect_id is not None and (not isinstance(effect_id, str) or not effect_id):
            raise ValueError("effect_id must be non-empty text when supplied")
        if not isinstance(config_key, str) or not config_key:
            raise ValueError("config_key must be non-empty text")
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination must be non-empty text")
        self.effect_id = effect_id
        self.config_key = config_key
        self.destination = destination

    def _stable_id(self) -> str:
        if self.effect_id is not None:
            return self.effect_id
        identity = f"{self.config_key}\0{self.destination}".encode("utf-8")
        return f"reconcile_file_set:{hashlib.sha256(identity).hexdigest()[:16]}"

    def plan(
        self,
        config: Mapping[str, object],
        context: PlanContext,
    ) -> tuple[EffectPlan, ...]:
        """Validate and snapshot config without touching the filesystem."""

        files = config.get(self.config_key, [])
        if not isinstance(files, list | tuple):
            raise TypeError(f"config.{self.config_key} must be an array")
        desired: list[JsonValue] = []
        for index, entry in enumerate(files):
            if not isinstance(entry, Mapping):
                raise TypeError(f"config.{self.config_key}[{index}] must be an object")
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError(
                    f"config.{self.config_key}[{index}].path must be non-empty text"
                )
            if "content" not in entry:
                raise ValueError(
                    f"config.{self.config_key}[{index}].content is required"
                )
            desired.append(_json_value(entry))

        args: dict[str, JsonValue] = {"desired": desired}
        destination_path = context.base_path
        if self.destination != ".":
            args["destination"] = self.destination
            destination_path = context.base_path / self.destination
        resource = canonical_resource_identity("path", destination_path)
        return (
            EffectPlan(
                id=self._stable_id(),
                type="reconcile_file_set",
                version=1,
                args=args,
                resources=(resource,),
            ),
        )


__all__ = ["FileSetProvider"]
