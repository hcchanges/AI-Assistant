"""
向量库封装（Chroma，本地持久化）。

只负责"文本 -> 相似记忆 id"，正文仍以 SQLite 为准。
向量由外部传入的向量函数计算（见 memory/embedding.py，中文模型）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import chromadb
from chromadb.api.types import EmbeddingFunction
from chromadb.config import Settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "memories"


class MemoryVectorStore:
    def __init__(self, path: Path, embedding_function: EmbeddingFunction):
        self.path = path
        # 自己留一份：清空之后要照原样重建 collection，而 chroma 那边取回
        # embedding_function 的属性名各版本不一样（`_embedding_function` / 属性），
        # 与其猜不如把传进来的这个记住
        self._ef = embedding_function
        self._client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embedding_function,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("向量库就绪: %s（现有 %d 条）", path, self._collection.count())

    def add(self, memory_id: int, content: str) -> None:
        self._collection.upsert(ids=[str(memory_id)], documents=[content])

    def remove(self, memory_id: int) -> None:
        self._collection.delete(ids=[str(memory_id)])

    def count(self) -> int:
        return int(self._collection.count())

    def clear(self) -> int:
        """清空这个 collection（v2-reset-state），返回清之前有多少条。

        chroma 的 `delete` 不收空条件——没有 id、没有 where 就删不动。这里走
        "整条 collection 删掉再建一个同名空壳"，比逐条算 id 简单，也不会漏。
        删完立刻重建：调用方手里握着的是 `self._collection`，不重建就等于把这根
        索引的腿砍了，后面再记东西会写不进去。
        """
        before = self.count()
        self._client.delete_collection(COLLECTION_NAME)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("向量库已清空: %s（原 %d 条）", self.path, before)
        return before

    def search(self, query: str, top_k: int) -> list[int]:
        """返回最相关的记忆 id，按相关度从高到低。"""
        total = self._collection.count()
        if total == 0:
            return []
        result = self._collection.query(query_texts=[query], n_results=min(top_k, total))
        ids = (result.get("ids") or [[]])[0]
        return [int(i) for i in ids]
