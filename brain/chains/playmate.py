"""
游戏陪玩链：把一张游戏截图 + 前台应用名交给能看图的模型，让它给一句用得上的提示。

"没什么可说的"（菜单、加载中）复用主动搭话那套 [SILENT] 约定，由 life/playmate.py 判断。
"""

from __future__ import annotations

import logging

from brain.chains.proactive import SILENT_TOKEN
from brain.llm.factory import LLMFactory
from brain.prompts.templates import PLAYMATE_PROMPT
from core.config import Config
from pet.persona import resolve_voice

logger = logging.getLogger(__name__)


class PlaymateChain:
    def __init__(self, config: Config):
        self._config = config

    def advise(self, app_name: str, image: bytes) -> str:
        """看一张截图，回一句提示（或 [SILENT]）。"""
        provider = LLMFactory.create_vision(self._config)
        prompt = PLAYMATE_PROMPT.format(app_name=app_name or "未知", silent=SILENT_TOKEN)
        system = resolve_voice(self._config.pet).system_prompt(self._config.pet.name)
        return provider.chat_with_image(prompt, image, system=system)
