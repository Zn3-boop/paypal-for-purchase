"""Graph hook 集成层（P1-5）。

graph.py 在 HOOKS_ENABLED=true 时尝试从本模块导入 6 个 *_with_hooks 节点并
替换原始节点。此前模块从未存在，每次建图都会打 ImportError warning 并降级
回原始节点。

本实现采用**透明转发**策略：每个 hook 节点直接调用 app.services.graph.nodes
里的原始实现，保持行为 100% 不变，同时让 Hook 层成为将来切插件的落点：

  detect_intent_with_hooks   -> nodes.detect_intent_node
  extract_entities_with_hooks -> nodes.extract_entities_node
  clarification_with_hooks   -> nodes.clarification_node
  rag_retrieve_with_hooks    -> nodes.rag_retrieve_node
  tool_execute_with_hooks    -> nodes.tool_execute_node
  turn_end_with_hooks        -> 空操作收尾（memory_persist 已完成持久化，
                                重复转发会写重复 evidence）

注意：各原始节点内部已自带 NodeMetrics / traced_execution 观测，转发不加
重复埋点。
"""
from __future__ import annotations

from typing import Any

from app.services.graph.state import AgentState

__all__ = [
    "detect_intent_with_hooks",
    "extract_entities_with_hooks",
    "clarification_with_hooks",
    "rag_retrieve_with_hooks",
    "tool_execute_with_hooks",
    "turn_end_with_hooks",
]


async def detect_intent_with_hooks(state: AgentState) -> dict[str, Any]:
    """Hook 版意图识别：转发 nodes.detect_intent_node（原实现）。"""
    from app.services.graph.nodes import detect_intent_node

    return await detect_intent_node(state)


async def extract_entities_with_hooks(state: AgentState) -> dict[str, Any]:
    """Hook 版实体抽取：转发 nodes.extract_entities_node（原实现）。"""
    from app.services.graph.nodes import extract_entities_node

    return await extract_entities_node(state)


async def clarification_with_hooks(state: AgentState) -> dict[str, Any]:
    """Hook 版澄清节点：转发 nodes.clarification_node（原实现）。"""
    from app.services.graph.nodes import clarification_node

    return await clarification_node(state)


async def rag_retrieve_with_hooks(state: AgentState) -> dict[str, Any]:
    """Hook 版 RAG 检索：转发 nodes.rag_retrieve_node（原实现）。"""
    from app.services.graph.nodes import rag_retrieve_node

    return await rag_retrieve_node(state)


async def tool_execute_with_hooks(state: AgentState) -> dict[str, Any]:
    """Hook 版工具执行：转发 nodes.tool_execute_node（原实现）。"""
    from app.services.graph.nodes import tool_execute_node

    return await tool_execute_node(state)


async def turn_end_with_hooks(state: AgentState) -> dict[str, Any]:
    """Turn 结束收尾钩子。

    memory_persist_node 已完成长期记忆写入，本节点不重复持久化（避免重复
    evidence 记录）；保留空操作作为插件扩展落点（例如统计/turn 级观测）。
    """
    return {}
