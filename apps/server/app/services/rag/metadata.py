from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ChunkMeta:
    """
    RAG 文档切片的元数据结构。

    被 chunker.py 中 Chunker / ParentChildChunker / SentenceWindowChunker 使用，
    要求：
    1. 支持初始化后赋值（ParentChildChunker 会设置 parent_meta.is_parent = True）
    2. 提供 to_dict() / from_dict() 序列化
    """

    chunk_id: str
    doc_id: str = ""
    doc_title: str = ""
    section_path: list[str] = field(default_factory=list)
    chunk_index: int = 0
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    source_type: str = "text"
    acl_tags: list[str] = field(default_factory=lambda: ["public"])
    source_url: str = ""
    checksum: str = ""
    char_count: int = 0
    content_hash: str = ""

    is_parent: bool = False
    child_count: int = 0
    parent_id: str = ""
    parent_chunk_index: int = -1

    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChunkMeta:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def make_chunk_meta(
    *,
    content: str,
    doc_id: str,
    doc_title: str,
    section_path: list[str] | None = None,
    chunk_index: int = 0,
    version: int = 1,
    source_type: str = "text",
    acl_tags: list[str] | None = None,
    source_url: str = "",
) -> ChunkMeta:
    now = time.time()
    text = content or ""
    try:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    except Exception:
        digest = uuid.uuid4().hex[:10]

    doc_part = (doc_id or "doc").replace(" ", "_")
    return ChunkMeta(
        chunk_id=f"{doc_part}_c{chunk_index}_{digest}",
        doc_id=doc_id,
        doc_title=doc_title,
        section_path=list(section_path or []),
        chunk_index=chunk_index,
        version=version,
        created_at=now,
        updated_at=now,
        source_type=source_type,
        acl_tags=list(acl_tags) if acl_tags else ["public"],
        source_url=source_url,
        checksum=hashlib.md5(text.encode("utf-8")).hexdigest()[:12],
        char_count=len(text),
        content_hash=digest,
    )