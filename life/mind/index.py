"""碎片向量索引（S1）：语义召回的那条腿，放在 chroma 里。

跟旧向量库（memory/vector.py）的关系：同一个库目录（data/chroma）、同一台 chroma，
只是另起一个 collection ``mind_fragments``，metadata 里只放 ``fragment_id``。
复用现成的 chromadb + bge-small-zh-v1.5，不新增任何依赖。

**正文不进这里，只进向量。** 这一条是有意的，不是省事：

chroma 的 ``documents`` 是把正文原样落进 ``data/chroma/chroma.sqlite3`` 的。
碎片正文在 mind.db 里是 Fernet 密文，要是同一个碎片在隔壁 chroma 里躺着明文，
"整库加密"就成了摆设——翻到 chroma 文件的人照样看得到内容。所以这里自己用
``OnnxChineseEmbedding`` 算向量、只写 ``embeddings`` + ``fragment_id``，
检索时同样传 ``query_embeddings``，全程不走 chroma 的文本通道。

由此带来的两个附带好处：正文只有 mind.db 那一份，不存在"两处正文对不上"；
chroma 也不需要为它加载内置的英文模型。

**距离闸门（MAX_DISTANCE）**：向量召回没有"查不到"这个选项——topK 总会返回最近的
几条。不设闸门，`量子力学编译器` 也会硬拽出一条碎片回来。所以按余弦距离卡一道，
取值是量出来的（bge-small-zh-v1.5，碎片为生活短句）：

    查询「早饭吃什么」  对四条碎片的距离（升序）
      0.599  爱吃楼下包子铺的鲜肉包     ← 相关
      0.644  楼下新开那家的鲜肉包真不错  ← 相关
      -------------------------------------------------- 0.66 这条线切在这儿
      0.670  周末去爬了趟香山，腿酸了两天 ← 无关，得挡住
      0.672  今天下午闲聊了一会儿      ← 无关，得挡住
    另外几个查询：肉包子→0.348 / 爬山→0.468（都进得来）；
                 量子力学编译器→最近邻 0.745（挡得住）

**这个缝隙只有 0.026，很窄，得说清楚。** 0.66 是压在这条缝中间选的，不是有理论依据的
圆整数字；它能同时满足"早饭吃什么能召回"（验收 2）和"不相关的查不到"，但语料一长、
说法一偏，就可能翻车。这是模型在短句上的分辨力所限，不是调参能解决的。
碎片攒到几百条以后，这个数值得重新标一次；真要往深里做，是换成 rerank 而不是继续挪线。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from chromadb.config import Settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "mind_fragments"
# 余弦距离闸门，取值见模块开头的实测表（缝隙很窄，改之前先重测）
MAX_DISTANCE = 0.66
DEFAULT_TOP_K = 5


class MindVectorIndex:
    """fragment_id ↔ 句向量。正文不进来。"""

    def __init__(self, path: Path | str, embedding_function: Any):
        import chromadb

        self.path = Path(path)
        self._ef = embedding_function
        # 跟 memory/vector.py 用同一份 Settings：chroma 按 settings 复用同一个 system，
        # 所以这不是第二个 sqlite 连接，也不会跟旧 collection 抢锁
        self._client = chromadb.PersistentClient(
            path=str(self.path), settings=Settings(anonymized_telemetry=False)
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            # 传它只是别让 chroma 去初始化内置的英文模型：这里全程传向量，不走文本通道
            embedding_function=embedding_function,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("情感记忆向量索引就绪: %s（现有 %d 条）", self.path, self._collection.count())

    # ---------- 写 ----------

    def add(self, fragment_id: int, text: str) -> None:
        """给一条碎片算向量。正文用完就丢，只把向量写进去。"""
        if not text or not text.strip():
            return
        self._collection.upsert(
            ids=[str(fragment_id)],
            embeddings=[self._ef([text])[0]],
            metadatas=[{"fragment_id": int(fragment_id)}],
        )

    def add_many(self, items: Sequence[tuple[int, str]]) -> int:
        """批量补算（S3）。已经在索引里的跳过，所以重复调用是幂等的。

        返回这次真正补了几条——调用方（回归 / 日志）靠它确认"补过了"而不是"又补一遍"。
        """
        pending = [(fid, text) for fid, text in items if text and text.strip() and not self.has(fid)]
        if not pending:
            return 0
        self._collection.upsert(
            ids=[str(fid) for fid, _ in pending],
            embeddings=list(self._ef([text for _, text in pending])),
            metadatas=[{"fragment_id": int(fid)} for fid, _ in pending],
        )
        return len(pending)

    # ---------- 读 ----------

    def has(self, fragment_id: int) -> bool:
        got = self._collection.get(ids=[str(fragment_id)])
        return bool(got.get("ids"))

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> list[int]:
        """按语义找回 fragment_id，最近的在前。够不上闸门的直接不要。"""
        raw = (query or "").strip()
        total = self._collection.count()
        if not raw or not total:
            return []
        result = self._collection.query(
            # 查询侧带 bge 建议的前缀（见 OnnxChineseEmbedding.embed_query）
            query_embeddings=[self._ef.embed_query([raw])[0]],
            n_results=min(top_k, total),
            include=["distances", "metadatas"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits: list[int] = []
        for index, item in enumerate(ids):
            distance = float(distances[index]) if index < len(distances) else 1.0
            if distance > MAX_DISTANCE:
                continue
            metas = (result.get("metadatas") or [[]])[0]
            meta = metas[index] if index < len(metas) else None
            fragment_id = (meta or {}).get("fragment_id")
            if fragment_id is None:
                continue
            hits.append(int(fragment_id))
        return hits

    def count(self) -> int:
        return int(self._collection.count())

    def clear(self) -> int:
        """把碎片索引整个清掉（v2-reset-state），返回清之前有多少条。

        跟 memory/vector.py 的 clear 是同一套做法：chroma 不收空条件的 delete，
        所以删掉整条 collection 再照原样建一条空的。`self._ef` 是构造时留下的
        那份 embedding_function——不重建 collection 的话，`add` 会写到一个已经
        不存在的壳上。
        """
        before = self.count()
        self._client.delete_collection(COLLECTION_NAME)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("情感记忆向量索引已清空: %s（原 %d 条）", self.path, before)
        return before
