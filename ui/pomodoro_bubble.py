"""
番茄钟倒计时气泡：贴在宠物旁边，上面一行写阶段、下面一行写剩余时间。

和说话气泡一样是独立的无边框透明窗口，跟着宠物位置走，不抢焦点。
阶段名 + 颜色一起区分（专注=暖橙、短休=柔绿、长休=蓝紫），不开面板也能一眼看出
现在到底是短休还是长休；空闲时自动收起。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from focus.pomodoro import PHASE_TITLES, Phase
from ui.theme import (
    BUBBLE_BG,
    BUBBLE_BORDER,
    POMODORO_IDLE,
    POMODORO_PHASE_COLORS,
    POMODORO_TRACK,
    TEXT,
)

BUBBLE_W = 104
BUBBLE_H = 50
RING_MARGIN = 10       # 圆环外接方框距上/下边距
RING_WIDTH = 4         # 圆环线宽
TEXT_LEFT = 46         # 文字区左边（让开圆环）
TEXT_RIGHT = 8         # 文字区距右边距
SIDE_GAP = 16          # 气泡与宠物边缘的间距
SCREEN_MARGIN = 6      # 气泡与屏幕边缘的最小距离


class PomodoroBubble(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(BUBBLE_W, BUBBLE_H)

        self._color = QColor(POMODORO_IDLE)
        self._fraction = 0.0
        self._title = PHASE_TITLES[Phase.IDLE]
        self._text = "00:00"
        self._pet_rect: QRect | None = None

    # ---------- 对外 ----------

    def apply_state(self, remaining: int, planned: int, phase_key: str) -> None:
        """刷新倒计时；phase_key 为 idle 时收起飞泡。"""
        if phase_key == "idle":
            self.hide_bubble()
            return
        planned = max(1, int(planned))
        self._fraction = max(0.0, min(1.0, 1.0 - remaining / planned))
        self._text = f"{remaining // 60:02d}:{remaining % 60:02d}"
        phase = Phase(phase_key)
        self._title = PHASE_TITLES[phase]
        self._color = QColor(POMODORO_PHASE_COLORS.get(phase_key, POMODORO_IDLE))
        if self._pet_rect is not None:
            pos = self._position(self._pet_rect)
            if pos != (self.x(), self.y()):
                self.move(*pos)   # 位置没变就别搬窗口，搬窗口比重绘贵得多
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def hide_bubble(self) -> None:
        self.hide()

    def follow(self, pet_rect: QRect) -> None:
        """宠物移动时同步位置；宠物还没定位过时只记下来，等显示时再用。"""
        self._pet_rect = pet_rect
        if self.isVisible() and self._pet_rect is not None:
            self.move(*self._position(pet_rect))

    # ---------- 内部 ----------

    def _position(self, pet_rect: QRect) -> tuple[int, int]:
        """默认贴在宠物右侧，右边放不下就换到左侧。"""
        screen = QGuiApplication.screenAt(pet_rect.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = pet_rect.right() + SIDE_GAP
        if x + self.width() > area.right() - SCREEN_MARGIN:
            x = pet_rect.left() - SIDE_GAP - self.width()
        y = pet_rect.center().y() - self.height() // 2
        x = max(area.left() + SCREEN_MARGIN, min(x, area.right() - self.width() - SCREEN_MARGIN))
        y = max(area.top() + SCREEN_MARGIN, min(y, area.bottom() - self.height() - SCREEN_MARGIN))
        return x, y

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        body = QRectF(0.75, 0.75, self.width() - 1.5, self.height() - 1.5)
        radius = self.height() / 2
        painter.setPen(QPen(QColor(BUBBLE_BORDER), 1.5))
        painter.setBrush(QColor(BUBBLE_BG))
        painter.drawRoundedRect(body, radius, radius)

        ring = QRectF(
            RING_MARGIN,
            RING_MARGIN,
            self.height() - 2 * RING_MARGIN,
            self.height() - 2 * RING_MARGIN,
        )
        pen = QPen(QColor(POMODORO_TRACK), RING_WIDTH)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(ring)
        if self._fraction > 0.0:
            pen.setColor(self._color)
            painter.setPen(pen)
            # Qt 角度以 1/16 度为单位、逆时针为正；从正上方起算，取负值即顺时针推进
            painter.drawArc(ring, 90 * 16, int(-360 * 16 * self._fraction))

        text_x = TEXT_LEFT
        text_w = self.width() - TEXT_LEFT - TEXT_RIGHT

        title_font = QFont(self.font())
        title_font.setPointSizeF(8)
        painter.setFont(title_font)
        painter.setPen(self._color)
        painter.drawText(QRectF(text_x, 9, text_w, 15), Qt.AlignCenter, self._title)

        time_font = QFont(self.font())
        time_font.setPointSizeF(12.5)
        time_font.setBold(True)
        painter.setFont(time_font)
        painter.setPen(QColor(TEXT))
        painter.drawText(QRectF(text_x, 23, text_w, 20), Qt.AlignCenter, self._text)
