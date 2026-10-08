"""
卡牌触发阈值模型（对应指令 13 第 2~4 节，指令 25-B 第 2 项改成"三门槛"）。

同一个品类在 7 天里反复被吐槽，就该问一句"要不要给它建张卡"：

  门槛一 · 攒次数：7 天内同品类 ≥3 次负向（这条最常用）
  门槛二 · 看强度：任意一条情绪 ≥0.7（很强），单条就直接触发
  门槛三 · 可行动：话题得是能做成卡的那一类（危机表达、心理类议题一律不成卡，
                   见 life/cards/spawnable.py）

"负向"怎么算（指令 25-B 第 1 项）：命中本地词表的吐槽词就算一次（词表本身就是吐槽词），
词表没命中、由模型判类进来的，得情绪分 ≥0.5 才算。判定记在观察记录上，这里只管数。

为什么要这样：阈值太低会变成"随口一提就被追问"的打扰，太高又会错过真正难受的时候，
所以卡在"已经反复出现、但还没到烦人"的平衡点上；单条强情绪是例外——那种当下就该接住。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

# 窗口：7 天内同品类反复出现才算
WINDOW = timedelta(days=7)
# 门槛一：同品类至少几次负向
NEGATIVE_COUNT = 3
# 门槛二：单条到这个强度就直接触发
STRONG_NEGATIVE = 0.7
# 词表没命中、由模型判类时，情绪分到多少才算"负向"
NEGATIVE = 0.5


@dataclass(frozen=True)
class CategoryStats:
    """一次评估的结果。"""

    total: int                  # 7 天窗口内这个品类观察到几次
    negatives: int              # 其中几次算负向
    emotion_max: float
    trigger_count: bool         # 门槛一：攒够 3 次负向
    trigger_strong: bool        # 门槛二：有一条强情绪

    @property
    def triggered(self) -> bool:
        return self.trigger_count or self.trigger_strong


def is_negative(event) -> bool:
    """这条观察算不算一次负向：词表命中记为 True；模型判类的看情绪分。"""
    if event.negative is not None:
        return bool(event.negative)
    return event.emotion_score >= NEGATIVE


def evaluate(events: Sequence, now: datetime) -> CategoryStats:
    """按三门槛算一遍。events 需要按时间正序（窗口取最近 7 天）。"""
    window = [event for event in events if now - event.created_at <= WINDOW]

    negatives = sum(1 for event in window if is_negative(event))
    emotion_max = max((event.emotion_score for event in window), default=0.0)
    return CategoryStats(
        total=len(window),
        negatives=negatives,
        emotion_max=emotion_max,
        trigger_count=negatives >= NEGATIVE_COUNT,
        trigger_strong=emotion_max >= STRONG_NEGATIVE,
    )
