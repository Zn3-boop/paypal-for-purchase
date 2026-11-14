"""一次性运维脚本：把现有 JSONL 订单/审计数据迁移进 PostgreSQL（幂等、可回滚）。

背景：
  - 存储后端已接入「JSONL + PG 双写」（apps/server/app/services/group_purchase/pg_store.py）。
    本脚本负责把双写接入前积累的 JSONL 历史数据补写进 PG，让 PG 成为完整数据源。
  - 迁移完成后 JSONL 原文件保留（读写路径仍以 JSONL 为兜底），仅做备份，不删除。

幂等性：
  - 订单走 upsert_order（ON CONFLICT (order_id) DO UPDATE）—— 重复跑以最后一次为准。
  - 审计走 insert_audit_if_absent（timestamp+session_id+event+user_id+actor+data 查重）——
    重复跑不会产生重复行。

安全：
  - 迁移前先把 JSONL 目录复制到 backups/jsonl_<时间戳>/（--skip-backup 可跳过）。
  - 坏行（JSON 解析失败 / 缺 order_id 等关键字段）跳过并记 warning，不中断整批。
  - 迁移后用条数对账，不一致会以非零退出码明确报出，不静默吞掉。

用法：
    python scripts/migrate_jsonl_to_pg.py                     # 默认迁移 data/orders + data/audit_logs
    python scripts/migrate_jsonl_to_pg.py --dry-run           # 只盘点 + 预估，不写 PG
    python scripts/migrate_jsonl_to_pg.py --skip-backup       # 已备份过，跳过重复备份

环境变量：DATABASE_URL（未设置时从 apps/server/.env 读取）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 允许从仓库任意位置运行（脚本定位到仓库根，再挂 apps/server 到 sys.path）
REPO_ROOT = Path(__file__).resolve().parent.parent
APPS_SERVER = REPO_ROOT / "apps" / "server"
if str(APPS_SERVER) not in sys.path:
    sys.path.insert(0, str(APPS_SERVER))

from dotenv import load_dotenv  # 项目依赖自带

load_dotenv(APPS_SERVER / ".env", override=False)

from app.services.group_purchase.pg_store import (  # noqa: E402
    ensure_schema,
    insert_audit_if_absent,
    load_audit,
    load_orders,
    pg_enabled,
    upsert_order,
)

# 控制台输出兜底：Windows GBK 控制台无法编码 emoji，强制 UTF-8，
# 避免 print("✅") 在迁移成功末尾抛 UnicodeEncodeError 导致退出码 1。
try:  # pragma: no cover
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_jsonl_to_pg")


# ============================================================
# 配置
# ============================================================

DEFAULT_ORDERS_DIR = APPS_SERVER / "data" / "orders"
DEFAULT_AUDIT_DIR = APPS_SERVER / "data" / "audit_logs"
DEFAULT_BACKUP_ROOT = REPO_ROOT / "backups"


def _check_pg() -> None:
    """PG 不可用直接退出（迁移必须落在真实数据库上）。"""
    if not pg_enabled():
        logger.error("DATABASE_URL 未配置（.env 缺失或未导出），无法迁移到 PG")
        sys.exit(2)
    if not ensure_schema():
        logger.error("PG 建表/连接失败，终止迁移")
        sys.exit(2)


# ============================================================
# 备份
# ============================================================

def backup_jsonl(orders_dir: Path, audit_dir: Path, backup_root: Path) -> Path:
    """把 JSONL 目录复制到 backups/jsonl_<时间戳>/，返回备份根目录。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_root / f"jsonl_{stamp}"
    copied = 0
    for src in (orders_dir, audit_dir):
        if not src.exists():
            continue
        shutil.copytree(src, dest / src.name)
        copied += 1
        logger.info("backup: %s -> %s", src, dest / src.name)
    logger.info("备份完成：%s（%d 个目录）", dest, copied)
    return dest


# ============================================================
# 盘点（dry-run 与迁移共用）
# ============================================================

