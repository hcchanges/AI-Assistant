"""
番茄钟的自动测试。

计时器是纯状态机（可以注入临时存档路径），先单独验；
再接上服务层验落库与"专注中屏蔽提醒"的开关有没有残留。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary

from core.config import PomodoroSection
import focus.pomodoro as pomodoro_mod
from focus.pomodoro import PomodoroTimer, Phase


def new_timer(state_path: Path, **overrides) -> PomodoroTimer:
    cfg = PomodoroSection(**overrides)
    return PomodoroTimer(cfg, state_path=state_path)


def main() -> int:
    # 番茄钟的存档默认写在项目 data/ 下，测试期间必须改到临时目录，别动用户的真实存档；
    # 这个常量是在构造服务时读的，所以要在建应用之前改。
    box, stub, app = make_env(
        prepare=lambda b: setattr(pomodoro_mod, "STATE_FILE", str(b.dir / "pomodoro_state.json"))
    )

    section("计时状态机")
    timer = new_timer(box.state_file, focus_min=25, short_break_min=5, long_break_min=15, long_break_every=4)

    timer.start_focus()
    check("开始专注 → 进入专注阶段", timer.phase == Phase.FOCUS, timer.phase.value)
    check("计划时长按分钟换算成秒", timer.planned == 25 * 60, str(timer.planned))
    check("剩余时间约等于计划时长", abs(timer.remaining - timer.planned) <= 1, str(timer.remaining))
    check("专注中 is_focusing 为真", timer.is_focusing)

    timer.pause()
    frozen = timer.remaining
    check("暂停后标记为暂停", timer.paused)
    time.sleep(1.05)
    check("暂停期间剩余时间不动", timer.remaining == frozen, f"{frozen} → {timer.remaining}")
    timer.resume()
    check("继续后取消暂停", not timer.paused)
    check("继续后剩余时间接着走", timer.remaining <= frozen, str(timer.remaining))

    timer._deadline = time.time() - 1      # 把截止时刻拨到过去，模拟"到点了"
    timer._on_tick()
    check("专注走完 → 完成数 +1", timer.completed_focus == 1, str(timer.completed_focus))
    check("专注走完 → 自动进入短休", timer.phase == Phase.SHORT_BREAK, timer.phase.value)

    section("跳过与重置")
    before_completed = timer.completed_focus
    timer.skip()
    check("跳过短休 → 回到专注", timer.phase == Phase.FOCUS, timer.phase.value)
    check("跳过不计入完成数", timer.completed_focus == before_completed, str(timer.completed_focus))
    timer.reset()
    check("重置 → 回到空闲", timer.phase == Phase.IDLE, timer.phase.value)
    check("重置 → 不再是激活状态", not timer.is_active)
    check("空闲时 is_focusing 为假", not timer.is_focusing)

    section("长休节奏")
    timer.set_completed_focus_count(3)      # 今天已经做过 3 个番茄
    timer.start_focus()
    timer._deadline = time.time() - 1
    timer._on_tick()
    check("第 4 个专注走完 → 进入长休", timer.phase == Phase.LONG_BREAK, timer.phase.value)
    check("完成数记到 4", timer.completed_focus == 4, str(timer.completed_focus))
    timer._deadline = time.time() - 1
    timer._on_tick()
    check("长休走完 → 回到空闲等用户再开", timer.phase == Phase.IDLE, timer.phase.value)

    section("关程序再打开接着走")
    timer2 = new_timer(box.state_file, focus_min=25)
    timer2.start_focus()
    timer2._deadline = time.time() + 600      # 还剩 10 分钟
    timer2.save_state()

    timer3 = new_timer(box.state_file, focus_min=25)
    outcome = timer3.restore()
    check("能接着上次的专注跑", outcome == "running" and timer3.phase == Phase.FOCUS, f"{outcome}/{timer3.phase.value}")
    check("剩余时间按墙钟算对了", 595 <= timer3.remaining <= 600, str(timer3.remaining))

    timer3.pause()
    timer4 = new_timer(box.state_file, focus_min=25)
    outcome = timer4.restore()
    check("上次是暂停态 → 恢复成暂停", outcome == "paused" and timer4.paused, f"{outcome}/{timer4.paused}")

    data = json.loads(box.state_file.read_text(encoding="utf-8"))
    data["saved_at"] = (datetime.now() - timedelta(days=1)).timestamp()
    box.state_file.write_text(json.dumps(data), encoding="utf-8")
    timer5 = new_timer(box.state_file, focus_min=25)
    check("隔天的存档不恢复", timer5.restore() == "none", timer5.restore())
    timer5.reset()

    section("服务层：落库与屏蔽提醒")
    service = app.pomodoro_service
    service.timer.reset()
    start = time.time()
    service.timer.start_focus()
    running = app.pomodoro_dao.latest_running()
    check("开始时落一条运行中的会话", running is not None, f"会话 id={running.id}" if running else "没有记录")
    check("专注中打开屏蔽提醒开关", app.jobs.focus_mode, str(app.jobs.focus_mode))

    service.timer._deadline = time.time() - 1
    service.timer._on_tick()
    check("走完一个番茄 → 今日完成数 +1", app.pomodoro_dao.today_completed_focus_count() == 1,
          str(app.pomodoro_dao.today_completed_focus_count()))
    check("走完一个番茄 → 累计专注时长记上了", app.pomodoro_dao.today_focus_seconds() > 0,
          str(app.pomodoro_dao.today_focus_seconds()))
    check("进入短休 → 屏蔽提醒自动关掉", not app.jobs.focus_mode, str(app.jobs.focus_mode))
    check("阶段变化会通知界面", service.timer.phase == Phase.SHORT_BREAK, service.timer.phase.value)
    check("这一步耗时很短（不是真等 25 分钟）", time.time() - start < 30, f"{time.time() - start:.1f} 秒")

    service.timer.reset()          # 回到空闲，才开得起下一个专注
    service.timer.start_focus()
    check("再次专注 → 屏蔽提醒又打开", app.jobs.focus_mode, str(app.jobs.focus_mode))
    service.timer.reset()
    check("从专注直接重置 → 屏蔽开关不残留", not app.jobs.focus_mode, str(app.jobs.focus_mode))

    drain_threads()
    box.cleanup()
    return summary("番茄钟")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
