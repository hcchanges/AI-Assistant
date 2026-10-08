"""
电子宠物 入口。

启动顺序：创建 QApplication → 装配应用 → 拉起桌宠 → 进入事件循环。
关闭面板不会退出程序（宠物常驻托盘），退出请用右键菜单或托盘菜单。
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from core.bootstrap import DesktopPetApp
from core.config import ConfigError


def _install_excepthook() -> None:
    def handle(exc_type, exc_value, exc_tb):
        logging.getLogger("main").critical(
            "未捕获的异常", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = handle


def main() -> int:
    _install_excepthook()
    app = QApplication(sys.argv)
    app.setApplicationName("电子宠物")
    app.setQuitOnLastWindowClosed(False)

    try:
        pet_app = DesktopPetApp()
    except (ConfigError, FileNotFoundError) as exc:
        # 缺前置条件（配置写错、向量模型没下载）时不静默退出，弹窗说清楚怎么办
        QMessageBox.critical(None, "电子宠物 启动失败", str(exc))
        return 1

    pet_app.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