def scan_orders(order_file: Path) -> tuple[list[dict], int]:
    """扫描订单 JSONL：返回 (有效行 dict 列表, 坏行数)。追加写含重复 order_id。"""
    rows: list[dict] = []
    bad = 0
    if not order_file.exists():
        return rows, bad
    with open(order_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                logger.warning("订单坏行（JSON 解析失败）已跳过: %s", line[:80])
                continue
            if not data.get("order_id"):
                bad += 1
                logger.warning("订单坏行（缺 order_id）已跳过: %s", line[:80])
                continue
            rows.append(data)
    return rows, bad


def scan_audit(audit_dir: Path) -> tuple[list[dict], int]:
    """扫描审计 JSONL（按用户目录 audit_*.jsonl）：返回 (有效行, 坏行数)。"""
    rows: list[dict] = []
    bad = 0
    if not audit_dir.exists():
        return rows, bad
    for user_dir in sorted(audit_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        for log_file in sorted(user_dir.glob("audit_*.jsonl")):
            with open(log_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        bad += 1
                        continue
                    if not data.get("timestamp") or not data.get("event"):
                        bad += 1
                        continue
                    rows.append(data)
    return rows, bad


def _pg_counts() -> tuple[int, int]:
    """返回 PG 当前 (orders 行数, audit_events 行数)。"""
    return len(load_orders()), len(load_audit(limit=10**9))


# ============================================================
# 迁移
# ============================================================

def migrate(orders_rows: list[dict], audit_rows: list[dict]) -> tuple[int, int, int, int]:
    """写入 PG：订单 upsert、审计查重插入。

    返回 (订单成功条数, 审计插入, 审计已存在跳过, 审计失败)。
    订单是 upsert：JSONL 追加写行序 = 时间序，逐行覆盖后最后一行（最新状态）胜出。
    """
    ok_orders = 0
    inserted = 0
    exists = 0
    failed = 0
    for row in orders_rows:
        if upsert_order(row):
            ok_orders += 1
    for row in audit_rows:
        result = insert_audit_if_absent(row)
        if result == "inserted":
            inserted += 1
        elif result == "exists":
            exists += 1
        else:
            failed += 1
    return ok_orders, inserted, exists, failed


def verify(
    orders_rows: list[dict],
    audit_rows: list[dict],
    ok_orders: int,
    inserted: int,
    exists: int,
    failed: int,
    orders_bad: int,
    audit_bad: int,
) -> bool:
    """对账（幂等安全口径，重跑不误报）。

      - 订单：逐行 upsert 全部成功（835 行含同一 order_id 的历史状态行，
        唯一订单 186 个），且 PG 中每个 order_id 的 status == JSONL 最后一行 status
        （追加写行序=时间序，最后一行即最新状态）—— 订单去了重、状态落在最终态。
      - 审计：插入 + 已存在跳过 == JSONL 行数，且失败 == 0。
      用「插入 + 已存在」而非「PG 增量」：幂等重跑时增量为 0 但数据已就位，
      用增量对账会把第二跑误判为失败。
    """
    unique_order_ids = len({r["order_id"] for r in orders_rows})

    # 订单状态一致性：JSONL 最后一行（最新状态）vs PG 落库 status
    last_by_id: dict[str, dict] = {}
    for row in orders_rows:
        last_by_id[row["order_id"]] = row  # 追加写：后出现的行为准
    pg_orders = {r["order_id"]: r["status"] for r in load_orders()}
    mismatch = 0
    for oid, last_row in last_by_id.items():
        if pg_orders.get(oid) != last_row.get("status"):
            mismatch += 1
            if mismatch <= 5:
                logger.error(
                    "订单状态不一致：%s JSONL=%s PG=%s",
                    oid, last_row.get("status"), pg_orders.get(oid),
                )

    pg_orders_after, pg_audit_after = _pg_counts()
    print("\n===== 对账结果 =====")
    print(
        f"订单：JSONL 唯一 order_id = {unique_order_ids}，"
        f"upsert 成功 = {ok_orders}（PG 当前共 {pg_orders_after}）"
    )
    print(
        f"审计：JSONL 行数 = {len(audit_rows)}，"
        f"插入 = {inserted}，已存在跳过 = {exists}，失败 = {failed}（PG 当前共 {pg_audit_after}）"
    )
    print(f"坏行：订单 = {orders_bad}，审计 = {audit_bad}")

    ok = True
    # 订单：835 行是按追加写逐行 upsert（含同一 order_id 的历史状态行），
    # 成功数应等于 JSONL 行数；唯一订单数 186 由上方 status 一致性检查兜底。
    if ok_orders != len(orders_rows):
        logger.error(
            "订单写入未全覆盖：upsert 成功 %d != JSONL 行数 %d",
            ok_orders, len(orders_rows),
        )
        ok = False
    if mismatch:
        logger.error("订单状态一致性：%d 个 order_id 的 PG status 与 JSONL 最新行不一致", mismatch)
        ok = False
    if inserted + exists != len(audit_rows):
        logger.error(
            "审计对账不一致：插入 %d + 已存在 %d != JSONL 行数 %d",
            inserted, exists, len(audit_rows),
        )
        ok = False
    if failed:
        logger.error("审计写入失败 %d 条（PG 不可用或异常）", failed)
        ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="把 JSONL 订单/审计历史迁移进 PostgreSQL（幂等、可回滚）")
    parser.add_argument("--orders-dir", type=Path, default=DEFAULT_ORDERS_DIR)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="只盘点 + 预估，不写 PG、不备份")
    parser.add_argument("--skip-backup", action="store_true", help="跳过备份（已备份过时用）")
    parser.add_argument("--no-verify", action="store_true", help="迁移后跳过条数对账")
    args = parser.parse_args()

    _check_pg()

    orders_file = args.orders_dir / "orders.jsonl"
    orders_rows, orders_bad = scan_orders(orders_file)
    audit_rows, audit_bad = scan_audit(args.audit_dir)
    logger.info(
        "盘点：订单 %d 行（坏行 %d，唯一 order_id %d），审计 %d 行（坏行 %d）",
        len(orders_rows), orders_bad, len({r["order_id"] for r in orders_rows}),
        len(audit_rows), audit_bad,
    )

    if args.dry_run:
        print("\n[dry-run] 未备份、未写 PG。上面是预估数据。")
        return 0

    if not args.skip_backup:
        backup_jsonl(args.orders_dir, args.audit_dir, args.backup_root)
    else:
        logger.info("跳过备份")

    pg_orders_before, pg_audit_before = _pg_counts()
    logger.info("迁移前 PG：orders=%d, audit_events=%d", pg_orders_before, pg_audit_before)

    ok_orders, inserted, exists, failed = migrate(orders_rows, audit_rows)
    logger.info(
        "迁移完成：订单 %d/%d，审计 插入 %d / 已存在 %d / 失败 %d",
        ok_orders, len(orders_rows), inserted, exists, failed,
    )

    if not args.no_verify:
        consistent = verify(
            orders_rows, audit_rows,
            ok_orders, inserted, exists, failed,
            orders_bad, audit_bad,
        )
        if not consistent:
            logger.error("对账未通过：请检查上方不一致项（JSONL 原文件与备份仍在，可回滚重来）")
            return 1

    print("\n迁移完成 ✅ JSONL 原文件保留（备份副本见 backups/），PG 已可查询历史数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())