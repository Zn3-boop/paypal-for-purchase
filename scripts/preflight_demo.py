"""录制前硬门（pre-flight）：以下检查都必须通过才能开始录制。

  1) A/B 不能是两个相同的方案：B 的实现是「A 的逐件可辩护代换」，
     候选池来自 Channel3 —— Channel3 挂了，B 会退化成 A 的复制品，整个对比叙事塌掉；
     因此「B ≠ A」比「推荐区有卡」更硬，只过第二条不能开录。
  2) 替换必须可辩护：selection_mode_b=substitution 时 upgrades 非空且每项 tier ≤ T3；
     selection_mode_b=relevance_reselect 时 upgrades 必须为空（B 只是同池重排），
     这时不再把「无升级」当硬伤，而是要求 A≠B 并把它作为一句现场话术。
  3) 推荐泳道必须是 status=ok、正好 3 张卡，且每张卡都有 reason_code
     （纯函数判定，评委可复算）；query_used 里不得混进别的会话的检索词。

  4) 目标端口只允许「一个」监听进程。双实例是录制事故的头号成因：
     Vite 代理只指向一个端口，另一个进程占着 8001 继续用旧代码，
     表面上一切正常，实际拍到的是旧构建。先 kill 到只剩一个再开录。

  用法（在提交一次需求、选过一个方案之后运行）：
      python scripts/preflight_demo.py --session-id <id> [--base http://127.0.0.1:8000]
  退出码 0 = 全部通过；1 = 有硬伤。只读，不写任何数据。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _listening_pids(base: str) -> list[int] | None:
    """返回监听该端口的 PID 列表；平台不支持/探测失败时返回 None（降级跳过）。

    Windows: Get-NetTCPConnection -State Listen
    macOS  : lsof -iTCP:<port> -sTCP:LISTEN -t
    Linux  : ss -ltnp 'sport = :<port>'（解析 pid=<n>）
    """
    parsed = urlparse(base)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                    f"-ErrorAction SilentlyContinue).OwningProcess",
                ],
                capture_output=True, text=True, timeout=20,
            ).stdout
            return sorted({int(t) for t in out.split() if t.isdigit() and int(t) > 0})
        if sys.platform == "darwin":
            out = subprocess.run(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=20,
            ).stdout
            return sorted({int(t) for t in out.split() if t.isdigit() and int(t) > 0})
        out = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return sorted({int(m) for m in re.findall(r"pid=(\d+)", out)})
    except (OSError, subprocess.SubprocessError):
        return None


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    ok: list[str] = []
    fails: list[str] = []

    # 检查 0：端口单实例。放在最前——双实例时，后面的检查结果可能来自旧代码。
    pids = _listening_pids(args.base)
    if pids is None:
        ok.append("端口监听检查跳过（当前平台无法枚举 PID）")
    elif len(pids) == 1:
        ok.append(f"端口只被一个进程监听（PID {pids[0]}），排除旧代码双实例")
    elif not pids:
        print(f"[FAIL] {args.base} 没有进程在监听：先启动单实例 uvicorn。")
        return 1
    else:
        print(f"[FAIL] {args.base} 有 {len(pids)} 个监听进程（PID {pids}）：")
        print("       Vite 只代理其中一个，可能一半镜头跑的是旧代码。先 kill 到只剩一个。")
        return 1

    try:
        plans = _get(args.base, f"/api/v1/plans?session_id={args.session_id}")
    except urllib.error.HTTPError as e:
        print(f"[FAIL] /api/v1/plans -> HTTP {e.code}（会话不存在？先提交一次需求）")
        return 1

    # /plans 返回 plans 数组（plan_id=A/B）；兼容旧结构的 plan_a / plan_b 字段。
    by_id = {
        p.get("plan_id"): p
        for p in (plans.get("plans") or [])
        if isinstance(p, dict)
    }
    plan_a = by_id.get("A") or plans.get("plan_a") or {}
    plan_b = by_id.get("B") or plans.get("plan_b") or {}
    items_a = plan_a.get("items") or []
    items_b = plan_b.get("items") or []
    criteria = (plans.get("comparison") or {}).get("criteria") or {}
    mode_b = criteria.get("selection_mode_b")
    upgrades = (plans.get("comparison") or {}).get("upgrades") or []

    if not items_a or not items_b:
        fails.append("方案没生成（plan_a/plan_b.items 为空）：先提交需求并生成 A/B。")
    else:
        ids_a = [i.get("id") for i in items_a]
        ids_b = [i.get("id") for i in items_b]
        if ids_a == ids_b:
            fails.append(
                "A 与 B 是同一套商品：没有可辩护的同款代换，镜头里 B 会像 A 的复制品。"
            )
        else:
            ok.append(
                f"A 与 B 确实不同（A {len(items_a)} 件 / B {len(items_b)} 件，"
                f"selection_mode_b={mode_b or 'unknown'}）"
            )

    if mode_b in ("substitution", "independent"):
        if upgrades:
            bad = [u for u in upgrades if (u.get("tier") if u.get("tier") is not None else 9) > 3]
            if bad:
                fails.append(
                    f"有 {len(bad)} 项替换的 tier > T3（超出可辩护范围）："
                    + "; ".join(f"{u.get('item')} → {u.get('to_item')} (T{u.get('tier')})" for u in bad[:3])
                )
            else:
                ok.append(
                    f"逐件 diff 可辩护（{len(upgrades)} 项替换 ≤ T3，合计 "
                    f"${sum(u.get('delta') or 0 for u in upgrades):.2f}）"
                )
        elif mode_b == "substitution":
            fails.append(
                "selection_mode_b=substitution 但 upgrades 为空：逐件 diff 讲不出来，"
                "说明替换被硬门挡掉了却仍声明在做替换。"
            )
        else:
            ok.append(
                "B 为同池独立选品（independent），本会话没有可辩护升级："
                "现场只讲「A≠B 来自目标函数不同」，不要讲升级"
            )
    elif mode_b == "relevance_reselect":
        if upgrades:
            fails.append(
                "selection_mode_b=relevance_reselect 却给出了 upgrades："
                "这是把「同池重排」包装成「升级」，属于不可辩护叙事。"
            )
        else:
            ok.append(
                "B 为同池按匹配分重排（relevance_reselect）：现场话术用「交集为 0 是数据结论」，"
                "不要讲升级"
            )
    else:
        fails.append(f"selection_mode_b 缺失或未知（{mode_b!r}）：无法判断 B 是否在做替换。")

    try:
        recs = _get(args.base, f"/api/v1/recommendations?session_id={args.session_id}&limit=3")
    except urllib.error.HTTPError as e:
        print(f"[FAIL] /api/v1/recommendations -> HTTP {e.code}")
        return 1

    status = (recs.get("channel3") or {}).get("status")
    shown = int(recs.get("shown") or 0)
    items = recs.get("items") or []
    no_code = [c.get("name") for c in items if not c.get("reason_code")]
    query_used = str(recs.get("query_used") or "")
    if status != "ok":
        detail = (recs.get("channel3") or {}).get("message") or ""
        fails.append(
            f"推荐泳道状态为 {status}（不是 ok）：Channel3 不可用，B 也可能跟着退化。"
            + (f" 上游信息：{detail}" if detail else "")
            + "（本机常见原因：DNS/网络不可达 —— 先确认能解析赞助商 API）"
        )
    elif shown != 3:
        fails.append(f"推荐泳道只展示了 {shown} 张卡（要求正好 3 张）。")
    elif no_code:
        fails.append(f"{len(no_code)} 张卡没有 reason_code（规格 C 判定缺失，无法复算）：{no_code}")
    elif any(c.get("relevance", 0) < 0.25 for c in items):
        fails.append("有卡片 relevance < 0.25（不该通过 MMR 的 floor，疑似候选池被劫持）。")
    else:
        ok.append(
            f"推荐泳道 ok，3 张卡均有 reason_code（查询「{query_used}」→ 命中 "
            f"{recs.get('search_hits')} 件）"
        )

    for line in ok:
        print(f"  [OK] {line}")
    if fails:
        print("===== 硬门未过，不能开录 =====")
        for line in fails:
            print(f"  [FAIL] {line}")
        return 1
    print("===== 全部通过，可以开录 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())