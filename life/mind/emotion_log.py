"""情感事件归因与分类（v2-emotion-log）：一句话在吐露情感/压力吗，归到哪一类。

跟卡牌那条线分家：用户说「和对象吵架了」「工作压力大」「爸妈催婚」这类话时，
**不再建成「情感」卡**（感情卡），改记一条**情感事件**——带上归因（谁/什么引起的）
与分类标签（工作压力 / 人际 / 家庭 / 健康），存进加密的情感记忆库（mind.db），
以后能翻出来，但绝不以卡牌的形式出现。

为什么这么分：感情/压力是"一个人此刻的状态"，不是"一件可以打卡推进的事"；
把它做成卡等于把情绪当待办。事件这条线只记录、不行动、不出卡。

归因与分类都是纯规则、本地词表，一个模型都不调——跟情绪路由同一个姿态：
这是"模型回来之前"就该定下来的即时值，落库也不该依赖网络。

词表口径照搬 v2-mood-anger 的教训：只收**光看词面就知道在吐露情感/压力**的整词，
单字一概不碰（"累"不收、"家"不收——"大家/回家"到处都是），免得把"我工作还行"
这种中性句记成工作压力。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------- 分类标签 ----------

CATEGORY_WORK = "工作压力"
CATEGORY_RELATION = "人际"
CATEGORY_FAMILY = "家庭"
CATEGORY_HEALTH = "健康"

# 合法的分类标签（越界检查用；模型不参与，这里只是自己兜底）
VALID_CATEGORIES: tuple[str, ...] = (
    CATEGORY_RELATION,
    CATEGORY_FAMILY,
    CATEGORY_HEALTH,
    CATEGORY_WORK,
)


@dataclass(frozen=True)
class EmotionAttribution:
    """一次情感事件的归因与分类。

    attribution：归因——谁/什么引起的（"关系""家人""身体""工作"）。
    category：分类标签——工作压力 / 人际 / 家庭 / 健康。
    """

    attribution: str
    category: str


# (分类标签, 归因, 关键词)。按顺序先中先算——排前面的类别更具体、更情绪化，
# 一个句子撞上多个类时归到最靠前的那类（"和对象吵架"里的"吵架"归人际，
# 而不是被后面"工作"里的某个词抢走）。
_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        CATEGORY_RELATION,
        "关系",
        (
            "对象", "男朋友", "女朋友", "恋人", "暧昧", "分手", "吵架", "冷战",
            "闹矛盾", "闹翻", "失恋", "朋友", "室友", "同学", "闺蜜", "兄弟", "想念",
        ),
    ),
    (
        CATEGORY_FAMILY,
        "家人",
        (
            "爸妈", "父母", "家人", "妈妈", "爸爸", "母亲", "父亲", "我妈", "我爸",
            "家里", "催婚", "婆媳", "亲戚", "孩子", "子女",
        ),
    ),
    (
        CATEGORY_HEALTH,
        "身体",
        (
            "失眠", "生病", "头疼", "胃疼", "胃痛", "感冒", "发烧", "疲惫", "睡不着",
        ),
    ),
    (
        CATEGORY_WORK,
        "工作",
        (
            "加班", "老板", "领导", "上司", "甲方", "客户", "绩效", "压力大", "压力",
            "活太多", "活干不完", "忙不过来", "忙死", "好忙", "辞职", "离职", "被裁",
        ),
    ),
)


def classify(text: str) -> EmotionAttribution | None:
    """这句话在吐露情感/压力吗：是就返回 (归因, 分类标签)，不是返回 None。

    纯词表、按 _RULES 顺序先中先算。认不出来返回 None——调用方就当没这回事。
    """
    t = text or ""
    for category, attribution, words in _RULES:
        if any(word in t for word in words):
            return EmotionAttribution(attribution, category)
    return None
