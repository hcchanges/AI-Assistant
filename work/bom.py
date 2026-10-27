"""
BOM 解析（指令 16 第 4 节）：上传 Excel/CSV → 自动识别列头 → 列表展示。

为什么用"关键词打分"而不是固定列名：真实 BOM 的表头千奇百怪
（物料编码 / 料号 / 编码 / Part Number 指的都是同一列），所以给每个标准列配一组关键词，
哪一列命中的关键词最长就认作那一列；认不出来的列不丢，原样留着并出现在导出文件里。

BOM 不预置、也不入库：上传一次算一次（指令 16 第 4 节），文件在内存里过一遍就完。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# 标准列（识别到就按这个顺序展示在列表里，并喂给"找替代"）
CODE, NAME, SPEC, BRAND, QTY, PRICE, VENDOR = (
    "code",
    "name",
    "spec",
    "brand",
    "qty",
    "price",
    "vendor",
)
STANDARD_COLUMNS: tuple[str, ...] = (CODE, NAME, SPEC, BRAND, QTY, PRICE, VENDOR)
COLUMN_TITLES: dict[str, str] = {
    CODE: "编码",
    NAME: "名称",
    SPEC: "规格",
    BRAND: "品牌",
    QTY: "用量",
    PRICE: "单价",
    VENDOR: "供应商",
}

# 每个标准列的关键词（越具体的写在越前面，命中的关键词越长越可信）
HEADER_KEYWORDS: dict[str, tuple[str, ...]] = {
    CODE: ("物料编码", "物料编号", "物料号", "零件号", "料号", "编码", "编号", "partnumber", "partno", "part", "code", "sku"),
    NAME: ("物料名称", "物料描述", "品名", "零件名称", "名称", "description", "desc", "name", "item"),
    SPEC: ("规格型号", "规格", "型号", "spec", "model"),
    BRAND: ("品牌", "厂牌", "制造商", "brand", "mfr", "manufacturer"),
    QTY: ("单机用量", "用量", "数量", "qty", "quantity"),
    PRICE: ("含税单价", "未税单价", "单价", "价格", "unitprice", "price", "cost"),
    VENDOR: ("供应商名称", "供应商", "供货商", "厂商", "vendor", "supplier"),
}

# 表格文件后缀：CSV 用标准库，Excel 用 openpyxl
CSV_SUFFIXES = (".csv", ".txt")
EXCEL_SUFFIXES = (".xlsx", ".xlsm")

# 从单元格里抠数字（"￥1.20" "1.2元" "1,234.5" 都要能读）
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


class BomError(Exception):
    """BOM 文件读不了或没有可用列。"""


@dataclass(frozen=True)
class BomRow:
    index: int                    # 界面上的行号（从 1 起，不含表头）
    values: dict[str, str]        # 标准列 → 单元格文本
    raw: tuple[str, ...]          # 原始整行（导出时原样写回）

    def cell(self, column: str) -> str:
        return self.values.get(column, "")

    @property
    def label(self) -> str:
        """界面上代表这一行的名字："编码 名称"。"""
        parts = [self.cell(CODE), self.cell(NAME)]
        text = " ".join(part for part in parts if part)
        return text or f"第 {self.index} 行"

    def number(self, column: str) -> float | None:
        """这一列当数字读（单价、用量）。读不出来返回 None，别硬当 0。"""
        return parse_number(self.cell(column))


def parse_number(text: str) -> float | None:
    """从一段文本里读出数字（"￥1.20" "1.2元" "1,234.5" 都要能读）；读不出来返回 None。"""
    cleaned = str(text or "").replace("￥", "").replace("¥", "").replace("元", "")
    match = _NUMBER_RE.search(cleaned)
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


@dataclass(frozen=True)
class BomTable:
    filename: str
    headers: tuple[str, ...]
    columns: dict[str, int]       # 标准列 → 表头下标（没识别到的列不在里面）
    rows: tuple[BomRow, ...]

    @property
    def recognized(self) -> tuple[str, ...]:
        return tuple(column for column in STANDARD_COLUMNS if column in self.columns)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(column for column in STANDARD_COLUMNS if column not in self.columns)

    @property
    def amount(self) -> float:
        """整张 BOM 的金额（单价 × 用量，缺哪个都按 1 算，用来给"节省比例"当分母）。"""
        total = 0.0
        for row in self.rows:
            price = row.number(PRICE)
            if price is None:
                continue
            total += price * (row.number(QTY) or 1.0)
        return total


def _normalize(text: str) -> str:
    """表头归一化：去空格、去常见分隔符、转小写，中文原样留着。"""
    return re.sub(r"[\s（）()\[\]【】:：\-_/]", "", str(text or "")).lower()


def recognize_columns(headers: tuple[str, ...]) -> dict[str, int]:
    """表头 → 标准列下标。同一个表头只许认给一个标准列（按命中关键词长度优先）。"""
    scores: list[tuple[int, str, int]] = []
    for index, header in enumerate(headers):
        normalized = _normalize(header)
        if not normalized:
            continue
        for column, keywords in HEADER_KEYWORDS.items():
            for keyword in keywords:
                if keyword in normalized:
                    scores.append((len(keyword), column, index))
                    break
    scores.sort(reverse=True)
    columns: dict[str, int] = {}
    used_index: set[int] = set()
    for _score, column, index in scores:
        if column in columns or index in used_index:
            continue
        columns[column] = index
        used_index.add(index)
    return columns


def _read_csv(path: Path) -> list[list[str]]:
    """读 CSV。中文 Windows 导出的 CSV 常是 GBK，所以先按 UTF-8 读、报编码错再用 GBK 读。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise BomError(f"{path.name} 的编码认不出来（试过 UTF-8 与 GBK），请另存为 UTF-8 的 CSV")
    return [row for row in csv.reader(text.splitlines()) if any(cell.strip() for cell in row)]


