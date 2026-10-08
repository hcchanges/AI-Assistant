"""
宠物状态机：待机 / 说话 / 休眠。

- 说话：被触发后短时间进入"说话"动画，到点自动回到待机；
- 休眠：长时间没人理就闭眼睡觉（顺便让动画停下省电），一有互动立刻醒来。
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal


class PetState(Enum):
    IDLE = "idle"
    TALKING = "talking"
    SLEEPING = "sleeping"


class PetStateMachine(QObject):
    state_changed = Signal(object)  # 参数为 PetState

    TALK_DURATION_MS = 6000
    IDLE_TO_SLEEP_MS = 10 * 60 * 1000

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._state = PetState.IDLE

        self._talk_timer = QTimer(self)
        self._talk_timer.setSingleShot(True)
        self._talk_timer.timeout.connect(self._on_talk_finished)

        self._sleep_timer = QTimer(self)
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(lambda: self.set_state(PetState.SLEEPING))
        self._sleep_timer.start(self.IDLE_TO_SLEEP_MS)

    @property
    def state(self) -> PetState:
        return self._state

    def set_state(self, state: PetState) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def speak(self) -> None:
        """宠物开口说话。"""
        self._sleep_timer.stop()
        self.set_state(PetState.TALKING)
        self._talk_timer.start(self.TALK_DURATION_MS)

    def touch(self) -> None:
        """用户与宠物互动：唤醒并重新开始休眠倒计时。"""
        if self._state == PetState.SLEEPING:
            self.set_state(PetState.IDLE)
        self._sleep_timer.start(self.IDLE_TO_SLEEP_MS)

    def _on_talk_finished(self) -> None:
        if self._state == PetState.TALKING:
            self.set_state(PetState.IDLE)
        self._sleep_timer.start(self.IDLE_TO_SLEEP_MS)
