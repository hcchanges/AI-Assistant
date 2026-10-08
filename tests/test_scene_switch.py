"""
场景/姿态/状态词时序修正（v2-scene-switch）的自动测试。

用户真机三条反馈合并修：
① 专注→桌面切换延迟几秒——前台检测只挂在 sample_sec（默认 5 秒）那一拍上；
② 专注态拖几下宠物就退出专注——拖宠物让前台变成本进程，发出去的 foreground_changed
   被自动番茄钟当成"切走了专注应用"收场；
③ 桌面场景（浏览网页）乱飘状态词"好开心"——浏览器在 is_watchable 名单里，刷网页也被
   拉去 OCR 认字，撞上"喜"就判 festive、冒状态词。

对应三条改动（全在 life/screen/monitor.py，零新依赖）：
- 前台检测拆成 `_poll_foreground()`，单独挂一个 500ms 快计时器（`_fg_timer`）——
  前台切换即时响应，不再等 5 秒那一拍；
- `_poll_foreground()` 里前台是本进程时**不发任何信号**（拖宠物不再被当成"切走专注
  应用"）——但要**把记账那一格 `_proc` 跟到现状**（v2-social-fg-residual）：
  只认"变"不认"现状"的话，"微信 → 点一下宠物 → 切回微信"这条路上 `proc` 跟没动过的
  `_proc` 正好相等，一个信号都不发，下游那条社交线永远等不到"微信回到前台"；
- `sample()` 里浏览器前台但标题不是 watch 时直接跳过 OCR——浏览网页不认字、不飘词，
  只有电影态（watch）才认。

红线：pet.db / mind.db 零写入、零新依赖。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QImage

from harness import check, drain_threads, make_env, section, summary, wait_for

from life.screen import monitor as mon
from life.screen.browser import TITLE_WATCH
from life.screen.monitor import FOREGROUND_POLL_MS


class FakeReader:
    """假 OCR：永远读回同一句。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假截屏：记自己被叫了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None) -> QImage:
        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


def _fake_foreground(monitor, proc_holder, title_holder, pid=1) -> None:
    mon.foreground.foreground_pid = lambda: pid
    monitor._pids.name_of = lambda value: proc_holder["proc"]
    mon.foreground.foreground_title = lambda: title_holder["title"]


