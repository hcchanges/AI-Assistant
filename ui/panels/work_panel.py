"""
工作模块的两页（公开版占位）。

内部版这里是一个横向卡片轮播（部门卡 + 卡位）与一个"底层连接"流程图页。公开仓把两页
换成同一块**锁着的提示**：一句话 + 一行小字，居中放着；菜单里那一项也写着「待解锁」，
点进来看到的就是这块。

装配层要的东西（构造签名、`status_note` 信号、`current_note()` / `apply_role()` /
`focus_dept()` / `refresh()`）一个不少，所以 `core/bootstrap.py` 那边一行都不用改。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

LOCK_TITLE = "🔒 工作模块（待解锁）"
LOCK_NOTE = "这个模块需要内部授权。\n公开版里它只是一个占位页：这个模块的功能没有随公开版发布。"


class LockedPage(QWidget):
    """锁着的一页：居中一句话 + 一行小字。工作模块的几页都用它（见 work_card_panel.py）。"""

    def __init__(self, note: str = LOCK_NOTE, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addStretch(1)

        title = QLabel(LOCK_TITLE, self)
        title.setObjectName("TitleLabel")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        body = QLabel(note, self)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        layout.addWidget(body)

        layout.addStretch(1)

    def sizeHint(self) -> QSize:
        # 面板容器按各页的 sizeHint 定窗口高度（ui/panels/container.py），
        # 所以这一页得给一个"跟别的一级页差不多高"的数，别让窗口忽大忽小。
        return QSize(420, 340)


class WorkPanel(LockedPage):
    """工作模块的首页（公开版：锁着）。"""

    # 窗口标题栏该显示什么：装配层把它接到 PanelWindow 上（core/bootstrap.py）
    status_note = Signal(str)

    def __init__(
        self,
        service=None,
        bus=None,
        on_dept: Callable[[str, object], None] | None = None,
        on_flow: Callable[[object], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent=parent)
        self._service = service
        self._bus = bus
        self._on_dept = on_dept
        self._on_flow = on_flow

    def current_note(self) -> str:
        """窗口标题栏上那一行：公开版只说锁着。"""
        return LOCK_TITLE

    def apply_role(self) -> None:
        """内部版按身份重摆按钮与标题栏；公开版没有身份可切，只把标题栏那一句同步过去。"""
        self.status_note.emit(self.current_note())

    def focus_dept(self, dept_id: str) -> None:
        """内部版把某张部门卡滑到中间；公开版没有部门卡。"""
        return None


class WorkFlowPanel(LockedPage):
    """底层连接页（内部版：仅管理员，画流程图、右键改节点与连线）。公开版锁着。"""

    def __init__(self, service=None, bus=None, parent: QWidget | None = None):
        super().__init__(parent=parent)
        self._service = service
        self._bus = bus

    def refresh(self) -> None:
        """内部版重画流程图；公开版没有图可画。"""
        return None
