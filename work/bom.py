"""
BOM 表的读写（公开版占位）。

内部版在这里做三件事：认表头（中英文别名都可）、把 CSV / Excel 读成一行行、把选好的
替代料导回去。公开仓只留下类型与签名——**不读文件、不认列、不导出**，用到就抛
`BomError`，免得有人以为公开版还能跑通那条路。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 从单元格里抠数字（"￥1.20" "1.2元" "1,234.5" 都要能读）
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")

# 标准列名（内部版：编码 / 名称 / 规格 / 品牌 / 单机用量 / 含税单价 / 供应商）
CODE = "编码"
NAME = "名称"
SPEC = "规格"
BRAND = "品牌"
QTY = "用量"
PRICE = "单价"
VENDOR = "供应商"

STANDARD_COLUMNS = (CODE, NAME, SPEC, BRAND, QTY, PRICE, VENDOR)

# 表头别名（内部版用它认列；这里只留形状）
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    CODE: ("物料编码", "编码", "料号", "code", "partno"),
    NAME: ("物料名称", "名称", "品名", "name"),
    SPEC: ("规格型号", "规格", "型号", "spec"),
    BRAND: ("品牌", "厂牌", "brand"),
    QTY: ("单机用量", "用量", "数量", "qty", "quantity"),
    PRICE: ("含税单价", "未税单价", "单价", "价格", "unitprice", "price", "cost"),
    VENDOR: ("供应商名称", "供应商", "供货商", "厂商", "vendor", "supplier"),
}

CSV_SUFFIXES = (".csv", ".txt")
EXCEL_SUFFIXES = (".xlsx", ".xlsm")


class BomError(Exception):
    """BOM 文件读不了 / 没有可用列 / 公开版不做这件事。"""


@dataclass(frozen=True)
class BomRow:
    index: int
    values: dict[str, str]
    raw: tuple[str, ...] = ()

    def cell(self, column: str) -> str:
        return self.values.get(column, "")

    @property
    def label(self) -> str:
        parts = [self.cell(CODE), self.cell(NAME)]
        text = " ".join(part for part in parts if part)
        return text or f"第 {self.index} 行"

    def number(self, column: str) -> float | None:
        return parse_number(self.cell(column))


@dataclass(frozen=True)
class BomTable:
    filename: str
    headers: tuple[str, ...]
    columns: dict[str, int]
    rows: tuple[BomRow, ...]

    @property
    def recognized(self) -> tuple[str, ...]:
        return tuple(column for column in STANDARD_COLUMNS if column in self.columns)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(column for column in STANDARD_COLUMNS if column not in self.columns)

    @property
    def amount(self) -> float:
        total = 0.0
        for row in self.rows:
            price = row.number(PRICE)
            if price is None:
                continue
            total += price * (row.number(QTY) or 1.0)
        return total


def parse_number(text: str) -> float | None:
    """从一段文本里读出数字（"￥1.20" "1.2元" "1,234.5" 都要能读）；读不出来返回 None。

    公开版留着这只纯函数：它不碰文件也不联网，就是一段文本处理。
    """
    cleaned = str(text or "").replace("￥", "").replace("¥", "").replace("元", "")
    match = _NUMBER_RE.search(cleaned)
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def recognize_columns(headers: tuple[str, ...]) -> dict[str, int]:
    raise BomError("公开版不认 BOM 表头，上传功能需要内部授权版。")


def parse_bom(path: str | Path) -> BomTable:
    raise BomError("公开版不读 BOM 文件，上传功能需要内部授权版。")


def export_csv(rows, path: str | Path) -> None:
    raise BomError("公开版不导出 BOM，导出功能需要内部授权版。")
