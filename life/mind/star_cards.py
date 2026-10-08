"""星念（B 养成）：把关系里那几件"值得留一颗"的事，从提炼结果里认出来、存成一张卡。

**为什么要有它。** 碎片是"细节"，用来日后搭话（"上次你说想买新能源车"）；
星念是"节点"，用来回头看——和好、表白、一起扛过的一段。两者寿命不一样：
碎片会越攒越杂，星念应该一直只有那么几颗。所以这里不是"给每条碎片发一张卡"，
而是**一轮提炼最多认一颗，认那颗权重最高的**。

认谁（三条来源，都是提炼已经写下的东西，不额外采集）：

    rel_state        关系总述        —— 命中里程碑词 → milestone
    events           关键事件        —— 命中强情绪词 → emotion；否则 event
    fragment_items   这一轮落的碎片  —— 同上

权重定 rarity：milestone 3 → epic、emotion 2 → rare、event 1 → common。
权重是**写死的规则**，不是模型给的——"这张卡珍不珍贵"得是确定的，
让模型每次判一遍，同一件事今天史诗明天普通，收藏册就失去意义了。

两条自我约束：
- **危机内容不立卡。** 一句"撑不住了"该走的是关怀（见 life/psych/crisis），
  不是被做成一张标着"史诗"的纪念品。命中危机词直接跳过。
- **同一件事只立一张。** 靠 ``source_id``（`rel_state:<原话>` / `event:<原话>` /
  `fragment:<原话>`）去重，重复提炼同一批对话不会攒出一摞一样的卡。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Iterable

from life.mind.store import MindStore
from life.psych import crisis

logger = logging.getLogger(__name__)

KIND_MILESTONE = "milestone"
KIND_EMOTION = "emotion"
KIND_EVENT = "event"

# 关系里程碑：和好、表白这类"关系状态变了"的事
MILESTONE_WORDS = (
    "和好", "复合", "表白", "在一起", "和解", "原谅", "道歉", "吵完",
    "确认关系", "见家长", "求婚", "订婚", "结婚",
)
# 强情绪：不是普通的不高兴，是压得住人的那种
EMOTION_WORDS = (
    "崩溃", "哭", "难受", "焦虑", "害怕", "绝望", "委屈", "撑不住",
    "压力大", "失眠", "抑郁", "孤独", "无助",
)

# 权重 → 稀有度。中间没有别的档，所以就是这三档
RARITY_BY_WEIGHT = {3: "epic", 2: "rare", 1: "common"}
_KIND_WEIGHT = {KIND_MILESTONE: 3, KIND_EMOTION: 2, KIND_EVENT: 1}

# 卡片标题最多留多少字：星念是一句话的记忆，不是一段摘要
_MAX_TITLE = 24

# 给界面用的说法。**纯常量、不进库、不进提示词**——库里的 kind / rarity 是英文短标签，
# 中文说法是现推的，改文案不用迁库（跟 STAGE_NAMES 一个路子）。
KIND_NAMES: dict[str, str] = {
    KIND_MILESTONE: "里程碑",
    KIND_EMOTION: "心绪",
    KIND_EVENT: "事件",
}
RARITY_NAMES: dict[str, str] = {
    "epic": "重",
    "rare": "中",
    "common": "轻",
}
# 来源那截前缀（source_id 形如 rel_state:<原话>）→ 中文出处
SOURCE_NAMES: dict[str, str] = {
    "rel_state": "关系总述",
    "event": "关键事件",
    "fragment": "记下的碎片",
}


def kind_name(kind: str) -> str:
    """kind → 中文说法。认不出来就原样返回，不硬造一个词出来。"""
    return KIND_NAMES.get(kind, kind or "")


def rarity_name(rarity: str) -> str:
    return RARITY_NAMES.get(rarity, rarity or "")


def source_name(source_id: str) -> str:
    """source_id → 这张卡是从哪儿认出来的。取冒号前那截查表。"""
    return SOURCE_NAMES.get((source_id or "").split(":", 1)[0], "")


def weight_of(kind: str) -> int:
    return _KIND_WEIGHT.get(kind, 1)


def rarity_of(weight: int) -> str:
    """权重算稀有度。冲出表外（以后加权了）按最高档给，不返回 None。"""
    return RARITY_BY_WEIGHT.get(weight, "epic" if weight > 3 else "common")


def classify(text: str, allow_plain: bool = False) -> tuple[str, int] | None:
    """一句话算不算值得立卡，算的话是哪一类、多重。不值得就返回 None。

    里程碑压在情绪前面判：同时出现"和好"和"哭"（和好那天哭了），
    该记的是和好那件事，不是哭。

    ``allow_plain=True`` 时没命中词表也按普通关键事件算，给 ``events`` 那条来源用：
    那些本来就是模型自己挑出来、认定"是件大事"的，不必再用一张词表筛第二遍。
    碎片没有这待遇——碎片是细节，随手一句都可能沾上"难受"两个字，得靠词表把门。
    """
    body = (text or "").strip()
    if not body:
        return None
    if any(word in body for word in MILESTONE_WORDS):
        return KIND_MILESTONE, weight_of(KIND_MILESTONE)
    if any(word in body for word in EMOTION_WORDS):
        return KIND_EMOTION, weight_of(KIND_EMOTION)
    if allow_plain:
        return KIND_EVENT, weight_of(KIND_EVENT)
    return None


class StarCardService:
    """认星念、立星念。接在提炼回调上（见 distill.MindDistiller._on_distilled）。"""

    def __init__(
        self,
        store: MindStore,
        now_provider: Callable[[], datetime] = datetime.now,
    ):
        self._store = store
        self._now = now_provider

    # ---------- 读 ----------

    def all(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self._store.star_cards(limit=limit)

    def count(self) -> int:
        return self._store.star_card_count()

    def latest_line(self) -> str:
        """最近一颗星念的一句话说法，给主动搭话当由头用（没卡返回空串）。"""
        cards = self._store.star_cards(limit=1)
        if not cards:
            return ""
        return str(cards[0].get("title") or "")

    # ---------- 立卡 ----------

    def observe(self, result: dict[str, Any]) -> dict[str, Any] | None:
        """提炼完一批 → 看看有没有值得留一颗的。返回新立的那张，没有就是 None。"""
        hits: list[tuple[int, str, str, str]] = []   # (权重, kind, source_id, 原话)
        for source_id, text, allow_plain in self._sources(result):
            if crisis.is_crisis(text):
                # 重话不立卡：该走关怀那条线，不该被做成收藏品
                logger.info("星念：这条来源踩到危机词，不立卡")
                continue
            hit = classify(text, allow_plain=allow_plain)
            if hit is None:
                continue
            kind, weight = hit
            hits.append((weight, kind, source_id, text))

        if not hits:
            return None
        # **够格的说法里只要有一条已经立过卡，就认定这一轮是那件事的重提炼**，不立新卡。
        # 为什么不是"逐条比对来源"：同一件事在一轮里会以三种说法同时出现——
        # 关系总述写"和家人闹过别扭，刚和好"、事件写"和家人和好了"、碎片写"今天跟妈妈和好了"。
        # 按来源去重的话，第一轮只记下它挑中的那一条，第二轮换了另一条说法就又能立一张，
        # 三张卡记的是同一件事。所以这里按**这一轮**判：这轮够格的说法里有旧的，就是旧事重提。
        #
        # 代价说清楚：一轮里要是既有旧里程碑又冒出一件新的，新的这颗会被一起让掉。
        # 宁可漏一颗，也不要把同一件事攒成一摞——星念的价值全在"只有那么几颗"。
        existing = self._existing_sources()
        if any(source_id in existing for _, _, source_id, _ in hits):
            logger.info("星念：这一轮够格的说法里有已立过卡的，按重提炼处理，不立新卡")
            return None

        best = max(hits, key=lambda hit: hit[0])
        weight, kind, source_id, text = best
        card_id = self._store.add_star_card(
            title=_trim(text),
            kind=kind,
            source_id=source_id,
            rarity=rarity_of(weight),
            created_at=self._now().isoformat(timespec="seconds"),
        )
        if card_id is None:
            return None
        card = {"id": card_id, "title": _trim(text), "kind": kind,
                "source_id": source_id, "rarity": rarity_of(weight),
                "created_at": self._now().isoformat(timespec="seconds")}
        logger.info("星念 +1：%s（%s / %s）", card["title"], kind, card["rarity"])
        return card

    # ---------- 内部 ----------

    def _sources(self, result: dict[str, Any]) -> Iterable[tuple[str, str, bool]]:
        """这一批提炼里，所有可能立卡的 (source_id, 原文, 没命中词表也算数吗)。

        三个 source_id 都是**按内容**算的，不是按行号 / 条数：``rel_state`` 是会被覆写的
        活文本，光用 ``"rel_state"`` 当 id，第一次立过卡之后关系**变了**也再立不出第二张；
        碎片要是拿 ``fragment:<id>`` 当 id，同一件事被提炼第二遍就会落一条新碎片、
        换一个新 id、再立一张一样的卡——文案一字不差，卡却攒了两张。
        按内容算，"同一件事"才真的是同一件。
        """
        rel_state = str(result.get("rel_state") or "").strip()
        if rel_state:
            yield f"rel_state:{rel_state}", rel_state, False
        for event in result.get("events") or []:
            text = str(event).strip()
            if text:
                yield f"event:{text}", text, True
        for item in result.get("fragment_items") or []:
            text = str(item.get("text") or "").strip()
            if text:
                yield f"fragment:{text}", text, False

    def _existing_sources(self) -> set[str]:
        return {str(card.get("source_id") or "") for card in self._store.star_cards()}


def _trim(text: str, limit: int = _MAX_TITLE) -> str:
    body = " ".join((text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"
