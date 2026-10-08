"""
开机自启：写 Windows 注册表 HKCU 的 Run 项，不需要管理员权限。

对应方案 P1 的"开机自启"。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows 平台（仅开发调试用）
    winreg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Desktoppet"


def _launch_command() -> str:
    """返回开机要执行的命令行，优先用 pythonw.exe 以不弹控制台窗口。"""
    python_dir = Path(sys.executable).parent
    pythonw = python_dir / "pythonw.exe"
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{exe}" "{main_py}"'


def is_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except OSError:
        # 项不存在，或者读不了（被策略锁住等）：一律当作没开，别让设置页打不开
        return False


def set_enabled(enabled: bool) -> None:
    if winreg is None:
        logger.warning("当前平台不支持开机自启设置")
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _launch_command())
            logger.info("已开启开机自启: %s", _launch_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
                logger.info("已关闭开机自启")
            except FileNotFoundError:
                pass
