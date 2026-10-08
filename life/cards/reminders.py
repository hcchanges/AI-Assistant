"""
对象攻略卡 · 节点提醒抽取（v2-card-suite 增量②）。

从对方信息里把"约会 / 生日 / 事件"的时间点抽成提醒节点，交给现有的提醒线
（`data.dao.ReminderDAO` + `scheduler.jobs.ReminderJobs`）落库、到点提醒。

这里只做**纯抽取**——给定一段文字，给出几个「触发时刻 + 提醒文案」的节点；落库与调度
由装配处（core/bootstrap）去做，本模块不碰 pet.db、不碰调度器、不引任何新依赖。

支持三类时间点：

- 带年的绝对日期："2026年10月20日" / "2026-10-20"；
- 不带年的月日："10月20日" / "10月20号" / "10月20"——按"下一个还没到的这一天"锚定
  （生日、纪念日这类每年都有的日子）；
- 相对时间："明早 / 下周五 / 下个月"——复用 core.reltime 的词表与锚定。

每个节点带一个标签（对方生日 / 纪念日 / 约会 / 重要日子），按时间点前后一小段文字里
有没有这些词来定，没有就落"重要日子"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from core.reltime import DEFAULT_HOUR, find_all

# 带年的绝对日期："2026年10月20日" / "2026-10-20" / "2026/10/20" / "2026.10.20"
_FULL_DATE_RE = re.compile(
    r"(?P<year>\d{4})\s*[年/\-.]\s*(?P<month>\d{1,2})\s*[月/\-.]\s*(?P<day>\d{1,2})\s*[日号]?"
)

# 不带年的月日："10月20日" / "10月20号" / "10月20"（必须带"月"，免得撞上小数、版本号）
_MONTH_DAY_RE = re.compile(r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?")

# 时间点前后一小段里出现这些词 → 给节点定个更贴切的标签（先长后短，免得"纪念"抢在
# "纪念日"前面）。没有命中就落"重要日子"。
_LABELS = (
    ("纪念日", "纪念日"),
    ("周年", "纪念日"),
    ("生日", "对方生日"),
    ("约会", "约会"),
    ("见面", "约会"),
    ("纪念", "纪念日"),
)

# 给时间点定标签时，只在它所在的"小句"里找（逗号 / 句号 / 换行把两件事隔开），
# 别跨过去把隔壁时间点的"生日"抢到这一条头上。冒号不算是隔断——"生日：10月20日"
# 这种"标签 + 冒号 + 日期"的写法很常见，隔断了就认不出标签了。
_CLAUSE_BREAKS = "，。；、！？\n\r\t,.!?;"


@dataclass(frozen=True)
class ReminderNode:
    """一个提醒节点：到 trigger_at 弹一句 content。"""

    trigger_at: datetime
    content: str


def extract_reminder_nodes(text: str, base: datetime | None = None) -> list[ReminderNode]:
    """把文字里的时间点抽成提醒节点（去重后按触发时间升序）。

    base 是"说话的当天"（相对时间按它锚定，不带年的月日也按它判断今年还是明年）；
    不给就用当前时刻。
    """
    base = base or datetime.now()
    body = (text or "").strip()
    if not body:
        return []

    occupied: list[tuple[int, int]] = []
    nodes: list[ReminderNode] = []

    # 带年的绝对日期先占位，避免下面那条"不带年"的正则把它的"10月20日"再抽一遍
    for match in _FULL_DATE_RE.finditer(body):
        node = _full_date_node(match, body, base)
        if node is not None:
            nodes.append(node)
            occupied.append((match.start(), match.end()))

    for match in _MONTH_DAY_RE.finditer(body):
        if _overlaps((match.start(), match.end()), occupied):
            continue
        node = _month_day_node(match, body, base)
        if node is not None:
            nodes.append(node)
            occupied.append((match.start(), match.end()))

    for rel in find_all(body, base):
        if _overlaps((rel.start, rel.end), occupied):
            continue
        label = _label_for(body, rel.start, rel.end)
        nodes.append(ReminderNode(rel.when, _content(label, rel.when, base)))
        occupied.append((rel.start, rel.end))

    # 去重（同一个时刻同一句话只留一条），再按触发时间排好
    seen: set[tuple[datetime, str]] = set()
    unique: list[ReminderNode] = []
    for node in nodes:
        key = (node.trigger_at, node.content)
        if key in seen:
            continue
        seen.add(key)
        unique.append(node)
    unique.sort(key=lambda node: node.trigger_at)
    return unique


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in occupied)


def _full_date_node(match: re.Match, text: str, base: datetime) -> ReminderNode | None:
    when = _at(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    if when is None:
        return None
    label = _label_for(text, match.start(), match.end())
    return ReminderNode(when, _content(label, when, base))


def _month_day_node(match: re.Match, text: str, base: datetime) -> ReminderNode | None:
    when = _next_occurrence(int(match.group("month")), int(match.group("day")), base)
    if when is None:
        return None
    label = _label_for(text, match.start(), match.end())
    return ReminderNode(when, _content(label, when, base))


def _at(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, DEFAULT_HOUR, 0)
    except ValueError:
        return None


def _next_occurrence(month: int, day: int, base: datetime) -> datetime | None:
    """不带年的月日 → 下一个还没到的这一天（今年过了就落到明年）。"""
    try:
        candidate = datetime(base.year, month, day, DEFAULT_HOUR, 0)
    except ValueError:
        return None
    if candidate.date() < base.date():
        try:
            candidate = datetime(base.year + 1, month, day, DEFAULT_HOUR, 0)
        except ValueError:
            return None
    return candidate


def _label_for(text: str, start: int, end: int) -> str:
    left = start
    while left > 0 and text[left - 1] not in _CLAUSE_BREAKS:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _CLAUSE_BREAKS:
        right += 1
    clause = text[left:right]
    for word, label in _LABELS:
        if word in clause:
            return label
    return "重要日子"


def _fmt(when: datetime, base: datetime) -> str:
    if when.year != base.year:
        return f"{when.year}年{when.month}月{when.day}日"
    return f"{when.month}月{when.day}日"


def _content(label: str, when: datetime, base: datetime) -> str:
    return f"{label}（{_fmt(when, base)}）"


__all__ = ["ReminderNode", "extract_reminder_nodes"]