def _read_excel(path: Path) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # 依赖没装好时说清楚，而不是丢一句 ImportError
        raise BomError("读 Excel 需要 openpyxl，请先安装：uv pip install openpyxl") from exc

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows: list[list[str]] = []
        for raw_row in sheet.iter_rows(values_only=True):
            cells = ["" if cell is None else str(cell).strip() for cell in raw_row]
            if any(cells):
                rows.append(cells)
        return rows
    finally:
        workbook.close()


def parse_bom(path: str | Path) -> BomTable:
    """读一个 BOM 文件（CSV / Excel），识别列头并整理成行。"""
    file_path = Path(path)
    if not file_path.exists():
        raise BomError(f"找不到文件：{file_path}")
    suffix = file_path.suffix.lower()
    if suffix in CSV_SUFFIXES:
        table = _read_csv(file_path)
    elif suffix in EXCEL_SUFFIXES:
        table = _read_excel(file_path)
    else:
        raise BomError(f"只认 CSV 与 Excel（xlsx），这个打不开：{file_path.name}")
    if len(table) < 2:
        raise BomError(f"{file_path.name} 里只有表头或根本没有内容")

    headers = tuple(cell.strip() for cell in table[0])
    columns = recognize_columns(headers)
    if CODE not in columns and NAME not in columns:
        raise BomError(
            f"没认出物料编码或名称这两列，表头是：{'、'.join(headers)}。"
            "请把表头写成「物料编码 / 物料名称 / 规格 / 单价 / 用量 / 供应商」这类常见叫法"
        )

    rows: list[BomRow] = []
    for number, raw_row in enumerate(table[1:], start=1):
        values = {
            column: (raw_row[index].strip() if index < len(raw_row) else "")
            for column, index in columns.items()
        }
        rows.append(BomRow(index=number, values=values, raw=tuple(raw_row)))

    return BomTable(
        filename=file_path.name,
        headers=headers,
        columns=columns,
        rows=tuple(rows),
    )


def export_csv(rows: list[tuple[BomRow, dict[str, str]]], path: str | Path) -> None:
    """导出优化后的 BOM：原始列 + 替代料编码/单价/预计节省（只写接受过替代的行）。"""
    file_path = Path(path)
    headers = ["行号", "原始行", "替代料编码", "替代料单价", "预计节省"]
    with file_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row, replacement in rows:
            writer.writerow(
                [
                    row.index,
                    " ".join(cell for cell in row.raw if cell),
                    replacement.get("model", ""),
                    replacement.get("price", ""),
                    replacement.get("saving", ""),
                ]
            )
