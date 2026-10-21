"""服务启动入口：PC / AutoDL 通用，不硬编码镜像路径。

    python -m service.run_server
    HOST=0.0.0.0 PORT=8000 DATE_MODEL_DIR=/root/autodl-tmp/models python -m service.run_server

依赖只有标准库也够跑；装了 FastAPI 时 `service.api.create_fastapi_app()` 另给 ASGI 入口。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.api import DEFAULT_HOST, DEFAULT_PORT, InferenceService, serve   # noqa: E402
from service.models import load_models                                       # noqa: E402

logger = logging.getLogger("service.run_server")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.environ.get("HOST", DEFAULT_HOST)
    try:
        port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    except ValueError:
        logger.error("PORT 不是整数：%r", os.environ.get("PORT"))
        return 2
    models = load_models(os.environ.get("DATE_MODEL_DIR", ""))
    logger.info("模型加载入口：%s", models.describe())
    serve(InferenceService(), host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
