"""
关怀服务：提醒到点时，让宠物弹到屏幕前并说一句结合长期记忆的话。

这是 MVP 的验收点（"被心疼"的那一下）：
   吐嘈 → 记住 → 到点 → 宠物主动开口。
"""

from __future__ import annotations

import logging
from datetime import datetime

from brain.chains.care import CareChain
from core.config import Config
from core.events import EventBus
from core.reltime import memory_text, rewrite
from core.worker import run_async
from data.dao import ReminderDAO
from data.models import Reminder
from memory.store import MemoryStore

logger = logging.getLogger(__name__)


class CareService:
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        reminder_dao: ReminderDAO,
        memory_store: MemoryStore,
        care_chain: CareChain,
    ):
        self._config = config
        self._bus = bus
        self._reminder_dao = reminder_dao
        self._memory_store = memory_store
        self._care_chain = care_chain

    def on_reminder_due(self, reminder_id: int) -> None:
        reminder = self._reminder_dao.get(reminder_id)
        if reminder is None:
            return
        # 先把它弹到屏幕前，再慢慢想说什么
        self._bus.summon_pet.emit()
        run_async(
            self._compose,
            reminder,
            on_done=self._speak,
            on_failed=lambda exc: self._speak_fallback(reminder, exc),
        )

    def _compose(self, reminder: Reminder) -> str:
        now = datetime.now()
        # 提醒与记忆的正文里可能还留着"明早"这类说法：提醒按 trigger_at、记忆按 abs_time，
        # 都按"现在"重新说一遍，免得几天后还在念"明早"
        memories = [
            memory_text(memory, now) for memory in self._memory_store.recall(reminder.content)
        ]
        return self._care_chain.generate(rewrite(reminder.content, reminder.trigger_at, now), memories)

    def _speak(self, text: str) -> None:
        self._bus.pet_speak_proactive.emit(text)

    def _speak_fallback(self, reminder: Reminder, exc: Exception) -> None:
        """模型不可用时，至少把提醒本身说出来，别让用户白等一场。"""
        logger.error("关怀话术生成失败: %s", exc)
        content = rewrite(reminder.content, reminder.trigger_at)
        self._bus.pet_speak_proactive.emit(f"到点啦，你说过的：{content}")
