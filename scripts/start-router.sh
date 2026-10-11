#!/bin/sh
set -eu

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -x ".venv/bin/python" ]; then
    echo "缺少项目 Python 环境，请先在项目目录运行 uv sync --extra dev --frozen。" >&2
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "缺少 .env，请先运行 .venv/bin/python scripts/init_env.py。" >&2
    exit 1
fi

echo "启动 Smart Router：http://127.0.0.1:4000"
echo "实际模型和推理程度会显示在 route 日志中；按 Ctrl+C 停止。"

exec .venv/bin/python - <<'PY'
import logging

import uvicorn

logger = logging.getLogger("server.app")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(handler)
logger.propagate = False

uvicorn.run("server.app:app", host="127.0.0.1", port=4000, workers=1)
PY
