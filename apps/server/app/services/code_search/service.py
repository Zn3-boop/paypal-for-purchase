from __future__ import annotations

import fnmatch
import logging
import os
import re
import time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_IGNORE_PATTERNS = [
    "node_modules", ".git", ".idea", ".vscode", "__pycache__",
    "*.pyc", ".DS_Store", "*.log", ".env", "venv", ".venv",
    "dist", "build", ".next", ".nuxt", "coverage",
]

# ===== P0-3: 代码搜索根目录白名单（防扫系统盘/C:/全盘）=====
ALLOWED_SEARCH_ROOTS = [
    os.path.abspath(r) for r in os.getenv("CODE_SEARCH_ALLOWED_ROOTS", "").split(os.pathsep) if r
] or [os.getcwd()]

_FORBIDDEN_SEARCH_ROOTS = [
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "/etc", "/usr", "/lib", "/bin", "/sbin", "/root", "/proc", "/sys",
    "/var", "/opt", "/boot",
]


def sanitize_search_root(root_dir: str) -> str | None:
    """
    P0-3: 将搜索根目录约束到允许的工程根目录内。
    返回安全的绝对路径；若目标被禁止或不在允许根内，返回 None（应拒绝执行）。
    """
    try:
        root = os.path.abspath(os.path.expanduser(root_dir or "."))
    except Exception:
        return None
    raw = (root_dir or ".").strip()
    # 识别 Windows 盘符绝对路径（如 C:\Windows / C:/Windows）。
    # 在 Linux/CI 上 os.path.abspath 不识别盘符，会把 "C:\Windows" 拼接到 cwd 下，
    # 导致系统目录黑名单检查失效；这里单独按 Windows 语义归一化后校验。
    win_drive = bool(re.match(r"^[a-zA-Z]:[\\/]", raw))
    if win_drive:
        lower = re.sub(r"[\\/]+", lambda _m: "\\", raw).lower().rstrip("\\")
        sep = "\\"
    else:
        lower = root.lower().rstrip("\\/")
        sep = os.sep
    for bad in _FORBIDDEN_SEARCH_ROOTS:
        bad_l = bad.rstrip("\\/")
        if lower == bad_l or lower.startswith(bad_l + sep):
            return None
    for allowed in ALLOWED_SEARCH_ROOTS:
        allow = os.path.abspath(allowed).lower()
        if win_drive:
            allow = re.sub(r"[\\/]+", lambda _m: "\\", allow).rstrip("\\")
        else:
            allow = allow.rstrip("\\/")
        if lower == allow or lower.startswith(allow + sep):
            return root
    return None


class SearchType(Enum):
    """
    S1: 搜索策略路由 — 按请求类型分流
    """
    GLOB = "glob"
    GREP = "grep"
    SEMANTIC = "semantic"


def classify_search_type(query: str) -> SearchType:
    r"""
    S1: 自动判断搜索类型
    - 含glob通配符(*, **, ?, [) → GLOB
    - 含正则特征(/^..$/, \d, \w) → GREP
    - 自然语言 → SEMANTIC
    """
    glob_chars = {"*", "?", "[", "{"}
    if any(c in query for c in glob_chars) and ("**" in query or "/" in query or "\\" in query or "." in query):
        return SearchType.GLOB
    if re.search(r'\\[dDwWsS]|\\d|\\w|\^|\$|\|', query):
        return SearchType.GREP
    if len(query.split()) <= 3 and not re.search(r'[^\w\u4e00-\u9fff]', query):
        return SearchType.GREP
    return SearchType.SEMANTIC


