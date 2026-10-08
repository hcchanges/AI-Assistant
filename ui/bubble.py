"""
说话气泡：桌宠头顶弹出的圆角对话框。

自己画（圆角 + 小尖角），单独一个无边框透明窗口，跟着宠物位置移动，
显示若干秒后淡出。对应方案 P2 的"文字气泡"。
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from pet.persona import split_kaomoji
from ui.theme import BUBBLE_BG, BUBBLE_BORDER, BUBBLE_QSS, TEXT_MUTED

PADDING = 12
TAIL_HEIGHT = 12
CORNER = 12
MAX_TEXT_WIDTH = 240
SHOW_MS = 9000

# 气泡的"来源"档（v2-pose-sync）：同一个气泡窗口谁都能写，但**清的时候得认得出是谁那条**。
# 打盹的 zzz 是**姿态的附属品**——睡觉这个动作一结束（演完、或被打断去演别的），它就该
# 跟着消失；而正文 / 开场白 / 情绪词是**独立的一句话**，寿命自己说了算，不该被姿态连坐。
# 所以 zzz 单独一档，其余一律走默认的 `KIND_SPEECH`。
KIND_SPEECH = "speech"
KIND_ZZZ = "zzz"


def kaomoji_html(text: str) -> str | None:
    """把一句话做成"颜文字淡一档"的富文本；一个颜文字都没有就返回 None。

    返回 None 时调用方走**原文那条路**——没有颜文字的那绝大多数气泡跟加这一手之前
    逐字节相同，不引入任何排版风险。

    判据跟语音那条线共用 `persona.split_kaomoji`：**淡化的和剥掉的永远是同一批字符**，
    不会出现"这个在语音里被剥了、在界面上又没淡"的分叉。
    """
    segments = split_kaomoji(text)
    if not any(is_kaomoji for _part, is_kaomoji in segments):
        return None
    parts: list[str] = []
    for part, is_kaomoji in segments:
        escaped = escape(part).replace("\n", "<br/>")
        if is_kaomoji:
            parts.append(f'<span style="color:{TEXT_MUTED}">{escaped}</span>')
        else:
            parts.append(escaped)
    return "".join(parts)


class _DimTextLabel(QLabel):
    """会按段上色的 QLabel：颜文字淡一档，别的一个字不动。

    **`.text()` 仍然回纯文本**，跟加这一手之前一模一样：这句话的"原文"是它对外的
    唯一说法，颜色只是渲染层的事。这样所有读 `_label.text()` 的地方（测试断言、
    排查时想看它到底说了什么）一个字都不用改。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._plain = ""

    def setText(self, text: str) -> None:      # noqa: N802 (Qt 命名)
        raw = str(text or "")
        self._plain = raw
        rich = kaomoji_html(raw)
        if rich is None:
            # 每次都要显式定死格式：富文本那一次会把格式粘住，下一次的原文
            # （比如 "捂眼睛的那种(>_<)"）就会被当成标签吃掉。
            self.setTextFormat(Qt.PlainText)
            super().setText(raw)
        else:
            self.setTextFormat(Qt.RichText)
            super().setText(rich)

    def text(self) -> str:
        return self._plain


