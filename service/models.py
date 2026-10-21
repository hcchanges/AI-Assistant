"""模型加载入口（v2-cloud-service 先留壳，③④两批接真实模型）。

本批**不加载任何权重**：`load_models()` 只把目录记下来、返回一个空 bundle。这样做是
为了先把"模型从哪来、谁负责加载"这件事固定在一个入口上——③接 pose、④接 ASR 时只改
这里的实现，HTTP 层与轮询方一行不用动。

模型目录用环境变量 `DATE_MODEL_DIR` 指定（AutoDL 上落到数据盘），不硬编码镜像路径。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ModelBundle:
    """一次进程生命周期内加载的模型。空 = 本批还没接。"""

    model_dir: Path | None = None
    pose: Any = None
    asr: Any = None

    @property
    def loaded(self) -> bool:
        return self.pose is not None or self.asr is not None

    def describe(self) -> dict[str, Any]:
        return {
            "model_dir": str(self.model_dir) if self.model_dir else "",
            "pose": self.pose is not None,
            "asr": self.asr is not None,
        }


def load_models(model_dir: str | Path | None = None) -> ModelBundle:
    """加载模型。本批只登记目录，不导入任何重型依赖。

    TODO(v2-pose-local)：`model_dir` 下找 pose 权重并加载；
    TODO(v2-asr-local)：`model_dir` 下找 ASR 权重并加载。
    """
    path = Path(model_dir) if model_dir else None
    if path is not None and not path.exists():
        logger.info("模型目录不存在，先按空 bundle 跑：%s", path)
        path = None
    return ModelBundle(model_dir=path)


__all__ = ["ModelBundle", "load_models"]
