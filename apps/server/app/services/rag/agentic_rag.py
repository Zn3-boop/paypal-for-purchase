from __future__ import annotations

import logging
from typing import Any

from app.services.rag import get_rag_service

logger = logging.getLogger(__name__)

RAG_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rag_search",
        "description": (
            "搜索内部知识库，检索与问题相关的文档片段。"
            "当需要查找内部资料、文档、规范、历史记录等信息时使用此工具。"
            "Agent可以自主决定：何时检索、检索什么关键词、是否需要多次检索不同关键词。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索查询，应使用与问题相关的关键词",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回最相关的K个结果，默认5",
                    "default": 5,
                },
                "rerank": {
                    "type": "boolean",
                    "description": "是否启用交叉编码器精排（更准确但更慢），默认false",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    },
}

RAG_SEARCH_AND_ANSWER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rag_search_and_answer",
        "description": (
            "搜索内部知识库并基于检索结果生成回答。"
            "适用于需要从内部资料中查找信息并直接回答的场景。"
            "检索后自动构建上下文并生成回答，包含证据引用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户的完整问题",
                },
                "top_k": {
                    "type": "integer",
                    "description": "检索结果数量，默认5",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}


class AgenticRAGTool:
    """
    Agentic RAG：把RAG封装为Agent可调用的工具
    Agent自主决定何时检索、检索什么、检索几次
    与固定管线RAG的区别：Agent可以多轮检索、根据结果决定是否继续
    """

    def __init__(self):
        self._call_count = 0
        self._total_results = 0

    @property
    def name(self) -> str:
        return "rag_search"

    @property
    def schema(self) -> dict[str, Any]:
        return RAG_TOOL_SCHEMA

    async def execute(self, query: str, top_k: int = 5, rerank: bool = False, **kwargs: Any) -> dict[str, Any]:
        """
        执行RAG检索，返回结构化结果供Agent使用
        """
        try:
            rag = get_rag_service()
            user_acl = kwargs.get("user_acl", ["public"])

            result = await rag.query(
                query=query,
                user_acl_tags=user_acl,
                top_k=top_k,
                enable_rerank=rerank,
            )

            retrieval = result.get("retrieval", {})
            raw_results = retrieval.get("results", [])

            formatted_results = []
            for i, r in enumerate(raw_results):
                formatted_results.append({
                    "index": i + 1,
                    "content": r.get("content", "")[:500],
                    "doc_title": r.get("doc_title", ""),
                    "chunk_id": r.get("chunk_id", ""),
                    "score": round(r.get("rrf_score", r.get("score", 0)), 4),
                    "source_type": r.get("source_type", "text"),
                })

            self._call_count += 1
            self._total_results += len(formatted_results)

            return {
                "tool": "rag_search",
                "query": query,
                "result_count": len(formatted_results),
                "results": formatted_results,
                "evidence_map": retrieval.get("evidence_map", {}),
                "suggestion": self._make_suggestion(query, formatted_results),
            }

        except Exception as e:
            logger.error(f"Agentic RAG search failed: {e}")
            return {
                "tool": "rag_search",
                "query": query,
                "result_count": 0,
                "results": [],
                "error": str(e),
            }

    async def search_and_answer(self, query: str, top_k: int = 5, **kwargs: Any) -> dict[str, Any]:
        """
        检索+生成：搜索知识库并基于检索结果生成回答
        """
        try:
            rag = get_rag_service()
            user_acl = kwargs.get("user_acl", ["public"])

            result = await rag.query(
                query=query,
                user_acl_tags=user_acl,
                top_k=top_k,
                enable_rerank=True,
            )

            retrieval = result.get("retrieval", {})
            raw_results = retrieval.get("results", [])

            if not raw_results:
                return {
                    "tool": "rag_search_and_answer",
                    "query": query,
                    "answer": "未在知识库中找到相关信息。",
                    "evidence_count": 0,
                    "should_search_web": True,
                }

            evidence_parts = []
            for i, r in enumerate(raw_results[:5]):
                cid = r.get("chunk_id", "unknown")
                content = r.get("content", "")[:300]
                evidence_parts.append(f"[{i+1}] (来源:{cid}) {content}")

            context_text = "\n\n".join(evidence_parts)

            answer = await self._generate_answer(query, context_text, kwargs)

            return {
                "tool": "rag_search_and_answer",
                "query": query,
                "answer": answer,
                "evidence_count": len(raw_results),
                "evidence_ids": [r.get("chunk_id", "") for r in raw_results[:5]],
                "context_used": context_text[:1000],
            }

        except Exception as e:
            logger.error(f"Agentic RAG search_and_answer failed: {e}")
            return {
                "tool": "rag_search_and_answer",
                "query": query,
                "answer": f"检索生成失败: {e}",
                "error": str(e),
            }

    async def _generate_answer(self, query: str, context: str, kwargs: dict) -> str:
        try:
            from app.services.llm.provider import provider
            system_prompt = (
                "你是一个知识库问答助手。请基于以下检索到的证据回答用户问题。\n"
                "要求：\n"
                "1. 只基于证据内容回答，不编造信息\n"
                "2. 引用证据编号[1][2]等\n"
                "3. 如果证据不足以回答，明确说明\n"
            )
            user_prompt = f"证据：\n{context}\n\n问题：{query}"
            answer = await provider.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
            )
            return answer
        except Exception as e:
            logger.warning(f"LLM generation failed: {e}, returning context summary")
            return f"基于检索证据的摘要：{context[:500]}"

    @staticmethod
    def _make_suggestion(query: str, results: list[dict]) -> str:
        if not results:
            return "未找到相关结果，建议：1)改写查询关键词 2)使用web搜索补充 3)降低检索精度要求"
        if len(results) < 3:
            return "结果较少，建议：1)用更通用的关键词重新检索 2)结合web搜索扩展信息"
        return ""

    def get_stats(self) -> dict[str, Any]:
        return {
            "call_count": self._call_count,
            "total_results": self._total_results,
            "avg_results_per_call": self._total_results / self._call_count if self._call_count > 0 else 0,
        }


_agentic_rag_tool: AgenticRAGTool | None = None


def get_agentic_rag_tool() -> AgenticRAGTool:
    global _agentic_rag_tool
    if _agentic_rag_tool is None:
        _agentic_rag_tool = AgenticRAGTool()
    return _agentic_rag_tool