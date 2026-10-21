#!/usr/bin/env bash
# AutoDL / Linux 通用启动脚本（不绑定 AutoDL 镜像路径）。
# 用法：
#   bash service/start_autodl.sh
#   HOST=0.0.0.0 PORT=8000 DATE_MODEL_DIR=/root/autodl-tmp/models bash service/start_autodl.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8000}"
exec python -m service.run_server
