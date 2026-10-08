"""
Windows 前台窗口与空闲检测（对应指令 5 第 2 节 / 指令 6 第 1 节）。

只用系统自带的 ctypes，不依赖 pywin32，也不需要管理员权限：
- 前台窗口 → GetForegroundWindow + GetWindowThreadProcessId → 进程号；
- 空闲时长 → GetLastInputInfo（最后一次键鼠操作距今多久）；
- 进程号 → 进程名 用 psutil（进程号会被系统复用，所以带缓存并按上限清理）。

默认只取进程名，不读窗口标题：标题里可能有文档名或网址，落在隐私红线里。

**一处破例**（v2 屏幕陪伴 S4 扩展，v2-game-activate 加了一个调用点）：窗口标题**可以**
读一次，范围卡死在三处——只有 `foreground_title` 一个入口、只有屏幕感知那条线会调、
标题用完即弃（不落任何盘）。两种情形各读一次：浏览器前台下判"网页版网课 / 在线创作"
还是"在看剧"；以及**用户自己在本地游戏名单里写了 `title:` 关键词**时判这是哪款游戏
（后者要名单里真有这类词才会发生，默认那份名单是空的）。不想破例就把配置
screen.read_browser_title 关掉，这两条线整个不动。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

try:
    import psutil
except ImportError:   # 没装 psutil 就采不到应用名，采样器会自动不记录
    psutil = None

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

PROC_CACHE_MAX = 256   # 进程号缓存上限，超过就整体清空（进程号会被复用，别让缓存无限涨）


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


# EnumWindows 的回调类型（v2-social-await-prime-fix 改动二）。`LPARAM` 那一格我们不用，
# 但签名得对上，不然 64 位下参数会被截断。**WINFUNCTYPE 只有 Windows 上有**，所以只在
# IS_WINDOWS 那一支里才会被真正用到。
_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) \
    if IS_WINDOWS else None


if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    # 必须声明原型：窗口句柄是 64 位指针，不声明就会被当成 32 位整数截断
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
    _user32.GetLastInputInfo.restype = wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    # 枚举顶层窗口（v2-social-await-prime-fix 改动二）：**只问"这些窗口归哪个进程"**，
    # 标题一个字都不读（见 `visible_pids`）。回调签名必须声明清楚——HWND 是 64 位指针。
    _user32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    # 窗口样式（v2-game-activate）：查 GWL_STYLE 看窗口有没有标题栏/边框。
    # **必须用 GetWindowLongPtrW**：64 位下 GetWindowLongW 只回 32 位，样式位虽然
    # 落在低 32 位、但拿 LONG_PTR 那个入口才是官方指定，且返回值要声明成指针宽度，
    # 不然 ctypes 默认按 c_int 截断。老系统没有 Ptr 版（32 位 Windows），退回 Long 版。
    _get_long = getattr(_user32, "GetWindowLongPtrW", None) or _user32.GetWindowLongW
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_long.restype = ctypes.c_ssize_t
    # GetTickCount 在 user32 里取不到（不是每个系统都转发这个导出），kernel32 的 64 位版本才稳
    _kernel32.GetTickCount64.restype = ctypes.c_ulonglong

# 窗口样式里"这是个有标题栏、可拖动、可缩放的正经窗口"的那几个位（GWL_STYLE = -16）。
# 一个窗口**同时没有**标题栏、没有可缩放边框、没有系统菜单，才像全屏游戏那种
# 无边框铺满——普通的窗口化全屏（浏览器 F11、播放器全屏）都留着这些位。
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
_CHROME_BITS = WS_CAPTION | WS_THICKFRAME | WS_SYSMENU


def available() -> bool:
    """当前环境能不能采样：只做了 Windows 实现，且需要 psutil 读进程名。"""
    return IS_WINDOWS and psutil is not None


def foreground_pid() -> int | None:
    """当前前台窗口所属进程号；取不到返回 None。"""
    if not IS_WINDOWS:
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


def visible_pids() -> list[int]:
    """桌面上这会儿**可见、且没最小化**的顶层窗口都归哪些进程（去重，取不到返回空表）。

    用途只有一个（v2-social-await-prime-fix 改动二）：冷启动那一刻问不出"谁在前台焦点"
    的时候，退一步问"社交软件这会儿在不在桌面上摆着"。真机上是这么一条路——微信个人窗
    先开着（**不是焦点**）→ 再开宠物，宠物自己的窗口抢了前台，`foreground_now()` 答不出
    微信，社交那条线就此把"社交软件在不在前台"定成 False，用户不手动切一次窗就永远不冒话。

    **这一步只看进程号，窗口内容一个字都不读。** `EnumWindows` 会递过来每一个顶层窗口的
    句柄，这里只问三件事：可见吗（`IsWindowVisible`）、最小化了吗（`IsIconic`）、
    归哪个进程（`GetWindowThreadProcessId`）。标题、类名、文本长度**一概不问**——
    模块开头那条"默认只取进程名，不读窗口标题"的红线在这儿照旧成立。

    最小化的不算：窗口都收起来了，谈不上"在桌面上摆着"，用户也没在看它。
    枚举失败（系统调用出岔子）返回空表，调用方按"一个都没看见"处理——不该因为这一问
    把启动带崩。

    **这是"补充"不是"替代"**：焦点那一问照旧先问（`foreground_pid`），它答得出社交软件
    就用它；只有答不出时才退到这儿。
    """
    if not IS_WINDOWS:
        return []
    found: list[int] = []

    def _visit(hwnd, _lparam):
        # 回调必须回 True 才继续枚举下一个；一旦回 False，后面的窗口就全不看了。
        try:
            if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
                return True
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        except Exception as exc:      # noqa: BLE001 —— 单个窗口问不动，不该丢掉整张表
            logger.debug("枚举可见窗口时单个窗口问不动: %s", exc)
            return True
        value = int(pid.value)
        if value and value not in found:
            found.append(value)
        return True

    try:
        _user32.EnumWindows(_ENUM_PROC(_visit), 0)
    except Exception as exc:      # noqa: BLE001 —— 见上：枚举不成当"没看见"
        logger.debug("枚举可见窗口失败: %s", exc)
        return []
    return found


def foreground_title() -> str:
    """当前前台窗口的标题；取不到返回空串。

    隐私破例的入口（见模块开头那段）：标题里可能带文档名或网址，所以**只能**由屏幕
    感知那条线在两种情形下调一次，拿到就当场用完，别留着、别写盘：

    1. 浏览器分类（`monitor._read_title`）：判"网课 / 在线创作"还是"在看剧"；
    2. **本地游戏名单的 `title:` 关键词**（v2-game-activate，
       `monitor._game_title`）：用户自己写的"标题里带某词 = 某游戏"。

    第 2 条有三道闸才走到这里：名单里**真有** `title:` 词、`screen.read_browser_title`
    开着、且前面几条便宜的线索一条都没中。刚装好的名单是空的 → 这里永远不会被调，
    所以加这一档**没有**把口子变大（口径写在 `ScreenSection` 与 `game.py` 的注释里）。
    """
    if not IS_WINDOWS:
        return ""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = int(_user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    # 标题可能很长，给足缓冲；GetWindowTextW 写入的是 UTF-16，长度按字符算
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def foreground_window_class() -> str:
    """当前前台窗口的**窗口类名**（`GetClassNameW`）；取不到返回空串。

    跟标题是两回事，所以不算破例：类名是**程序注册的常量**（`UnityWndClass`、
    `UnrealWindow`、`Chrome_WidgetWin_1`…），由 exe 自己写在代码里，跟用户打开
    了什么文档、访问了哪个网址一点关系都没有——它认不出内容，只认得出"这是谁家的
    窗口"。`screen.read_browser_title` 那条破例的口径（文档名、网址）在这里不成立。

    用途只有一个（v2-game-fix）：进程名那份游戏表注定认不全，认不出名字的全屏
    游戏只能再借这一类信号去认。取不到就给空串，调用方按"没这条线索"处理。
    """
    if not IS_WINDOWS:
        return ""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    # 类名有 256 字符的上限（Win32 的规定），照着开就够，不会截断
    buffer = ctypes.create_unicode_buffer(256)
    length = int(_user32.GetClassNameW(hwnd, buffer, 256))
    if length <= 0:
        return ""
    return buffer.value


def foreground_window_style() -> int:
    """当前前台窗口的样式位（`GetWindowLongPtrW(GWL_STYLE)`）；取不到返回 0。

    用途只有一个（v2-game-activate 的"无边框全屏"判据，见 `window_has_frame`）：
    看窗口还留不留标题栏/边框。取不到就给 0——调用方按"没这条线索"处理。
    """
    if not IS_WINDOWS:
        return 0
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0
    return int(_get_long(hwnd, GWL_STYLE))


def window_has_frame(style: int) -> bool:
    """这份窗口样式还留着标题栏/边框/系统菜单吗（= 是个正经的窗口化窗口）。

    三条位**一条都不留**才算 False。全屏游戏（真独占和无边框铺满都算）通常一条不留；
    浏览器 F11、播放器全屏、以及一切普通窗口都至少留着一条。样式取不到（0）时返回
    True——**当它是窗口化**，也就是"没这条线索"，宁可漏判游戏也不误判。

    `style == 0` 必须**单独**判一次：0 跟 _CHROME_BITS 按位与也是 0，光看位的话会
    把"取不到样式"和"真无边框"混成同一件事——那样凡是从 `GetWindowLongPtrW` 拿不到
    值的机器上，每一个全屏窗口都会被当成无边框独占，全判成游戏。写这一节时踩过一次。
    """
    if not style:
        return True
    return bool(int(style) & _CHROME_BITS)


def idle_seconds() -> float:
    """距离最后一次键鼠操作过去了多少秒。"""
    if not IS_WINDOWS:
        return 0.0
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(_LastInputInfo)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # dwTime 是 32 位的开机毫秒数（约 49.7 天回绕一次），按 32 位取差值把回绕抵掉
    elapsed_ms = (int(_kernel32.GetTickCount64()) - int(info.dwTime)) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


class ProcNames:
    """进程号 → 进程名 / exe 路径，带缓存。"""

    def __init__(self) -> None:
        self._cache: dict[int, str] = {}
        self._exe_cache: dict[int, str] = {}

    def name_of(self, pid: int | None) -> str | None:
        if pid is None:
            return None
        cached = self._cache.get(pid)
        if cached is not None:
            return cached
        name = self._query(pid)
        if name is not None:
            if len(self._cache) >= PROC_CACHE_MAX:
                self._cache.clear()
            self._cache[pid] = name
        return name

    def exe_of(self, pid: int | None) -> str:
        """进程号 → exe **完整路径**（v2-game-activate 的安装路径判据用）；取不到返回空串。

        空串不是"这个进程没有路径"，而是"这一条查不到"——调用方按"没这条线索"处理。
        跟名字分开两个缓存：名字几乎每次都要用，路径只有判游戏那条线才问。
        """
        if pid is None:
            return ""
        cached = self._exe_cache.get(pid)
        if cached is not None:
            return cached
        path = self._query_exe(pid)
        if path:
            if len(self._exe_cache) >= PROC_CACHE_MAX:
                self._exe_cache.clear()
            self._exe_cache[pid] = path
        return path

    @staticmethod
    def _query(pid: int) -> str | None:
        if psutil is None:
            return None
        try:
            return psutil.Process(pid).name()
        except Exception as exc:   # 进程刚退出、或是系统进程读不到名字
            logger.debug("读取进程 %s 的名字失败: %s", pid, exc)
            return None

    @staticmethod
    def _query_exe(pid: int) -> str:
        """读 exe 路径。**读不到就返回空串，不抛**——高权限进程（系统进程、别的
        用户跑的游戏启动器）会 AccessDenied，这是常态，不是异常情况。"""
        if psutil is None:
            return ""
        try:
            return psutil.Process(pid).exe() or ""
        except Exception as exc:
            logger.debug("读取进程 %s 的 exe 路径失败: %s", pid, exc)
            return ""
