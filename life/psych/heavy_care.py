"""
重情绪主动关怀（指令 26-A 第 3 项）：连着重下来的时候，林医生主动问一句、提醒休息。

三件事：
1. 记账——每次达到"重"（崩溃、连续失眠那一档）的话，按主题记一条，跨重启还在；
2. 开口——同一主题 7 天里第二次重下来，就从重情绪那档语料里挑一句，
   交给主动搭话那条线排着说出去（免打扰、专注中、人不在、开口太频繁，门控都在那边）；
3. 不产卡——这类话一律不进卡牌那条线，只在对话里陪着（不把痛苦做成收藏品）。

为什么借主动搭话那条线：它的门控与说话通道是现成的，重情绪关怀只是多了一条
"该说什么"的来源，时机判断不另起一套。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from data.dao import MoodEventDAO
from life.psych import comfort
from life.psych.comfort import HEAVY, ComfortLibrary

logger = logging.getLogger(__name__)

# 同一主题在这个窗口里重到这么多次，就该主动问一句了（指令 26-A 定的是连续 2 次）
WINDOW = timedelta(days=7)
TRIGGER_COUNT = 2
# 关怀过一次之后，同一个主题先静默这么久，别追着问
SILENCE = timedelta(days=7)


class HeavyCareService:
    def __init__(
        self,
        user_id: int,
        mood_dao: MoodEventDAO,
        queue_care: Callable[[str], None],
        library: ComfortLibrary | None = None,
    ):
        self._user_id = user_id
        self._mood_dao = mood_dao
        self._queue_care = queue_care
        self._library = library

    def note(
        self,
        text: str,
        module: str,
        topic: str,
        emotion_score: float,
        persona_id: str,
        may_invite: bool = True,
    ) -> None:
        """utterance_analyzed 的一个接听口：够"重"就记账，够格就排一句关怀。

        参数与信号一致；后三个这里用不上——主题按 comfort 那五个主题自己判
        （跟卡牌那 8 大类不是一回事），重不重看情绪分与用词。
        may_invite 是给卡牌服务看的（这一场主动服务动过手了没），关怀这边不关心。
        """
        if comfort.level_of(emotion_score, text) != HEAVY:
            return
        mood_topic = comfort.topic_of(text)
        self._mood_dao.add(self._user_id, mood_topic, text)
        if not self._due(mood_topic):
            return
        line = self._care_line(mood_topic)
        if not line:
            return
        self._mood_dao.mark_cared(self._user_id, mood_topic)
        logger.info("重情绪关怀（%s）：%s", mood_topic, line)
        self._queue_care(line)

    def _due(self, topic: str) -> bool:
        """这个主题现在该不该开口：够次数，且最近没关怀过。"""
        now = datetime.now()
        if self._mood_dao.count_since(self._user_id, topic, now - WINDOW) < TRIGGER_COUNT:
            return False
        last = self._mood_dao.last_cared_at(self._user_id, topic)
        return last is None or now - last >= SILENCE

    def _care_line(self, topic: str) -> str:
        """从重情绪那档语料里挑一条：共情句 + 陪伴句（陪伴句里就带着休息的提醒）。"""
        samples = self._library_or_default().samples(HEAVY, topic, limit=1)
        if not samples:
            return ""
        sample = samples[0]
        return f"{sample.empathy}{sample.company}"

    def _library_or_default(self) -> ComfortLibrary:
        return self._library or comfort.library()
