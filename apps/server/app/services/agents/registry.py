from __future__ import annotations

from typing import Any

from app.services.agents.base import SubAgentBase


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, SubAgentBase] = {}

    def register(self, agent: SubAgentBase) -> None:
        if agent.name in self._agents:
            raise ValueError(f"子Agent[{agent.name}]已注册，不允许重复注册")
        self._agents[agent.name] = agent

    def get(self, name: str) -> SubAgentBase | None:
        return self._agents.get(name)

    def all_tools(self) -> list[dict[str, Any]]:
        return [a.to_tool_schema() for a in self._agents.values()]

    def list_agents(self) -> list[dict[str, Any]]:
        return [
            {
                "name": a.name,
                "description": a.description,
                "required_keys": a.required_keys,
            }
            for a in self._agents.values()
        ]

    def __contains__(self, name: str) -> bool:
        return name in self._agents

    def __len__(self) -> int:
        return len(self._agents)


agent_registry = AgentRegistry()