"""
指令 26-A 的自动测试：安抚语料库 + 情绪分级 + 提示词注入 + 重情绪主动关怀 + 对话框样式。

离屏、桩模型、不联网：
- 语料库、分级、判题都是本地逻辑，直接测；
- "提示词里有没有按级别注入示例"用桩模型接住发给模型的消息来看；
- 重情绪关怀用"连着两次重情绪"这个事件驱动，不真等 7 天；
- 对话框只量参数（字号 / 行距 / 内边距 / 间距 / 气泡宽），不截图。
真模型的验收（连问 3 次"好累"，看回复重不重复、引没引用记忆）在 test_live_comfort.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, ensure_app, make_env, pump, section, summary, wait_for

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QStyleOptionViewItem

from life.psych import comfort
from life.psych.comfort import HEAVY, LIGHT, MEDIUM
from life.psych.persona_loader import DEFAULT_CHAT_PERSONA
from ui import chat_bubble
from ui.theme import CHAT_QSS


def _test_library() -> None:
    lib = comfort.ComfortLibrary()
    section("语料库：三级 × 五主题，每条都是共情 + 陪伴")
    thin: list[str] = []
    broken: list[str] = []
    preachy: list[str] = []
    for level in comfort.LEVELS:
        for topic in comfort.TOPIC_WORDS:
            items = lib.samples(level, topic, limit=99)
            if len(items) < 5:
                thin.append(f"{level}/{topic} {len(items)} 条")
            for item in items:
                if not item.empathy or not item.company:
                    broken.append(f"{level}/{topic}: {item}")
                if "你应该" in item.company or "你必须" in item.company:
                    preachy.append(f"{level}/{topic}: {item.company}")
    check("每级每主题至少 5 条（共 75 条）", not thin, str(thin))
    check("每条都是「共情 + 陪伴」两句", not broken, str(broken[:2]))
    check("没有「你要 / 你应该」式说教", not preachy, str(preachy[:2]))
    check(
        "同一句多次取到的示例会变（不是每次同一批）",
        len({tuple(s.empathy for s in lib.samples(HEAVY, "失眠", 2)) for _ in range(6)}) > 1,
        "六次里出现过不止一批",
    )


def _test_levels() -> None:
    section("分级：按情绪分三级，一听就重的说法直接判重")
    check("0.2 分 → 轻", comfort.level_of(0.2, "今天有点累") == LIGHT, comfort.level_of(0.2, "今天有点累"))
    check("0.5 分 → 中", comfort.level_of(0.5, "压力挺大的") == MEDIUM, comfort.level_of(0.5, "压力挺大的"))
    check("0.8 分 → 重", comfort.level_of(0.8, "烦") == HEAVY, comfort.level_of(0.8, "烦"))
    check(
        "分数不高但说「崩溃」→ 也按重处理",
        comfort.level_of(0.1, "我真的快崩溃了") == HEAVY,
        comfort.level_of(0.1, "我真的快崩溃了"),
    )
    check(
        "「连续失眠」→ 重",
        comfort.level_of(0.3, "连续失眠好几天了") == HEAVY,
        comfort.level_of(0.3, "连续失眠好几天了"),
    )

    section("判题：五个主题，判不出就用最泛用的那套")
    check("地铁 → 通勤", comfort.topic_of("地铁又挤爆了") == "通勤", comfort.topic_of("地铁又挤爆了"))
    check(
        "外卖 → 饮食挫败",
        comfort.topic_of("今天又点外卖了") == "饮食挫败",
        comfort.topic_of("今天又点外卖了"),
    )
    check("睡不好 → 失眠", comfort.topic_of("昨晚又没睡好") == "失眠", comfort.topic_of("昨晚又没睡好"))
    check("项目要交 → 加班", comfort.topic_of("项目明天要交") == "加班", comfort.topic_of("项目明天要交"))
    check(
        "判不出来 → 情绪低落",
        comfort.topic_of("嗯") == comfort.DEFAULT_TOPIC,
        comfort.topic_of("嗯"),
    )


def _test_hint() -> None:
    section("提示词注入：按级别给 2~3 条示例，并要求引用记得的事")
    lib = comfort.library()
    hint = lib.comfort_hint(0.5, "今天又加班到十一点")
    check("写着这次的分寸与话题", "中（压力大、焦虑）" in hint and "话题：加班" in hint, hint.splitlines()[0])
    check("给了 3 条参考话术", hint.count("- 共情：") == 3, str(hint.count("- 共情：")))
    check("要求引用记得的事、别生硬复述", "别生硬复述" in hint, "在")
    check("平静或开心的话不注入（不用安抚口径）", lib.comfort_hint(0.0, "今天天气不错") == "", "")

    heavy = lib.comfort_hint(0.9, "连续失眠好几天了")
    light = lib.comfort_hint(0.2, "有点累")
    check(
        "轻与重给的示例不是同一批",
        heavy != light and "重（崩溃、连续失眠）" in heavy and "轻（有点累、有点烦）" in light,
        heavy.splitlines()[0] + " / " + light.splitlines()[0],
    )


def _test_prompt_flow() -> None:
    box, stub, app = make_env()
    try:
        section("对话流：安抚请求真的把分寸段带给了模型")
        stub.messages.clear()
        app.psych_service.respond("今天又加班到十一点，好累", emotion_score=0.5)
        wait_for(lambda: bool(stub.messages), timeout=3.0)
        payload = stub.messages[-1] if stub.messages else []
        systems = "".join(m["content"] for m in payload if m["role"] == "system")
        check("system 里带上了这次的语气分寸", "这次的语气分寸" in systems, systems[:40])
        check("也带上了记得的事那一段", "你记得关于用户的事" in systems, "在")
        check(
            "分寸段排在记忆段前面（记忆紧挨用户那句话）",
            systems.index("这次的语气分寸") < systems.index("你记得关于用户的事"),
            "顺序对",
        )

        stub.messages.clear()
        app.psych_service.respond("今天天气不错", emotion_score=0.0)
        wait_for(lambda: bool(stub.messages), timeout=3.0)
        payload = stub.messages[-1] if stub.messages else []
        systems = "".join(m["content"] for m in payload if m["role"] == "system")
        check("没情绪的话不注入分寸段", "这次的语气分寸" not in systems, "没有")
    finally:
        drain_threads()
        box.cleanup()


def _test_heavy_care() -> None:
    box, _stub, app = make_env()
    try:
        section("重情绪：连着两次 → 排一句关怀；关怀过就静默")
        service = app.heavy_care_service
        proactive = app.proactive_service

        service.note("我今天真的崩溃了", "心理安抚", "情绪", 0.9, DEFAULT_CHAT_PERSONA)
        check("第一次重情绪只记账，不开口", not proactive._care_pending, proactive._care_pending or "空")

        service.note("又撑不住了", "心理安抚", "情绪", 0.85, DEFAULT_CHAT_PERSONA)
        line = proactive._care_pending
        check("同一主题第二次重情绪 → 排上一句关怀", bool(line), line[:20])
        check("关怀话术是本地挑的整句（共情 + 陪伴）", len(line) >= 15, f"{len(line)} 字")

        proactive._care_pending = ""
        service.note("第三次崩溃了", "心理安抚", "情绪", 0.9, DEFAULT_CHAT_PERSONA)
        check("关怀过之后同一主题先静默（不再追着问）", not proactive._care_pending, "静默")

        section("重情绪关怀走的是主动搭话那条线（共用门控）")
        proactive.queue_care("（测试）陪你说一句")
        check("排进去的话放得住", proactive._care_pending == "（测试）陪你说一句", proactive._care_pending)
        proactive._care_pending = ""
    finally:
        drain_threads()
        box.cleanup()


def _test_no_card() -> None:
    box, _stub, app = make_env()
    try:
        section("伦理边界：重情绪只安抚，绝不产卡")
        proposed: list[str] = []
        app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))
        for _ in range(3):
            app.card_service.observe(
                "我最近很崩溃，压力大到撑不住", "作息", "加班", 0.95, DEFAULT_CHAT_PERSONA
            )
        check("连着三次重情绪也不出卡", proposed == [], str(proposed))
        app.card_service.observe("加班到崩溃，实在受不了了", "其他", "加班", 0.9, DEFAULT_CHAT_PERSONA)
        check("重情绪混在加班话题里也不出卡", proposed == [], str(proposed))
    finally:
        drain_threads()
        box.cleanup()


def _test_bubble_style() -> None:
    ensure_app()
    section("对话框：字号 / 行距 / 留白 / 间距 / 气泡宽")
    check("消息字号 14px（比原来 +2）", "font-size: 14px" in CHAT_QSS, "在")
    check("行距 ≥1.4", chat_bubble.LINE_HEIGHT >= 140, f"{chat_bubble.LINE_HEIGHT}%")
    check("气泡上下内边距 ≥10px", chat_bubble.BUBBLE_PAD_Y >= 10, f"{chat_bubble.BUBBLE_PAD_Y}px")
    check("消息间距加大（原来 4）", chat_bubble.ITEM_GAP > 4, f"{chat_bubble.ITEM_GAP}px")
    check("气泡宽上限 ≤78%", chat_bubble.MAX_BUBBLE_RATIO <= 0.78, f"{chat_bubble.MAX_BUBBLE_RATIO:.0%}")

    bubble = chat_bubble.ChatBubble("天渌")
    bubble.setAttribute(Qt.WA_DontShowOnScreen, True)
    bubble.show()
    pump(0.05)
    bubble._append("很长的一句话" * 12, chat_bubble.SPEAKER_USER)
    pump(0.05)

    option = QStyleOptionViewItem()
    option.widget = bubble._history
    option.font = bubble._history.font()
    option.rect = QRect(0, 0, bubble._history.viewport().width(), 0)
    size = bubble._history.itemDelegate().sizeHint(
        option, bubble._history.model().index(0, 0)
    )
    limit = bubble._history.viewport().width() * chat_bubble.MAX_BUBBLE_RATIO
    check(
        "很长的消息也没有撑破 78%",
        size.width() <= limit + 1,
        f"气泡 {size.width()}px / 上限 {limit:.0f}px",
    )
    check(
        "气泡高度里含了上下留白",
        size.height() > 2 * chat_bubble.BUBBLE_PAD_Y,
        f"{size.height()}px",
    )
    bubble.close()


def main() -> int:
    _test_library()
    _test_levels()
    _test_hint()
    _test_prompt_flow()
    _test_heavy_care()
    _test_no_card()
    _test_bubble_style()
    return summary("对话体验：安抚语料 + 分级注入 + 重情绪关怀 + 对话框")


if __name__ == "__main__":
    sys.exit(main())
