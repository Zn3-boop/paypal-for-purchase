from __future__ import annotations

import logging
import os
from typing import Any

from app.services.rag.chunker import Chunker
from app.services.rag.gate import GenerationGate
from app.services.rag.metadata import make_chunk_meta
from app.services.rag.retriever import OnlineRetriever
from app.services.rag.store import DualIndexStore

logger = logging.getLogger(__name__)


class RAGService:
    """
    RAG工程落地主入口
    离线建库: chunk → dual_write (向量+BM25)
    在线检索: vector+BM25混搭 → RRF重排 → 交叉编码器精排
    生成闸门: 证据回溯 + 压幻觉 + 矛盾处理
    """

    def __init__(self, store_dir: str = ""):
        self.store = DualIndexStore(store_dir=store_dir)
        self.chunker = Chunker()
        self.retriever = OnlineRetriever(store=self.store)
        self.gate = GenerationGate()

    async def index_document(
        self,
        text: str,
        doc_id: str = "",
        doc_title: str = "",
        source_type: str = "text",
        acl_tags: list[str] | None = None,
        source_url: str = "",
        chunk_size: int = 0,
    ) -> dict[str, Any]:
        """
        离线建库: 文档 → 切分 → 双写(向量+BM25)
        R2: 结构化切分 + 重叠率
        R3: 四类切分法 (text/code/table/scan)
        R4: 元数据物理落库
        R5: 双写强约束
        """
        if chunk_size > 0:
            chunker = Chunker(chunk_size=chunk_size)
        else:
            chunker = self.chunker

        chunks = chunker.chunk_text(
            text=text,
            doc_id=doc_id,
            doc_title=doc_title,
            source_type=source_type,
            acl_tags=acl_tags,
            source_url=source_url,
        )

        if not chunks:
            return {"ok": False, "error": "切分结果为空", "chunk_count": 0}

        chunk_ids = self.store.add_chunks_batch(chunks)

        return {
            "ok": True,
            "doc_id": doc_id,
            "chunk_count": len(chunk_ids),
            "chunk_ids": chunk_ids,
            "source_type": source_type,
        }

    async def index_file(
        self,
        file_path: str,
        doc_id: str = "",
        doc_title: str = "",
        acl_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """索引单个文件"""
        if not os.path.exists(file_path):
            return {"ok": False, "error": f"文件不存在: {file_path}"}

        ext = os.path.splitext(file_path)[1].lower()
        source_type_map = {
            ".py": "code", ".js": "code", ".ts": "code", ".java": "code",
            ".go": "code", ".rs": "code", ".cpp": "code", ".c": "code",
            ".csv": "table", ".tsv": "table",
        }
        image_extensions = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".gif"}
        source_type = source_type_map.get(ext, "text")

        if ext in image_extensions:
            return await self._index_image(file_path, doc_id, doc_title, acl_tags)

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            return {"ok": False, "error": f"读取文件失败: {e}"}

        title = doc_title or os.path.basename(file_path)
        did = doc_id or os.path.basename(file_path)

        return await self.index_document(
            text=text, doc_id=did, doc_title=title,
            source_type=source_type, acl_tags=acl_tags,
            source_url=file_path,
        )

    async def _index_image(
        self,
        file_path: str,
        doc_id: str = "",
        doc_title: str = "",
        acl_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """索引图片文件：OCR提取文字 + VLM语义描述，互补融合"""
        import logging
        _log = logging.getLogger(__name__)

        title = doc_title or os.path.basename(file_path)
        did = doc_id or os.path.basename(file_path)
        text_parts: list[str] = []

        try:
            import asyncio
            from app.services.ocr import get_ocr_service
            ocr = get_ocr_service()
            ocr_result = await asyncio.to_thread(ocr.recognize_image, file_path)
            if ocr_result.success and ocr_result.text.strip():
                text_parts.append(f"[OCR文字]\n{ocr_result.text.strip()}")
                _log.info(f"Image index OCR: {len(ocr_result.text)} chars from {file_path}")
        except Exception as e:
            _log.debug(f"Image index OCR failed: {e}")

        try:
            from app.services.vlm import get_vlm_provider
            vlm = get_vlm_provider()
            if vlm.enabled:
                description = await vlm.describe_image(
                    prompt="请详细描述这张图片的内容，包括文字、UI元素、数据、图表等信息。",
                    image=file_path,
                )
                if description and description.strip():
                    text_parts.append(f"[VLM描述]\n{description.strip()}")
                    _log.info(f"Image index VLM: {len(description)} chars from {file_path}")
        except Exception as e:
            _log.debug(f"Image index VLM failed: {e}")

        if not text_parts:
            return {"ok": False, "error": f"图片无法提取内容(OCR和VLM均失败): {file_path}"}

        combined_text = "\n\n".join(text_parts)
        return await self.index_document(
            text=combined_text, doc_id=did, doc_title=title,
            source_type="scan", acl_tags=acl_tags,
            source_url=file_path,
        )

    async def query(
        self,
        query: str,
        user_acl_tags: list[str] | None = None,
        rewrite_queries: list[str] | None = None,
        top_k: int = 5,
        rerank_top_k: int = 3,
        enable_rerank: bool = False,
    ) -> dict[str, Any]:
        """
        在线检索 + 生成闸门
        R6: 向量+BM25混搭
        R7: 改写兜底(原查询永不替换)
        R8: 鉴权前置
        R9: RRF重排+精排
        R10: 证据回溯+压幻觉
        R12: 矛盾检测
        """
        retrieval_result = await self.retriever.retrieve(
            query=query,
            user_acl_tags=user_acl_tags,
            rewrite_queries=rewrite_queries,
            top_k=top_k,
            rerank_top_k=rerank_top_k,
            enable_rerank=enable_rerank,
        )

        retrieved = retrieval_result.get("results", [])

        context_result = self.gate.build_context(query=query, retrieved=retrieved)

        contradiction_result = self.gate.check_contradictions(retrieved)

        return {
            "query": query,
            "retrieval": retrieval_result,
            "context": context_result,
            "contradiction": contradiction_result,
            "answer_ready": not context_result.get("should_refuse", False),
        }

    async def generate_with_gate(
        self,
        query: str,
        context: str,
        evidence_ids: list[str],
        evidence_map: dict[str, str],
        llm_generate_fn=None,
    ) -> dict[str, Any]:
        """
        R10: 生成闸门 — 生成后校验
        """
        if not context:
            return {
                "output": "",
                "should_refuse": True,
                "refuse_reason": "无检索证据，拒绝回答",
                "validation": {},
            }

        if llm_generate_fn:
            try:
                output = await llm_generate_fn(query=query, context=context)
            except Exception as e:
                return {
                    "output": "",
                    "should_refuse": True,
                    "refuse_reason": f"LLM生成失败: {e}",
                    "validation": {},
                }
        else:
            output = f"基于检索证据回答问题: {query}\n\n{context[:1000]}"

        validation = self.gate.validate_output(output, evidence_ids, evidence_map)

        return {
            "output": output,
            "should_refuse": False,
            "validation": validation,
        }

    def delete_document(self, doc_id: str) -> dict[str, Any]:
        """删除文档所有chunk (R5: 禁止只增不删)"""
        deleted = 0
        for chunk in self.store.get_all_chunks():
            meta = chunk.get("meta", {})
            if meta.get("doc_id") == doc_id:
                if self.store.delete_chunk(chunk["chunk_id"]):
                    deleted += 1
        return {"ok": True, "doc_id": doc_id, "deleted_chunks": deleted}

    def expire_ttl(self) -> dict[str, Any]:
        """R5/R13: TTL过期清理"""
        before = len(self.store.get_all_chunks())
        self.store.expire_ttl_chunks()
        after = len(self.store.get_all_chunks())
        return {"ok": True, "expired": before - after}

    def stats(self) -> dict[str, Any]:
        return self.store.stats()


_rag_service: RAGService | None = None


def get_rag_service() -> RAGService:
    global _rag_service
    if _rag_service is None:
        store_dir = os.getenv("RAG_STORE_DIR", "")
        _rag_service = RAGService(store_dir=store_dir)
    return _rag_service