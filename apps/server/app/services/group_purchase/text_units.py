"""中文约束文本的切分原语 —— 抽取层与判定层共用的**唯一**连词规则。

为什么单独成模块（2026-11-10 全量审计 P0-4）：
「和 / 与 / 跟 / 以及」被两处各自实现过一遍——
  · 抽取层 `app/api/constraints.py::_split_clauses` 用 `str.replace("跟", "，")`
  · 判定层 `satisfaction._keywords` 用 `re.split(r"和|与|跟|以及")`
两处都把**词素**当连词切碎：`低跟` → `低` + `跟`（抽取侧整条约束静默消失，
判定侧关键词退化成「低」）。修左边、右边还会各自漂移，所以规则只能有一份。

规则：只有当连词**两侧都是内容字符**（汉字/字母/数字）时才算连接词。
「深棕色，低跟」的「跟」右侧是标点 → 不切；「鼠标和键盘」两侧都是汉字 → 切。
"""
from __future__ import annotations

import re

# 长连词在前：`以及` 先匹配，避免将来加入含短连词的长词时被抢先切开。
CONJUNCTION_RE = re.compile(
    r"(?<=[\u4e00-\u9fffA-Za-z0-9])(?:以及|和|与|跟)(?=[\u4e00-\u9fffA-Za-z0-9])"
)


def split_on_conjunctions(text: str) -> list[str]:
    """按并列连词切分文本；词素位置（低跟 / 面料柔和）不切。"""
    return CONJUNCTION_RE.split(text or "")
