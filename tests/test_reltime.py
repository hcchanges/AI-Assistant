"""
相对时间锚定与重写的自动测试（对应"相对时间没锚定"那个 bug）。

两部分：
- 纯逻辑：anchor（入库时按说话那天锚定）/ rewrite（召回时按现在重新措辞），
  时间全部由测试给定，不看真实时钟，结果才可重复；
- 数据链路：记忆入库带上了 abs_time，且老库启动时能补上这一列。

基准日期选 2026-09-28（周一），便于算"下周三"这类说法。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from core import reltime
from data.dao import MemoryDAO
from data.db import Database

MONDAY = datetime(2026, 9, 28, 23, 50)      # 周一深夜，用来造"跨午夜"


def main() -> int:
    section("入库锚定：相对说法按说话的日期换算成绝对时刻")
    check(
        "明早 → 次日早上 8 点",
        reltime.anchor("明早去体检", MONDAY)[1] == datetime(2026, 9, 29, 8, 0),
        str(reltime.anchor("明早去体检", MONDAY)),
    )
    check(
        "今晚 → 当天晚上 8 点",
        reltime.anchor("今晚要加班", MONDAY)[1] == datetime(2026, 9, 28, 20, 0),
        str(reltime.anchor("今晚要加班", MONDAY)),
    )
    check(
        "后天 → 两天后",
        reltime.anchor("后天交报告", MONDAY)[1] == datetime(2026, 9, 30, 9, 0),
        str(reltime.anchor("后天交报告", MONDAY)),
    )
    check(
        "下周三 → 下一个自然周的周三",
        reltime.anchor("下周三开会", MONDAY)[1] == datetime(2026, 10, 7, 9, 0),
        str(reltime.anchor("下周三开会", MONDAY)),
    )
    check(
        "下个月 → 下个月 1 号",
        reltime.anchor("下个月搬家", MONDAY)[1] == datetime(2026, 10, 1, 9, 0),
        str(reltime.anchor("下个月搬家", MONDAY)),
    )
    check(
        "明天下午 → 带上时段",
        reltime.anchor("明天下午面试", MONDAY)[1] == datetime(2026, 9, 29, 14, 0),
        str(reltime.anchor("明天下午面试", MONDAY)),
    )
    check(
        "今天 → 当天",
        reltime.anchor("今天好累", MONDAY)[1] == datetime(2026, 9, 28, 9, 0),
        str(reltime.anchor("今天好累", MONDAY)),
    )
    check(
        "整句没有时间说法 → 不锚定",
        reltime.anchor("好累啊", MONDAY) == ("", None),
        str(reltime.anchor("好累啊", MONDAY)),
    )

    section("召回重写：昨天说的\"明早\"，今天不能再说明早")
    text = "用户明早要去体检"
    morning = datetime(2026, 9, 29, 8, 0)      # 昨天 23:50 说的"明早"，锚到了今天早上
    today_early = datetime(2026, 9, 29, 6, 30)
    said = reltime.rewrite(text, morning, today_early)
    check("还没到 → 说\"今天早上\"", said == "用户今天早上要去体检", said)
    check("不再出现\"明早\"", "明早" not in said, said)

    today_late = datetime(2026, 9, 29, 10, 0)
    said = reltime.rewrite(text, morning, today_late)
    check("已经过了 → 标出\"已经过去了\"", said == "用户今天早上（已经过去了）要去体检", said)
    check("过了也不说\"明早\"", "明早" not in said, said)

    section("召回重写：今天说的\"明早\"，明天才是它")
    said = reltime.rewrite("用户明早要去体检", datetime(2026, 9, 30, 8, 0), datetime(2026, 9, 29, 20, 0))
    check("当晚看 → 说\"明天早上\"", said == "用户明天早上要去体检", said)

    section("跨午夜")
    # 周一 23:50 说"明早"（锚到周二 08:00），周二 00:10 就是刚过午夜
    said = reltime.rewrite("用户明早要去体检", morning, datetime(2026, 9, 29, 0, 10))
    check("午夜刚过 → 仍是\"今天早上\"，不该标已过去", said == "用户今天早上要去体检", said)

    section("跨多天")
    # 上周四说的"明天"（9-25），到 9-30 早就过去了
    said = reltime.rewrite("用户明天要去体检", datetime(2026, 9, 25, 9, 0), datetime(2026, 9, 30, 15, 0))
    check("过去多天 → 改说绝对日期", said == "用户9月25日要去体检", said)
    check("不再出现\"明天\"", "明天" not in said, said)

    section("没锚定的记性不瞎改")
    check("abs_time 为空 → 正文照原样", reltime.rewrite("用户明早要去体检", None, MONDAY) == "用户明早要去体检")

    section("提醒：正文按 trigger_at 重写")
    reminder = _Reminder("明早开会", datetime(2026, 9, 29, 8, 0))
    check(
        "到点时念的是\"今天早上\"",
        reltime.reminder_text(reminder, datetime(2026, 9, 29, 8, 0)) == "今天早上开会",
        reltime.reminder_text(reminder, datetime(2026, 9, 29, 8, 0)),
    )

    section("数据链路：记忆入库带 abs_time，老库启动能补列")
    with tempfile.TemporaryDirectory(prefix="pet_reltime_") as tmp:
        db_path = Path(tmp) / "pet.db"
        # 造一个"老库"：memory 表在，但没有 abs_time 列
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE memory (id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, "
            "content TEXT, importance REAL, created_at DATETIME)"
        )
        conn.commit()
        conn.close()

        db = Database(db_path)
        db.init()
        conn = sqlite3.connect(db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        conn.close()
        check("老库启动时补上了 abs_time 列", "abs_time" in columns, str(sorted(columns)))

        dao = MemoryDAO(db.session_factory)
        dao.add(1, "event", "用户明早要去体检", 0.8, abs_time=datetime(2026, 9, 29, 8, 0))
        row = dao.recent(1)[0]
        check("入库后 abs_time 存下来了", row.abs_time == datetime(2026, 9, 29, 8, 0), str(row.abs_time))
        check("正文保留原话（还是\"明早\"）", row.content == "用户明早要去体检", row.content)
        check(
            "召回时按今天重说",
            reltime.memory_text(row, datetime(2026, 9, 29, 10, 0)) == "用户今天早上（已经过去了）要去体检",
            reltime.memory_text(row, datetime(2026, 9, 29, 10, 0)),
        )
        db.engine.dispose()      # Windows 上要先放开句柄，临时目录才删得掉

    return summary("相对时间锚定")


class _Reminder:
    """提醒侧的轻量替身：reltime.reminder_text 只需要 content 与 trigger_at。"""

    def __init__(self, content: str, trigger_at: datetime):
        self.content = content
        self.trigger_at = trigger_at


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
