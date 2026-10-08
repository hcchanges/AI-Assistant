"""
系统托盘图标与菜单。

对应方案 P1 的"托盘"。菜单项统一通过 action_requested 抛给应用层处理，托盘自己不关心业务。
菜单只留：显示/隐藏桌宠 + 工作 / 生活 / 设置 / 退出——和桌宠右键菜单一致，
多出来的"显示/隐藏"是隐身之后把宠物叫回来的入口。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ui.pet_skin import POSE_IDLE, PetSkin


class PetTray(QSystemTrayIcon):
    action_requested = Signal(str)  # toggle / work / life / settings / quit

    def __init__(
        self,
        skin: PetSkin,
        pet_name: str = "电子宠物",
        parent=None,
    ):
        super().__init__(QIcon(skin.pixmap(POSE_IDLE)), parent)
        self.setToolTip(f"{pet_name} 在陪着你")
        self._pet_visible = True

        self._menu = QMenu()
        # 每次弹出**之前**重排一遍：桌面宠物在别的窗口里被隐藏过之后，菜单上
        # "隐藏/显示桌宠"那条得跟着当前可见状态走。
        self._menu.aboutToShow.connect(self._rebuild_menu)
        self._rebuild_menu()
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)

    def _rebuild_menu(self) -> None:
        """按当前状态重建菜单（跟宠物右键菜单同款：四条固定 + 显示/隐藏）。"""
        self._menu.clear()
        self._toggle_action = self._menu.addAction(
            "隐藏桌宠" if self._pet_visible else "显示桌宠"
        )
        self._toggle_action.triggered.connect(lambda: self.action_requested.emit("toggle"))
        self._menu.addSeparator()
        # 跟桌宠右键菜单同款：工作模块那一项写锁着的说法（公开版）
        for key, text in (
            ("work", "🔒 工作模块（待解锁）"),
            ("life", "生活"),
            ("settings", "设置"),
        ):
            self._menu.addAction(text).triggered.connect(
                lambda _checked=False, k=key: self.action_requested.emit(k)
            )
        # 专注那两条（「结束专注」/「番茄钟」）在 v2-focus-notes2 ③ 摘掉了：菜单就是
        # 四条固定的——工作 / 生活 / 设置 / 退出，跟宠物右键菜单同款（这个文件开头那条
        # "两边一致"的约定照旧成立，只是两边都不再有那两条）。专注的进出改在番茄钟面板上。
        self._menu.addAction("退出").triggered.connect(
            lambda: self.action_requested.emit("quit")
        )

    def set_pet_visible(self, visible: bool) -> None:
        self._pet_visible = visible
        self._toggle_action.setText("隐藏桌宠" if visible else "显示桌宠")

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.action_requested.emit("toggle")