def main() -> int:
    box, _stub, app = make_env()
    monitor = app.screen_monitor
    focus = app.screen_focus_timer

    proc = {"proc": ""}
    title = {"title": ""}
    reader, grabber = FakeReader(), FakeGrabber()
    _fake_foreground(monitor, proc, title)
    monitor._reader = reader
    monitor._grabber = grabber

    # ---------- ① 前台快轮询：换前台即时发信号、不绑 5 秒 OCR 那一拍 ----------

    section("① 前台快轮询：换前台即时响应、不绑 5 秒那一拍")

    check("新增了前台快计时器 _fg_timer", hasattr(monitor, "_fg_timer"), "")
    check("快轮询间隔钉在 500ms（不再是 sample_sec 的 5 秒）",
          monitor._fg_timer.interval() == FOREGROUND_POLL_MS == 500,
          f"{monitor._fg_timer.interval()} / {FOREGROUND_POLL_MS}")
    check("FOREGROUND_POLL_MS 常量存在且为 500", FOREGROUND_POLL_MS == 500,
          str(FOREGROUND_POLL_MS))

    emitted = []
    monitor.foreground_changed.connect(emitted.append)
    monitor._proc = ""          # 清一下，让它觉得"换人了"
    monitor._title_kind = ""
    proc["proc"] = "potplayer.exe"
    before_grab = grabber.calls
    monitor._poll_foreground()  # 只轮询前台，不跑 sample()
    check("_poll_foreground 同步把前台更新成 potplayer", monitor._proc == "potplayer.exe",
          repr(monitor._proc))
    check("当场发 foreground_changed（不用等 5 秒）", emitted == ["potplayer.exe"],
          str(emitted))
    check("快轮询不碰 OCR/截屏那一拍", grabber.calls == before_grab,
          f"截屏 {before_grab} → {grabber.calls}")

    # ---------- ② 前台是自己（拖宠物）：不发 foreground，专注不被拖散 ----------

    section("② 拖宠物（前台=自己）不再把专注拖散")

    emitted.clear()
    focus.reset()
    focus.enter("pycharm64.exe")     # 直接进专注
    check("前置：确实进了专注", focus.running and focus.proc == "pycharm64.exe",
          f"running={focus.running} proc={focus.proc}")
    monitor._proc = "pycharm64.exe"
    monitor._title_kind = ""
    proc["proc"] = "python.exe"
    _fake_foreground(monitor, proc, title, pid=os.getpid())   # 前台就是我们自己
    monitor._poll_foreground()
    check("认得出：这一拍前台就是我们自己的 pid",
          monitor._fg_pid == os.getpid() and monitor._is_self_foreground() is True,
          f"{monitor._fg_pid} / {os.getpid()}")
    check("前台是自己 → 不发 foreground_changed（拖宠物不误报换应用）", emitted == [],
          str(emitted))
    check("专注没被拖散（还在跑、还陪着同一个应用）",
          focus.running and focus.proc == "pycharm64.exe",
          f"running={focus.running} proc={focus.proc}")
    # 这一条是 **v2-social-fg-residual 有意改掉的旧断言**（原来钉的是"monitor 记住的
    # 前台也没被自己盖掉"，即 `_proc` 仍停在 pycharm64.exe）。旧口径会漏掉一条最平常的
    # 路：微信里点一下宠物再切回微信，`proc` 跟没动过的 `_proc` 正好相等 → 一个信号都
    # 不发，社交那条线永远等不到"微信回到前台"。现在记账跟到现状（**记的是现状、发的
    # 是变化**），"切回微信"就变回一次真的变化。知情的偏离，见报告 138-④。
    check("记账那一格跟到现状（前台是自己时也更新 _proc，不怕自己盖掉）",
          monitor._proc == "python.exe", repr(monitor._proc))

    def _poll_as(name: str, pid: int) -> list[str]:
        """把前台换成 name（pid 决定"是不是我们自己"），跑一拍，回这一拍**新发**的信号。"""
        before = len(emitted)
        proc["proc"] = name
        _fake_foreground(monitor, proc, title, pid=pid)
        monitor._poll_foreground()
        return emitted[before:]

    back = _poll_as("weixin.exe", 4321)          # 点完宠物，切回微信（不是自己）
    check("★ 于是「切回微信」重新变成一次真的变化（社交那条线这才等得到）",
          back == ["weixin.exe"], f"{back}")
    check("★ 切回去之后 `_proc` 记得的也是微信（现状跟着走）",
          monitor._proc == "weixin.exe", repr(monitor._proc))
    again = _poll_as("weixin.exe", 4321)
    check("★ 同一格再轮询一次不再重复发（记现状没有把信号变成每拍一发）",
          again == [], f"{again}")
    monitor._proc = "pycharm64.exe"        # 复位，接着跑下面几节
    monitor._title_kind = ""
    focus.reset()

    # ---------- ②-2 「现在前台是谁」问得出来（v2-social-foreground-fix） ----------

    section("②-2 现算前台 foreground_now()：只看不发信号，换人信号那条语义不动")

    emitted.clear()
    monitor._proc = "pycharm64.exe"          # 记账那一份：上一个真应用
    proc["proc"] = "python.exe"
    _fake_foreground(monitor, proc, title, pid=os.getpid())   # 前台就是我们自己
    check("前台是自己 → foreground_now() 回空串（对外部应用而言没人在前台）",
          monitor.foreground_now() == "", repr(monitor.foreground_now()))
    check("这一问**不动** _proc（记账那份还是上一个真应用，②那条口径没被动）",
          monitor._proc == "pycharm64.exe", repr(monitor._proc))
    check("这一问也**不发**信号（前台是自己时照旧静默）", emitted == [], str(emitted))

    proc["proc"] = "wechat.exe"
    _fake_foreground(monitor, proc, title)   # 前台换成微信（非自己）
    check("换到别的应用 → 当场问得出来（不用等下一拍换人）",
          monitor.foreground_now() == "wechat.exe", repr(monitor.foreground_now()))
    check("问完之后 _proc 仍归轮询那条路管（这一问不替它记账）",
          monitor._proc == "pycharm64.exe", repr(monitor._proc))

    # ---------- ③ 浏览器非电影态不认字不飘、电影态才认 ----------

    section("③ 浏览器非电影态不认字不飘，电影态才认")

    _fake_foreground(monitor, proc, title)   # 复位：前台 pid 回到非自己（上一节改成了自己）
    monitor._proc = ""
    monitor._title_kind = ""

    # 刷网页（标题判成空串，不是 watch）→ 不截屏、不认字、不冒场景
    proc["proc"] = "chrome.exe"
    title["title"] = "今日头条"
    grabber.calls = 0
    reader.calls = 0
    monitor.sample()
    check("刷网页（非 watch 标题）→ 判成空串", monitor.title_kind == "",
          repr(monitor.title_kind))
    check("刷网页不截屏、不认字（修复前会拉去 OCR）",
          grabber.calls == 0 and reader.calls == 0,
          f"截屏 {grabber.calls} / 认字 {reader.calls}")
    check("刷网页也不冒场景", monitor.scene == "", repr(monitor.scene))

    # 看剧（标题判 watch）→ 照旧认字（电影态才认这条腿没关）
    title["title"] = "某电视剧 第01集 - 在线观看"
    monitor._title_kind = ""
    grabber.calls = 0
    monitor._poll_foreground()
    check("看剧标题判成 watch", monitor.title_kind == TITLE_WATCH, repr(monitor.title_kind))
    monitor.sample()
    check("电影态照旧截屏（非电影态闸没把功能一起关掉）", grabber.calls >= 1,
          f"截屏 {grabber.calls} 次")
    wait_for(lambda: not monitor._reading)   # 让这拍的 OCR 跑完，别挡下一拍

    # 非浏览器（播放器）→ 也照旧截屏（闸只挡浏览器非 watch 这一档）
    proc["proc"] = "potplayer.exe"
    title["title"] = ""
    monitor._proc = ""
    monitor._title_kind = ""
    grabber.calls = 0
    monitor.sample()
    check("播放器前台照旧截屏（名单那条路没动）", grabber.calls >= 1,
          f"截屏 {grabber.calls} 次")
    wait_for(lambda: not monitor._reading)

    # ---------- 红线 ----------

    section("红线：零新依赖（只用既有的 PySide6 QTimer 与本地模块）")
    src = (Path(__file__).resolve().parents[1] / "life" / "screen" / "monitor.py").read_text(encoding="utf-8")
    check("新增导入来自既有本地模块（life.screen.browser），不是第三方",
          "from life.screen.browser import TITLE_FOCUS, TITLE_WATCH, classify_title, is_browser" in src, "")
    check("快轮询只用了早已在用的 PySide6 QTimer",
          "self._fg_timer = QTimer(self)" in src, "")

    drain_threads()
    box.cleanup()
    return summary("场景/姿态/状态词时序修正（v2-scene-switch）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
