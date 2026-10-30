from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from app.services.rag.metadata import ChunkMeta, make_chunk_meta

logger = logging.getLogger(__name__)

DEFAULT_STORE_DIR = os.path.join(os.path.expanduser("~"), ".ecv_rag_store")


class DualIndexStore:
    """
    R5: 双写落盘强约束 — 向量库 + BM25 同步双写
    R4: 元数据物理落库 (编号/层级/版本/ACL)
    - 更新/删除强制绑定版本号
    - 禁止只增不删，历史向量可失效(TTL)
    """

    def __init__(self, store_dir: str = ""):
        self.store_dir = store_dir or DEFAULT_STORE_DIR
        self.chunks_dir = os.path.join(self.store_dir, "chunks")
        self.meta_dir = os.path.join(self.store_dir, "meta")
        self.index_file = os.path.join(self.store_dir, "index.json")
        os.makedirs(self.chunks_dir, exist_ok=True)
        os.makedirs(self.meta_dir, exist_ok=True)
        self._index: dict[str, dict[str, Any]] = {}
        self._bm25_docs: list[dict[str, Any]] = []
        self._vector_ids: list[str] = []
        self._faiss_index: Any = None
        self._embedding_model: Any = None
        self._load_index()

    def _load_index(self):
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
                for cid, info in self._index.items():
                    if info.get("active", True):
                        chunk_path = os.path.join(self.chunks_dir, f"{cid}.txt")
                        if os.path.exists(chunk_path):
                            with open(chunk_path, "r", encoding="utf-8") as f:
                                doc = {"content": f.read(), "meta": info.get("meta", {}), "chunk_id": cid}
                            self._bm25_docs.append(doc)
                            self._vector_ids.append(cid)
                logger.info(f"DualIndexStore: loaded {len(self._bm25_docs)} active chunks")
                if self._bm25_docs and os.getenv("RAG_AUTO_BUILD_FAISS", "true").lower() in ("true", "1", "yes"):
                    logger.info("DualIndexStore: auto-building FAISS index on startup...")
                    try:
                        self._build_faiss_index()
                    except Exception as faiss_err:
                        logger.warning(f"FAISS自动构建失败(非致命): {faiss_err}, 降级为BM25-only")
            except Exception as e:
                logger.warning(f"加载索引失败: {e}, 从空开始")
                self._index = {}

    def _save_index(self):
        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(self._index, f, ensure_ascii=False, indent=2)

    def _get_embedding_model(self):
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                model_path = os.getenv(
                    "RAG_EMBEDDING_MODEL_PATH",
                    os.getenv("RAG_EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
                )
                if os.path.isdir(model_path):
                    os.environ["TRANSFORMERS_OFFLINE"] = "1"
                    os.environ["HF_HUB_OFFLINE"] = "1"
                    self._embedding_model = SentenceTransformer(model_path)
                    logger.info(f"[Embedding] loaded local model from {model_path} (offline-only)")
                else:
                    raise FileNotFoundError(
                        f"Embedding model not found at {model_path}. "
                        "Please download all-MiniLM-L6-v2 manually and set "
                        "RAG_EMBEDDING_MODEL_PATH in .env. "
                        "Run: python scripts/download_rag_models.py"
                    )
            except ImportError:
                logger.warning("sentence_transformers未安装, 向量检索不可用")
                self._embedding_model = False
            except FileNotFoundError:
                raise
            except Exception as e:
                logger.warning(f"Embedding model加载失败: {e}, 向量检索降级为BM25-only")
                self._embedding_model = False
        return self._embedding_model if self._embedding_model is not False else None

    def add_chunk(self, content: str, meta: dict[str, Any]) -> str:
        chunk_id = meta.get("chunk_id", f"chk_{int(time.time()*1000)}")
        version = meta.get("version", 1)
        chunk_path = os.path.join(self.chunks_dir, f"{chunk_id}.txt")
        meta_path = os.path.join(self.meta_dir, f"{chunk_id}.json")
        with open(chunk_path, "w", encoding="utf-8") as f:
            f.write(content)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        self._index[chunk_id] = {
            "active": True,
            "version": version,
            "updated_at": time.time(),
            "ttl": float(os.getenv("RAG_CHUNK_TTL_DAYS", "365")) * 86400,
            "meta": meta,
        }
        self._save_index()
        doc = {"content": content, "meta": meta, "chunk_id": chunk_id}
        self._bm25_docs.append(doc)
        self._vector_ids.append(chunk_id)
        self._faiss_index = None
        logger.info(f"DualIndexStore: added chunk {chunk_id} v{version}")
        return chunk_id

    def add_chunks_batch(self, chunks: list[dict[str, Any]]) -> list[str]:
        ids = []
        for chunk in chunks:
            cid = self.add_chunk(chunk["content"], chunk["meta"])
            ids.append(cid)
        self._build_faiss_index()
        return ids

    def delete_chunk(self, chunk_id: str) -> bool:
        if chunk_id not in self._index:
            return False
        self._index[chunk_id]["active"] = False
        self._index[chunk_id]["updated_at"] = time.time()
        self._save_index()
        self._bm25_docs = [d for d in self._bm25_docs if d["chunk_id"] != chunk_id]
        self._vector_ids = [vid for vid in self._vector_ids if vid != chunk_id]
        self._faiss_index = None
        logger.info(f"DualIndexStore: deleted (soft) chunk {chunk_id}")
        return True

    def update_chunk(self, chunk_id: str, content: str, meta: dict[str, Any]) -> bool:
        if chunk_id not in self._index:
            return False
        old_version = self._index[chunk_id].get("version", 1)
        new_version = old_version + 1
        meta["version"] = new_version
        meta["chunk_id"] = chunk_id
        chunk_path = os.path.join(self.chunks_dir, f"{chunk_id}.txt")
        with open(chunk_path, "w", encoding="utf-8") as f:
            f.write(content)
        meta_path = os.path.join(self.meta_dir, f"{chunk_id}.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        self._index[chunk_id] = {
            "active": True,
            "version": new_version,
            "updated_at": time.time(),
            "ttl": float(os.getenv("RAG_CHUNK_TTL_DAYS", "365")) * 86400,
            "meta": meta,
        }
        self._save_index()
        for i, doc in enumerate(self._bm25_docs):
            if doc["chunk_id"] == chunk_id:
                self._bm25_docs[i] = {"content": content, "meta": meta, "chunk_id": chunk_id}
                break
        self._faiss_index = None
        logger.info(f"DualIndexStore: updated chunk {chunk_id} v{old_version}→v{new_version}")
        return True

    def expire_ttl_chunks(self):
        now = time.time()
        expired = []
        for cid, info in self._index.items():
            if not info.get("active", True):
                continue
            updated = info.get("updated_at", 0)
            ttl = info.get("ttl", 365 * 86400)
            if now - updated > ttl:
                self.delete_chunk(cid)
                expired.append(cid)
        if expired:
            logger.info(f"DualIndexStore: expired {len(expired)} TTL chunks: {expired[:5]}...")

    def get_bm25_docs(self) -> list[dict[str, Any]]:
        return list(self._bm25_docs)

    def get_all_chunks(self) -> list[dict[str, Any]]:
        return list(self._bm25_docs)

    def _build_faiss_index(self):
        model = self._get_embedding_model()
        if model is None or not self._bm25_docs:
            self._faiss_index = None
            return
        try:
            import faiss
            import numpy as np
            contents = [d["content"] for d in self._bm25_docs]
            embeddings = model.encode(contents, show_progress_bar=False)
            embeddings = np.array(embeddings, dtype=np.float32)
            faiss.normalize_L2(embeddings)
            dim = embeddings.shape[1]
            index = faiss.IndexFlatIP(dim)
            index.add(embeddings)
            self._faiss_index = index
            self._faiss_embeddings = embeddings
            logger.info(f"FAISS index built: {index.ntotal} vectors, dim={dim}")
        except ImportError:
            logger.warning("faiss-cpu未安装, 向量检索不可用")
            self._faiss_index = None
        except Exception as e:
            logger.warning(f"构建FAISS索引失败: {e}")
            self._faiss_index = None

    def vector_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        model = self._get_embedding_model()
        if model is None:
            return []
        if self._faiss_index is None:
            self._build_faiss_index()
        if self._faiss_index is None:
            return []
        try:
            import numpy as np
            q_emb = model.encode([query], show_progress_bar=False)
            q_emb = np.array(q_emb, dtype=np.float32)
            faiss = __import__("faiss")
            faiss.normalize_L2(q_emb)
            scores, indices = self._faiss_index.search(q_emb, min(top_k, self._faiss_index.ntotal))
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx >= 0 and idx < len(self._bm25_docs):
                    doc = self._bm25_docs[idx]
                    results.append({
                        "content": doc["content"],
                        "meta": doc["meta"],
                        "chunk_id": doc["chunk_id"],
                        "score": float(score),
                        "source": "vector",
                    })
            return results
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            return []

    def bm25_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not self._bm25_docs:
            return []
        try:
            from rank_bm25 import BM25Okapi
            tokenized_corpus = [list(d["content"]) for d in self._bm25_docs]
            bm25 = BM25Okapi(tokenized_corpus)
            tokenized_query = list(query)
            scores = bm25.get_scores(tokenized_query)
            ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            results = []
            for idx in ranked[:top_k]:
                doc = self._bm25_docs[idx]
                results.append({
                    "content": doc["content"],
                    "meta": doc["meta"],
                    "chunk_id": doc["chunk_id"],
                    "score": float(scores[idx]),
                    "source": "bm25",
                })
            return results
        except ImportError:
            logger.warning("rank-bm25未安装, BM25检索不可用")
            return self._keyword_fallback_search(query, top_k)

    def _keyword_fallback_search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        results = []
        q_low = query.lower()
        for doc in self._bm25_docs:
            content_low = doc["content"].lower()
            score = 0
            for token in q_low.split():
                if token in content_low:
                    score += content_low.count(token)
            if score > 0:
                results.append({
                    "content": doc["content"],
                    "meta": doc["meta"],
                    "chunk_id": doc["chunk_id"],
                    "score": float(score),
                    "source": "keyword_fallback",
                })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def stats(self) -> dict[str, Any]:
        active = sum(1 for info in self._index.values() if info.get("active", True))
        total = len(self._index)
        return {
            "total_chunks": total,
            "active_chunks": active,
            "bm25_docs": len(self._bm25_docs),
            "faiss_built": self._faiss_index is not None,
            "store_dir": self.store_dir,
        }