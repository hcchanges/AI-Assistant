"""
对象攻略卡 · 卡内攻略提示 + 节点提醒（v2-card-suite 增量②）的自动测试。

两块：

1. **纯抽取**：`extract_reminder_nodes` 从一段对方信息里抽出「触发时刻 + 提醒文案」的节点，
   时间全部由测试给定（固定 base），结果可重复——覆盖带年绝对日期、不带年月日、相对时间、
   生日/纪念日标签、去重、以及"小数 / 版本号不被误认成日期"。

2. **卡内攻略提示**：卡窗回话前看这张卡的专属知识库目录里有没有《对方信息.md》，
   有 → 提示词里多喂一段攻略口径（`guide_hint`）；没有 → 空串不占地方。

3. **数据链路**：对方信息里带日期建卡时，真会走现有提醒线（ReminderDAO + ReminderJobs）
   落进 pet.db 的 reminder 表，且到点都在将来。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.cards.chat import GUIDE_KB_FILE, guide_hint
from life.cards.reminders import ReminderNode, extract_reminder_nodes
from life.screen import social as smod

# 基准日期：2026-10-05（周一），相对时间的锚定都按它算，结果可重复
BASE = datetime(2026, 10, 5, 12, 0)


def _nodes(text: str) -> list[ReminderNode]:
    return extract_reminder_nodes(text, BASE)


def test_pure_extraction() -> None:
    section("纯抽取：对方信息里的时间点 → 提醒节点（固定基准日）")

    check(
        "生日（不带年月日）→ 今年还没到的这一天，早上 9 点",
        _nodes("对方生日是10月20日")
        == [ReminderNode(datetime(2026, 10, 20, 9, 0), "对方生日（10月20日）")],
        str(_nodes("对方生日是10月20日")),
    )
    check(
        "相对时间「明早」→ 复用 reltime 锚到次日早上 8 点",
        _nodes("明早约了体检")
        == [ReminderNode(datetime(2026, 10, 6, 8, 0), "重要日子（10月6日）")],
        str(_nodes("明早约了体检")),
    )
    check(
        "相对时间「下个月」→ 下个月 1 号",
        _nodes("下个月搬家")
        == [ReminderNode(datetime(2026, 11, 1, 9, 0), "重要日子（11月1日）")],
        str(_nodes("下个月搬家")),
    )
    check(
        "带年绝对日期 + 纪念日标签",
        _nodes("2026年12月1日是纪念日")
        == [ReminderNode(datetime(2026, 12, 1, 9, 0), "纪念日（12月1日）")],
        str(_nodes("2026年12月1日是纪念日")),
    )
    check(
        "ISO 式日期「2026-11-11」也能认",
        _nodes("2026-11-11 那天")
        == [ReminderNode(datetime(2026, 11, 11, 9, 0), "重要日子（11月11日）")],
        str(_nodes("2026-11-11 那天")),
    )
    check(
        "不带年的日子已经过了 → 落到明年",
        _nodes("1月1日")
        == [ReminderNode(datetime(2027, 1, 1, 9, 0), "重要日子（2027年1月1日）")],
        str(_nodes("1月1日")),
    )
    check(
        "同一时刻同一句话只留一条（去重）",
        len(_nodes("10月20日和10月20日")) == 1,
        str(_nodes("10月20日和10月20日")),
    )
    check("空文本 → 没有节点", _nodes("") == [], str(_nodes("")))
    check(
        "小数 / 版本号不被误认成日期（没有「月」、没有四位年份）",
        _nodes("版本 3.14 和 10.5") == [],
        str(_nodes("版本 3.14 和 10.5")),
    )
    check(
        "两个时间点按触发时间排好序，标签各归各（不把隔壁的生日抢到明早头上）",
        _nodes("10月20日生日，明早体检")
        == [
            ReminderNode(datetime(2026, 10, 6, 8, 0), "重要日子（10月6日）"),
            ReminderNode(datetime(2026, 10, 20, 9, 0), "对方生日（10月20日）"),
        ],
        str(_nodes("10月20日生日，明早体检")),
    )


def test_guide_hint_detection() -> None:
    section("卡内攻略提示：有《对方信息.md》才算对象攻略卡")
    check("没有卡上下文 → 空串", guide_hint({}) == "", repr(guide_hint({})))
    check("非 dict 的上下文 → 空串，不崩", guide_hint(None) == "", repr(guide_hint(None)))

    with tempfile.TemporaryDirectory(prefix="pet_guide_") as tmp:
        check("库目录里没有《对方信息.md》 → 空串",
              guide_hint({"kb": tmp}) == "", repr(guide_hint({"kb": tmp})))
        (Path(tmp) / GUIDE_KB_FILE).write_text("对方是谁：相亲对象\n", encoding="utf-8")
        hint = guide_hint({"kb": tmp})
        check("有《对方信息.md》 → 返回攻略口径", "当攻略卡用" in hint, hint)
        check("口径里点明了约会 / 送礼 / 找话题", "约会" in hint and "送礼" in hint, hint)


def test_guide_card_schedules_reminders() -> None:
    section("数据链路：带日期的对方信息建卡 → 走现有提醒线落库")
    box, stub, app = make_env()
    try:
        card = app.card_service.create_manual_card(
            smod.CARD_CATEGORY, smod.GUIDE_CARD_TITLE, smod.GUIDE_CARD_ICON, smod.GUIDE_CARD_GOAL
        )
        check("建了一张对象攻略卡", card is not None, str(card))
        before = len(app.reminder_dao.list_all(limit=100))
        text = "对方是谁：相亲对象\n生日：10月20日\n下个月约了一起看展"
        app._write_guide_card_kb(card, text)
        made = app._schedule_guide_reminders(card, text)
        rows = app.reminder_dao.list_all(limit=100)
        check("抽出了两个提醒节点（生日 + 下个月）", made == 2, f"made={made}")
        check("提醒真落进了 pet.db 的 reminder 表",
              len(rows) == before + made, f"{before} -> {len(rows)}")
        check("生日的提醒带「对方生日」标签",
              any("对方生日" in r.content for r in rows),
              " | ".join(r.content for r in rows))
        check("提醒的时间都在将来（不会建一个已经过了的点）",
              all(r.trigger_at > datetime.now() for r in rows),
              str([(r.content, r.trigger_at) for r in rows]))
        check("提醒的 utter_id 为空（不是从某句话里来的）",
              all(r.utter_id is None for r in rows[-made:]),
              str([r.utter_id for r in rows[-made:]]))
    finally:
        box.cleanup()
        drain_threads()


def test_guide_card_chat_gets_advice_hint() -> None:
    section("卡内攻略提示：对象攻略卡窗回话带上了攻略口径")
    box, stub, app = make_env()
    try:
        card = app.card_service.create_manual_card(
            smod.CARD_CATEGORY, smod.GUIDE_CARD_TITLE, smod.GUIDE_CARD_ICON, smod.GUIDE_CARD_GOAL
        )
        app._write_guide_card_kb(card, "对方是谁：相亲对象\n兴趣/爱好：咖啡、看展\n生日：10月20日")
        app.card_panel.show_card(card.id)
        panel = app.card_panel.card_chat
        ns = f"life:{card.id}"

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        panel._input.setText("第一次约会去哪好？")
        QTest.mouseClick(panel._send_button, Qt.LeftButton)
        got = wait_for(lambda: not app.card_chat_service.busy_with(ns), 5)
        pump(0.2)
        check("回话回到了卡窗", got, f"busy={app.card_chat_service.busy_with(ns)}")

        card_prompts = [p for p in stub.prompts if "专属小窗" in p]
        check("卡窗提示词里带上了攻略口径（当攻略卡用）",
              any("当攻略卡用" in p for p in card_prompts),
              f"{len(card_prompts)} 次卡窗提示词")
    finally:
        box.cleanup()
        drain_threads()


def main() -> int:
    test_pure_extraction()
    test_guide_hint_detection()
    test_guide_card_schedules_reminders()
    test_guide_card_chat_gets_advice_hint()
    return summary("对象攻略卡：卡内攻略提示 + 节点提醒（v2-card-suite 增量②）")


if __name__ == "__main__":
    raise SystemExit(main())
