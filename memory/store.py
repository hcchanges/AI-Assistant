"""
长期记忆的读写入口。

分工：SQLite 存正文与元信息（MemoryDAO），Chroma 存语义索引（MemoryVectorStore）。
写记忆 = 入库拿 id + 用该 id 建向量；检索 = 向量找 id + 回表取正文。
"""

from __future__ import annotations

import logging
from datetime import datetime

from core.config import Config
from data.dao import MemoryDAO
from data.models import Memory
from memory.extractor import ExtractedMemory
from memory.vector import MemoryVectorStore

logger = logging.getLogger(__name__)


class MemoryStore:
    def __init__(
        self,
        config: Config,
        memory_dao: MemoryDAO,
        vector_store: MemoryVectorStore,
        user_id: int,
    ):
        self._config = config
        self._dao = memory_dao
        self._vector = vector_store
        self._user_id = user_id

    def remember(
        self,
        kind: str,
        content: str,
        importance: float,
        abs_time: datetime | None = None,
    ) -> Memory:
        memory = self._dao.add(self._user_id, kind, content, importance, abs_time)
        self._vector.add(memory.id, content)
        logger.info("记住了一条记忆(#%d): %s", memory.id, content)
        return memory

    def remember_many(self, items: list[ExtractedMemory]) -> list[Memory]:
        saved: list[Memory] = []
        for item in items:
            try:  # 单条失败不影响其他
                saved.append(
                    self.remember(item.kind, item.content, item.importance, item.abs_time)
                )
            except Exception as exc:
                logger.error("记忆写入失败(%s): %s", item.content, exc)
        return saved

    def recent(self, limit: int = 50) -> list[Memory]:
        """界面展示用：最近记住的内容。"""
        return self._dao.recent(limit)

    def forget(self, memory_id: int) -> None:
        self._dao.delete(memory_id)
        self._vector.remove(memory_id)

    def recall(self, query: str, top_k: int | None = None) -> list[Memory]:
        """语义检索相关记忆，供生成关怀/建议时注入提示词。"""
        k = top_k or self._config.memory.top_k
        ids = self._vector.search(query, k)
        if not ids:
            return []
        memories = self._dao.by_ids(ids)
        # by_ids 返回顺序不保证与相关度一致，这里按检索结果重排
        order = {mid: index for index, mid in enumerate(ids)}
        memories.sort(key=lambda m: order.get(m.id, len(order)))
        return memories
