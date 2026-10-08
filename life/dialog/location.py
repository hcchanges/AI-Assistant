"""地点追踪与前后矛盾（v2-chat-core #6 的后半）：换了地方，得先问一句。

用户真机反馈的场景：先在对话里说「我先回广州」，过一会儿又说「我到上海了」。
这两句在回话里**完全接不上**——

1. 回话 prompt 只带当前这一句（`PsychService._compose` 的历史在 v2-chat-core 之前
   是空的），谁也没把前文带过去；
2. 全仓**没有任何**对话矛盾检测（搜过 矛盾/冲突/不一致/contradict，命中的全是屏幕
   情绪融合与文档措辞）；
3. 「到上海」里没有出行动词表里的词，`has_travel_destination` 抓不到它，出行卡也就
   不会被翻出来。

这个模块补第 2 条：记住**上一处提到的地方**，这一处跟它不一样时产出一条
`TravelConflict`。上层拿它做两件事（都在 `ChatterService` 里）——拼一段提示词让宠物
主动把不一致摆出来问一句，以及把出行卡递到眼前。

只认 `life/profile/extract.py` 那份城市名单，不猜、不编。纯内存，跟"一场对话"同寿：
进程退出就没了，不写任何库。
"""

from __future__ import annotations

from dataclasses import dataclass

from life.profile.extract import mentioned_city


@dataclass(frozen=True)
class TravelConflict:
    """用户这一句说的地方，跟前面说过的地方对不上。"""

    previous: str   # 前面说过的地方
    current: str    # 这一句说的地方


class LocationTracker:
    """记住用户上一次把自己放在哪儿。

    用法：每句用户的话都过一遍 `note()`；返回非 None 就是这一次换了地方。
    同一座城连着说多少遍都只算一处（第二次起 previous == current，不报冲突）。
    """

    def __init__(self) -> None:
        self._last = ""

    @property
    def last(self) -> str:
        """上一次提到的地方；还没提到过就是空串。"""
        return self._last

    def note(self, text: str) -> TravelConflict | None:
        """记下这一句提到的地方；换了地方就返回这次的不一致，没换就返回 None。"""
        city = mentioned_city(text)
        if not city:
            return None
        previous, self._last = self._last, city
        if previous and previous != city:
            return TravelConflict(previous=previous, current=city)
        return None

    def reset(self) -> None:
        """忘掉上一次的地方（换了一场对话时用）。"""
        self._last = ""


def conflict_hint(conflict: TravelConflict) -> str:
    """拼成要注入提示词的那一段：把不一致摆出来，问一句。

    措辞上刻意要求"用你自己的话说"——直接念这段会变成机械模板，那正是这一轮
    要治的毛病（#7）。
    """
    return (
        "【前后对不上，先问清楚】他前面说的地方是「"
        f"{conflict.previous}」，这一句说的是「{conflict.current}」。"
        "**先把这个不一致摆出来问他一句**（用你自己的话说，别照抄这一行），"
        "确认到底是换了地方、还是他顺口说错了；不要当没看见接着聊。"
    )
