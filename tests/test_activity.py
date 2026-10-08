"""
前台监控与提醒的自动测试（把"现在前台是哪个应用"换成假的，不依赖真实窗口切换）。

覆盖：采样归并成一段段记录 / 人离开不算使用 / 久坐与分心提醒 / 免打扰与专注时闭嘴 /
自己与黑名单不记录 / 今日活动报告。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

from monitor import foreground
from monitor.category import friendly_name


class FakeScreen:
    """假的"当前前台"：测试里改这两个值，就等于用户在切换应用。"""

    def __init__(self):
        self.pid = 4242
        self.proc: str | None = "Code.exe"
        self.idle = 0.0


def main() -> int:
    box, stub, app = make_env()
    service = app.activity_service
    fake = FakeScreen()

    # 把 Windows 那套前台查询换掉：只换取数，业务逻辑照跑
    foreground.idle_seconds = lambda: fake.idle
    foreground.foreground_pid = lambda: fake.pid
    service._pids.name_of = lambda pid: fake.proc

    def tick() -> None:
        service._tick()
        pump(0.02)

    box_spoken: list[str] = []
    summoned: list[bool] = []
    # 久坐/分心提醒是**宠物自己**开的口（v2-speech-polish 起走主动线），
    # 所以听 pet_speak_proactive：听老口子等于把这条断言测废
    app.bus.pet_speak_proactive.connect(box_spoken.append)
    app.bus.summon_pet.connect(lambda: summoned.append(True))

    app.config.activity.remind = True
    app.config.schedule.quiet_hours = ["23:59", "23:59"]   # 测试期间永远不在免打扰里

    section("采样归并成一段段记录")
    fake.proc = "Code.exe"
    tick()
    check("记住当前应用", service.current_proc == "Code.exe", service.current_proc)
    check("这一段还没结束，先不落库", not app.activity_dao.today_top_apps(limit=10))

    service._current_start = time.time() - 60      # 假装这个应用已经用了一分钟
    service._flushed_at = service._current_start
    fake.proc = "chrome.exe"
    tick()
    names = [proc for proc, _sec in app.activity_dao.today_top_apps(limit=10)]
    check("切走时把上一段落库", "Code.exe" in names, str(names))
    check("当前应用换成新的", service.current_proc == "chrome.exe", service.current_proc)

    section("刷新统计不该打断久坐计时")
    fake.idle = 0.0
    fake.proc = "Code.exe"
    tick()
    app.activity_dao.clear_all()
    service._current_start = time.time() - 29 * 60      # 已经连续盯了 29 分钟
    service._flushed_at = service._current_start
    start_before = service._current_start
    app.activity_dao.clear_all()
    digest = service.today_summary_text()               # 主动搭话每 15 分钟会做这件事
    check("刷新统计不重置连续使用起点", service._current_start == start_before,
          f"{start_before:.0f} → {service._current_start:.0f}")
    check("刷新统计不算一次应用切换", service.current_proc == "Code.exe", service.current_proc)
    check("刷新统计确实把当前这段记进去了", "Code" in digest, digest.splitlines()[0] if digest else "")

    service._current_start = time.time() - 31 * 60      # 再过一会儿就到久坐线了
    service._last_sedentary = 0.0
    box_spoken.clear()
    tick()
    check("刷新过统计之后久坐照样能判出来", any("起来动动" in text for text in box_spoken), str(box_spoken))

    section("人离开不算使用时间")
    fake.idle = 10 * 60          # 超过 5 分钟没操作
    tick()
    check("人离开 → 当前段结束", service.current_proc == "", service.current_proc)
    check("人离开 → 不算有人在用电脑", not service.user_present)

    section("久坐提醒")
    fake.idle = 0.0
    fake.proc = "Code.exe"
    tick()
    service._current_start = time.time() - 31 * 60      # 同一个应用已经盯了 31 分钟
    service._last_sedentary = 0.0
    box_spoken.clear()
    summoned.clear()
    tick()
    check("久坐会说一句", any("起来动动" in text for text in box_spoken), str(box_spoken))
    check("久坐会把宠物叫到前面", bool(summoned))

    section("该闭嘴的时候闭嘴")
    def sedentary_once() -> list[str]:
        service._current_start = time.time() - 31 * 60
        service._last_sedentary = 0.0
        box_spoken.clear()
        tick()
        return box_spoken

    app.config.activity.remind = False
    check("关掉专注提醒 → 不出声", sedentary_once() == [])
    app.config.activity.remind = True

    now = datetime.now()
    app.config.schedule.quiet_hours = [
        now.strftime("%H:%M"),
        (now + timedelta(minutes=1)).strftime("%H:%M"),
    ]
    check("免打扰时段 → 不出声", sedentary_once() == [])
    app.config.schedule.quiet_hours = ["23:59", "23:59"]

    app.jobs.focus_mode = True
    check("专注期间 → 不出声", sedentary_once() == [])
    app.jobs.focus_mode = False

    section("分心提醒")
    service._switches.clear()
    service._last_distract = 0.0
    box_spoken.clear()
    for index in range(app.config.activity.distract_switches):
        fake.proc = "app%d.exe" % (index % 2)
        tick()
    check("切来切去会说一句分心", any("分心" in text for text in box_spoken), str(box_spoken))

    section("不该记的不记")
    fake.idle = 0.0
    fake.proc = "Code.exe"
    tick()
    fake.pid = os.getpid()      # 前台是宠物自己
    tick()
    check("宠物自己不被记录", service.current_proc == "", service.current_proc)

    fake.pid = 4242
    app.config.activity.blacklist = ["secret.exe"]
    fake.proc = "secret.exe"
    tick()
    check("黑名单里的应用不记录", service.current_proc == "", service.current_proc)
    app.config.activity.blacklist = []

    section("今日活动报告")
    service._close_segment(time.time(), reset_switches=True)
    app.activity_dao.clear_all()
    base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    app.activity_dao.record(app.user.id, "Code.exe", "工作", base, base + timedelta(minutes=40))
    app.activity_dao.record(
        app.user.id, "chrome.exe", "浏览", base + timedelta(minutes=40), base + timedelta(minutes=50)
    )
    report = service.today_report()
    text = report.render()
    first_line = text.splitlines()[1] if text else ""
    check("报告里有总时长", "总共用了" in text, first_line)
    check("报告里有应用排行", friendly_name("Code.exe") in text, "有" if friendly_name("Code.exe") in text else "缺")
    check("报告里有最长一段", "最长一段" in text, "有" if "最长一段" in text else "缺")
    check("报告里有类别占比", "类别占比" in text, "有" if "类别占比" in text else "缺")
    check("总时长算对了", report.total_sec == 50 * 60, str(report.total_sec))

    reports: list[str] = []
    app.bus.activity_report.connect(reports.append)
    service.announce_today()
    check("说今日活动会弹报告", any("今日活动" in item for item in reports), f"{len(reports)} 条")
    check("报告期间会把宠物叫出来", bool(summoned))

    drain_threads()
    box.cleanup()
    return summary("前台监控")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
