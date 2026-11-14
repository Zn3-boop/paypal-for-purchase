"""把历史遗留的仓库根数据目录合并进 `apps/server/data/`（一次性迁移脚本）。

背景 / 为什么需要它
-------------------
后端服务曾经用相对路径 `./data` 当存储根，于是"从哪个目录启动"决定了读写哪一份：

    D:\\ecv-paypal> python -m uvicorn app...   → D:\\ecv-paypal\\data
    D:\\ecv-paypal\\apps\\server> uvicorn ...    → D:\\ecv-paypal\\apps\\server\\data

两个目录都真实存在、都有文件时，最坏的组合是「读 A 写 B」：前端看到的订单没了、
TTL 明明改过却"不生效"、审计日志时间轴缺一段 —— 全部是目录归属问题，不是逻辑问题。
`app/paths.py` 把默认锚点统一成 `apps/server/data`（`__file__` 是唯一与 cwd 无关的锚点），
本脚本负责把散落在旧目录里的数据搬进来，让"改完锚点"不会以丢数据为代价。

合并策略（宁可留下重复，也绝不丢一条事实）
------------------------------------------
- JSONL（orders / audit_logs / search_memory）：按**整行精确去重**做并集。重复行没有副作用
  （读路径本身就按 id 去重），但丢行不可恢复。
- 会话 JSON（sessions，工作台状态，本来就允许过期重建）：源文件更新的覆盖目标。
- graph memory_store（按用户的目录树）：逐文件补齐，已存在的不覆盖。
- 迁移前把 `apps/server/data` 整体复制到 `apps/server/data.bak-<时间戳>`。
- **不清空**旧目录：验证通过后由人决定什么时候删。

用法：
    python scripts/merge_data_dirs.py            # 演练 + 实际执行
    python scripts/merge_data_dirs.py --dry-run  # 只看会发生什么
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_ROOT = REPO_ROOT / "apps" / "server" / "data"
SOURCES = {
    "sessions": REPO_ROOT / "data" / "sessions",
    "orders": REPO_ROOT / "data" / "orders",
    "audit_logs": REPO_ROOT / "data" / "audit_logs",
    "search_memory": REPO_ROOT / "data" / "search_memory",
    "memory_store": REPO_ROOT / "memory_store",
}
JSONL_DIRS = {"orders", "audit_logs", "search_memory"}


def _union_jsonl(src: Path, dst: Path, *, dry_run: bool) -> tuple[int, int]:
    """把 src 的行并入 dst，返回 (源行数, 新增行数)。"""
    src_lines = [ln for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()]
    dst_lines = [ln for ln in dst.read_text(encoding="utf-8").splitlines() if ln.strip()] if dst.exists() else []
    seen = set(dst_lines)
    added = [ln for ln in src_lines if ln not in seen]
    if added and not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("\n".join(dst_lines + added) + "\n", encoding="utf-8")
    return len(src_lines), len(added)


def _merge_sessions(src: Path, dst: Path, *, dry_run: bool) -> tuple[int, int]:
    """会话按 mtime 取新；返回 (源文件数, 覆盖数)。"""
    dst.mkdir(parents=True, exist_ok=True)
    total = overwritten = 0
    for path in sorted(src.glob("*.json")):
        total += 1
        target = dst / path.name
        if target.exists() and target.stat().st_mtime >= path.stat().st_mtime:
            continue
        overwritten += 1
        if not dry_run:
            shutil.copy2(path, target)
    return total, overwritten


def _merge_tree(src: Path, dst: Path, *, dry_run: bool) -> tuple[int, int]:
    """目录树逐文件补齐（不覆盖已有文件）。返回 (源文件数, 复制数)。"""
    total = copied = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        total += 1
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        copied += 1
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return total, copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    args = parser.parse_args()

    if not any(src.exists() for src in SOURCES.values()):
        print(f"没有可合并的旧目录（检查过：{', '.join(str(s) for s in SOURCES.values())}）")
        return 0

    if not args.dry_run:
        backup = TARGET_ROOT.with_name(f"data.bak-{datetime.now():%Y%m%d-%H%M%S}")
        if TARGET_ROOT.exists():
            shutil.copytree(TARGET_ROOT, backup)
            print(f"已备份现有数据目录 → {backup}")

    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    for name, src in SOURCES.items():
        if not src.exists():
            continue
        dst = TARGET_ROOT / name
        if name == "sessions":
            src_n, acted = _merge_sessions(src, dst, dry_run=args.dry_run)
            verb = "覆盖"
        elif name in JSONL_DIRS:
            # 递归处理：audit_logs 是 <user>/audit_<date>.jsonl 两层结构，
            # 按**相对路径**对齐合并，才不会把 user 目录合并丢。
            files = sorted(p for p in src.rglob("*") if p.is_file())
            src_n = acted = 0
            for path in files:
                n, added = _union_jsonl(path, dst / path.relative_to(src), dry_run=args.dry_run)
                src_n += n
                acted += added
            verb = "行新增"
        else:
            src_n, acted = _merge_tree(src, dst, dry_run=args.dry_run)
            verb = "文件复制"
        print(f"{name:<13} 源 {src_n:>5}  {verb} {acted:>5}  → {dst}")

    print("\n完成。" + ("（dry-run，未写盘）" if args.dry_run else "旧目录保留未删，确认无误后自行移除。"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
