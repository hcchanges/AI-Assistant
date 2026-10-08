"""
下载长期记忆用的本地向量模型（bge-small-zh-v1.5，int8 量化版，约 24MB）。

为什么需要它：Chroma 自带的向量模型是英文的，对中文句子几乎等于随机排序，
所以换用中文模型；又因为官方模型源（Amazon S3）在国内常被阻断，
这里走 hf-mirror（HuggingFace 国内镜像）下载，下载后完全离线运行。

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\fetch_embedding_model.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

from core.config import Config

# 项目根目录（tools/ 的上一级）
ROOT_DIR = Path(__file__).resolve().parent.parent

BASE_URL = "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/"


def _target_dir() -> Path:
    """向量模型下载到数据根下的 models/（v2-data-root）。

    读得到 config 就按它的 data_dir/model_path 走；读不了（还没配好）就退回
    项目内 data/models 的老位置。
    """
    try:
        return Config.load().model_dir
    except Exception:
        return ROOT_DIR / "data" / "models" / "bge-small-zh-v1.5"


TARGET_DIR = _target_dir()

# 源文件路径 -> 本地文件名（推理只需要 model.onnx 与 tokenizer.json，其余一并备着）
FILES = {
    "onnx/model_quantized.onnx": "model.onnx",
    "tokenizer.json": "tokenizer.json",
    "config.json": "config.json",
    "tokenizer_config.json": "tokenizer_config.json",
}

MAX_ATTEMPTS = 6


def fetch_file(remote: str, local: str) -> None:
    dest = TARGET_DIR / local
    if dest.exists() and dest.stat().st_size > 0:
        print(f"已存在，跳过：{local}（{dest.stat().st_size} 字节）")
        return

    for attempt in range(MAX_ATTEMPTS):
        try:
            with httpx.stream(
                "GET", BASE_URL + remote, timeout=180, follow_redirects=True
            ) as response:
                response.raise_for_status()
                part = dest.with_name(dest.name + ".part")
                with part.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        handle.write(chunk)
                part.replace(dest)
            print(f"下载完成：{local}（{dest.stat().st_size} 字节）")
            return
        except Exception as exc:  # 网络抖动就重试
            print(f"第 {attempt + 1} 次失败：{local} -> {type(exc).__name__}: {exc}")


def main() -> int:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for remote, local in FILES.items():
        fetch_file(remote, local)

    missing = [name for name in FILES.values() if not (TARGET_DIR / name).exists()]
    if missing:
        print(f"以下文件仍缺失，请重跑本脚本：{missing}")
        return 1
    print(f"模型就绪：{TARGET_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
