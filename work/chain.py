"""
工作卡片的大模型链（公开版占位）。

内部版这里把 BOM 替代料、爆品卖点、结果点评三段提示词接成链，走联网检索。
公开仓既不联网也不产内容：同名方法都在，调用一律抛 `WorkLocked`——装作"返回空结果"
更糟，那会让上游以为真跑过了。
"""

from __future__ import annotations

from typing import Any

EMPTY_CELL = "（未提供）"


class WorkLocked(Exception):
    """公开版的工作卡功能是锁着的（需要内部授权版）。"""


class WorkChain:
    """只保留构造签名，方法全部不做。"""

    def __init__(self, config: Any = None):
        self._config = config

    def alternatives(self, row: Any):
        raise WorkLocked("BOM 找替代料需要内部授权版。")

    def trends(self, category: str, platform: str, period: str):
        raise WorkLocked("爆品卖点预测需要内部授权版。")

    def comment(self, kind: str, summary: str) -> str:
        raise WorkLocked("工作卡结果点评需要内部授权版。")
