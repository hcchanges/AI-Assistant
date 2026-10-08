"""
关怀话术链：到点提醒时，结合检索到的长期记忆，生成一句像"它会心疼人"的话。
"""

from __future__ import annotations

from brain.llm.factory import LLMFactory
from brain.prompts.templates import CARE_PROMPT, now_text
from core.config import Config
from pet.persona import resolve_voice

NO_MEMORY_TEXT = "（暂无）"


class CareChain:
    def __init__(self, config: Config):
        self._config = config

    def generate(self, content: str, memories: list[str]) -> str:
        provider = LLMFactory.create(self._config)
        now, _ = now_text()
        memory_text = "\n".join(f"- {m}" for m in memories) if memories else NO_MEMORY_TEXT
        prompt = CARE_PROMPT.format(
            now=now,
            content=content,
            memories=memory_text,
            pet_name=self._config.pet.name,
        )
        system = resolve_voice(self._config.pet).system_prompt(self._config.pet.name)
        return provider.chat(prompt, system=system)
