"""
番茄钟面板页（对应指令 1；归在生活模块下）。

- 环形进度显示剩余时间，颜色区分专注（暖橙）/休息（柔绿）；
- 开始 / 结束 / 重置 / 跳过 四个操作，全部转给 focus/pomodoro.py 里的计时器；
  （主按钮原先是 开始 → 暂停 → 继续 三态，v2-focus-notes2 ① 拿掉了暂停那一态：
  点了暂停是个出不来的死胡同，用户要的是"暂停即退出"）
- 统计：今日完成番茄数、累计专注时长、今日专注时段分布；
- 专注提醒开关（久坐 / 分心要不要开口）也放在这里——它属于"专注"这件事。

本页只做展示与转发，计时逻辑在 focus/pomodoro.py，数据在 data/dao.py。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.config import Config
from core.events import EventBus
from data.dao import PomodoroSessionDAO
from focus.pomodoro import PHASE_TITLES, Phase, PomodoroService
from ui.theme import (
    POMODORO_FOCUS,
    POMODORO_IDLE,
    POMODORO_PHASE_COLORS,
    POMODORO_TRACK,
    TEXT,
    TEXT_MUTED,
)

BAR_FLOOR_SEC = 25 * 60      # 柱状图高度参照：不足 25 分钟也按 25 分钟画，避免一根独苗顶满
LABEL_ROW_H = 18


def format_mmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class RingProgress(QWidget):
    """环形进度：外圈按已过时间顺时针推进，中心显示剩余时间和阶段名。"""

    THICKNESS = 12
    MARGIN = 8

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(190, 190)
        self._fraction = 0.0
        self._text = "00:00"
        self._sub = PHASE_TITLES[Phase.IDLE]
        self._color = QColor(POMODORO_IDLE)

    def set_state(self, remaining: int, planned: int, phase_key: str, paused: bool) -> None:
        planned = max(1, int(planned))
        self._fraction = max(0.0, min(1.0, 1.0 - remaining / planned))
        self._text = format_mmss(remaining)
        title = PHASE_TITLES.get(Phase(phase_key), phase_key)
        self._sub = f"{title} · 已暂停" if paused else title
        self._color = QColor(POMODORO_PHASE_COLORS.get(phase_key, POMODORO_IDLE))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        ring = QRectF(
            self.MARGIN,
            self.MARGIN,
            self.width() - 2 * self.MARGIN,
            self.height() - 2 * self.MARGIN,
        )
        pen = QPen(QColor(POMODORO_TRACK), self.THICKNESS)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(ring)
        if self._fraction > 0.0:
            pen.setColor(self._color)
            painter.setPen(pen)
            painter.drawArc(ring, 90 * 16, int(-360 * 16 * self._fraction))

        center_y = int(ring.center().y())
        time_font = QFont(self.font())
        time_font.setPointSizeF(23)
        time_font.setBold(True)
        painter.setFont(time_font)
        painter.setPen(QColor(TEXT))
        painter.drawText(
            QRectF(ring.left(), center_y - 44, ring.width(), 42),
            Qt.AlignCenter,
            self._text,
        )

        sub_font = QFont(self.font())
        sub_font.setPointSizeF(10)
        painter.setFont(sub_font)
        painter.setPen(QColor(self._color))
        painter.drawText(
            QRectF(ring.left(), center_y + 4, ring.width(), 22),
            Qt.AlignCenter,
            self._sub,
        )


class HourlyBars(QWidget):
    """今日专注时段分布：24 根小柱子，高度对应该小时的专注时长。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(96)
        self._values = [0] * 24

    def set_data(self, buckets: list[tuple[int, int]]) -> None:
        self._values = [0] * 24
        for hour, seconds in buckets:
            if 0 <= hour < 24:
                self._values[hour] = max(0, int(seconds))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        left, right = 4, self.width() - 4
        base_y = self.height() - LABEL_ROW_H
        top_y = 6
        usable_h = base_y - top_y
        slot = (right - left) / 24
        bar_w = max(3.0, slot * 0.62)

        # 基线
        painter.setPen(QPen(QColor(TEXT_MUTED), 1))
        painter.drawLine(left, base_y, right, base_y)

        scale = max(max(self._values), BAR_FLOOR_SEC)
        for hour, seconds in enumerate(self._values):
            cx = left + slot * (hour + 0.5)
            if seconds > 0:
                h = max(3.0, usable_h * seconds / scale)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(POMODORO_FOCUS))
                painter.drawRoundedRect(QRectF(cx - bar_w / 2, base_y - h, bar_w, h), 2, 2)

        # 横轴刻度
        label_font = QFont(self.font())
        label_font.setPointSizeF(8)
        painter.setFont(label_font)
        painter.setPen(QColor(TEXT_MUTED))
        for hour in (0, 6, 12, 18, 23):
            cx = left + slot * (hour + 0.5)
            painter.drawText(
                QRectF(cx - 14, base_y + 2, 28, LABEL_ROW_H - 2),
                Qt.AlignCenter,
                f"{hour}时",
            )

        if not any(self._values):
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(QRectF(left, top_y, right - left, usable_h), Qt.AlignCenter, "今天还没有专注记录")


