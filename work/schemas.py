"""
工作卡片的结果类型（指令 16 第 4、5 节）。

两条卡片的能力都走"大模型联网检索"，回来的是模型给的正文 + 厂商给的结构化来源；
这里把两边拼成界面能直接摆的数据：每条结论后面都要能挂上真实来源，
拿不到来源就老实留空——编链接比不给链接更糟。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from brain.llm.search import SearchSource

# 来源就是厂商联网检索返回的那一条；工作模块不自己造链接
Source = SearchSource


@dataclass(frozen=True)
class Alternative:
    """一条替代料建议（BOM 卡）。"""

    model: str                 # 替代型号
    price: str                 # 参考单价（模型从检索内容里读到的，可能带货币符号）
    vendor: str                # 供应商
    why: str                   # 性价比理由
    source: Source | None

    @property
    def source_text(self) -> str:
        if self.source is None:
            return "未给出处"
        return f"{self.source.site or self.source.title}"


@dataclass(frozen=True)
class AlternativeResult:
    """一行物料找下来的结果。"""

    row_index: int
    item_label: str                       # "M-1001 六角螺丝"
    alternatives: tuple[Alternative, ...] = ()
    notes: str = ""                       # 模型的补充说明（例如"该料为专用件，市面无通用替代"）


@dataclass(frozen=True)
class TrendKeyword:
    word: str
    heat: str          # 热度描述（高/中/低 或 具体数字，模型给什么记什么）
    trend: str         # 趋势（上升/平稳/回落）


@dataclass(frozen=True)
class SellingPoint:
    point: str
    reason: str
    source: Source | None


@dataclass(frozen=True)
class TrendReport:
    """爆品卖点预测的结果（卖点卡）。"""

    category: str
    platform: str
    period: str
    summary: str = ""
    keywords: tuple[TrendKeyword, ...] = ()
    points: tuple[SellingPoint, ...] = ()
    advice: tuple[str, ...] = ()
    sources: tuple[Source, ...] = ()
    fetched_at: datetime = field(default_factory=datetime.now)
