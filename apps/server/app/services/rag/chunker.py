from __future__ import annotations

import re
import logging
from typing import Any

from app.services.rag.metadata import ChunkMeta, make_chunk_meta

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 600
DEFAULT_OVERLAP_RATIO = 0.12
MIN_CHUNK_SIZE = 100
MAX_CHUNK_SIZE = 1200


class Chunker:
    """
    R2: 结构化切分 + 重叠率 (400-800字, 10-15% overlap)
    R3: 四类切分法 (text/ocr/table/code)
    """

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    ):
        self.chunk_size = max(MIN_CHUNK_SIZE, min(chunk_size, MAX_CHUNK_SIZE))
        self.overlap_size = int(self.chunk_size * overlap_ratio)

    def chunk_text(
        self,
        text: str,
        doc_id: str = "",
        doc_title: str = "",
        source_type: str = "text",
        acl_tags: list[str] | None = None,
        source_url: str = "",
    ) -> list[dict[str, Any]]:
        if source_type == "code":
            return self._chunk_code(text, doc_id, doc_title, acl_tags, source_url)
        if source_type == "table":
            return self._chunk_table(text, doc_id, doc_title, acl_tags, source_url)
        if source_type == "scan":
            return self._chunk_scan(text, doc_id, doc_title, acl_tags, source_url)
        return self._chunk_structured(text, doc_id, doc_title, source_type, acl_tags, source_url)

    def _chunk_structured(
        self,
        text: str,
        doc_id: str,
        doc_title: str,
        source_type: str,
        acl_tags: list[str] | None,
        source_url: str,
    ) -> list[dict[str, Any]]:
        sections = self._split_by_structure(text)
        results: list[dict[str, Any]] = []
        chunk_idx = 0
        for section_path, content in sections:
            content = content.strip()
            if not content:
                continue
            if len(content) <= self.chunk_size:
                meta = make_chunk_meta(
                    content=content, doc_id=doc_id, doc_title=doc_title,
                    section_path=section_path, chunk_index=chunk_idx,
                    source_type=source_type, acl_tags=acl_tags, source_url=source_url,
                )
                results.append({"content": content, "meta": meta.to_dict()})
                chunk_idx += 1
            else:
                for chunk in self._sliding_window(content):
                    meta = make_chunk_meta(
                        content=chunk, doc_id=doc_id, doc_title=doc_title,
                        section_path=section_path, chunk_index=chunk_idx,
                        source_type=source_type, acl_tags=acl_tags, source_url=source_url,
                    )
                    results.append({"content": chunk, "meta": meta.to_dict()})
                    chunk_idx += 1
        return results

    def _split_by_structure(self, text: str) -> list[tuple[list[str], str]]:
        heading_pattern = re.compile(r'^(#{1,6}\s+.+|[一二三四五六七八九十]+[、．.].+|\d+[\.．]\s+.+)$', re.MULTILINE)
        parts = heading_pattern.split(text)
        sections: list[tuple[list[str], str]] = []
        current_path: list[str] = []
        current_content: list[str] = []
        i = 0
        while i < len(parts):
            part = parts[i]
            if heading_pattern.match(part.strip()):
                if current_content:
                    content = "\n".join(current_content).strip()
                    if content:
                        sections.append((list(current_path), content))
                    current_content = []
                heading_text = re.sub(r'^#{1,6}\s+', '', part.strip())
                current_path = [heading_text]
            else:
                current_content.append(part)
            i += 1
        if current_content:
            content = "\n".join(current_content).strip()
            if content:
                sections.append((list(current_path), content))
        if not sections:
            sections = [([], text)]
        return sections

    def _sliding_window(self, text: str) -> list[str]:
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            chunk = text[start:end]
            if end < len(text):
                last_period = max(
                    chunk.rfind("。"), chunk.rfind("。"), chunk.rfind("\n"),
                    chunk.rfind(". "), chunk.rfind("! "), chunk.rfind("? "),
                )
                if last_period > self.chunk_size * 0.5:
                    chunk = chunk[: last_period + 1]
                    end = start + last_period + 1
            chunks.append(chunk.strip())
            start = end - self.overlap_size
            if start <= (end - self.chunk_size):
                start = end - self.overlap_size
        return [c for c in chunks if c]

    def _chunk_code(
        self,
        text: str,
        doc_id: str,
        doc_title: str,
        acl_tags: list[str] | None,
        source_url: str,
    ) -> list[dict[str, Any]]:
        blocks = re.split(r'\n(?=(class |def |async def |function |public |private |protected ))', text)
        results: list[dict[str, Any]] = []
        chunk_idx = 0
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if len(block) > MAX_CHUNK_SIZE:
                for sub in self._sliding_window(block):
                    meta = make_chunk_meta(
                        content=sub, doc_id=doc_id, doc_title=doc_title,
                        section_path=["code"], chunk_index=chunk_idx,
                        source_type="code", acl_tags=acl_tags, source_url=source_url,
                    )
                    results.append({"content": sub, "meta": meta.to_dict()})
                    chunk_idx += 1
            else:
                meta = make_chunk_meta(
                    content=block, doc_id=doc_id, doc_title=doc_title,
                    section_path=["code"], chunk_index=chunk_idx,
                    source_type="code", acl_tags=acl_tags, source_url=source_url,
                )
                results.append({"content": block, "meta": meta.to_dict()})
                chunk_idx += 1
        return results

    def _chunk_table(
        self,
        text: str,
        doc_id: str,
        doc_title: str,
        acl_tags: list[str] | None,
        source_url: str,
    ) -> list[dict[str, Any]]:
        rows = text.strip().split("\n")
        if not rows:
            return []
        header = rows[0]
        results: list[dict[str, Any]] = []
        chunk_idx = 0
        batch_size = max(1, self.chunk_size // max(len(r) for r in rows[:5]) if rows[:5] else 20)
        for i in range(0, len(rows), batch_size):
            batch = [header] + rows[i + 1 : i + batch_size + 1] if i == 0 else [header] + rows[i : i + batch_size]
            content = "\n".join(batch)
            meta = make_chunk_meta(
                content=content, doc_id=doc_id, doc_title=doc_title,
                section_path=["table"], chunk_index=chunk_idx,
                source_type="table", acl_tags=acl_tags, source_url=source_url,
            )
            results.append({"content": content, "meta": meta.to_dict()})
            chunk_idx += 1
        return results

    def _chunk_scan(
        self,
        text: str,
        doc_id: str,
        doc_title: str,
        acl_tags: list[str] | None,
        source_url: str,
    ) -> list[dict[str, Any]]:
        cleaned = re.sub(r'\s+', ' ', text).strip()
        return self._chunk_structured(cleaned, doc_id, doc_title, "scan", acl_tags, source_url)


class ParentChildChunker:
    """
    父子切分：小块检索、大块喂给LLM
    - 子chunk（小，200字）：用于精确检索
    - 父chunk（大，800字）：用于提供完整上下文给LLM
    - 检索命中子chunk时，返回其所属的父chunk
    """

    def __init__(
        self,
        parent_size: int = 800,
        child_size: int = 200,
        parent_overlap_ratio: float = 0.1,
        child_overlap_ratio: float = 0.15,
    ):
        self.parent_size = parent_size
        self.child_size = child_size
        self.parent_overlap = int(parent_size * parent_overlap_ratio)
        self.child_overlap = int(child_size * child_overlap_ratio)

    def chunk_text(
        self,
        text: str,
        doc_id: str = "",
        doc_title: str = "",
        source_type: str = "text",
        acl_tags: list[str] | None = None,
        source_url: str = "",
    ) -> list[dict[str, Any]]:
        parent_chunks = self._sliding_window(text, self.parent_size, self.parent_overlap)
        results: list[dict[str, Any]] = []

        for parent_idx, parent_text in enumerate(parent_chunks):
            parent_id = f"{doc_id}_p{parent_idx}" if doc_id else f"p{parent_idx}"
            child_chunks = self._sliding_window(parent_text, self.child_size, self.child_overlap)

            parent_meta = make_chunk_meta(
                content=parent_text,
                doc_id=doc_id,
                doc_title=doc_title,
                section_path=[f"parent_{parent_idx}"],
                chunk_index=parent_idx,
                source_type=source_type,
                acl_tags=acl_tags,
                source_url=source_url,
            )
            parent_meta.is_parent = True
            parent_meta.child_count = len(child_chunks)

            results.append({
                "content": parent_text,
                "meta": {**parent_meta.to_dict(), "is_parent": True, "child_count": len(child_chunks)},
            })

            for child_idx, child_text in enumerate(child_chunks):
                child_meta = make_chunk_meta(
                    content=child_text,
                    doc_id=doc_id,
                    doc_title=doc_title,
                    section_path=[f"parent_{parent_idx}", f"child_{child_idx}"],
                    chunk_index=parent_idx * 10 + child_idx,
                    source_type=source_type,
                    acl_tags=acl_tags,
                    source_url=source_url,
                )

                results.append({
                    "content": child_text,
                    "meta": {
                        **child_meta.to_dict(),
                        "is_parent": False,
                        "parent_id": parent_id,
                        "parent_chunk_index": parent_idx,
                    },
                })

        return results

    @staticmethod
    def _sliding_window(text: str, chunk_size: int, overlap: int) -> list[str]:
        if len(text) <= chunk_size:
            return [text] if text.strip() else []
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk)
            start += chunk_size - overlap
            if start >= len(text):
                break
        return chunks