class PomodoroPanel(QWidget):
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        service: PomodoroService,
        dao: PomodoroSessionDAO,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._service = service
        self._dao = dao

        self._build_ui()

        timer = self._service.timer
        timer.tick.connect(self._on_tick)
        timer.state_changed.connect(self._on_state_changed)
        bus.pomodoro_stats_changed.connect(self.refresh_stats)

        self._on_state_changed(timer.phase)
        self.refresh_stats()

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self._intro = QLabel(
            f"专注的时候{self._config.pet.name}会安静陪你，到点再喊你起来动动。", self
        )
        self._intro.setObjectName("MutedLabel")
        self._intro.setWordWrap(True)
        layout.addWidget(self._intro)

        self._ring = RingProgress(self)
        layout.addWidget(self._ring, 0, Qt.AlignHCenter)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._start_button = QPushButton("开始专注", self)
        self._start_button.setObjectName("PrimaryButton")
        self._start_button.clicked.connect(self._on_start_clicked)
        buttons.addWidget(self._start_button)

        self._reset_button = QPushButton("重置", self)
        self._reset_button.clicked.connect(self._service.timer.reset)
        buttons.addWidget(self._reset_button)

        self._skip_button = QPushButton("跳过本阶段", self)
        self._skip_button.clicked.connect(self._service.timer.skip)
        buttons.addWidget(self._skip_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._remind_check = QCheckBox("专注提醒：久坐、分心时让宠物提醒我（关了照样记录）", self)
        self._remind_check.setChecked(self._config.activity.remind)
        self._remind_check.toggled.connect(self._on_remind_toggled)
        layout.addWidget(self._remind_check)

        self._stats_label = QLabel("", self)
        self._stats_label.setObjectName("MutedLabel")
        layout.addWidget(self._stats_label)

        bars_title = QLabel("今日专注时段分布", self)
        bars_title.setObjectName("MutedLabel")
        layout.addWidget(bars_title)

        self._bars = HourlyBars(self)
        layout.addWidget(self._bars)
        layout.addStretch(1)

    # ---------- 计时器回调 ----------

    def _on_tick(self, remaining: int, phase: Phase) -> None:
        timer = self._service.timer
        self._ring.set_state(remaining, timer.planned, phase.value, timer.paused)

    def _on_state_changed(self, phase: Phase) -> None:
        timer = self._service.timer
        self._ring.set_state(timer.remaining, timer.planned, phase.value, timer.paused)
        self._sync_buttons()

    def _on_start_clicked(self) -> None:
        """主按钮：没在跑就起一轮，在跑就**结束这一轮**（v2-focus-notes2 ①）。

        原来是 开始 → 暂停 → 继续 的三态。暂停那一态真机上是个死胡同：点了暂停，
        计时冻住、浮窗挂着，用户要退只能去翻别的地方。用户要的是**暂停即退出**，
        所以中间那一态整个拿掉——在跑的时候按一下就是结束，回空闲、浮窗收掉。
        （要接着跑就重新点开始，一轮重新计时。）
        """
        timer = self._service.timer
        if not timer.is_active:
            timer.start_focus()
        else:
            timer.reset()

    def _sync_buttons(self) -> None:
        timer = self._service.timer
        if not timer.is_active:
            self._start_button.setText("开始专注")
            self._reset_button.setEnabled(False)
            self._skip_button.setEnabled(False)
            return
        self._start_button.setText("结束专注")
        self._reset_button.setEnabled(True)
        self._skip_button.setEnabled(True)

    # ---------- 统计 ----------

    def refresh_stats(self) -> None:
        count = self._dao.today_completed_focus_count()
        seconds = self._dao.today_focus_seconds()
        self._stats_label.setText(
            f"今日完成 {count} 个番茄 · 累计专注 {seconds // 60} 分钟"
        )
        self._bars.set_data(self._dao.today_hourly_focus_seconds())

    def apply_pet_name(self, pet_name: str) -> None:
        self._intro.setText(f"专注的时候{pet_name}会安静陪你，到点再喊你起来动动。")

    # ---------- 专注提醒开关 ----------

    def _on_remind_toggled(self, checked: bool) -> None:
        self._config.activity.remind = checked
        try:
            self._config.save()
        except OSError as exc:
            self._bus.toast.emit(f"专注提醒开关没能保存：{exc}")
            return
        self._bus.toast.emit(
            "专注提醒已打开" if checked else "专注提醒已关闭（照样记录，不打扰你）"
        )

    def refresh_remind(self) -> None:
        """设置页里改过同一项之后，把勾选状态同步回来（不要反过来又触发一次保存）。"""
        self._remind_check.blockSignals(True)
        self._remind_check.setChecked(self._config.activity.remind)
        self._remind_check.blockSignals(False)
