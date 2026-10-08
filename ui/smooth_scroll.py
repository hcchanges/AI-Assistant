"""列表滚动的「顺滑」那一层（v2-social-ui-batch 批次10）。

需求是一句大白话：**助手窗 / 回复方框里上下滚，要滑，不要一跳一跳。**

两处"一跳一跳"的根：

1. **列表默认按「条」滚**（`QAbstractItemView.ScrollPerItem`）：一格滚轮 = 整整一条
   跳过去。这一档不管怎么加动画都顺不起来——值本身就是按条目边界取整的。先换成
   **按像素**（`ScrollPerPixel`），后面的平滑才有落脚点。
2. **滚轮那一路 Qt 自己不带平滑**：一格滚轮 = 立即把滚动条挪一格。所以另挂一个
   事件过滤器把滚轮接过来，自己用一条 ~170ms 的缓动动画把滚动条滑到目标位置
   （连续转几格就在同一条动画上接着走，不会"一格一顿"）。

**拖拽那一路交给 `QScroller`**：`QScroller::grabGesture(..., TouchGesture)` 抓住视口，
撒手之后带惯性继续滑；减速度与"不弹过头"两格属性按桌面口径调（桌面没有触屏那种
回弹，`OvershootAlwaysOff` 关掉它）。滚轮那一路 `QScroller` **不管**，所以两件事
各归各：拖拽走 `QScroller`，滚轮走上面那个过滤器。

零新依赖：`QScroller` / `QScrollerProperties` / `QVariantAnimation` 都在 PySide6 既有
的两块里（`QtWidgets` / `QtCore`），没引别的库，也没自己写定时器。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QVariantAnimation,
)
from PySide6.QtWidgets import QAbstractItemView, QScroller, QScrollerProperties

WHEEL_STEP_PX = 64      # 滚轮一格走多少像素（比"整条跳过去"细得多）
WHEEL_MS = 170          # 一格滚轮滑完用多久：再长就显得肉，再短就看不出滑
DECELERATION = 0.12     # 拖拽撒手之后的减速度（`QScroller` 那一格，越小滑得越远）


class _WheelSmoother(QObject):
    """把"滚轮一格 = 列表跳一截"改成"滚轮一格 = 朝那一头滑过去"。

    只在**滚轮**上动手（事件过滤器挂在视口上）：

    - 目标位置按"当前滑到哪儿 + 这一格该走多少"算，连续转几格就累加成一条更长的
      动画，不会每一格都从头起一段；
    - 到头了（上下限）就停在那儿，不弹、不回弹；
    - **别人挪了滚动条就让路**：`scrollToBottom()` / 拖滑块也会改这个值，这时动画
      必须停——不然它会把刚滚到底的列表又拽回它自己的目标上（加一条新消息那一拍
      正是这么打架的）。
    """

    def __init__(self, area) -> None:
        super().__init__(area)
        self._area = area
        self._bar = area.verticalScrollBar()
        self._target = float(self._bar.value())
        self._applying = False          # 正在由动画自己写值：这一幕不算"别人挪的"
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(WHEEL_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._apply)
        self._bar.valueChanged.connect(self._on_value)
        self._bar.rangeChanged.connect(self._on_range)
        area.viewport().installEventFilter(self)

    # ---------- 滚轮 ----------

    def eventFilter(self, watched, event):      # noqa: N802 —— Qt 的命名
        if event.type() == QEvent.Wheel and watched is self._area.viewport():
            if self._on_wheel(event):
                return True                     # 吃掉：不让列表自己"跳"那一下
        return super().eventFilter(watched, event)

    def _on_wheel(self, event) -> bool:
        """这一格滚轮接过来自己滑；不吃就回 False（没得滚时别白吃人家的事件）。"""
        bar = self._bar
        if bar.maximum() <= bar.minimum():
            return False                        # 内容还没长到要滚：这一下没什么可做的
        notches = -event.angleDelta().y() / 120.0   # 一格 = 120（八分之一度）
        if not notches:
            return False                        # 横向滚轮 / 空事件：不掺和
        running = self._anim.state() != QAbstractAnimation.State.Stopped
        # 上一段还没滑完就接着转：从**它要去的那个地方**再往前，不是从屏幕上那一格
        base = self._target if running else float(bar.value())
        target = min(max(base + notches * WHEEL_STEP_PX,
                         float(bar.minimum())), float(bar.maximum()))
        self._anim.stop()
        if abs(target - bar.value()) < 1:
            self._target = target
            return True                         # 已经贴边：吃掉这一下，什么也不动
        self._target = target
        self._anim.setStartValue(int(bar.value()))
        self._anim.setEndValue(int(target))
        self._anim.start()
        return True

    # ---------- 动画那两拍 ----------

    def _apply(self, value) -> None:
        self._applying = True
        try:
            self._bar.setValue(int(value))
        finally:
            self._applying = False

    def _on_value(self, value: int) -> None:
        if not self._applying:
            self._anim.stop()                   # 见类开头那段：别人挪的就让路

    def _on_range(self, low: int, high: int) -> None:
        """可滚范围变了（又加了一条消息、或者列表被清过）：目标跟着夹回范围内。"""
        if self._anim.state() != QAbstractAnimation.State.Stopped:
            clamped = min(max(self._target, float(low)), float(high))
            if clamped != self._target:
                self._target = clamped
                self._anim.setEndValue(int(clamped))


def enable_smooth_scroll(area):
    """给一个滚动区（助手窗 / 卡内小助手那种 `QListWidget`）装上顺滑滚动。

    返回那个滚轮平滑器（它自己挂在 `area` 底下，调用方不用留着它）。
    """
    # ① 按像素滚（不改这一格，后面的动画会被条目边界取整吃掉，等于白做）
    area.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    # ② 拖拽那一路：抓住视口，撒手带惯性
    props = QScrollerProperties()
    props.setScrollMetric(QScrollerProperties.ScrollMetric.DecelerationFactor,
                          DECELERATION)
    props.setScrollMetric(QScrollerProperties.ScrollMetric.VerticalOvershootPolicy,
                          QScrollerProperties.OvershootPolicy.OvershootAlwaysOff)
    QScroller.scroller(area.viewport()).setScrollerProperties(props)
    QScroller.grabGesture(area.viewport(),
                          QScroller.ScrollerGestureType.TouchGesture)
    # ③ 滚轮那一路：Qt 不管，自己接
    return _WheelSmoother(area)


__all__ = ["DECELERATION", "WHEEL_MS", "WHEEL_STEP_PX", "enable_smooth_scroll"]
