"""碎片检索：语义主召回（向量）+ 字面兜底（关键词），两路融合去重。

**这条路走过一轮 A → A+。** 第一版只有字面匹配，理由是两条：
".text/.tags 落盘是 Fernet 密文，密文上做不了 LIKE"，于是把匹配挪到内存里做；
以及"不引入向量库"。第一版能用，但有个说不通的短板——**换了个说法就找不着**。
存的是"爱吃楼下包子铺的鲜肉包"，用户问"早饭吃什么"，一个字都对不上，
字面这条路直接判 0 分，可这明明就是该想起那条碎片的时候。

所以 A+ 补上向量那条腿（S1~S3），字面那条**一条都没删**：

    向量腿（主）：语义相近就召回，管"换了说法"。正文不进 chroma，只进向量（见 index.py）。
    字面腿（兜底）：向量覆盖不到的那两种，靠它接住——

      a) 还没进索引的碎片（加这个功能之前存下的老碎片；
         正常由 ensure_indexed 补齐，但补不上的时候不能瞎）；
      b) 原话里那个词，语义模型未必当回事，字面却是铁的（人名、编号、店名）。

两条腿的产出按 id 去重，向量在前（语义排序），字面在后（按分数排）。
返回结构与 A 版完全一致，所以 proactive 那边一行没改。

先按 2~3 字滑窗把中文切开，是因为中文没有空格：整句当一个词去比，
除非用户原样复述，否则一条都命中不了，等于没检索。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from life.mind.index import DEFAULT_TOP_K
from life.mind.store import MindStore

logger = logging.getLogger(__name__)

# 切词用的标点：按这些断开，剩下的当一个个词
_SPLIT = re.compile(r"[\s,，。、；;：:!！?？~～\-—_/\\|()（）\[\]【】\"'“”‘’]+")
# 只有一个字的滑窗噪声太大（"的""了"遍地都是），从 2 字起
_MIN_GRAM = 2
_MAX_GRAM = 3
# 标签命中比正文命中值钱：标签是提炼时专门打的记号
_TAG_WEIGHT = 3
_WORD_WEIGHT = 2
_GRAM_WEIGHT = 1


def keywords(text: str) -> list[str]:
    """把一句话拆成可用于比对的词：先按标点切，再补 2~3 字滑窗。"""
    words = [w for w in _SPLIT.split(text or "") if w]
    grams: list[str] = []
    for word in words:
        if len(word) < _MIN_GRAM:
            continue
        for size in range(_MIN_GRAM, _MAX_GRAM + 1):
            if len(word) < size:
                continue
            grams.extend(word[i : i + size] for i in range(len(word) - size + 1))
    # 去重但保序：短词在前（越短越泛，先算完再补长的，分数不会重复累加）
    seen: set[str] = set()
    unique: list[str] = []
    for token in words + grams:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def score(fragment: dict[str, Any], tokens: list[str], raw_query: str) -> int:
    """一条碎片跟这次查询有多相关。0 分就是不相干，不返回。"""
    text = fragment.get("text", "")
    tags = [str(t) for t in fragment.get("tags", [])]
    total = 0
    for token in tokens:
        if any(token in tag or tag in token for tag in tags):
            total += _TAG_WEIGHT
        elif token in text:
            total += _WORD_WEIGHT
        elif raw_query and raw_query in text:
            total += _GRAM_WEIGHT
    return total


class MindRecall:
    """翻碎片：语义主召回 + 字面兜底。只在本机算，不联网。

    不传 index 就是纯字面那一路（索引起不来时的退化形态，测试也用它验退化）。
    """

    def __init__(self, store: MindStore, index: Any = None, scan_limit: int = 500):
        self._store = store
        self._index = index
        # 单次检索最多解密多少条：个人库远到不了这个数，写出来是为了万一
        self._scan_limit = scan_limit
        # 历史碎片补向量只做一次，见 search
        self._backfilled = False

    def ensure_indexed(self) -> int:
        """S3：把没进索引的碎片补上向量（老版本存下的那些）。幂等，返回补了几条。

        正常在第一次检索时自动触发；返回条数是为了在日志 / 回归里能确认
        "补过了"而不是"每次都在补"。
        """
        if self._index is None:
            return 0
        try:
            items = [
                (f["id"], f["text"])
                for f in self._store.fragments(limit=self._scan_limit)
            ]
            added = self._index.add_many(items)
        except Exception as exc:  # 补不上不影响字面那条腿
            logger.error("情感记忆：历史碎片补向量没成（%s），这次只走字面", exc)
            return 0
        if added:
            logger.info("情感记忆：给 %d 条历史碎片补了向量", added)
        return added

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """找回跟 query 沾边的碎片，最相关的在前。找不着就返回空表。"""
        raw = (query or "").strip()
        if not raw:
            return []
        tokens = keywords(raw)
        if not tokens:
            return []

        found: list[dict[str, Any]] = []
        seen: set[int] = set()

        # 第一条腿：向量语义召回。第一次检索顺手把历史碎片补齐（只补一次）
        if self._index is not None:
            if not self._backfilled:
                self._backfilled = True
                self.ensure_indexed()
            for fragment_id in self._vector_hits(raw):
                if fragment_id in seen:
                    continue
                fragment = self._store.fragment(fragment_id)
                if fragment is None:
                    # 索引里有、库里没有（库被清过 / 换过钥匙）：跳过，别当命中
                    continue
                seen.add(fragment_id)
                found.append(fragment)

        # 第二条腿：字面兜底，接住向量覆盖不到的（见模块开头 a / b 两条）
        scored: list[tuple[int, dict[str, Any]]] = []
        for fragment in self._store.fragments(limit=self._scan_limit):
            if fragment["id"] in seen:
                continue
            hit = score(fragment, tokens, raw)
            if hit > 0:
                scored.append((hit, fragment))
        # 同分时新的在前：翻旧账先翻最近的
        scored.sort(key=lambda pair: (pair[0], pair[1]["id"]), reverse=True)
        found.extend(fragment for _, fragment in scored)

        return found[:limit]

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        return self._store.fragments(limit=limit)

    # ---------- 内部 ----------

    def _vector_hits(self, query: str) -> list[int]:
        try:
            return self._index.search(query, top_k=DEFAULT_TOP_K)
        except Exception as exc:  # 向量腿塌了不能连累字面腿
            logger.error("情感记忆：向量召回失败（%s），这次只走字面", exc)
            return []
