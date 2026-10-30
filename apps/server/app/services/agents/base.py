from __future__ import annotations

from typing import Any


class SubAgentBase:
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    required_keys: list[str] = []

    def validate_args(self, args: dict[str, Any]) -> None:
        for k in self.required_keys:
            if k not in args:
                raise ValueError(f"SubAgent[{self.name}] missing required param: {k}")

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError