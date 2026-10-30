#!/bin/bash
cd "$(dirname "$0")"

# 加载 .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs -d '\n')
fi

# 确保缓存目录存在
mkdir -p "$HF_HOME"

echo "[start] HF_HOME=$HF_HOME"
echo "[start] HTTP_PROXY=$HTTP_PROXY"
echo "[start] HF_ENDPOINT=$HF_ENDPOINT"

# 启动后端
cd apps/server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
