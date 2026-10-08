"""
卡面拖动编排（v2-card-tools-C）的自动测试。

对着指令的 S4 五条，加上几条必须一起钉住的边界：

1. **卡上每一块都能拖**（卡头 / 建议 / 辅助工具 / 部件区 / 卡窗），拖完次序真变、
   位置真变，而且**次序与坐标都记到了文件里**（坐标是照实量的，不是编的）；
2. **重启按记录恢复** —— 同一个沙箱再装配一只，次序照记录摆回来；
3. **「重置布局」** —— 记录删掉、次序回到默认、按钮自己收起来；
4. **跟"卡面本来是响应式"的边界** —— 没摆过的卡一个字不变（默认次序、不建记录、
   收起来的那几块不留空档），记录里多出来的 / 少掉的块号都不会让卡上少一块；
5. **不抢控件的点击** —— 只在把手上按下去才算拖，按在块身上（输入框、按钮）不算。

另外两件不能省的：**换卡不串**（这张卡拖成的样子不许跑到别的卡上）、
**没碰用户真机上的 data/card_layout/**（沙箱那条红线）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from harness import build_app, check, drain_threads, make_env, pump, section, summary
from life.cards.layout import (
    BLOCK_ADVICE,
    BLOCK_BODY,
    BLOCK_CHAT,
    BLOCK_HEAD,
    BLOCK_TOOLS,
    BLOCK_WIDGETS,
    DEFAULT_ORDER,
    WORK_DEFAULT_ORDER,
    apply_order,
    is_default,
)

ROOT = Path(__file__).resolve().parents[1]
REMINDER = {"type": "reminder", "props": {"label": "喝水", "at": "15:00"}}


# ---------- 小工具 ----------


def _trip_card(app, title: str = "成都行"):
    app.card_service.create_manual("出行", title, "✈️", "玩一周")
    return next(c for c, _pos in app.card_service.plaza() if c.title == title)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _mouse(kind, widget, local: QPoint) -> None:
    """造一个真实的鼠标事件送给控件（走的是控件自己的事件处理，不绕过它）。"""
    event = QMouseEvent(
        kind,
        QPointF(local),
        QPointF(widget.mapToGlobal(local)),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def _drag(board, key: str, to_y: int) -> None:
    """按住 key 的把手、拖到板子上 to_y 那个高度、松开。

    中间发**两次**移动：第一次让 `_drag_to` 算出新位置（真实拖动中也是连续多次），
    第二次确认停在原地不再乱动——一次就松手的话，"移动那一下算错了"这类问题看不出来。
    """
    grip = board.grip(key)
    _mouse(QEvent.MouseButtonPress, grip, QPoint(grip.width() // 2, 4))
    _mouse(QEvent.MouseMove, grip, grip.mapFrom(board, QPoint(5, to_y)))
    _mouse(QEvent.MouseMove, grip, grip.mapFrom(board, QPoint(5, to_y)))
    _mouse(QEvent.MouseButtonRelease, grip, grip.mapFrom(board, QPoint(5, to_y)))


def _record(app, namespace: str) -> Path:
    return Path(app.card_layout_store.path(namespace))


def _read(app, namespace: str) -> dict:
    path = _record(app, namespace)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------- S1 · 拖动真能换位置，而且记得下来 ----------


def test_drag_reorders_and_records() -> None:
    section("S1 卡上每一块都能拖：拖完次序变、位置变、记录落盘")
    box, stub, app = make_env()
    try:
        trip = _trip_card(app)
        panel = app.card_panel
        panel.show_card(trip.id)
        pump(0.2)
        board = panel.board
        namespace = f"life:{trip.id}"

        check("卡面上有五块（卡头 / 建议 / 辅助工具 / 部件区 / 卡窗）",
              board.keys() == list(DEFAULT_ORDER), f"{board.keys()!r}")
        check("每一块都配了一个把手",
              all(board.grip(key) is not None for key in board.keys()),
              f"{[k for k in board.keys() if board.grip(k) is None]!r}")
        check("没摆过的时候次序就是默认次序（跟这一版以前一模一样）",
              board.order() == list(DEFAULT_ORDER), f"{board.order()!r}")
        check("没摆过的卡不建记录（不替用户建）",
              not _record(app, namespace).exists(), f"{_record(app, namespace)}")
        check("「重置布局」这时候不亮（没有可重置的）",
              panel._reset_button.isHidden())

        before_order = board.order()
        before_pos = board.positions()
        mem_before = _digest(box.dir / "pet.db")
        mind_before = _digest(box.dir / "mind.db")

        _drag(board, BLOCK_CHAT, 2)
        pump(0.2)

        check("拖完次序真变了（卡窗挪到了最前面）",
              board.order() == [BLOCK_CHAT, BLOCK_HEAD, BLOCK_ADVICE, BLOCK_TOOLS, BLOCK_WIDGETS],
              f"{before_order!r} -> {board.order()!r}")
        check("位置也是真变了（卡窗的 y 从最后一位挪到了 0）",
              before_pos[BLOCK_CHAT]["y"] > 0 and board.positions()[BLOCK_CHAT]["y"] == 0,
              f"{before_pos[BLOCK_CHAT]} -> {board.positions()[BLOCK_CHAT]}")
        check("拖完每一块的坐标还是从上到下递增的（没有重叠）",
              _sorted_by_y(board.positions()), f"{board.positions()!r}")
        check("坐标是照实量的：x 就是把手那一栏的宽度，不是编的",
              all(pos["x"] == 18 for pos in board.positions().values()),
              f"{board.positions()!r}")

        record = _read(app, namespace)
        check("这一下记到了文件里", record.get("order") == board.order(), f"{record!r}")
        check("记录里的坐标就是刚才量到的那一份（不是另算的）",
              record.get("pos") == board.positions(),
              f"记录={record.get('pos')!r} 板上={board.positions()!r}")
        check("记录落在沙箱目录下（没碰用户真机上的 data/card_layout/）",
              _record(app, namespace).is_relative_to(box.dir),
              f"{_record(app, namespace)}")
        check("用户真实的 data/card_layout 没被建出来",
              not (ROOT / "data" / "card_layout").exists())

        check("拖过之后「重置布局」亮出来了", not panel._reset_button.isHidden())
        check("摆卡面不碰 pet.db", _digest(box.dir / "pet.db") == mem_before)
        check("摆卡面不碰 mind.db", _digest(box.dir / "mind.db") == mind_before)
    finally:
        box.cleanup()
        drain_threads()


def _sorted_by_y(positions: dict) -> bool:
    ys = [pos["y"] for _key, pos in sorted(positions.items(), key=lambda kv: kv[1]["y"])]
    return all(b > a for a, b in zip(ys, ys[1:]))


# ---------- S2 · 重启还在 ----------


def test_layout_survives_restart() -> None:
    section("S2 重启之后卡面还是拖成的那个样子")
    box, stub, app = make_env()
    try:
        trip = _trip_card(app)
        panel = app.card_panel
        panel.show_card(trip.id)
        pump(0.2)
        _drag(panel.board, BLOCK_CHAT, 2)
        pump(0.2)
        dragged = panel.board.order()
        record = _read(app, f"life:{trip.id}")

        drain_threads()
        app2 = build_app()          # 同一份沙箱配置重开一只：新服务、重新读盘
        panel2 = app2.card_panel
        panel2.show_card(trip.id)
        pump(0.3)

        check("重启之后次序照记录摆回来了",
              panel2.board.order() == dragged == record["order"],
              f"记录={record['order']!r} 重启后={panel2.board.order()!r}")
        check("重启之后每一块都还在（没有谁被记录吃掉）",
              sorted(panel2.board.order()) == sorted(DEFAULT_ORDER),
              f"{panel2.board.order()!r}")
        check("重启之后「重置布局」还亮着", not panel2._reset_button.isHidden())
        check("重启之后量到的坐标，跟记录里那一份对得上",
              all(panel2.board.positions()[k]["y"] == v["y"]
                  for k, v in record["pos"].items() if k in panel2.board.positions()),
              f"板上={panel2.board.positions()!r} 记录={record['pos']!r}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- S3 · 重置布局 ----------


def test_reset_puts_everything_back() -> None:
    section("S3「重置布局」：记录删掉、次序回默认、按钮自己收起来")
    box, stub, app = make_env()
    try:
        trip = _trip_card(app)
        panel = app.card_panel
        panel.show_card(trip.id)
        pump(0.2)
        _drag(panel.board, BLOCK_CHAT, 2)
        pump(0.2)
        check("先确认真的摆过（记录在、次序不是默认）",
              _record(app, f"life:{trip.id}").exists()
              and panel.board.order() != list(DEFAULT_ORDER),
              f"{panel.board.order()!r}")

        panel._reset_button.click()
        pump(0.2)

        check("次序回到了默认那一个", panel.board.order() == list(DEFAULT_ORDER),
              f"{panel.board.order()!r}")
        check("记录文件被删掉了（不是写一份默认次序进去）",
              not _record(app, f"life:{trip.id}").exists())
        check("按钮自己也收起来了", panel._reset_button.isHidden())
        check("重置之后每一块都还在", sorted(panel.board.order()) == sorted(DEFAULT_ORDER))
    finally:
        box.cleanup()
        drain_threads()


# ---------- S4 · 跟"老记录 / 响应式布局"的边界 ----------


def test_stale_record_never_eats_a_block() -> None:
    section("S4 记录对不上现在的卡：认识的照排、不认识的丢掉、缺的补到最后")
    check("记录里多了一个不认识的块号 → 丢掉它，别的照记的次序排",
          apply_order(["chat", "没有这一块", "head"], ["head", "advice", "tools", "widgets", "chat"])
          == ["chat", "head", "advice", "tools", "widgets"])
    check("记录里少了几个块 → 缺的补到最后（一块都不会消失）",
          apply_order(["chat"], ["head", "advice", "tools", "widgets", "chat"])
          == ["chat", "head", "advice", "tools", "widgets"])
    check("记录里同一个块号重复 → 只算一次",
          apply_order(["chat", "chat", "head"], ["head", "chat"]) == ["chat", "head"])
    check("记录是空的（没摆过 / 按了重置）→ 默认次序",
          apply_order([], ["head", "advice", "tools", "widgets", "chat"])
          == ["head", "advice", "tools", "widgets", "chat"])
    check("「是不是默认」拿调用方那份比（工作卡是另一套块）",
          is_default(list(WORK_DEFAULT_ORDER), WORK_DEFAULT_ORDER)
          and not is_default(list(DEFAULT_ORDER), WORK_DEFAULT_ORDER))
    check("生活卡与工作卡的默认次序不一样（所以不能由记录层替它猜）",
          DEFAULT_ORDER != WORK_DEFAULT_ORDER
          and BLOCK_BODY not in DEFAULT_ORDER
          and BLOCK_BODY in WORK_DEFAULT_ORDER)


def test_untouched_card_stays_untouched() -> None:
    section("S4 没摆过的卡：看不见的东西不留空档，看得见的按老样子排")
    box, stub, app = make_env()
    try:
        trip = _trip_card(app)
        panel = app.card_panel
        panel.show_card(trip.id)
        pump(0.2)
        board = panel.board

        check("部件区一个部件都没有时，它那一行（连把手）也收起来了",
              panel.card_tools.isHidden() and board.row(BLOCK_WIDGETS).isHidden(),
              f"部件区={panel.card_tools.isHidden()} 行={board.row(BLOCK_WIDGETS).isHidden()}")
        check("收起来的那一块不记位置（它在卡上这会儿就没有位置）",
              BLOCK_WIDGETS not in board.positions(), f"{board.positions()!r}")
        check("它还在次序里（只是暂时不露面，不是被删了）",
              BLOCK_WIDGETS in board.order())

        stub.tools_result = {"components": [REMINDER], "say": "好"}
        panel.card_chat._input.setText("加个喝水提醒")
        panel.card_chat._send_button.click()
        pump(0.5)
        check("长出部件之后那一行跟着放回来（不会被落在收起来的状态里）",
              not panel.card_tools.isHidden() and not board.row(BLOCK_WIDGETS).isHidden(),
              f"部件区={panel.card_tools.isHidden()} 行={board.row(BLOCK_WIDGETS).isHidden()}")
        check("放回来之后它又出现在卡面上、也有了位置",
              BLOCK_WIDGETS in board.positions()
              and board.positions()[BLOCK_WIDGETS]["y"] > board.positions()[BLOCK_TOOLS]["y"],
              f"{board.positions()!r}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- S5 · 不抢控件、不串卡 ----------


def test_drag_only_from_the_grip() -> None:
    section("S5 只有按在把手上才算拖：块身上的输入框 / 按钮照旧归它们自己")
    box, stub, app = make_env()
    try:
        trip = _trip_card(app)
        panel = app.card_panel
        panel.show_card(trip.id)
        pump(0.2)
        board = panel.board
        before = board.order()

        # 按在建议区身上（块自己）拖到最上面：**不该**换位置
        pane = panel._advice_pane
        _mouse(QEvent.MouseButtonPress, pane, QPoint(pane.width() // 2, 5))
        _mouse(QEvent.MouseMove, pane, QPoint(pane.width() // 2, 5))
        _mouse(QEvent.MouseButtonRelease, pane, QPoint(pane.width() // 2, 5))
        pump(0.1)
        check("在块身上按一下拖，次序一动不动", board.order() == before,
              f"{before!r} -> {board.order()!r}")
        check("卡面上没有留下拖动中的痕迹（那一行没被标成正在拖）",
              not any(board.row(key).property("dragging") for key in board.keys()))

        # 把手按下去、**没松手**的时候是"正在拖"，松开就结束
        grip = board.grip(BLOCK_CHAT)
        _mouse(QEvent.MouseButtonPress, grip, QPoint(grip.width() // 2, 4))
        check("按住把手的时候那一行被标成正在拖",
              bool(board.row(BLOCK_CHAT).property("dragging")))
        _mouse(QEvent.MouseButtonRelease, grip, QPoint(grip.width() // 2, 4))
        check("松开之后标记撤掉了",
              not board.row(BLOCK_CHAT).property("dragging"))
    finally:
        box.cleanup()
        drain_threads()


def test_two_cards_do_not_share_a_layout() -> None:
    section("S5 一张卡拖成的样子不会跑到另一张卡上")
    box, stub, app = make_env()
    try:
        first = _trip_card(app, "成都行")
        second = _trip_card(app, "上海行")
        panel = app.card_panel

        panel.show_card(first.id)
        pump(0.2)
        _drag(panel.board, BLOCK_CHAT, 2)
        pump(0.2)
        dragged = panel.board.order()

        panel.show_card(second.id)
        pump(0.2)
        check("换到另一张卡，次序是它自己的默认次序",
              panel.board.order() == list(DEFAULT_ORDER), f"{panel.board.order()!r}")
        check("另一张卡没有跟着建记录",
              not _record(app, f"life:{second.id}").exists())

        panel.show_card(first.id)
        pump(0.2)
        check("回到原来那张卡，拖成的样子还在", panel.board.order() == dragged,
              f"{dragged!r} -> {panel.board.order()!r}")
        check("两张卡的记录各自一份",
              _record(app, f"life:{first.id}").name != _record(app, f"life:{second.id}").name)
    finally:
        box.cleanup()
        drain_threads()


if __name__ == "__main__":
    test_drag_reorders_and_records()
    test_layout_survives_restart()
    test_reset_puts_everything_back()
    test_stale_record_never_eats_a_block()
    test_untouched_card_stays_untouched()
    test_drag_only_from_the_grip()
    test_two_cards_do_not_share_a_layout()
    sys.exit(1 if summary() else 0)
