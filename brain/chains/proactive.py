"""
主动搭话链：把"现在是什么时候 + 用户今天过得怎么样 + 记忆里的事"交给模型，
由它自己决定要不要开口、说什么（不想说话就回 [SILENT]）。

判断权交给模型而不是写死规则，是因为"什么时候值得说一句"很依赖情境与记忆；
本地只负责把关硬条件（免打扰、专注中、上限），见 life/proactive.py。
"""

from __future__ import annotations

import logging

from brain.llm.factory import LLMFactory
from brain.prompts.templates import PROACTIVE_PROMPT, now_text
from core.config import Config
from pet.persona import resolve_voice

logger = logging.getLogger(__name__)

SILENT_TOKEN = "[SILENT]"
NO_MEMORY_TEXT = "（暂无）"
NO_ACTIVITY_TEXT = "（今天还没记到使用情况）"


def is_silent(text: str) -> bool:
    """模型表示"没什么要说的"。"""
    return SILENT_TOKEN in text.upper() or not text.strip()


class ProactiveChain:
    def __init__(self, config: Config):
        self._config = config

    def compose(self, activity: str, memories: list[str]) -> str:
        provider = LLMFactory.create(self._config)
        now, weekday = now_text()
        prompt = PROACTIVE_PROMPT.format(
            now=now,
            weekday=weekday,
            activity=activity or NO_ACTIVITY_TEXT,
            memories="\n".join(f"- {m}" for m in memories) if memories else NO_MEMORY_TEXT,
            silent=SILENT_TOKEN,
        )
        system = resolve_voice(self._config.pet).system_prompt(self._config.pet.name)
        return provider.chat(prompt, system=system)
