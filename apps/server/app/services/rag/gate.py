from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class GenerationGate:
    """
    R10: 压幻觉三招 — 证据回溯 / 中间遗忘规避 / 权限前置拒答
    R11: 插图占位 / 大表格分治
    R12: 矛盾片段处理 — 元数据加权 → 多立场对比 → 交用户校验
    """

    def __init__(self):
        pass

    def build_context(
        self,
        query: str,
        retrieved: list[dict[str, Any]],
        max_evidence: int = 5,
    ) -> dict[str, Any]:
        """
        R10: 构建生成上下文
        - 证据放上下文头尾(规避中间遗忘)
        - 每条证据带chunk_id(可回溯)
        - 证据不足时标记should_refuse
        """
        if not retrieved:
            return {
                "context": "",
                "evidence_ids": [],
                "should_refuse": True,
                "refuse_reason": "无检索证据支撑，拒绝回答",
            }

        sorted_evidence = self._sort_by_relevance(retrieved[:max_evidence])

        top_evidence = sorted_evidence[:2]
        mid_evidence = sorted_evidence[2:-2] if len(sorted_evidence) > 4 else []
        bottom_evidence = sorted_evidence[-2:] if len(sorted_evidence) > 2 else []

        context_parts = []
        evidence_ids = []

        for item in top_evidence:
            cid = item.get("chunk_id", "unknown")
            evidence_ids.append(cid)
            context_parts.append(f"[证据#{cid}](头) {item['content'][:500]}")

        for item in mid_evidence:
            cid = item.get("chunk_id", "unknown")
            evidence_ids.append(cid)
            summary = item["content"][:200] + "..." if len(item["content"]) > 200 else item["content"]
            context_parts.append(f"[证据#{cid}](摘要) {summary}")

        for item in bottom_evidence:
            cid = item.get("chunk_id", "unknown")
            evidence_ids.append(cid)
            context_parts.append(f"[证据#{cid}](尾) {item['content'][:500]}")

        context = "\n\n".join(context_parts)

        should_refuse = len(retrieved) == 0
        refuse_reason = "" if not should_refuse else "证据不足，拒绝回答"

        return {
            "context": context,
            "evidence_ids": evidence_ids,
            "should_refuse": should_refuse,
            "refuse_reason": refuse_reason,
        }

    def _sort_by_relevance(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda x: x.get("rrf_score", x.get("score", 0)), reverse=True)

    def check_contradictions(
        self,
        retrieved: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        R12: 矛盾片段处理(三步递进)
        1. 元数据加权(越新、越权威 → 权重越高)
        2. 多立场内容对比
        3. 交用户校验(不替用户拍板)
        """
        if len(retrieved) < 2:
            return {"has_contradiction": False, "groups": [], "user_action_needed": False}

        weighted = self._apply_metadata_weight(retrieved)
        groups = self._group_by_stance(weighted)
        has_contradiction = len(groups) > 1

        return {
            "has_contradiction": has_contradiction,
            "groups": groups,
            "user_action_needed": has_contradiction,
            "instruction": "检测到矛盾信息，已按权威性和时效性加权排列，请用户自行判断" if has_contradiction else "",
        }

    def _apply_metadata_weight(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now_ts = __import__("time").time()
        for item in items:
            meta = item.get("meta", {})
            version = meta.get("version", 1)
            created_at = meta.get("created_at", now_ts)
            age_days = max(0, (now_ts - created_at) / 86400)
            freshness = 1.0 / (1.0 + age_days / 30.0)
            source_type = meta.get("source_type", "text")
            authority = 1.5 if source_type in ("official", "doc") else 1.0
            base_score = item.get("rrf_score", item.get("score", 0))
            item["weighted_score"] = base_score * freshness * authority * (1 + 0.1 * version)
        return sorted(items, key=lambda x: x["weighted_score"], reverse=True)

    def _group_by_stance(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            return []
        groups: list[dict[str, Any]] = []
        used: set[int] = set()
        for i, item in enumerate(items):
            if i in used:
                continue
            group = {"items": [item], "max_weighted_score": item.get("weighted_score", 0)}
            used.add(i)
            for j, other in enumerate(items):
                if j in used or j == i:
                    continue
                similarity = self._text_similarity(item["content"], other["content"])
                if similarity < 0.3:
                    group["items"].append(other)
                    used.add(j)
            groups.append(group)
        return groups

    def _text_similarity(self, a: str, b: str) -> float:
        set_a = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', a.lower()))
        set_b = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', b.lower()))
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / max(len(set_a | set_b), 1)

    def format_table_input(
        self,
        table_content: str,
        max_rows: int = 5,
    ) -> dict[str, Any]:
        """
        R11: 大表格分治喂入
        - 先给表头+摘要确认有效性
        - 按需抽选相关行列
        """
        rows = table_content.strip().split("\n")
        if len(rows) <= max_rows + 1:
            return {"preview": table_content, "full_available": True, "row_count": len(rows)}
        header = rows[0]
        preview_rows = rows[:max_rows + 1]
        preview = "\n".join(preview_rows)
        return {
            "preview": preview,
            "full_available": False,
            "row_count": len(rows),
            "instruction": f"表格共{len(rows)}行，已提供前{max_rows}行预览+表头，按需请求更多行",
        }

    def make_image_placeholder(self, image_desc: str, placeholder_id: str = "") -> str:
        """
        R11: 插图占位标记
        - 模型仅识别占位标记
        - 前端后续替换为真实图片
        """
        pid = placeholder_id or f"img_{abs(hash(image_desc)) % 10000:04d}"
        return f"[插图占位:{pid}|{image_desc}]"

    def validate_output(
        self,
        output: str,
        evidence_ids: list[str],
        evidence_map: dict[str, str],
    ) -> dict[str, Any]:
        """
        R10: 生成闸门 — 输出校验
        - 检查输出是否引用了证据
        - 无证据支撑的声明标记为可疑
        """
        cited_evidence = set()
        for eid in evidence_ids:
            if eid in output:
                cited_evidence.add(eid)
        citation_rate = len(cited_evidence) / max(len(evidence_ids), 1)

        suspicious = citation_rate < 0.2 and len(evidence_ids) > 0

        return {
            "cited_evidence": list(cited_evidence),
            "citation_rate": round(citation_rate, 2),
            "suspicious": suspicious,
            "warning": "输出可能未基于检索证据，存在幻觉风险" if suspicious else "",
        }