class GlobEngine:
    """
    S5: GlobTool — 文件批量匹配工具
    - 支持标准Glob语法
    - 自动过滤忽略目录
    - 按修改时间排序
    """

    def __init__(self, ignore_patterns: list[str] | None = None):
        self.ignore_patterns = ignore_patterns or DEFAULT_IGNORE_PATTERNS

    def search(
        self,
        pattern: str,
        root_dir: str = ".",
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        root_dir = os.path.abspath(root_dir)
        safe_root = sanitize_search_root(root_dir)
        if safe_root is None:
            logger.warning(f"GlobEngine: 搜索根目录被白名单拒绝: {root_dir}")
            return []
        root_dir = safe_root
        if not os.path.isdir(root_dir):
            return []
        results = []
        try:
            for dirpath, dirnames, filenames in os.walk(root_dir):
                dirnames[:] = [d for d in dirnames if not self._should_ignore(d, os.path.join(dirpath, d))]
                for filename in filenames:
                    full_path = os.path.join(dirpath, filename)
                    rel_path = os.path.relpath(full_path, root_dir)
                    if self._should_ignore(filename, full_path):
                        continue
                    if self._match_glob(rel_path, pattern) or self._match_glob(filename, pattern):
                        try:
                            stat = os.stat(full_path)
                            results.append({
                                "path": full_path,
                                "rel_path": rel_path.replace("\\", "/"),
                                "name": filename,
                                "size": stat.st_size,
                                "modified": stat.st_mtime,
                                "type": "file",
                            })
                        except OSError:
                            continue
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
        except Exception as e:
            logger.warning(f"GlobEngine搜索失败: {e}")
        results.sort(key=lambda x: x.get("modified", 0), reverse=True)
        return results

    def _should_ignore(self, name: str, full_path: str = "") -> bool:
        for pat in self.ignore_patterns:
            if pat.startswith("*"):
                if fnmatch.fnmatch(name, pat):
                    return True
            elif name == pat or (full_path and f"\\{pat}\\" in full_path or f"/{pat}/" in full_path):
                return True
        return False

    def _match_glob(self, path: str, pattern: str) -> bool:
        if "**" in pattern:
            regex = self._glob_to_regex(pattern)
            return bool(re.match(regex, path))
        return fnmatch.fnmatch(path, pattern)

    def _glob_to_regex(self, pattern: str) -> str:
        regex = ""
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == "*":
                if i + 1 < len(pattern) and pattern[i + 1] == "*":
                    regex += ".*"
                    i += 2
                    if i < len(pattern) and pattern[i] == "/":
                        i += 1
                else:
                    regex += "[^/]*"
                    i += 1
            elif c == "?":
                regex += "[^/]"
                i += 1
            elif c == ".":
                regex += r"\."
                i += 1
            elif c == "/":
                regex += r"[/\\]"
                i += 1
            else:
                regex += re.escape(c)
                i += 1
        return f"^{regex}$"


class GrepEngine:
    """
    S2: GrepTool — 正则内容检索
    """

    def __init__(self, ignore_patterns: list[str] | None = None):
        self.ignore_patterns = ignore_patterns or DEFAULT_IGNORE_PATTERNS

    def search(
        self,
        pattern: str,
        root_dir: str = ".",
        file_glob: str = "",
        max_results: int = 50,
        context_lines: int = 2,
    ) -> list[dict[str, Any]]:
        root_dir = os.path.abspath(root_dir)
        safe_root = sanitize_search_root(root_dir)
        if safe_root is None:
            logger.warning(f"GrepEngine: 搜索根目录被白名单拒绝: {root_dir}")
            return []
        root_dir = safe_root
        if not os.path.isdir(root_dir):
            return []
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
        results = []
        glob_engine = GlobEngine(self.ignore_patterns)
        files = glob_engine.search(file_glob or "**/*", root_dir, max_results=500)
        for file_info in files:
            file_path = file_info["path"]
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                continue
            for line_num, line in enumerate(lines, start=1):
                if regex.search(line):
                    start = max(0, line_num - 1 - context_lines)
                    end = min(len(lines), line_num + context_lines)
                    context = "".join(lines[start:end])
                    results.append({
                        "path": file_path,
                        "rel_path": file_info["rel_path"],
                        "line": line_num,
                        "content": line.strip(),
                        "context": context.strip(),
                        "type": "match",
                    })
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break
        return results


class SemanticEngine:
    """
    S2: 嵌入型语义向量检索工具
    S4: 核心处理层 — 融合AI嵌入模型/向量索引
    """

    def __init__(self):
        self._model = None
        self._index: Any = None
        self._code_chunks: list[dict[str, Any]] = []

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                model_path = os.getenv(
                    "CODE_SEARCH_EMBEDDING_MODEL",
                    os.getenv("RAG_EMBEDDING_MODEL_PATH", "all-MiniLM-L6-v2"),
                )
                if os.path.isdir(model_path):
                    os.environ["TRANSFORMERS_OFFLINE"] = "1"
                    os.environ["HF_HUB_OFFLINE"] = "1"
                    self._model = SentenceTransformer(model_path)
                    logger.info(f"CodeSearch SemanticEngine: loaded local model from {model_path}")
                else:
                    raise FileNotFoundError(
                        f"Embedding model not found at {model_path}. "
                        "Please download all-MiniLM-L6-v2 manually and set "
                        "RAG_EMBEDDING_MODEL_PATH in .env."
                    )
            except ImportError:
                logger.warning("sentence_transformers不可用, 语义搜索降级")
                self._model = False
            except FileNotFoundError:
                raise
            except Exception as e:
                logger.warning(f"Embedding model加载失败: {e}, 语义搜索降级")
                self._model = False
        return self._model if self._model is not False else None

    def index_directory(
        self,
        root_dir: str = ".",
        file_glob: str = "**/*.py",
        chunk_size: int = 50,
    ) -> int:
        root_dir = os.path.abspath(root_dir)
        glob_engine = GlobEngine()
        files = glob_engine.search(file_glob, root_dir, max_results=500)
        self._code_chunks = []
        for file_info in files:
            try:
                with open(file_info["path"], "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                continue
            for i in range(0, len(lines), chunk_size):
                chunk_lines = lines[i:i + chunk_size]
                content = "".join(chunk_lines)
                if content.strip():
                    self._code_chunks.append({
                        "path": file_info["path"],
                        "rel_path": file_info["rel_path"],
                        "start_line": i + 1,
                        "end_line": min(i + chunk_size, len(lines)),
                        "content": content,
                    })
        self._build_index()
        return len(self._code_chunks)

    def _build_index(self):
        model = self._get_model()
        if model is None or not self._code_chunks:
            self._index = None
            return
        try:
            import faiss
            import numpy as np
            contents = [c["content"][:512] for c in self._code_chunks]
            embeddings = model.encode(contents, show_progress_bar=False)
            embeddings = np.array(embeddings, dtype=np.float32)
            faiss.normalize_L2(embeddings)
            dim = embeddings.shape[1]
            index = faiss.IndexFlatIP(dim)
            index.add(embeddings)
            self._index = index
            self._embeddings = embeddings
            logger.info(f"CodeSearch FAISS: {index.ntotal} vectors, dim={dim}")
        except Exception as e:
            logger.warning(f"构建代码搜索FAISS失败: {e}")
            self._index = None

    def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        model = self._get_model()
        if model is None or self._index is None:
            return self._keyword_fallback(query, top_k)
        try:
            import numpy as np
            import faiss
            q_emb = model.encode([query], show_progress_bar=False)
            q_emb = np.array(q_emb, dtype=np.float32)
            faiss.normalize_L2(q_emb)
            scores, indices = self._index.search(q_emb, min(top_k, self._index.ntotal))
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx >= 0 and idx < len(self._code_chunks):
                    chunk = self._code_chunks[idx]
                    results.append({
                        **chunk,
                        "score": float(score),
                        "source": "semantic",
                    })
            return results
        except Exception as e:
            logger.warning(f"语义搜索失败: {e}")
            return self._keyword_fallback(query, top_k)

    def _keyword_fallback(self, query: str, top_k: int) -> list[dict[str, Any]]:
        q_low = query.lower()
        results = []
        for chunk in self._code_chunks:
            if q_low in chunk["content"].lower():
                results.append({**chunk, "score": 1.0, "source": "keyword_fallback"})
        return results[:top_k]


class CodeSearchService:
    """
    S1-S7: 智能代码搜索工具链主入口
    S1: 搜索策略路由
    S2: 三类引擎分工
    S3: 权限检查 + 实时索引
    S4: 四层解耦架构
    S7: 结果处理层统一聚合输出
    """

    def __init__(self):
        self.glob_engine = GlobEngine()
        self.grep_engine = GrepEngine()
        self.semantic_engine = SemanticEngine()

    async def search(
        self,
        query: str,
        root_dir: str = ".",
        search_type: str = "",
        file_glob: str = "",
        max_results: int = 20,
        user_acl: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        S1: 策略路由 → S2: 引擎分发 → S3: 权限检查 → S7: 聚合输出
        """
        if search_type:
            stype = SearchType(search_type)
        else:
            stype = classify_search_type(query)

        # P0-3: 主入口根目录白名单
        safe_root = sanitize_search_root(root_dir)
        if safe_root is None:
            return {
                "query": query,
                "search_type": stype.value,
                "results": [],
                "stats": {"total": 0, "filtered": 0, "returned": 0},
                "error": "搜索根目录超出允许范围，已被安全策略拒绝",
            }
        root_dir = safe_root

        raw_results: list[dict[str, Any]] = []

        if stype == SearchType.GLOB:
            raw_results = self.glob_engine.search(query, root_dir, max_results=max_results * 2)
        elif stype == SearchType.GREP:
            raw_results = self.grep_engine.search(query, root_dir, file_glob=file_glob, max_results=max_results * 2)
        elif stype == SearchType.SEMANTIC:
            if not self.semantic_engine._code_chunks:
                chunk_count = self.semantic_engine.index_directory(root_dir, file_glob or "**/*.py")
                logger.info(f"SemanticEngine indexed {chunk_count} chunks")
            raw_results = self.semantic_engine.search(query, top_k=max_results * 2)

        filtered = self._permission_filter(raw_results, user_acl)
        truncated = filtered[:max_results]
        stats = {
            "total": len(raw_results),
            "filtered": len(raw_results) - len(filtered),
            "returned": len(truncated),
        }
        return {
            "query": query,
            "search_type": stype.value,
            "results": truncated,
            "stats": stats,
        }

    def _permission_filter(
        self,
        results: list[dict[str, Any]],
        user_acl: list[str] | None,
    ) -> list[dict[str, Any]]:
        """
        S3: 权限检查 — 敏感文件过滤
        """
        if not user_acl:
            user_acl = ["public"]
        admin = "admin" in [t.lower() for t in user_acl]
        if admin:
            return results
        sensitive_patterns = [".env", "secret", "credential", "private", ".key", "password"]
        filtered = []
        for item in results:
            path = item.get("path", item.get("rel_path", ""))
            is_sensitive = any(p in path.lower() for p in sensitive_patterns)
            if not is_sensitive:
                filtered.append(item)
        return filtered


_code_search_service: CodeSearchService | None = None


def get_code_search_service() -> CodeSearchService:
    global _code_search_service
    if _code_search_service is None:
        _code_search_service = CodeSearchService()
    return _code_search_service