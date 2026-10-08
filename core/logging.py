"""日志初始化：同时输出到控制台和按天滚动的文件。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """配置根日志器。log_dir 为 None 时只输出到控制台。"""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "pet.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT,
        handlers=handlers,
        force=True,
    )
    # 第三方库日志降噪
    for noisy in ("httpx", "httpcore", "chromadb", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
