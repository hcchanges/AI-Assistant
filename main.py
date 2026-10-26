"""
电子宠物 入口。

启动顺序：创建 QApplication → 装配应用 → 拉起桌宠 → 进入事件循环。
关闭面板不会退出程序（宠物常驻托盘），退出请用右键菜单或托盘菜单。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from core.paths import bundle_dir, ensure_app_file

_LAUNCH_LOGGER = "launch"


def _setup_launch_log() -> logging.Logger | None:
    """把启动过程单独落一份 data/logs/launch.log。

    `--noconsole` 打包后没有控制台，导入/启动阶段的异常只能靠文件查。落点跟
    pet.log 同一个 data/logs：开发态是项目内 data/logs，打包态是 _internal/data/logs。
    写不进去不拦启动，返回 None，调用方逐处判空。
    """
    path = bundle_dir() / "data" / "logs" / "launch.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger = logging.getLogger(_LAUNCH_LOGGER)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers.clear()
        logger.addHandler(handler)
    except OSError:
        return None
    return logger


def _install_excepthook(launch_log: logging.Logger | None = None) -> None:
    def handle(exc_type, exc_value, exc_tb):
        logging.getLogger("main").critical(
            "未捕获的异常", exc_info=(exc_type, exc_value, exc_tb)
        )
        if launch_log is not None:
            # pet.log 要等 DesktopPetApp 里配置好日志才有；启动早期只有这一份
            launch_log.critical(
                "未捕获的异常", exc_info=(exc_type, exc_value, exc_tb)
            )

    sys.excepthook = handle


def _first_run_files() -> list[str]:
    """打包后首次运行：把内置的 config.yaml / .env 落到 exe 旁边。

    返回这次新建出来的文件名，用来在窗口起来后提示用户。开发态这些文件本来就在
    项目根，ensure_app_file 会直接判定「已存在」，不会误报。
    """
    created = []
    for name, template in (
        ("config.yaml", "config.yaml"),
        (".env", ".env.example"),
    ):
        _, made = ensure_app_file(name, template)
        if made:
            created.append(name)
    return created


def main() -> int:
    launch_log = _setup_launch_log()
    if launch_log is not None:
        launch_log.info(
            "启动 frozen=%s exe=%s cwd=%s",
            bool(getattr(sys, "frozen", False)),
            sys.executable,
            sys.argv,
        )
    _install_excepthook(launch_log)

    # 重依赖放在日志就位之后再导入：--noconsole 下导入失败也能在 launch.log 里看到
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        from core.bootstrap import DesktopPetApp
        from core.config import ConfigError
    except Exception:
        if launch_log is not None:
            launch_log.exception("启动导入失败")
        raise

    app = QApplication(sys.argv)
    app.setApplicationName("电子宠物")
    app.setQuitOnLastWindowClosed(False)

    created = _first_run_files()
    if created:
        hint = "、".join(created)
        message = f"首次运行，已在程序目录生成：{hint}。"
        if ".env" in created:
            message += "\n需要完整 AI 功能，请打开 .env 填 API Key。"
        logging.getLogger("main").info(message.replace("\n", " "))
        if launch_log is not None:
            launch_log.info(message.replace("\n", " "))
        QMessageBox.information(None, "电子宠物 首次运行", message)

    try:
        pet_app = DesktopPetApp()
    except (ConfigError, FileNotFoundError) as exc:
        # 缺前置条件（配置写错、向量模型没下载）时不静默退出，弹窗说清楚怎么办
        if launch_log is not None:
            launch_log.error("启动失败：%s", exc)
        QMessageBox.critical(None, "电子宠物 启动失败", str(exc))
        return 1

    if launch_log is not None:
        launch_log.info("应用装配完成，进入事件循环")
    pet_app.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
