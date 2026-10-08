"""
可卡牌化过滤（指令 12 + 指令 14 的红线 + 指令 25-B 的"可行动"门槛）：生活卡与玄学卡都要过这一关。

为什么：心理咨询的本质是连续关系，而卡牌是"一件事"；把用户的焦虑、痛苦做成
"可收集的卡牌"显得轻浮——不把别人的痛苦当收藏品。所以这类话题一律不建卡，
只由林医生陪着聊（现在所有聊天本来就是林医生在答）。

玄学卡是娱乐，正常放行；但**不能借算命绕过心理红线**：
"帮我算算我还能撑多久，我不想活了"这种，危机判定与心理词表一样拦住它。

三道闸门：
1. 主题得是能成卡的那一档：9 大类（生活卡）或玄学主题，其余（工作、其他）不成卡；
2. 命中危机表达 → 不放行（用跟聊天同一个判定，不做第二套标准）；
3. 话题虽归到别的类别、但话里明显在讲心理状态（例如"最近很焦虑，晚上想吃点清淡的"）
   ——词表宁可多拦一点：漏建一张卡只是少个功能，错建一张卡是伦理问题。
"""

from __future__ import annotations

from collections.abc import Sequence

from life.cards.templates import family_of
from life.psych.crisis import is_crisis

# 明显在讲心理/精神状态的词（不用单个字，避免误伤）
PSYCH_WORDS: tuple[str, ...] = (
    "焦虑", "抑郁", "失眠", "情绪", "难过", "委屈", "崩溃", "自卑", "恐慌",
    "孤独", "创伤", "想哭", "心里", "心理", "精神", "烦躁", "撑不住", "压力",
    "睡不着", "没意思", "绝望", "无助",
)

def is_safe_text(texts: Sequence[str], metaphysics_words: Sequence[str] = ()) -> bool:
    """这段话说的是正常的生活事吗：命中危机表达或心理词表就不算。

    先看这一步再看归到哪个品类——不然"最近很焦虑，晚上想吃点清淡的"会被归到餐饮、
    记成一条观察，就违背了指令 14 的红线。

    metaphysics_words 是命理那一类的关键词：命理话题本来就常跟"诸事不顺""心里没底"
    这类话一起说，心理词表照拦会把整类话题误伤成心理议题（命理卡因此永远生不出来）。
    所以命中命理词时放宽一档——只拦真危机词（不想活 / 伤害自己 / 活不下去…），
    泛泛的心理词不拦。红线没松：真危机最高优先，照拦不误。
    """
    joined = "".join(texts)
    if is_crisis(joined):
        return False
    if metaphysics_words and any(word in joined for word in metaphysics_words):
        return True
    return not any(word in joined for word in PSYCH_WORDS)


def is_cardable(
    theme: str, texts: Sequence[str], metaphysics_words: Sequence[str] = ()
) -> bool:
    """这个话题能不能做成卡牌：既要是能成卡的那一类，内容也得是正常的生活事。"""
    return bool(family_of(theme)) and is_safe_text(texts, metaphysics_words)
