"""
今日活动报告（对应指令 5 第 4 节）。

把当天记录整理成一段能直接念给用户看的文字：应用排行、类别占比、最长一段、时间黑洞。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

TOP_APPS = 5                     # 排行里最多列几个应用
BLACK_HOLE_SEC = 30 * 60         # 至少半小时才够格叫"时间黑洞"
BLACK_HOLE_CATEGORIES = ("娱乐", "摸鱼")


def format_duration(seconds: int) -> str:
    """秒 → "2 小时 5 分" / "12 分"。"""
    minutes = max(0, int(seconds)) // 60
    if minutes < 60:
        return f"{minutes} 分"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时 {rest} 分" if rest else f"{hours} 小时"


@dataclass
class DayReport:
    day: date
    total_sec: int
    apps: list[tuple[str, str, int]]        # (显示名, 类别, 秒)，按时长降序
    categories: list[tuple[str, int]]       # (类别, 秒)，按时长降序
    longest: tuple[str, int] | None         # 连续使用最久的一段：显示名 + 秒

    @property
    def empty(self) -> bool:
        return not self.apps

    @property
    def black_hole(self) -> tuple[str, int] | None:
        """时间黑洞：娱乐/摸鱼里花得最多的那个应用（不够久就不提）。"""
        for name, category, seconds in self.apps:   # apps 已按时长降序
            if category in BLACK_HOLE_CATEGORIES and seconds >= BLACK_HOLE_SEC:
                return name, seconds
        return None

    def render(self, top: int = TOP_APPS) -> str:
        if self.empty:
            return "今天还没记到什么呢，我一直在看着～"
        lines = [
            f"今日活动（{self.day.strftime('%m-%d')}）",
            f"总共用了 {format_duration(self.total_sec)}",
            "",
        ]
        for index, (name, _category, seconds) in enumerate(self.apps[:top], start=1):
            ratio = round(seconds / self.total_sec * 100) if self.total_sec else 0
            lines.append(f"{index}. {name}  {format_duration(seconds)}（{ratio}%）")
        rest_sec = sum(seconds for _, _, seconds in self.apps[top:])
        if rest_sec > 0:
            lines.append(f"…另外 {len(self.apps) - top} 个应用共 {format_duration(rest_sec)}")
        lines.append("")
        if self.longest is not None:
            lines.append(f"最长一段：{self.longest[0]} {format_duration(self.longest[1])}")
        if self.categories:
            lines.append(
                "类别占比：" + " · ".join(f"{c} {format_duration(s)}" for c, s in self.categories)
            )
        hole = self.black_hole
        if hole is not None:
            lines.append(f"时间黑洞：{hole[0]} {format_duration(hole[1])}")
        return "\n".join(lines)
