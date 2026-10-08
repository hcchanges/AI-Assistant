"""
今日活动点评链（对应指令 5 第 4 节 / 指令 6 第 4 节）。

把"今日活动"那段统计交给模型，用宠物当前的人设点评一句，并给一条能落地的建议。
"""

from __future__ import annotations

from brain.llm.factory import LLMFactory
from brain.prompts.templates import ACTIVITY_PROMPT, now_text
from core.config import Config
from pet.persona import resolve_voice


class ActivityChain:
    def __init__(self, config: Config):
        self._config = config

    def comment(self, report: str) -> str:
        provider = LLMFactory.create(self._config)
        now, _ = now_text()
        prompt = ACTIVITY_PROMPT.format(
            now=now, report=report, pet_name=self._config.pet.name
        )
        system = resolve_voice(self._config.pet).system_prompt(self._config.pet.name)
        return provider.chat(prompt, system=system)
