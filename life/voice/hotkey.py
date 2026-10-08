"""全局快捷键（C 多模态 · 听说闭环）：宠物窗口没聚焦时也能按一下开始说话。

**为什么是"按一下开始、再按一下结束"，不是"按住不放"。** 后者要在系统层面挂一个
键盘钩子（`WH_KEYBOARD_LL`）才能收到"松开"那一瞬，那等于**常驻监听整个键盘**——
每敲一个字都从我们这儿过一遍。为了省一次按键，把用户的键盘全看在眼里，这笔账
不划算。按一下开始、再按一下结束，`RegisterHotKey` 就够，系统只在用户真按了这
一个组合键时叫我们一声，其余时间它听不见我们、我们也听不见它。

**它不碰 Qt 的事件循环。** 注册是**按线程**生效的（`RegisterHotKey(hwnd=None)`
把消息投给调用它的那个线程），所以自己在一条线程上注册 + 跑消息循环，跟主线程
的事件循环各过各的。信号从那条线程发出，Qt 自动排队到主线程派发——界面照常改。

注册不上（键被别的软件占了、系统不给）不是错误：记一笔，这条路就是没有，
界面上那个按钮照常用。非 Windows 直接不注册。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Any

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

# RegisterHotKey 的修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# 按住不放时别一直重复触发（否则按住 Ctrl+Alt+空格 会疯狂开始/结束）
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}

# 认得的键名 → 虚拟键码。只列常用的一小撮：这个功能是"按一下说话"，
# 不需要变成一套完整的快捷键系统
_VK_NAMES: dict[str, int] = {
    "space": 0x20, "空格": 0x20,
    "enter": 0x0D, "return": 0x0D, "回车": 0x0D,
    "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
_VK_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 13)})          # F1~F12
_VK_NAMES.update({chr(ord("a") + i): 0x41 + i for i in range(26)})   # A~Z
_VK_NAMES.update({str(i): 0x30 + i for i in range(10)})              # 0~9


def parse(spec: str) -> tuple[int, int] | None:
    """把 "Ctrl+Alt+Space" 解析成 (修饰键, 虚拟键码)；解析不了返回 None。

    纯函数，不碰系统——所以这条能被测试压住。
    """
    parts = [part.strip().lower() for part in str(spec or "").split("+") if part.strip()]
    if len(parts) < 2:          # 至少要"一个修饰键 + 一个键"，不然会抢占普通按键
        return None
    mods = 0
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            return None
        mods |= _MODIFIERS[part]
    key = _VK_NAMES.get(parts[-1])
    if key is None or not mods:
        return None
    return mods | MOD_NOREPEAT, key


class GlobalHotkey(QObject):
    """一个全局快捷键。`pressed` 每次按下发一次（按住不重复）。"""

    pressed = Signal()

    def __init__(self, spec: str, parent: QObject | None = None):
        super().__init__(parent)
        self._spec = spec
        self._parsed = parse(spec)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._user32: Any = None

    # ---------- 对外 ----------

    @property
    def spec(self) -> str:
        return self._spec

    def start(self) -> bool:
        """开始听这个键。没配、平台不对、注册不上都返回 False（不抛）。"""
        if self._parsed is None:
            if self._spec:
                logger.warning("快捷键「%s」认不出来，跳过（格式像这样：Ctrl+Alt+Space）", self._spec)
            return False
        if sys.platform != "win32":
            logger.info("快捷键只在 Windows 上注册，这个平台跳过")
            return False
        if self._thread is not None:
            return True
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(ready,), name="pet-hotkey", daemon=True
        )
        self._thread.start()
        ready.wait(timeout=3.0)
        return self._thread_id != 0

    def stop(self) -> None:
        """注销并让那条线程退出。"""
        if self._thread is None:
            return
        if self._user32 is not None and self._thread_id:
            # 往那条线程的消息队列里丢一个 WM_QUIT，GetMessage 就返回 0 了
            self._user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        self._thread = None
        self._thread_id = 0

    # ---------- 内部 ----------

    def _run(self, ready: threading.Event) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32 = user32
        try:
            thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            self._thread_id = thread_id
            mods, key = self._parsed
            if not user32.RegisterHotKey(None, 1, mods, key):
                logger.warning(
                    "快捷键「%s」注册不上（多半被别的软件占了），这次就不用了", self._spec
                )
                self._thread_id = 0
                ready.set()
                return
            ready.set()
            logger.info("全局快捷键已就绪：%s（按一下开始说话，再按一下结束）", self._spec)
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    self._fire()
            user32.UnregisterHotKey(None, 1)
        except Exception as exc:  # noqa: BLE001 —— 这条线塌了不该把程序带走
            logger.error("快捷键线程出错（%s），这次就不用了", exc)
            self._thread_id = 0
            ready.set()

    def _fire(self) -> None:
        """按下了一次。跨线程发信号：Qt 自动排队到主线程派发。"""
        try:
            self.pressed.emit()
        except Exception as exc:  # noqa: BLE001 —— 同上
            logger.error("快捷键回调出错（%s）", exc)
