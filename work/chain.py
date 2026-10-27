"""
工作模块的两条 AI 能力（指令 16 第 4、5 节）。

为什么这条链放在工作层而不是 brain/chains：
它的输入输出都是工作域的类型（BOM 的一行、替代料结果、卖点报告），
而 brain 在业务层**下面**，不能反过来依赖工作模块。跟 life/cards/service.py
自己拼 system、只把"调模型"这一步交给 brain 是同一个道理。

两类处理：
1. 提示词在 brain/prompts/templates.py（跟其他提示词放一起，方便对照着改）；
2. 联网检索与来源按编号回填在这里：模型只给 source_index，真链接一律取厂商返回的
   search_info。拿不到就留空——编链接比不给链接更糟。
"""

from __future__ import annotations

import logging

from brain.llm.base import parse_json_object
from brain.llm.factory import LLMFactory
from brain.llm.search import build_query
from brain.prompts.templates import (
    WORK_ALTERNATIVE_PROMPT,
    WORK_ALTERNATIVE_SYSTEM,
    WORK_COMMENT_PROMPT,
    WORK_TREND_PROMPT,
    WORK_TREND_SYSTEM,
)
from core.config import Config
from pet.persona import persona_style_layer
from work.bom import BRAND, CODE, NAME, PRICE, QTY, SPEC, VENDOR, BomRow
from work.schemas import (
    Alternative,
    AlternativeResult,
    SellingPoint,
    TrendKeyword,
    TrendReport,
)

logger = logging.getLogger(__name__)

# 这行物料本来就没填的列，给模型一个明确说法，免得它把空字符串当"无"
EMPTY_CELL = "（未提供）"


class WorkChain:
    def __init__(self, config: Config):
        self._config = config

    # ---------- BOM 卡：逐行找替代料 ----------

    def alternatives(self, row: BomRow) -> AlternativeResult:
        prompt = WORK_ALTERNATIVE_PROMPT.format(
            code=row.cell(CODE) or EMPTY_CELL,
            name=row.cell(NAME) or EMPTY_CELL,
            spec=row.cell(SPEC) or EMPTY_CELL,
            brand=row.cell(BRAND) or EMPTY_CELL,
            qty=row.cell(QTY) or EMPTY_CELL,
            price=row.cell(PRICE) or EMPTY_CELL,
            vendor=row.cell(VENDOR) or EMPTY_CELL,
        )
        answer = LLMFactory.create_search(self._config).ask(
            prompt,
            WORK_ALTERNATIVE_SYSTEM,
            search_query=build_query(
                row.cell(NAME), row.cell(SPEC), row.cell(BRAND), "替代料", "价格", "供应商"
            ),
        )
        data = parse_json_object(answer.text)
        by_index = {source.index: source for source in answer.sources}

        items: list[Alternative] = []
        for raw in data.get("alternatives") or []:
            if not isinstance(raw, dict):
                continue
            model = str(raw.get("model") or "").strip()
            if not model:
                continue
            items.append(
                Alternative(
                    model=model,
                    price=str(raw.get("price") or "").strip(),
                    vendor=str(raw.get("vendor") or "").strip(),
                    why=str(raw.get("why") or "").strip(),
                    source=by_index.get(_as_index(raw.get("source_index"))),
                )
            )
        logger.info(
            "BOM 第 %d 行（%s）找到 %d 条替代建议，来源 %d 条",
            row.index,
            row.label,
            len(items),
            len(answer.sources),
        )
        return AlternativeResult(
            row_index=row.index,
            item_label=row.label,
            alternatives=tuple(items),
            notes=str(data.get("notes") or "").strip(),
        )

    # ---------- 卖点卡：潮流词 + Top N 卖点 ----------

    def trends(self, category: str, platform: str, period: str) -> TrendReport:
        top_n = self._config.work.top_n
        prompt = WORK_TREND_PROMPT.format(
            category=category, platform=platform, period=period, top_n=top_n
        )
        answer = LLMFactory.create_search(self._config).ask(
            prompt,
            WORK_TREND_SYSTEM,
            search_query=build_query(category, platform, period, "趋势", "爆款"),
        )
        data = parse_json_object(answer.text)
        by_index = {source.index: source for source in answer.sources}

        keywords = tuple(
            TrendKeyword(
                word=str(item.get("word") or "").strip(),
                heat=str(item.get("heat") or "").strip(),
                trend=str(item.get("trend") or "").strip(),
            )
            for item in (data.get("keywords") or [])
            if isinstance(item, dict) and str(item.get("word") or "").strip()
        )
        points = tuple(
            SellingPoint(
                point=str(item.get("point") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
                source=by_index.get(_as_index(item.get("source_index"))),
            )
            for item in (data.get("points") or [])
            if isinstance(item, dict) and str(item.get("point") or "").strip()
        )
        advice = tuple(
            str(item).strip() for item in (data.get("advice") or []) if str(item).strip()
        )
        logger.info(
            "卖点预测（%s / %s / %s）：关键词 %d 个、卖点 %d 条、来源 %d 条",
            category,
            platform,
            period,
            len(keywords),
            len(points),
            len(answer.sources),
        )
        return TrendReport(
            category=category,
            platform=platform,
            period=period,
            summary=str(data.get("summary") or "").strip(),
            keywords=keywords,
            points=points,
            advice=advice,
            sources=answer.sources,
        )

    # ---------- 结果末尾的宠物点评 ----------

    def comment(self, kind: str, summary: str) -> str:
        """让宠物用它的性格点评一句工作结果。

        报告主体（卖点 / 替代料）走的是专业 system，天然不带性格；
        这一句是**另外一次调用**、单独带上 persona_style_layer，所以性格腔调只可能出现在这里，
        不会混进报告里。点评是锦上添花，失败由调用方静默处理。
        """
        pet = self._config.pet
        system = persona_style_layer(pet, pet.name)
        prompt = WORK_COMMENT_PROMPT.format(kind=kind, summary=summary)
        return LLMFactory.create(self._config).chat(prompt, system=system)


def _as_index(value) -> int:
    """模型给的来源编号：读不出来就当 0（表示"没标依据"），别让一条脏数据毁掉整次结果。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
