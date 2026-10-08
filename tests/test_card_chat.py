"""
卡内小助手（v2-card-tools-A）的自动测试。

这一轮验三件事（对着指令的 S4 ①③④；那一轮的 S4-②「工作卡底有聊天窗」没随公开版发布，
公开树里没有工作卡，这个用例一并摘了——见 work/ 的空壳与 `ui/panels/work_card_panel.py`）：

1. **生活卡底有聊天窗**：卡面最底下真嵌着 `CardChatPanel`，绑到的是**这张卡**的卡号，
   而且它是卡面上的控件（`isWindow()` 为假），不是另一个飘着的窗口；
2. **卡窗不串门**：卡窗里说的话**不进** pet.db（utterance / memory 一行不增）、
   **不进** mind.db（文件一字不变）、**不进**全局那个气泡，也**不进**别的卡——
   但它**确实**被记下来了（记在按卡号分开的那一份 JSON 里，落在沙箱目录中）；
3. **全局窗口照常**：全局那条链一个字没改，`chatter_service.handle` 照旧收到话。

另外钉一条红线：**危机词在卡窗里也最高优先**——不调模型、不落卡窗记录，
话交给宠物本体按全局同一段话术说。

隔离这件事不能只在提示词里"写一句别串"，所以这里既查**行为**（另一张卡、全局真的
什么也没收到），也查**落点**（记录文件在不在沙箱的 card_chat/ 下、pet.db 涨没涨）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from harness import Recorder, check, drain_threads, make_env, pump, section, summary, wait_for

CARD_LINE = "我下周去成都，想抽一天去看熊猫"
GLOBAL_LINE = "我养了一只叫阿黄的金毛"
CRISIS_LINE = "我真的不想活了"


# ---------- 小工具 ----------


def _mem_counts(box) -> dict[str, int]:
    """pet.db 里那两张"记忆"的表的行数（卡窗一句话都不许往这儿写）。"""
    conn = sqlite3.connect(box.dir / "pet.db")
    try:
        return {
            table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("utterance", "memory")
        }
    finally:
        conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _card(service, title: str):
    for card, _pos in service.plaza():
        if card.title == title:
            return card
    return None


def _texts(panel) -> list[str]:
    return [panel._history.item(i).text() for i in range(panel._history.count())]


def _send(panel, text: str) -> bool:
    """在卡窗里真打一句话、真点一下「发送」（不绕过按钮那条路）。

    按钮没启用就不硬点，返回 False——那说明这个窗当时不该收话（正等回话 / 没绑卡）。
    """
    panel._input.setText(text)
    if not panel._send_button.isEnabled():
        return False
    QTest.mouseClick(panel._send_button, Qt.LeftButton)
    return True


# ---------- S4-① 生活卡底有聊天窗 ----------


def test_life_card_has_chat_window() -> None:
    section("S4-① 生活卡底嵌着这张卡自己的聊天窗")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        trip = _card(app.card_service, "成都行")
        diet = _card(app.card_service, "少喝奶茶")
        check("两张生活卡都建出来了（出行 / 餐饮）", trip is not None and diet is not None)

        app.card_panel.show_card(trip.id)
        panel = app.card_panel.card_chat
        check(
            "出行卡底部有聊天窗，绑的是这张卡的卡号",
            panel.namespace == f"life:{trip.id}",
            f"namespace={panel.namespace!r}",
        )
        check("它是卡面上的控件，不是另一个能飘走的窗口", not panel.isWindow())
        check("聊天窗真摆进了卡面那一列（在卡面那块板上，见 v2-card-tools-C）",
              app.card_panel.board.row("chat") is not None
              and app.card_panel.board.row("chat").block is panel,
              f"板上的行={app.card_panel.board.row('chat')}")
        check(
            "窗眉写着这张卡的名字",
            "成都行" in panel._title.text(),
            f"标题={panel._title.text()!r}",
        )
        check("有输入框、有发送键，能直接在里面说话",
              panel._input.isEnabled() and panel._send_button.isEnabled())

        app.card_panel.show_card(diet.id)
        check(
            "换到餐饮卡，窗跟着换成那张卡的号",
            panel.namespace == f"life:{diet.id}",
            f"namespace={panel.namespace!r}",
        )
        check("换卡时记录区是空的（这张卡还没说过话）", _texts(panel) == [], f"{_texts(panel)!r}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- S4-③ 卡窗不串门 ----------


def test_card_chat_is_isolated() -> None:
    section("S4-③ 卡窗里说的话不进全局、不进别的卡")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        trip = _card(app.card_service, "成都行")
        diet = _card(app.card_service, "少喝奶茶")

        # 对照：先让**全局**那条链真说一句话——pet.db 的 utterance 该涨。
        # （不装 Recorder：装了的话 handle 被换成探针，链本身就不跑了，对照就假了）
        before = _mem_counts(box)
        app._on_chat_send(GLOBAL_LINE)
        drain_threads()
        pump(0.3)
        after_global = _mem_counts(box)
        check(
            "对照：全局那句话确实进了 pet.db 的 utterance",
            after_global["utterance"] > before["utterance"],
            f"{before['utterance']} -> {after_global['utterance']}",
        )

        # 打开出行卡，在卡窗里说一句
        app.card_panel.show_card(trip.id)
        panel = app.card_panel.card_chat
        ns = f"life:{trip.id}"
        mind_before = _digest(box.dir / "mind.db")
        mem_before = _mem_counts(box)
        bubble_before = app.chat_bubble._history.count()
        prompts_before = len(stub.prompts)

        chat_rec = Recorder(app.chatter_service, "handle")
        clicked = _send(panel, CARD_LINE)
        got = wait_for(lambda: not app.card_chat_service.busy_with(ns), 5)
        pump(0.2)
        chat_rec.restore()

        check("点下发送后它开始想，并且回话回到了这个窗",
              clicked and got, f"clicked={clicked} busy={app.card_chat_service.busy_with(ns)}")
        mem_after = _mem_counts(box)
        check(
            "卡窗那句话没进 pet.db 的 utterance",
            mem_after["utterance"] == mem_before["utterance"],
            f"{mem_before['utterance']} -> {mem_after['utterance']}",
        )
        check(
            "卡窗那句话也没在 pet.db 里留下记忆",
            mem_after["memory"] == mem_before["memory"],
            f"{mem_before['memory']} -> {mem_after['memory']}",
        )
        check("mind.db 一个字节没动", _digest(box.dir / "mind.db") == mind_before)
        check(
            "卡窗那条路根本没经过全局聊天链（chatter_service.handle 没被叫过）",
            not chat_rec.called,
            f"calls={chat_rec.calls}",
        )
        check(
            "全局那个气泡里一条都没多",
            app.chat_bubble._history.count() == bubble_before,
            f"{bubble_before} -> {app.chat_bubble._history.count()}",
        )
        check(
            "界面上的记录区有这两条（我说的 + 它说的）",
            _texts(panel) == [CARD_LINE, stub.text],
            f"{_texts(panel)!r}",
        )

        # 落点：真写下来了，而且写在沙箱里（不是用户真实的 data/card_chat/）
        store = app.card_chat_service._store
        path = Path(store.path(ns))
        check(
            "这句话确实被记下来了（记在这张卡自己的那一份里）",
            store.load(ns) == [{"who": "user", "text": CARD_LINE},
                               {"who": "pet", "text": stub.text}],
            f"{store.load(ns)!r}",
        )
        check("记录落在沙箱目录下（没碰用户真实的 data/card_chat/）",
              path.is_relative_to(box.dir), f"path={path}")

        # 提示词里带上的是这张卡的上下文，不是全局记忆
        card_prompt = stub.prompts[-1] if len(stub.prompts) > prompts_before else ""
        check("卡窗的提示词里带着这张卡（类型 / 名字 / 卡上现状）",
              "出行卡" in card_prompt and "成都行" in card_prompt and "玩一周" in card_prompt,
              f"prompt 片段={card_prompt[:60]!r}")
        check("卡窗的提示词里没有全局那句记忆（不串全局）",
              GLOBAL_LINE not in card_prompt and "阿黄" not in card_prompt)

        # 界面这一层的隔离（跟提示词、落盘那两道并列的第三道）：卡号对不上的回话，
        # 这个窗一个字都不接
        app.bus.card_chat_reply.emit(f"life:{diet.id}", "隔壁卡的话")
        check("别的卡号的回话进不了这个窗", _texts(panel) == [CARD_LINE, stub.text],
              f"{_texts(panel)!r}")
        check("卡窗的回话也不会落进全局那个气泡",
              app.chat_bubble._history.count() == bubble_before,
              f"{bubble_before} -> {app.chat_bubble._history.count()}")
        app.bus.card_chat_reply.emit(ns, "这张卡的话")
        check("自己卡号的回话照常进来", _texts(panel)[-1:] == ["这张卡的话"],
              f"{_texts(panel)!r}")

        # 换到另一张卡：隔壁一个字都看不见
        app.card_panel.show_card(diet.id)
        diet_ns = f"life:{diet.id}"
        diet_path = Path(store.path(diet_ns))
        diet_text = diet_path.read_text(encoding="utf-8") if diet_path.exists() else ""
        check("换到餐饮卡，记录区空了（隔壁听不见）", _texts(panel) == [], f"{_texts(panel)!r}")
        check("餐饮卡自己的那一份里也是空的", app.card_chat_service.history(diet_ns) == [])
        check("两张卡的记录是两个文件", store.path(ns) != store.path(diet_ns))
        check("餐饮卡那份里没有出行卡那句话", CARD_LINE not in diet_text)

        # 换回出行卡：说过的话还在
        app.card_panel.show_card(trip.id)
        check("换回出行卡，刚才那两句还在", _texts(panel) == [CARD_LINE, stub.text],
              f"{_texts(panel)!r}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- S4-④ 全局窗口照常 ----------


def test_global_chat_untouched() -> None:
    section("S4-④ 全局那个窗照常（一个字没改）")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        trip = _card(app.card_service, "成都行")
        app.card_panel.show_card(trip.id)
        ns = f"life:{trip.id}"
        bubble_before = app.chat_bubble._history.count()

        rec = Recorder(app.chatter_service, "handle")
        app.chat_bubble._input.setText(GLOBAL_LINE)
        QTest.mouseClick(app.chat_bubble._send_button, Qt.LeftButton)
        pump(0.2)
        check("全局那句话照旧交给 chatter_service", rec.called and rec.last[0] == GLOBAL_LINE,
              f"calls={rec.calls}")
        rec.restore()
        check("全局气泡里出现了我说的那句",
              app.chat_bubble._history.count() == bubble_before + 1,
              f"{bubble_before} -> {app.chat_bubble._history.count()}")
        check("卡窗没接住全局那句话（记录区里没有）",
              GLOBAL_LINE not in _texts(app.card_panel.card_chat),
              f"{_texts(app.card_panel.card_chat)!r}")
        check("全局那句话没写进卡窗的记录", app.card_chat_service.history(ns) == [],
              f"{app.card_chat_service.history(ns)!r}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- 红线：危机词在卡窗里也最高优先 ----------


def test_crisis_in_card_window() -> None:
    section("红线：危机词在卡窗里也最高优先")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        trip = _card(app.card_service, "成都行")
        app.card_panel.show_card(trip.id)
        panel = app.card_panel.card_chat
        ns = f"life:{trip.id}"

        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        notes: list[tuple[str, str]] = []
        app.bus.card_chat_note.connect(lambda n, t: notes.append((n, t)))
        replies: list[tuple[str, str]] = []
        app.bus.card_chat_reply.connect(lambda n, t: replies.append((n, t)))

        # 只数**卡窗那条链**发出的提示词：卡面自己的建议流（`advise_card`）本来就会
        # 在打开卡的时候调一次模型，数全部的会把那一笔记到这句话头上，冤枉它。
        def _card_chat_prompts() -> list[str]:
            return [p for p in stub.prompts if "专属小窗" in p]

        prompts_before = len(_card_chat_prompts())
        _send(panel, CRISIS_LINE)
        pump(0.3)
        prompts_after = _card_chat_prompts()

        check("危机话术交给了宠物本体说（不是卡窗自己回）",
              any("12356" in line for line in spoken), f"spoken={spoken}")
        check("卡窗没有为这句话调模型", len(prompts_after) == prompts_before,
              f"{prompts_before} -> {len(prompts_after)}")
        check("危机那句话没有进过任何一次模型调用",
              not any(CRISIS_LINE in p for p in stub.prompts))
        check("这句话没有作为卡窗回话冒出来", replies == [], f"{replies!r}")
        check("这句话没写进这张卡的记录", app.card_chat_service.history(ns) == [],
              f"{app.card_chat_service.history(ns)!r}")
        check("卡窗里留了一句交代（说明它被当成要紧的事处理了）", bool(notes), f"{notes!r}")
        check("输入框没被永远锁住（没有卡在「在想…」）", not panel.busy)
    finally:
        box.cleanup()
        drain_threads()


def main() -> int:
    test_life_card_has_chat_window()
    test_card_chat_is_isolated()
    test_global_chat_untouched()
    test_crisis_in_card_window()
    return summary("卡内小助手（v2-card-tools-A）：")


if __name__ == "__main__":
    raise SystemExit(main())
