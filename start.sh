#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到可用的 Python 解释器: $PYTHON_BIN" >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo ">>> 创建本地虚拟环境 .venv"
  "$PYTHON_BIN" -m venv .venv
fi

source ".venv/bin/activate"

if ! python - <<'PY' >/dev/null 2>&1
import flask, requests, dotenv
PY
then
  echo ">>> 安装启动依赖"
  pip install -r requirements.txt
fi

echo ">>> 启动 rita2api"
exec python server.py
