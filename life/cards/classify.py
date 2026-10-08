"""
品类判定（指令 25-B 第 1 项）：三层层级，防串卡。

① 本地词表（config.card.categories，管理员可编辑）：9 大类各配一串关键词。
   命中就归到那一类，不走模型、不花钱、结果稳定。
② 一句话命中多个类时，只算"主要类别"一个——挑命中最多的那类；并列时先看谁在"缺什么"
   的说法里（"没衣服穿"里的"衣服"，那才是主诉求，"见客户"只是场合），再并列才取关键词
   出现位置最靠前的（先说的那件事是主诉求）。所以一条消息绝不会给多个品类各刷一次计数。
③ 词表命不中、但情绪很强（≥0.7）或者"没归上类"的吐槽攒够了次数时，才请模型判类 + 给置信度；
   置信度低于 card.classify_confidence 就只记观察、不出邀请。

这类只管"归到哪一类"；能不能做成卡（危机表达、心理类议题）由 life/cards/spawnable.py 管。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import Config
from life.cards import threshold

# 词表命不中时，攒到这么多次"没归上类的吐槽"才值得请一次模型
FALLBACK_MIN_COUNT = 3

# "缺什么"的说法：命中词前面紧挨着这几个字，就是"缺那样东西"，那样东西才是主诉求
# （"没衣服穿""少了双鞋"）。只往前看 MISSING_LOOKBACK 个字，免得把前一句的否定
# 也算到这一句头上（"今天没睡好，跑步也没劲"里的"跑步"不该算缺）
MISSING_MARKERS = ("没", "没有", "缺", "少", "不够", "买不起")
MISSING_LOOKBACK = 3


@dataclass(frozen=True)
class CategoryMatch:
    """本地词表的一次命中。"""

    category: str
    hits: int          # 命中了几个词
    first_at: int      # 最早命中的那个词在句中的位置（越小越靠前）
    missing: bool = False   # 命中的词是不是落在"缺什么"的说法里


class CardClassifier:
    def __init__(self, config: Config):
        # 词表按大类整理成"词 -> 类"的反查不必做：8 类各几十个词，逐类扫一遍就够快
        self._words: dict[str, tuple[str, ...]] = {
            name: tuple(words) for name, words in config.card.categories.items()
        }
        self.min_confidence = config.card.classify_confidence

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(self._words)

    def local_match(self, text: str) -> CategoryMatch | None:
        """按本地词表归类；一句话命中多个类时只回主要的那一类。认不出来返回 None。"""
        best: CategoryMatch | None = None
        for category, words in self._words.items():
            positions = [text.find(word) for word in words]
            positions = [position for position in positions if position >= 0]
            if not positions:
                continue
            match = CategoryMatch(
                category,
                len(positions),
                min(positions),
                any(_is_missing(text, position) for position in positions),
            )
            if best is None or _better(match, best):
                best = match
        return best

    def should_ask_llm(self, emotion_score: float, unmatched_count: int) -> bool:
        """词表没命中时，这一次值不值得请模型判类：情绪很强，或同类没归上的攒够了。"""
        return (
            emotion_score >= threshold.STRONG_NEGATIVE
            or unmatched_count >= FALLBACK_MIN_COUNT
        )


def _better(candidate: CategoryMatch, best: CategoryMatch) -> bool:
    """命中的词更多就更好；一样多时看是不是"缺什么"；再一样才算谁出现得靠前。

    "缺什么"这一档为什么排在位置前面："明天见客户没衣服穿愁死"里，出行（见客户）和
    穿搭（衣服）各命中一次、出行还更靠前，可这句的诉求是"没衣服穿"，见客户只是场合。
    中文的场合/时间状语本来就在前头，光按位置取，句首那个词永远赢。
    """
    if candidate.hits != best.hits:
        return candidate.hits > best.hits
    if candidate.missing != best.missing:
        return candidate.missing
    return candidate.first_at < best.first_at


def _is_missing(text: str, position: int) -> bool:
    """这个词是不是落在"没……"的说法里（"没衣服穿"的"衣服"）。"""
    before = text[max(0, position - MISSING_LOOKBACK) : position]
    return any(marker in before for marker in MISSING_MARKERS)
