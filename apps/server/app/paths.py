"""统一的本地数据目录锚点 —— 不依赖进程的工作目录（cwd）。

为什么必须有这个文件：所有 store 默认写成 `./data/...` 时，实际路径取决于
**从哪里启动 uvicorn**：

    从 D:\\ecv-paypal           启动 → D:\\ecv-paypal\\data\\sessions
    从 D:\\ecv-paypal\\apps\\server 启动 → ...\\apps\\server\\data\\sessions

两个目录都存在、都有旧文件时，会出现「你去看 A，后端在读 B」的幽灵 bug：
表象是 TTL 没生效、订单查不到、审计缺记录，实际是根本没读到同一份数据。

`__file__` 是唯一与 cwd 无关的锚点：无论从哪启动，都指向 `apps/server/data/`。
环境变量仍然优先（部署可挂载卷 / 测试用 tmp_path）。
"""
from __future__ import annotations

import os
from pathlib import Path

# app/paths.py → app/ → apps/server
SERVER_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = SERVER_ROOT / "data"


def data_dir(env_var: str, default_name: str) -> Path:
    """返回数据子目录：`<env_var>` 优先，否则 `<apps/server>/data/<default_name>`。

    绝对路径的环境变量值原样使用（部署/测试）；相对值按 SERVER_ROOT 解释，
    这样 `SESSION_STORE_DIR=data/sessions` 也不会随 cwd 漂移。
    """
    raw = os.getenv(env_var)
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (SERVER_ROOT / path)
    return DATA_ROOT / default_name
