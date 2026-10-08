"""聊天记录区「滚得顺」的自动测试（v2-social-ui-batch 批次10）。

需求是一句大白话：助手窗 / 回复方框里上下滚，**要滑，不要一跳一跳**。两处根，各一条：

- **按「条」滚**（`QAbstractItemView.ScrollPerItem`，Qt 对 `QListWidget` 的默认档）：
  一格滚轮 = 整整一条跳过去。这一档加什么动画都白搭——值本身按条目边界取整。
  修的是换成 `ScrollPerPixel`。
- **滚轮那一路 Qt 不带平滑**：一格滚轮 = 立即挪一格。修的是挂一个事件过滤器把滚轮接过来，
  用 ~170ms 的缓动动画滑到目标（连续转几格累加成一条更长的，不每格重来）。
  拖拽那一路归 `QScroller`（`grabGesture` 抓住视口，撒手带惯性）。

**这一批测得出什么、测不出什么**：滚轮那一拍是**可测的**——离屏下"发一个滚轮事件、
连着采几拍滚动条的值"就能把"滑过去"与"一步到位"分开（下面 ③ 与 ② 的反向对照就是
这一对）。而拖拽那一路（`QScroller` 的惯性）离屏里没有真的指针拖拽可发，只能验
"抓住了视口 + 两格属性真设进去了"（①），惯性本身得在真机上滑一下才算。

红线：零新依赖（`QScroller` / `QScrollerProperties` / `QVariantAnimation` 都在 PySide6
既有的两块里，没引别的库）；pet.db / mind.db 零写入；只碰画那一层，社交那条线不动。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    ensure_app,
    make_env,
    pump,
    section,
    summary,
)

from PySide6.QtCore import QAbstractAnimation, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QListWidget,
    QScroller,
    QScrollerProperties,
)

from ui import smooth_scroll as smooth_mod  # noqa: E402
from ui.smooth_scroll import DECELERATION, WHEEL_STEP_PX, enable_smooth_scroll  # noqa: E402

LONG_LINE = "这一条挺长的，用来把记录区撑到能滚，好看看滚轮那一格是滑过去还是一步跳过去"


def _wheel(notches: int) -> QWheelEvent:
    """一个滚轮事件：正数往下滚（内容往上走），负数往上滚。"""
    return QWheelEvent(
        QPointF(20.0, 20.0), QPointF(20.0, 20.0),
        QPoint(0, 0), QPoint(0, -120 * notches),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


def _fill(history, count: int) -> None:
    for index in range(count):
        history.addItem(f"{LONG_LINE}（第 {index + 1} 条）")
    pump(0.05)


def _samples(bar, times: int = 12, step: float = 0.02) -> list[int]:
    """滚轮事件发出去之后，连着采滚动条的值。"""
    out = []
    for _ in range(times):
        pump(step)
        out.append(bar.value())
    return out


def main() -> int:
    ensure_app()
    box, stub, app = make_env()
    try:
        bubble_history = app.chat_bubble._history
        app.chat_bubble.show()
        pump(0.05)
        _fill(bubble_history, 40)
        card_history = app.card_panel.card_chat._history
        app.card_panel.card_chat.resize(420, 214)
        app.card_panel.card_chat.show()
        pump(0.05)
        _fill(card_history, 40)

        # ---------- ① 两个记录区都装上了：抓住视口 + 两格属性 ----------

        section("① 两个记录区都装上了顺滑滚动（助手窗 + 卡内小助手）")

        for name, history in (("助手窗", bubble_history), ("卡内小助手", card_history)):
            viewport = history.viewport()
            check(f"{name}：`QScroller` 抓住了这个视口",
                  QScroller.hasScroller(viewport) is True,
                  str(QScroller.hasScroller(viewport)))
            check(f"{name}：抓住的是个真手势（不是「没抓手势」那条零）",
                  int(QScroller.grabbedGesture(viewport)) != 0,
                  str(QScroller.grabbedGesture(viewport)))
            props = QScroller.scroller(viewport).scrollerProperties()
            got = props.scrollMetric(QScrollerProperties.ScrollMetric.DecelerationFactor)
            check(f"{name}：减速度按桌面口径设进去了（撒手之后带惯性滑一段）",
                  abs(float(got) - DECELERATION) < 1e-6, f"{got} / 期望 {DECELERATION}")
            policy = props.scrollMetric(
                QScrollerProperties.ScrollMetric.VerticalOvershootPolicy)
            check(f"{name}：不弹过头（桌面没有触屏那种回弹）",
                  policy == QScrollerProperties.OvershootPolicy.OvershootAlwaysOff,
                  str(policy))
            check(f"{name}：滚的是**像素**不是整条（一跳一跳的另一半根）",
                  history.verticalScrollMode()
                  == QAbstractItemView.ScrollMode.ScrollPerPixel,
                  str(history.verticalScrollMode()))

        # ---------- ② 反向对照：老口径（按条滚）一格滚轮是一步到位 ----------

        section("② 反向对照：按条滚的列表，同一格滚轮是「一步到位」")

        plain = QListWidget()
        plain.resize(320, 200)
        plain.show()
        plain.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        _fill(plain, 40)
        bar = plain.verticalScrollBar()
        check("前置：这张列表确实有得滚（不是滚不动才看不出跳）",
              bar.maximum() > bar.minimum(), f"{bar.minimum()}..{bar.maximum()}")
        start = bar.value()
        QApplication.sendEvent(plain.viewport(), _wheel(1))
        moved = bar.value()
        samples = _samples(bar, times=4)
        check("★ 老口径：一格滚轮当场就跳完了（发出去那一下值就变了，之后一动不动）",
              moved != start and all(v == moved for v in samples),
              f"{start} → 发出后 {moved}，随后 {samples}")

        # ---------- ③ 新口径：滚轮那一格是「滑过去」 ----------

        section("③ 助手窗：一格滚轮滑过去，不是一步到位")

        bar = bubble_history.verticalScrollBar()
        check("前置：助手窗的记录区确实有得滚", bar.maximum() > bar.minimum(),
              f"{bar.minimum()}..{bar.maximum()}")
        bar.setValue(bar.minimum())
        pump(0.05)
        start = bar.value()
        QApplication.sendEvent(bubble_history.viewport(), _wheel(1))
        # 发出去那一下**不许**动：这一条与 ② 的老口径正好相反
        check("★ 滚轮发出去那一下滚动条没动（不是当场跳一格）",
              bar.value() == start, f"{start} → {bar.value()}")
        samples = _samples(bar)
        middle = [v for v in samples if start < v < start + WHEEL_STEP_PX]
        check("★ 中间采到了好几拍（真的在滑，不是只有头尾两下）",
              len(middle) >= 3, repr(samples))
        check("★ 一路朝一个方向走，没有来回抖",
              samples == sorted(samples), repr(samples))
        check(f"★ 滑到「一格 = {WHEEL_STEP_PX}px」停下（不多不少）",
              bar.value() == start + WHEEL_STEP_PX,
              f"{start} → {bar.value()}（期望 {start + WHEEL_STEP_PX}）")
        check("★ 反过来说：这一下比老口径那种「整条跳」细得多（一格只走 "
              f"{WHEEL_STEP_PX}px）", WHEEL_STEP_PX < bar.maximum(),
              f"{WHEEL_STEP_PX} / 全长 {bar.maximum()}")

        # ---------- ④ 连转两格：目标是累加的，不是每格重来 ----------

        section("④ 连着转两格：目标累加（一段更长的滑动，不每格重新起一段）")

        bar.setValue(bar.minimum())
        pump(0.05)
        start = bar.value()
        QApplication.sendEvent(bubble_history.viewport(), _wheel(2))
        first = _samples(bar, times=3)
        rest = _samples(bar, times=14)
        check("★ 两格 = 2 × 一格那个距离（目标按格数累加）",
              bar.value() == start + 2 * WHEEL_STEP_PX,
              f"{start} → {bar.value()}（期望 {start + 2 * WHEEL_STEP_PX}）")
        check("★ 滑的过程中没有在中途「顿一下重新起」（采到的值一路涨上去）",
              first + rest == sorted(first + rest),
              repr(first + rest))

        # ---------- ⑤ 贴边不越界 ----------

        section("⑤ 滑到头就停在那儿，不越界、也不弹")

        bar.setValue(bar.minimum())
        pump(0.05)
        for _ in range(3):
            QApplication.sendEvent(bubble_history.viewport(), _wheel(-1))
            pump(0.25)
        check("★ 在最顶上还往上滚 → 仍旧停在最小值（不越界）",
              bar.value() == bar.minimum(), f"{bar.value()} / min {bar.minimum()}")
        bar.setValue(bar.maximum())
        pump(0.05)
        for _ in range(3):
            QApplication.sendEvent(bubble_history.viewport(), _wheel(1))
            pump(0.25)
        check("★ 在最底下还往下滚 → 仍旧停在最大值", bar.value() == bar.maximum(),
              f"{bar.value()} / max {bar.maximum()}")

        # ---------- ⑥ 别人挪了滚动条，动画让路 ----------

        section("⑥ 滑动还没完就滚到底（加新消息那一拍）→ 动画让路，不被拽回去")

        bar.setValue(bar.minimum())
        pump(0.05)
        QApplication.sendEvent(bubble_history.viewport(), _wheel(1))
        # 这里**不按时间**判"在飞"（离屏下动画第一拍什么时候落说不准），直接看那个动画
        # 自己的状态——它是确定量，"还在跑"就是还没滑完。
        anim = app.chat_bubble._smoother._anim
        check("前置：这一拍动画真在飞（滚轮那一段还没滑完）",
              anim.state() == QAbstractAnimation.State.Running, str(anim.state()))
        bubble_history.scrollToBottom()          # 加一条新消息那一拍就走这一句
        pump(0.3)
        check("★ 让路了：滚到底之后没被在飞的那段动画拽回原目标",
              bar.value() == bar.maximum(), f"{bar.value()} / max {bar.maximum()}")
        check("★ 那一段也确实停了（不是靠运气刚好滑到同一边）",
              anim.state() == QAbstractAnimation.State.Stopped, str(anim.state()))

        # ---------- ⑦ 红线 ----------

        section("⑦ 红线：零新依赖 / 两库零写入 / 只碰画那一层")

        import ast
        import inspect

        tree = ast.parse(inspect.getsource(smooth_mod))
        refs: set[str] = set()          # 只看**模块名**：`from PySide6.QtCore import …`
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.add((node.module or "").split(".")[0])
        refs.discard("__future__")
        refs.discard("")
        check("新那一层只引了 PySide6（没引第三方，也没引本项目别的模块）",
              refs <= {"PySide6"}, "、".join(sorted(refs - {"PySide6"})) or "（只有 PySide6）")
        check("没自己写定时器（用的是 QVariantAnimation）",
              "QTimer" not in inspect.getsource(smooth_mod), "")

        from life.screen import social as social_mod

        social_src = inspect.getsource(social_mod)
        touched = [word for word in ("QScroller", "ScrollPerPixel", "enable_smooth_scroll")
                   if word in social_src]
        check("★ 社交那条线一个字没动（这一批只碰画那一层）",
              not touched, "、".join(touched) or "（一处都没有）")

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        _fill(bubble_history, 2)
        _fill(card_history, 2)
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        box.cleanup()

    return summary("聊天记录区顺滑滚动（v2-social-ui-batch 批次10）")


if __name__ == "__main__":
    raise SystemExit(main())
