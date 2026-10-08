"""
自动番茄钟（S4）：前台是网课 / 创作 / 工作类软件，宠物就自己进番茄钟。

跟 focus/pomodoro.py 那套手动番茄钟的关系，一句话说清：
**状态机不共用、浮窗共用**。手动那套要落库（PomodoroSession）、要写
data/pomodoro_state.json（重启接着算），而这一套按约定只存内存——
不碰 pet.db、不写任何盘，程序一关就没了。所以这里自己拿一个轻量倒计时，
只在"显示"这一层跟手动那套共用 ui/pomodoro_bubble.py 那个浮窗。

切换规则（用户定的）：**切入自动开始，切走即结束**（v2-focus-notes2 ①：原先是"切走暂停、
回来接着跑"，但暂停态在界面上没有出口，浮窗会一直挂着；改成切走就收起浮窗、回空闲，
下次进来重新起一轮）。一轮跑完自己归零、安静收工。
同一时刻只有一套番茄钟在显示：手动那套在跑的时候，这套让位（装配层判断）。

不做的事：不弹卡、不落库、不调模型、不打断用户（只在结束时说一句）。

v2-focus-quiet：进专注**不再冒「看你进状态了，我陪你计时…」**。用户真机反馈那句在
专注中反复弹（同一状态被重复推），而且专注要的是安静——宠物自己又冒一句状态词，
等于没静下来。现在进专注只发 `changed`，由浮窗把倒计时亮出来（只显闹钟，不说话）。
"""


from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal

from core.config import Config
from core.events import EventBus
from life.screen.browser import TITLE_FOCUS
from monitor.category import resolve_category

logger = logging.getLogger(__name__)

TICK_MS = 1000

DONE_LINE = "{minutes} 分钟到啦，抬头喝口水，歇一会儿~"

# 用户自己点「开始专注」时 `_proc` 上的标记（不是进程名，只是让"这一轮陪着谁"
# 有个说法；菜单那条路跟前台无关）。
MANUAL_PROC = "手动"

# 网课 / 创作类：这些进程在内置分类表里有的归"工作"、有的归"其他"，
# 但它们都属于"坐下来做正事"这一档，所以单独列一份并到工作类里。
FOCUS_EXTRA: frozenset[str] = frozenset({
    # 网课 / 在线课堂
    "chaoxing.exe", "xuexitong.exe", "ketang.exe", "yuketang.exe", "classin.exe",
    "tencentmeeting.exe", "wemeetapp.exe", "voov.exe", "echoonline.exe",
    "xxt.exe", "ketangpai.exe", "schoology.exe",
    # 创作：剪辑 / 修图 / 音频 / 建模 / 设计
    "premiere.exe", "adobe premiere pro.exe", "afterfx.exe", "jianying.exe",
    "capcut.exe", "audition.exe", "au.exe", "vegas.exe", "davinci resolve.exe",
    "fl64.exe", "flstudio.exe", "cubase.exe", "studio one.exe",
    "3dsmax.exe", "maya.exe", "cinema 4d.exe", "sketchup.exe", "rhino.exe",
    "figma.exe", "axure.exe", "sketch.exe", "krita.exe", "clipstudiopaint.exe",
    "sai2.exe", "csp.exe",
})


def is_focus_proc(proc: str | None, overrides: dict[str, str] | None = None) -> bool:
    """这个前台应用算不算"该进番茄钟"的那种（网课 / 创作 / 工作类）。"""
    if not proc:
        return False
    key = proc.lower()
    if key in FOCUS_EXTRA:
        return True
    return resolve_category(key, overrides or {}) == "工作"


