"""
相对时间的锚定与重新措辞。

问题：用户说"明早开会"，这句话被记下来之后，隔天宠物再念出来时还是"明早"，
指的却已经是说话的那一天——相对时间没有锚定到"说话当天"。

两步解决：
- 入库锚定 anchor()：按说话那天，把原文里的相对说法（明早/今晚/后天/下周三/下个月…）
  折算成绝对时刻，存进 abs_time；正文照原样留着（原文里有相对词也不改）。
- 召回重写 rewrite()：按"现在"重新措辞——还没到就用对的相对词（今天早上/明天），
  已经过去就标明"已经过去了"，不再让"明早"这种词漂在几天之后的话里。

词表只做"天"这一层精度：时点是几点由原文的时段词（早上/下午/晚上）或默认点决定，
不猜用户没说的时间。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

# 只说"明天"这类没带时段的说法时，按这个点锚定
DEFAULT_HOUR = 9

# 时段词 -> 默认小时。长词排在前面，免得"早"抢在"早上"前面匹配
_PERIOD_HOURS = (
    ("清晨", 6),
    ("早上", 8),
    ("早晨", 8),
    ("一早", 8),
    ("上午", 9),
    ("中午", 12),
    ("下午", 14),
    ("傍晚", 18),
    ("晚上", 20),
    ("深夜", 23),
    ("夜里", 22),
    ("凌晨", 5),
    ("早", 8),
    ("晚", 20),
)

# 天词 -> 距说话那天的天数
_DAY_OFFSETS = (
    ("大后天", 3),
    ("大前天", -3),
    ("后天", 2),
    ("前天", -2),
    ("明早", 1),
    ("明晚", 1),
    ("明天", 1),
    ("明日", 1),
    ("今早", 0),
    ("今晚", 0),
    ("今天", 0),
    ("今日", 0),
    ("昨晚", -1),
    ("昨天", -1),
    ("昨日", -1),
)

# 缩略说法自带的时段（"明早" = 明天早上）
_SHORTHAND_PERIOD = {
    "明早": "早上",
    "明晚": "晚上",
    "今早": "早上",
    "今晚": "晚上",
    "昨晚": "晚上",
}

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# "周X"的前缀 -> 距本周的周数
_WEEK_STEP = {
    "下下周": 2,
    "下个星期": 1,
    "下个礼拜": 1,
    "下周": 1,
    "下星期": 1,
    "下礼拜": 1,
    "这周": 0,
    "本周": 0,
    "这个星期": 0,
    "这个礼拜": 0,
    "周": 0,
    "星期": 0,
    "礼拜": 0,
}

# "下个月"这类说法 -> 距本月的月数
_MONTH_STEP = {"下下个月": 2, "下个月": 1, "下月": 1, "这个月": 0, "本月": 0}


def _alts(words: tuple) -> str:
    return "|".join(re.escape(word) for word, _ in words)


_DAY_RE = re.compile(rf"(?P<text>{_alts(_DAY_OFFSETS)})(?P<period>{_alts(_PERIOD_HOURS)})?")
_WEEK_RE = re.compile(
    r"(?P<text>(?P<prefix>下下周|下个星期|下个礼拜|下周|下星期|下礼拜|这周|本周|这个星期|这个礼拜|周|星期|礼拜)"
    r"(?P<wd>[一二三四五六日天]))"
)
_WEEK_ONLY_RE = re.compile(r"(?P<text>下下周|下个星期|下个礼拜|下周|下星期|下礼拜)")
_MONTH_RE = re.compile(r"(?P<text>下下个月|下个月|下月|这个月|本月)")


@dataclass
class RelativeTime:
    """原文里的一处相对时间说法，以及按说话那天锚定出的绝对时刻。"""

    text: str          # 匹配到的原文片断，如"明早"
    start: int
    end: int
    when: datetime     # 锚定出的绝对时刻
    period: str        # 原文自带的时段词（"早上"/"晚上"…），没有则为空串


def _period_of(match: re.Match, shorthand: str) -> tuple[str, int]:
    """取这处说法自带的时段词与对应的小时。"""
    raw = match.groupdict().get("period") or ""
    if shorthand:
        return _SHORTHAND_PERIOD[shorthand], _hour_of(_SHORTHAND_PERIOD[shorthand])
    return raw, (_hour_of(raw) if raw else DEFAULT_HOUR)


def _hour_of(period: str) -> int:
    for word, hour in _PERIOD_HOURS:
        if word == period:
            return hour
    return DEFAULT_HOUR


def _week_when(prefix: str, weekday_word: str, base: datetime) -> datetime:
    monday = base.date() - timedelta(days=base.weekday())
    target = monday + timedelta(days=7 * _WEEK_STEP[prefix] + _WEEKDAYS[weekday_word])
    # 只说"周三"（没前缀）时，如果这个周三已经过了，就理解成最近的、还没到的那个周三
    if _WEEK_STEP[prefix] == 0 and target < base.date():
        target += timedelta(days=7)
    return datetime.combine(target, time(DEFAULT_HOUR, 0))


def _month_when(prefix: str, base: datetime) -> datetime:
    step = _MONTH_STEP[prefix]
    index = base.month - 1 + step
    year = base.year + index // 12
    month = index % 12 + 1
    day = 1 if step > 0 else base.day    # "这个月"就说本月的今天，别当成月初
    return datetime(year, month, day, DEFAULT_HOUR)


def find_all(text: str, base: datetime | None = None) -> list[RelativeTime]:
    """找出文本里所有的相对时间说法，按 base（说话那天）锚定成绝对时刻。"""
    base = base or datetime.now()
    found: list[RelativeTime] = []

    for match in _WEEK_RE.finditer(text):
        found.append(
            RelativeTime(
                text=match.group("text"),
                start=match.start(),
                end=match.end(),
                when=_week_when(match.group("prefix"), match.group("wd"), base),
                period="",
            )
        )
    for match in _WEEK_ONLY_RE.finditer(text):
        monday = base.date() - timedelta(days=base.weekday())
        target = monday + timedelta(days=7 * _WEEK_STEP[match.group("text")])
        found.append(
            RelativeTime(
                text=match.group("text"),
                start=match.start(),
                end=match.end(),
                when=datetime.combine(target, time(DEFAULT_HOUR, 0)),
                period="",
            )
        )
    for match in _MONTH_RE.finditer(text):
        found.append(
            RelativeTime(
                text=match.group("text"),
                start=match.start(),
                end=match.end(),
                when=_month_when(match.group("text"), base),
                period="",
            )
        )
    for match in _DAY_RE.finditer(text):
        shorthand = match.group("text") if match.group("text") in _SHORTHAND_PERIOD else ""
        period, hour = _period_of(match, shorthand)
        target = base.date() + timedelta(days=dict(_DAY_OFFSETS)[match.group("text")])
        found.append(
            RelativeTime(
                text=match.group("text"),
                start=match.start(),
                end=match.end(),
                when=datetime.combine(target, time(hour, 0)),
                period=period,
            )
        )

    # 同一段文字可能被两条规则都命中（"下周三"不会，但"明天早上"的"早上"要跟着"明天"走），
    # 按出现位置排序，重叠的只留先出现的那个
    found.sort(key=lambda item: (item.start, -item.end))
    picked: list[RelativeTime] = []
    last_end = -1
    for item in found:
        if item.start < last_end:
            continue
        picked.append(item)
        last_end = item.end
    return picked


def anchor(text: str, base: datetime | None = None) -> tuple[str, datetime | None]:
    """入库用：取正文里第一处相对时间说法，锚定成绝对时刻。

    返回（原文说法, 绝对时刻）；没找到说法时返回（空串, None）。
    """
    items = find_all(text, base)
    if not items:
        return "", None
    return items[0].text, items[0].when


_PAST_SUFFIX = "（已经过去了）"


def describe(when: datetime, now: datetime | None = None) -> str:
    """按"现在"给一个时刻选措辞：近几天用相对词，远的用绝对日期。"""
    now = now or datetime.now()
    days = (when.date() - now.date()).days
    if days == 3:
        return "大后天"
    if days == 2:
        return "后天"
    if days == 1:
        return "明天"
    if days == 0:
        return "今天"
    if days == -1:
        return "昨天"
    if days == -2:
        return "前天"
    if days == -3:
        return "大前天"
    if when.year != now.year:
        return f"{when.year}年{when.month}月{when.day}日"
    return f"{when.month}月{when.day}日"


def rewrite(text: str, abs_time: datetime | None, now: datetime | None = None) -> str:
    """召回/显示用：按"现在"把正文里那处相对说法改成当前视角的说法。

    abs_time 为空表示入库时就没锚定到时间，此时原样返回——没有锚点就不去猜它指哪天。
    只改第一处说法：一条记忆/提醒通常只锚一个时间点，其余宁可不动，也不臆造。
    """
    if abs_time is None:
        return text
    now = now or datetime.now()
    items = find_all(text, now)
    if not items:
        return text
    first = items[0]
    days = (abs_time.date() - now.date()).days
    replacement = describe(abs_time, now) + first.period
    # 说的是今天及以后、但实际上已经过去了：把"已经过去了"标出来，别让人以为还没到
    if abs_time < now and days >= 0:
        replacement += _PAST_SUFFIX
    return text[: first.start] + replacement + text[first.end :]


def memory_text(memory, now: datetime | None = None) -> str:
    """把一条记忆的正文按"现在"重新措辞（记忆需带 content 与 abs_time）。"""
    return rewrite(memory.content, memory.abs_time, now)


def reminder_text(reminder, now: datetime | None = None) -> str:
    """把一条提醒的正文按"现在"重新措辞（提醒的绝对时间就是 trigger_at）。"""
    return rewrite(reminder.content, reminder.trigger_at, now)