class SpeechBubble(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setObjectName("SpeechBubble")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(BUBBLE_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(PADDING, PADDING, PADDING, PADDING + TAIL_HEIGHT)
        self._label = _DimTextLabel(self)
        self._label.setObjectName("BubbleText")
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_timeout)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(260)
        self._fade.setEasingCurve(QEasingCurve.InOutQuad)
        self._fade.finished.connect(self.hide)

        # 这会儿这条是谁写的（v2-pose-sync）：`hide_bubble(kind=...)` 据此判断该不该收。
        self._kind = KIND_SPEECH

    # ---------- 对外 ----------

    @property
    def kind(self) -> str:
        """屏上这条是谁写的（`KIND_*`）。"""
        return self._kind

    def show_text(
        self,
        text: str,
        pet_rect: QRect,
        show_ms: int = SHOW_MS,
        kind: str = KIND_SPEECH,
    ) -> None:
        """在宠物上方弹出气泡；上方没位置时改到下方。

        show_ms 可以延长停留时间：今日活动报告这种多行内容需要多留一会儿才看得完。
        show_ms <= 0 表示**常驻**（v2-scene-state-fix ④）：不排自动淡出，等下次
        `show_text` 盖掉、或 `hide_bubble` 收掉——观影情绪词要常驻到情绪变 / 散场。

        `kind` 记这条是谁写的（v2-pose-sync）。**后写的盖掉先写的**（同一个窗口只有
        一个标签），所以"任意时刻只有一个气泡"是结构上就成立的；`kind` 管的是**清的
        时候认人**——见 `hide_bubble`。
        """
        self._kind = kind
        self._show_text(text, pet_rect, show_ms)

    def _show_text(self, text: str, pet_rect: QRect, show_ms: int) -> None:
        self._label.setFixedWidth(self._measure_width(text))
        self._label.setText(text)   # 先定宽度再塞文字，最后 adjustSize 才能按换行算对高度
        self.adjustSize()
        self.move(*self._position(pet_rect))

        self._fade.stop()
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        if show_ms > 0:
            self._hide_timer.start(show_ms)
        else:
            self._hide_timer.stop()   # 常驻：不自动淡出，等盖掉或收掉

    def hide_bubble(self, kind: str | None = None) -> None:
        """收掉气泡。

        `kind=None`（默认）＝**无条件收**：聊天框打开、宠物藏起来、用户点它这类
        "这时候不该有任何气泡"的场合照旧全收。
        `kind="zzz"` ＝**只收这一档**（v2-pose-sync）：屏上现在是别人写的那句就一个字
        都不动——睡觉动作结束时收 zzz，不能顺手把刚飘上来的开场白 / 情绪词收掉。
        """
        if kind is not None and self._kind != kind:
            return
        self._hide_timer.stop()
        self._fade.stop()
        self.hide()

    def follow(self, pet_rect: QRect) -> None:
        """宠物移动时同步位置，不重置消失倒计时。"""
        if self.isVisible():
            self.move(*self._position(pet_rect))

    # ---------- 内部 ----------

    def _measure_width(self, text: str) -> int:
        metrics = QFontMetrics(self._label.font())
        rect = metrics.boundingRect(
            QRect(0, 0, MAX_TEXT_WIDTH, 2000), int(Qt.TextWordWrap), text
        )
        return max(60, min(MAX_TEXT_WIDTH, rect.width() + 4))

    def _position(self, pet_rect: QRect) -> tuple[int, int]:
        screen = QGuiApplication.screenAt(pet_rect.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = pet_rect.center().x() - self.width() // 2
        y = pet_rect.top() - self.height() + TAIL_HEIGHT
        if y < area.top():
            y = pet_rect.bottom() - TAIL_HEIGHT
        x = max(area.left() + 6, min(x, area.right() - self.width() - 6))
        y = max(area.top() + 6, min(y, area.bottom() - self.height() - 6))
        return x, y

    def _on_timeout(self) -> None:
        self._fade_out()

    def _fade_out(self) -> None:
        self._fade.stop()
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        body = QRectF(0.75, 0.75, self.width() - 1.5, self.height() - TAIL_HEIGHT - 0.75)
        path = QPainterPath()
        path.addRoundedRect(body, CORNER, CORNER)

        tail = QPainterPath()
        tail.moveTo(body.center().x() - 9, body.bottom() - 1)
        tail.lineTo(body.center().x(), body.bottom() + TAIL_HEIGHT - 1)
        tail.lineTo(body.center().x() + 9, body.bottom() - 1)
        tail.closeSubpath()
        path = path.united(tail).simplified()

        painter.setPen(QPen(QColor(BUBBLE_BORDER), 1.5))
        painter.setBrush(QColor(BUBBLE_BG))
        painter.drawPath(path)