class SentenceWindowChunker:
    """
    句子窗口检索：以句子为最小检索单元，命中后返回前后N句作为上下文窗口
    - 检索：精确匹配单个句子
    - 返回：匹配句子 ± window_size 句的上下文
    """

    def __init__(self, window_size: int = 2, min_sentence_len: int = 10):
        self.window_size = window_size
        self.min_sentence_len = min_sentence_len

    def chunk_text(
        self,
        text: str,
        doc_id: str = "",
        doc_title: str = "",
        source_type: str = "text",
        acl_tags: list[str] | None = None,
        source_url: str = "",
    ) -> list[dict[str, Any]]:
        sentences = self._split_sentences(text)
        if not sentences:
            return []

        results: list[dict[str, Any]] = []
        for i, sentence in enumerate(sentences):
            if len(sentence.strip()) < self.min_sentence_len:
                continue

            context_start = max(0, i - self.window_size)
            context_end = min(len(sentences), i + self.window_size + 1)
            context = " ".join(sentences[context_start:context_end])

            meta = make_chunk_meta(
                content=sentence,
                doc_id=doc_id,
                doc_title=doc_title,
                section_path=[f"sentence_{i}"],
                chunk_index=i,
                source_type=source_type,
                acl_tags=acl_tags,
                source_url=source_url,
            )

            results.append({
                "content": sentence,
                "meta": {
                    **meta.to_dict(),
                    "context_window": context,
                    "sentence_index": i,
                    "window_start": context_start,
                    "window_end": context_end - 1,
                },
            })

        return results

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        pattern = re.compile(r'(?<=[。！？；\.\!\?;])\s*')
        sentences = pattern.split(text)
        return [s.strip() for s in sentences if s.strip()]