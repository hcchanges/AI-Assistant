"""
意图兜底判定（指令 26-B）：本地词表都没认出来时，请模型看一眼这句话要不要帮忙。

只在一个窄口子上用——话里有求助信号、又带着情绪（见 life/intent.py 的 needs_fallback）。
模型回的是 work（工作上的任务）/ life（生活里哪一类急事）/ none（只是在聊天），
外加一个 urgent（这件事是不是特别急、得马上处理；只有 life 才有意义），
把握不到 0.7 由调用方丢掉（"宁可少帮一次，也别多问一句"）。
"""

from __future__ import annotations

from brain.llm.factory import LLMFactory
from brain.prompts.templates import (
    SERVICE_INTENT_PROMPT,
    SPEAKER_USER,
    speaker_label,
)
from core.config import Config

# 只要结构化结果，不要宠物腔调：给一句中性的系统提示，也顺带避开注入
INTENT_SYSTEM = "你是意图分类助手，只按给定的候选类别归类，只输出 JSON。"

WORK = "work"
LIFE = "life"
NONE = "none"


class IntentChain:
    def __init__(self, config: Config):
        self._config = config

    def classify(self, text: str, speaker: str = SPEAKER_USER) -> tuple[str, str, float, bool]:
        """请模型判一次：(kind, theme, confidence, urgent)。

        ``speaker``（v2-social-card-base）：这句话是谁说的。默认 ``"user"``——主聊天那条线
        一个字都不改；帮聊那条线传 ``"peer"``，问的就变成"**对方**这句话要不要帮忙"
        （对方在邀约 / 在问事 / 在求助），判定规则与词表完全一样，只是提示词里的主语换人。
        """
        provider = LLMFactory.create(self._config)
        categories = "、".join(self._config.card.categories)
        data = provider.chat_json(
            SERVICE_INTENT_PROMPT.format(
                categories=categories, text=text, speaker=speaker_label(speaker)
            ),
            system=INTENT_SYSTEM,
        )
        kind = str(data.get("kind") or "").strip()
        if kind not in (WORK, LIFE, NONE):
            return NONE, "", 0.0, False
        return (
            kind,
            str(data.get("theme") or "").strip(),
            _confidence(data.get("confidence")),
            bool(data.get("urgent")),
        )


def _confidence(value) -> float:
    """把模型给的把握夹到 0~1；给的不是数字就按 0（等于不可信）算。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(score, 0.0), 1.0)
