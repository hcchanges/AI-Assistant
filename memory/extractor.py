"""
事实抽取器：每轮吐嘈后，问模型"这里面有什么值得长期记住的"，转成结构化记忆。

对应方案第 7 节的写入流程。模型不可用或抽取失败时不阻断吐嘈主流程，只记日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from brain.llm.base import LLMError
from brain.llm.factory import LLMFactory
from brain.prompts.templates import MEMORY_EXTRACT_PROMPT, now_text
from core.config import Config
from core.reltime import anchor

logger = logging.getLogger(__name__)

VALID_KINDS = ("fact", "preference", "event")


@dataclass
class ExtractedMemory:
    kind: str
    content: str
    importance: float
    # 正文里"明早/下周三"这类相对时间说法锚定出的绝对时刻；没说时间则为空
    abs_time: datetime | None = None


class MemoryExtractor:
    def __init__(self, config: Config):
        self._config = config

    def extract(self, text: str) -> list[ExtractedMemory]:
        provider = LLMFactory.create(self._config)
        now_dt = datetime.now()
        now, _ = now_text(now_dt)
        prompt = MEMORY_EXTRACT_PROMPT.format(now=now, text=text)
        try:
            data = provider.chat_json(prompt)
        except LLMError as exc:
            logger.error("记忆抽取失败: %s", exc)
            return []

        items: list[ExtractedMemory] = []
        for raw in data.get("memories") or []:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            kind = str(raw.get("kind") or "fact").strip()
            if kind not in VALID_KINDS:
                kind = "fact"
            try:
                importance = float(raw.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            items.append(
                ExtractedMemory(
                    kind=kind,
                    content=content,
                    importance=min(max(importance, 0.0), 1.0),
                    # 相对时间（明早/今晚/下周三…）按"说话当天"锚成绝对时刻，正文照原样留着
                    abs_time=anchor(content, now_dt)[1],
                )
            )
        return items
