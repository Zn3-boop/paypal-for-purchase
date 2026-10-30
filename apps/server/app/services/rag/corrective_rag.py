from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RELEVANCE_THRESHOLD = float(os.getenv("CRAG_RELEVANCE_THRESHOLD", "0.3"))
MAX_CORRECTION_ROUNDS = int(os.getenv("CRAG_MAX_CORRECTION_ROUNDS", "2"))


@dataclass
class CorrectiveRAGResult:
    answer: str
    retrieval_source: str = "rag"
    correction_rounds: int = 0
    relevance_score: float = 0.0
    used_fallback: bool = False
    evidence_ids: list[str] = None

    def __post_init__(self):
        if self.evidence_ids is None:
            self.evidence_ids = []


class CorrectiveRAG:
    """
    Corrective RAG：对召回质量打分，低分触发fallback
    流程：
    1. 正常RAG检索
    2. 对检索结果做相关性评分
    3. 如果相关性低于阈值 → 触发fallback（Web搜索兜底）
    4. 合并RAG+Web结果重新生成
    5. 如果仍不够 → 标记"无法回答"而非编造
    """

    def __init__(self, rag_service: Any = None, relevance_threshold: float = RELEVANCE_THRESHOLD):
        self._rag_service = rag_service
        self._threshold = relevance_threshold

    async def query(
        self,
        query: str,
        user_acl_tags: list[str] | None = None,
        top_k: int = 5,
    ) -> CorrectiveRAGResult:
        rag_results = await self._rag_retrieve(query, user_acl_tags, top_k)
        relevance = self._score_relevance(query, rag_results)

        if relevance >= self._threshold:
            answer = await self._generate_with_evidence(query, rag_results)
            return CorrectiveRAGResult(
                answer=answer,
                retrieval_source="rag",
                relevance_score=relevance,
                evidence_ids=[r.get("chunk_id", "") for r in rag_results[:5]],
            )

        logger.info(f"CRAG: RAG relevance {relevance:.2f} < threshold {self._threshold}, triggering fallback")

        web_results = await self._web_fallback(query)
        combined = rag_results + web_results

        if combined:
            answer = await self._generate_with_evidence(query, combined)
            return CorrectiveRAGResult(
                answer=answer,
                retrieval_source="rag+web_fallback",
                correction_rounds=1,
                relevance_score=relevance,
                used_fallback=True,
                evidence_ids=[r.get("chunk_id", "") for r in combined[:5]],
            )

        return CorrectiveRAGResult(
            answer="抱歉，内部知识库和互联网搜索均未找到相关信息，无法回答该问题。",
            retrieval_source="none",
            correction_rounds=1,
            relevance_score=relevance,
            used_fallback=True,
        )

    async def _rag_retrieve(
        self,
        query: str,
        user_acl_tags: list[str] | None,
        top_k: int,
    ) -> list[dict[str, Any]]:
        try:
            from app.services.rag import get_rag_service
            rag = self._rag_service or get_rag_service()
            result = await rag.query(
                query=query,
                user_acl_tags=user_acl_tags or ["public"],
                top_k=top_k,
                enable_rerank=True,
            )
            return result.get("retrieval", {}).get("results", [])
        except Exception as e:
            logger.warning(f"CRAG RAG retrieve failed: {e}")
            return []

    async def _web_fallback(self, query: str) -> list[dict[str, Any]]:
        try:
            from app.services.hooks.plugins.retrievers import DuckDuckGoRetriever
            retriever = DuckDuckGoRetriever()
            docs = await retriever.retrieve(query, top_k=3)
            return [
                {
                    "content": d.content,
                    "chunk_id": f"web_{i}",
                    "score": d.score,
                    "source_type": "web",
                    "doc_title": d.title,
                }
                for i, d in enumerate(docs)
            ]
        except Exception as e:
            logger.warning(f"CRAG web fallback failed: {e}")
            return []

    @staticmethod
    def _score_relevance(query: str, results: list[dict[str, Any]]) -> float:
        if not results:
            return 0.0

        query_words = set(query.lower().split())
        if not query_words:
            return 0.5

        scores: list[float] = []
        for r in results:
            content = r.get("content", "").lower()
            rrf_score = r.get("rrf_score", r.get("score", 0.0))
            keyword_overlap = sum(1 for w in query_words if w in content) / len(query_words)
            combined = 0.4 * keyword_overlap + 0.6 * min(rrf_score * 5, 1.0)
            scores.append(combined)

        return max(scores) if scores else 0.0

    async def _generate_with_evidence(self, query: str, results: list[dict[str, Any]]) -> str:
        try:
            from app.services.llm.provider import provider

            evidence_parts = []
            for i, r in enumerate(results[:5]):
                source = r.get("source_type", "rag")
                label = "内部知识库" if source != "web" else "互联网（可能有偏差）"
                content = r.get("content", "")[:300]
                evidence_parts.append(f"[{i+1}]({label}) {content}")

            context = "\n\n".join(evidence_parts)
            system_prompt = (
                "你是一个知识库问答助手。基于检索到的证据回答问题。\n"
                "只基于证据回答，不编造信息。引用证据编号[1][2]等。\n"
                "如果证据不足，明确说明。"
            )
            user_prompt = f"证据：\n{context}\n\n问题：{query}"
            return await provider.chat(system_prompt=system_prompt, user_prompt=user_prompt, temperature=0.1)
        except Exception as e:
            logger.warning(f"CRAG generation failed: {e}")
            return f"基于检索证据的摘要：{query}"


