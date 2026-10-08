"""
主动搭话的自动测试。

它只在"七道门"全开时才开口，所以这里逐道门验一遍：
专注提醒开关 / 专注中 / 免打扰 / 人在不在 / 有没有 Key / 距上次开口 / 距上次互动 / 今日次数。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from brain.chains.proactive import SILENT_TOKEN
from life.proactive import MAX_PER_DAY, MIN_GAP_SEC, MIN_QUIET_AFTER_CHAT_SEC, ProactiveService


class StubMemories:
    def __init__(self, memories=None):
        self._memories = list(memories or [])

    def recent(self, limit: int = 8):
        return self._memories[:limit]


class StubChain:
    def __init__(self):
        self.reply = "歇会儿吧，你已经忙很久了"
        self.memories: list[str] = []

    def compose(self, summary: str, memories) -> str:
        self.memories = list(memories)
        return self.reply


def main() -> int:
    box, stub, app = make_env()
    chain = StubChain()
    service = ProactiveService(
        config=app.config,
        bus=app.bus,
        memory_dao=StubMemories(),
        chain=chain,
        activity_summary=lambda: "今天主要在写代码",
        is_user_present=lambda: True,
        is_focusing=lambda: False,
    )

    spoken: list[str] = []
    # 主动搭话走的是"宠物自己开口"那条线（v2-proactive-voice：出声前要先问一句），
    # 跟回用户话的 pet_speak 分开了；这儿验的是"话说到没说到"，盯的还是同一个出口位置
    app.bus.pet_speak_proactive.connect(spoken.append)

    now = datetime.now()
    app.config.activity.remind = True
    app.config.schedule.quiet_hours = ["23:59", "23:59"]   # 测试期间不在免打扰里
    active_provider = app.config.llm.active

    section("七道门全开才开口")
    check("条件都满足时可以开口", service._can_speak(now))

    app.config.activity.remind = False
    check("专注提醒开关关着 → 不开口", not service._can_speak(now))
    app.config.activity.remind = True

    service._is_focusing = lambda: True
    check("番茄钟专注中 → 不开口", not service._can_speak(now))
    service._is_focusing = lambda: False

    app.config.schedule.quiet_hours = [
        now.strftime("%H:%M"),
        (now + timedelta(minutes=1)).strftime("%H:%M"),
    ]
    check("免打扰时段 → 不开口", not service._can_speak(now))
    app.config.schedule.quiet_hours = ["23:59", "23:59"]

    service._is_user_present = lambda: False
    check("人不在电脑前 → 不开口", not service._can_speak(now))
    service._is_user_present = lambda: True

    from harness import add_no_key_provider

    no_key = add_no_key_provider(app.config)
    app.config.llm.active = no_key
    check("没配 API Key → 不开口", not service._can_speak(now))
    app.config.llm.active = active_provider

    section("节奏与次数")
    service._last_spoken = time.monotonic() - (MIN_GAP_SEC - 60)
    check(f"距上次开口不到 {MIN_GAP_SEC // 60} 分钟 → 不开口", not service._can_speak(now))
    service._last_spoken = time.monotonic() - (MIN_GAP_SEC + 60)
    check("隔够了 → 可以开口", service._can_speak(now))

    service._last_interaction = time.monotonic() - (MIN_QUIET_AFTER_CHAT_SEC - 60)
    check(
        f"刚跟它说过话不到 {MIN_QUIET_AFTER_CHAT_SEC // 60} 分钟 → 不插嘴",
        not service._can_speak(now),
    )
    service.touch()
    check("touch() 会刷新互动时间", not service._can_speak(now))
    service._last_interaction = time.monotonic() - (MIN_QUIET_AFTER_CHAT_SEC + 60)
    check("安静够久了 → 可以开口", service._can_speak(now))

    service._last_spoken = float("-inf")
    service._last_interaction = float("-inf")
    service._count_date = now.date()
    service._count_today = MAX_PER_DAY
    check(f"今天已经开口 {MAX_PER_DAY} 次 → 不开口", not service._can_speak(now))

    service._count_date = now.date() - timedelta(days=1)
    check("跨天 → 次数清零可以开口", service._can_speak(now))
    check("跨天后计数确实归零", service._count_today == 0, str(service._count_today))

    section("开口与静默")
    service._count_today = 0
    spoken.clear()
    service._check()
    wait_for(lambda: bool(spoken))
    check("该开口时真的说了", spoken == [chain.reply], str(spoken))
    check("开口后计一次数", service._count_today == 1, str(service._count_today))
    check("开口后刷新上次开口时间", service._last_spoken > 0)

    chain.reply = SILENT_TOKEN
    service._last_spoken = float("-inf")
    service._last_interaction = float("-inf")
    service._count_today = 0
    spoken.clear()
    service._check()
    pump(0.4)      # 模型回调是异步回来的，等它一下再看有没有乱说话
    check("模型回 [SILENT] 时保持安静", spoken == [], str(spoken))

    drain_threads()

    section("念旧记忆时，相对时间按今天重说")
    from core.reltime import memory_text
    from data.models import Memory

    when = (datetime.now() - timedelta(days=3)).replace(hour=8, minute=0)
    memory = Memory(content="用户明早要去体检", abs_time=when)
    service._memory_dao = StubMemories([memory])
    chain.memories = []
    service._compose()
    check("交给模型的是重写后的正文", chain.memories == [memory_text(memory)], str(chain.memories))
    check("不再念\"明早\"", chain.memories and "明早" not in chain.memories[0], str(chain.memories))
    check("改说了\"大前天\"", chain.memories and "大前天" in chain.memories[0], str(chain.memories))

    box.cleanup()
    return summary("主动搭话")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