class ScreenFocusTimer(QObject):
    """前台落在网课/创作/工作类软件上时自动跑的番茄钟（只内存）。"""

    changed = Signal()      # 状态有变（跑起来了 / 暂停了 / 归零了），界面据此刷浮窗

    def __init__(self, config: Config, bus: EventBus, parent: QObject | None = None):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

        # 只存内存：剩余秒数、跑没跑
        self._remaining = self._duration()
        self._running = False
        self._proc = ""
        self._manual = False      # 用户自己点「开始专注」起的这一轮（跟前台判定脱钩）
        self._monitor = None      # attach 时装上，用来回问当前的标题判类

    # ---------- 参数 ----------

    def _duration(self) -> int:
        return max(1, int(self._config.screen.focus_minutes)) * 60

    @property
    def remaining(self) -> int:
        return max(0, self._remaining)

    @property
    def planned(self) -> int:
        return self._duration()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def in_progress(self) -> bool:
        """这一轮算不算"正在进行"：跑着，或者跑了一半停下还有进度。

        v2-focus-notes2 ① 之后**暂停态不再出现**（切走即结束、直接回空闲），所以这里
        实际就等于 `running`；留着后半截是给"有进度但没在跑"这个状态兜底——将来若
        再引入暂停，浮窗该不该收的判据不用重写。
        """
        if self._running:
            return True
        return 0 < self._remaining < self._duration()

    @property
    def proc(self) -> str:
        """正在陪着的那个人是哪个应用（空串表示没在陪）。"""
        return self._proc

    @property
    def manual(self) -> bool:
        """这一轮是不是用户自己点起来的（不跟前台判定挂钩）。"""
        return self._manual

    # ---------- 接线 ----------

    def attach(self, monitor) -> None:
        """挂到采样器上。

        两条信号都得听：`foreground_changed` 管"换了应用"，`title_changed` 管
        "还在同一个浏览器里，但换标签页了"——进学习通和点开一部剧，进程名都是
        chrome.exe，只有标题那条线分得出来。
        """
        self._monitor = monitor
        monitor.foreground_changed.connect(self.on_foreground)
        monitor.title_changed.connect(self.on_title)

    # ---------- 切换 ----------

    def on_title(self, _kind: str) -> None:
        """浏览器标题判类变了：照当前前台重判一次。"""
        self._reevaluate()

    def on_foreground(self, proc: str) -> None:
        """前台换人了：进了专注类就开跑（或接着跑），走了就暂停。"""
        self._reevaluate(proc)

    def _reevaluate(self, proc: str | None = None) -> None:
        if self._manual:
            return                      # 用户自己起的那一轮：前台怎么换都不动它
        if proc is None:
            proc = getattr(self._monitor, "foreground_proc", "") if self._monitor else ""
        if not self._config.screen.enabled or not self._config.screen.focus_enabled:
            # 开关关了要**真停下来**。原先这里是直接 return——正在跑的那一轮
            # 关开关也照跑，用户看到的就是"关不掉"（v2-focus-web ②）。
            self.leave()
            return
        if self._is_focus(proc):
            self.enter(proc)
        else:
            self.leave()

    def _is_focus(self, proc: str) -> bool:
        """这个前台算不算"该进番茄钟"。

        除了进程名那份表，浏览器还要看标题判类——网页版网课、在线创作工具都在
        浏览器里，进程名只有 chrome.exe 一个。标题判成 watch 的（在看剧）不算。
        """
        if is_focus_proc(proc):
            return True
        kind = getattr(self._monitor, "title_kind", "") if self._monitor else ""
        return kind == TITLE_FOCUS

    def enter(self, proc: str) -> None:
        """进入专注类应用：没在跑的就开始，暂停着的就接着跑。"""
        if self._running:
            return
        if self.remaining <= 0:
            self._remaining = self._duration()   # 上一轮已经归零，重新起一轮
        self._running = True
        self._proc = proc
        self._timer.start()
        # v2-focus-quiet：这里不再冒「看你进状态了」——只发 changed，浮窗亮倒计时。
        # 同样的状态 `_running` 挡在函数头上，重复 enter 也不会重复推提示。
        self.changed.emit()

    def leave(self) -> None:
        """离开专注类应用：**这一轮就结束了**（v2-focus-notes2 ①）。

        原先是"暂停"——时间留住、浮窗一直挂在屏幕上，等用户切回来接着跑。真机上的
        问题是那个状态没有出口：用户切走了，屏幕上还杵着一个不走的倒计时，看着像
        "这一轮还在"，只能靠右键菜单或退出程序收掉。用户要的是**暂停即退出**：
        离开专注应用就收起浮窗、回空闲，下次进来重新起一轮。

        跟 `stop()` 的区别只在语义——`stop()` 是用户主动点「结束专注」，
        `leave()` 是前台自己走掉了；两者落在同一处（都回空闲），所以直接走 reset。
        """
        if not self._running and not self.in_progress:
            return
        self.reset()

    def start_manual(self) -> None:
        """用户自己点「开始专注」：不等前台判定，直接起一轮。

        真机反馈里"自动认不出来就没法测"的那半截（v2-focus-web ①）。起来之后
        跟前台脱钩（`_manual`），换窗口、切标签都不打断——用户点了才算数，
        要停就点「结束专注」。跟自动那套一样**只存内存**，不写 pet.db。
        """
        self._manual = True
        self.enter(MANUAL_PROC)

    def stop(self) -> None:
        """用户点的「结束专注」：不管跑着还是暂停着，这一轮作废、回到空闲。

        暂停态原先没有任何出口——浮窗一直挂着、切回专注应用又接着跑，
        只能退出程序（v2-focus-web ②）。这个出口就是给那种状态开的。
        """
        self.reset()

    def reset(self) -> None:
        """手动清掉这一轮（用户点结束 / 测试 / 设置改动时用）。"""
        self._timer.stop()
        self._running = False
        self._proc = ""
        self._manual = False
        self._remaining = self._duration()
        self.changed.emit()

    def apply_config(self) -> None:
        """配置改了：时长按新值走；开关关掉就真停。

        原先这里无条件把剩余拨回整轮：正在跑的那一轮被重置、关了开关的表照旧跑。
        现在只有"这一轮本来就没在跑"的时候才拨时长，跑着/暂停着的一律不打断
        （设置页那句"改动从下一个阶段开始生效"才是真的）。
        """
        if not self._config.screen.enabled or not self._config.screen.focus_enabled:
            self.reset()
            return
        if not self.in_progress:
            self._remaining = self._duration()
        self.changed.emit()

    # ---------- 计时 ----------

    def _tick(self) -> None:
        if not self._running:
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self._finish()
            return
        self.changed.emit()

    def _finish(self) -> None:
        """一轮跑完：归零、收工、说一句。"""
        self._timer.stop()
        self._running = False
        self._proc = ""
        self._remaining = 0
        self.changed.emit()
        self._bus.pet_speak_proactive.emit(DONE_LINE.format(minutes=self._config.screen.focus_minutes))