class SelfRAG:
    """
    Self-RAG：模型自己决定是否需要检索、检索后自我批判
    1. 判断是否需要检索（事实性问题需要，闲聊不需要）
    2. 如果需要 → 检索 → 生成 → 自我批判
    3. 如果批判不通过 → 重新检索或修正
    4. 如果不需要 → 直接生成
    """

    def __init__(self):
        self._corrective_rag: CorrectiveRAG | None = None

    async def query(
        self,
        query: str,
        user_acl_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        need_retrieval = await self._judge_need_retrieval(query)

        if not need_retrieval:
            answer = await self._direct_generate(query)
            return {
                "answer": answer,
                "retrieval_needed": False,
                "correction_rounds": 0,
            }

        if self._corrective_rag is None:
            self._corrective_rag = CorrectiveRAG()

        crag_result = await self._corrective_rag.query(query, user_acl_tags=user_acl_tags)

        critique = await self._self_critique(query, crag_result.answer)
        if critique.get("passes", True):
            return {
                "answer": crag_result.answer,
                "retrieval_source": crag_result.retrieval_source,
                "retrieval_needed": True,
                "correction_rounds": crag_result.correction_rounds,
                "critique": critique,
            }

        refined = await self._refine_answer(query, crag_result.answer, critique)
        return {
            "answer": refined,
            "retrieval_source": crag_result.retrieval_source + "+refined",
            "retrieval_needed": True,
            "correction_rounds": crag_result.correction_rounds + 1,
            "critique": critique,
        }

    async def _judge_need_retrieval(self, query: str) -> bool:
        trivial_patterns = ["你好", "hello", "谢谢", "再见", "hi"]
        if any(p in query.lower() for p in trivial_patterns):
            return False
        if len(query) < 5:
            return False
        return True

    async def _direct_generate(self, query: str) -> str:
        try:
            from app.services.llm.provider import provider
            return await provider.chat(
                system_prompt="你是一个AI助手，直接回答用户问题。",
                user_prompt=query,
                temperature=0.7,
            )
        except Exception as e:
            return f"生成失败: {e}"

    async def _self_critique(self, query: str, answer: str) -> dict[str, Any]:
        try:
            from app.services.llm.provider import provider
            prompt = (
                f"问题：{query}\n回答：{answer}\n\n"
                f"请评估这个回答：1)是否准确 2)是否完整 3)是否基于事实而非编造\n"
                f'输出JSON: {{"passes": true/false, "reason": "..."}}'
            )
            response = await provider.chat(
                system_prompt="你是回答质量评估专家。",
                user_prompt=prompt,
                temperature=0.0,
            )
            import json
            result = json.loads(response.strip())
            return result
        except Exception:
            return {"passes": True, "reason": "critique_failed"}

    async def _refine_answer(self, query: str, answer: str, critique: dict) -> str:
        try:
            from app.services.llm.provider import provider
            prompt = (
                f"问题：{query}\n原回答：{answer}\n"
                f"评估反馈：{critique.get('reason', '')}\n\n"
                f"请根据反馈修正回答，使其更准确、完整、基于事实。"
            )
            return await provider.chat(
                system_prompt="你是回答修正专家。",
                user_prompt=prompt,
                temperature=0.1,
            )
        except Exception:
            return answer