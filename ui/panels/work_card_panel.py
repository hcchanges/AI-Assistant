"""
工作卡片详情页与部门卡列表页（公开版占位）。

内部版这里是两张工作功能卡的界面和"点部门卡进来挑功能卡"
的列表页。公开仓一张工作卡都没有，两页都换成锁着的提示（跟首页同一块，见
`ui/panels/work_panel.py` 的 `LockedPage`）。

装配层要的东西照旧：构造签名、`card_chat_requested` 信号、`card_chat` 小窗、
`show_card()`、`show_dept()`。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from ui.panels.work_panel import LockedPage


class LockedCardChat(QWidget):
    """卡内小助手（公开版占位）：不接任何卡号，也不发消息。

    装配层会遍历「生活卡 / 工作卡」两个小窗、按卡号找对得上的那个
    （`core/bootstrap.py` 的 `_card_chat_panel`），所以这里必须有 `namespace`
    与 `set_busy`：卡号是空串，跟哪个真卡号都不相等，永远轮不到它。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.namespace = ""

    def set_busy(self, busy: bool) -> None:
        return None


class WorkCardPanel(LockedPage):
    """工作卡片详情页（公开版：锁着）。

    卡底那个小窗里说了一句（内部版：(卡号, 用户说的话)）——信号照旧留着，
    公开版不会有人发它。
    """

    card_chat_requested = Signal(str, str)

    def __init__(
        self,
        bus=None,
        service=None,
        chat_service=None,
        layout_store=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent=parent)
        self._bus = bus
        self._service = service
        self._chat_service = chat_service
        self._layout_store = layout_store
        self.card_chat = LockedCardChat(self)

    def show_card(self, key: str) -> None:
        """内部版按 key 切到对应那套界面；公开版一张工作卡都没有。"""
        return None


class WorkDeptPanel(LockedPage):
    """部门卡点开后的选择页（公开版：锁着）。"""

    def __init__(
        self,
        bus=None,
        service=None,
        on_card: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent=parent)
        self._bus = bus
        self._service = service
        self._on_card = on_card

    def show_dept(self, dept_id: str) -> None:
        """内部版列出这个部门的功能卡与空位；公开版没有部门。"""
        return None
