"""
P0-3: code_search 根目录白名单 / 越界拒绝 安全测试。

覆盖：
- sanitize_search_root：黑名单拒绝系统目录、白名单内放行、白名单外拒绝
- GlobEngine / GrepEngine：root_dir 越界直接返回空结果
- CodeSearchService.search：越界返回 error，不触发实际检索
"""

from __future__ import annotations

import asyncio
import os

from app.services.code_search import service
from app.services.code_search.service import (
    CodeSearchService,
    GlobEngine,
    GrepEngine,
    sanitize_search_root,
)


def _patch_allowed(monkeypatch, tmp_path):
    """将白名单收敛到 tmp_path，避免受 import 时 os.getcwd() 影响。"""
    monkeypatch.setattr(service, "ALLOWED_SEARCH_ROOTS", [str(tmp_path)])


class TestSanitizeSearchRoot:
    def test_rejects_forbidden_windows_system_dir(self):
        assert sanitize_search_root(r"C:\Windows") is None
        assert sanitize_search_root(r"C:\Windows\System32") is None

    def test_rejects_forbidden_posix_system_dir(self):
        assert sanitize_search_root("/etc") is None
        assert sanitize_search_root("/usr/bin") is None

    def test_allows_inside_allowed_root(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        sub = tmp_path / "src"
        sub.mkdir()
        assert sanitize_search_root(str(sub)) == str(sub.resolve())

    def test_allows_allowed_root_itself(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        assert sanitize_search_root(str(tmp_path)) == str(tmp_path.resolve())

    def test_rejects_outside_allowed_root(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        outside = str(tmp_path.parent / "somewhere_else")
        assert sanitize_search_root(outside) is None


class TestGlobEngineRejectRoot:
    def test_glob_rejects_forbidden_root(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        assert GlobEngine().search("*.py", root_dir=r"C:\Windows") == []

    def test_glob_rejects_outside_allowed(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        assert GlobEngine().search("*.py", root_dir=str(tmp_path.parent / "other")) == []

    def test_glob_allows_inside_allowed(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        res = GlobEngine().search("*.py", root_dir=str(tmp_path))
        assert any(os.path.basename(r["path"]) == "a.py" for r in res)


class TestGrepEngineRejectRoot:
    def test_grep_rejects_forbidden_root(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        assert GrepEngine().search("foo", root_dir=r"C:\Windows") == []

    def test_grep_rejects_outside_allowed(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        assert GrepEngine().search("foo", root_dir=str(tmp_path.parent / "other")) == []


class TestCodeSearchServiceRejectRoot:
    def test_service_rejects_forbidden_root(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        out = asyncio.run(CodeSearchService().search(
            "def foo", root_dir=r"C:\Windows", search_type="grep",
        ))
        assert out["results"] == []
        assert "拒绝" in out["error"]
        assert out["stats"]["total"] == 0

    def test_service_rejects_outside_allowed(self, monkeypatch, tmp_path):
        _patch_allowed(monkeypatch, tmp_path)
        out = asyncio.run(CodeSearchService().search(
            "def foo", root_dir=str(tmp_path.parent / "other"), search_type="grep",
        ))
        assert out["results"] == []
        assert out["error"]
