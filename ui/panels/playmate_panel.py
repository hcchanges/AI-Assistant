"""
游戏陪玩页（归在生活模块下）。

打开陪玩后：每 N 秒在本地截一帧 → 画面变了才把这一帧交给能看图的模型 →
模型给一句攻关提示。截图只在内存里过一下，不落盘；没变化就不发、不花钱。

本页只做展示与转发：开关交给 bootstrap 的 _start_playmate / _stop_playmate
（那里管着"有没有 Key"的判断与提示），状态靠 bus.playmate_changed 同步。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from core.config import Config
from core.events import EventBus
from life.playmate import PlaymateService


class PlaymatePanel(QWidget):
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        service: PlaymateService,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._service = service
        self._on_start = on_start
        self._on_stop = on_stop

        self._build_ui()

        bus.playmate_changed.connect(self._refresh)
        self._refresh(service.is_active)

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self._intro = QLabel(self)
        self._intro.setObjectName("MutedLabel")
        self._intro.setWordWrap(True)
        layout.addWidget(self._intro)

        self._status = QLabel("", self)
        self._status.setObjectName("TitleLabel")
        layout.addWidget(self._status)

        self._button = QPushButton("", self)
        self._button.setObjectName("PrimaryButton")
        self._button.setMinimumHeight(46)
        self._button.setCursor(Qt.PointingHandCursor)
        self._button.clicked.connect(self._on_clicked)
        layout.addWidget(self._button)

        self._detail = QLabel("", self)
        self._detail.setObjectName("MutedLabel")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)

        note = QLabel(
            "它只截鼠标所在那块屏幕，画面没什么可说的（菜单、加载中）就保持安静。\n"
            "截图只在内存里比一下变化，不落盘；画面变了才发给模型，不发就不花钱。\n"
            "也可以直接跟宠物说「开始陪玩」「不用陪玩了」。",
            self,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

    # ---------- 状态 ----------

    def _on_clicked(self) -> None:
        if self._service.is_active:
            self._on_stop()
        else:
            self._on_start()

    def _refresh(self, active: bool) -> None:
        pet_name = self._config.pet.name
        self._intro.setText(
            f"玩游戏时让{pet_name}看着画面，给你攻关、寻路、找宝的提示。"
        )
        self._status.setText(f"陪玩：{'已开启' if active else '未开启'}")
        self._button.setText("停止陪玩" if active else "开始陪玩")
        self._detail.setText(self._detail_text())

    def apply_pet_name(self, pet_name: str) -> None:
        self._refresh(self._service.is_active)

    def _detail_text(self) -> str:
        vision = self._config.vision
        provider = self._config.llm.provider(vision.provider)
        lines = [
            f"看图模型：{provider.name} 的 {vision.model}",
            f"每 {vision.sample_sec} 秒看一眼画面；画面变化超过 {vision.change_threshold:.0f} 才分析；"
            f"两次提示至少隔 {vision.min_gap_sec} 秒；发图前缩到 {vision.max_width} 像素宽",
        ]
        if not self._config.get_api_key(provider):
            lines.append(
                f"还没拿到 {provider.name} 的 API Key：右键→设置→陪玩里填一下才能看图。"
            )
        return "\n".join(lines)
