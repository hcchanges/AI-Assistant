"""
工作卡片的结果类型（公开版占位）。

内部版拿它装"模型给的正文 + 厂商给的结构化来源"：一条结论后面要能挂上真实出处，
拿不到就留空。公开仓不产这些结果，类型只把形状留下（字段名与内部版一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 来源（内部版是厂商联网检索返回的那一条；工作模块自己从不造链接）
Source = Any


@dataclass(frozen=True)
class Alternative:
    """一条替代料建议。"""

    model: str
    price: str
    vendor: str
    why: str
    source: Source | None = None

    @property
    def source_text(self) -> str:
        if self.source is None:
            return "未给出处"
        return f"{getattr(self.source, 'site', '') or getattr(self.source, 'title', '')}"


@dataclass(frozen=True)
class AlternativeResult:
    row_index: int
    item_label: str
    alternatives: tuple[Alternative, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class TrendKeyword:
    word: str
    heat: str
    trend: str


@dataclass(frozen=True)
class SellingPoint:
    point: str
    reason: str
    source: Source | None = None


@dataclass(frozen=True)
class TrendReport:
    category: str
    platform: str
    period: str
    summary: str = ""
    keywords: tuple[TrendKeyword, ...] = ()
    points: tuple[SellingPoint, ...] = ()
    advice: tuple[str, ...] = ()
    sources: tuple[Source, ...] = ()
    fetched_at: datetime = field(default_factory=datetime.now)
