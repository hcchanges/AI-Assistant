"""
聊天意图识别精准度（v2-chat-intent）。

真机问题：闲聊「我今晚吃了红烧肉你呢」被当成提醒意图——宠物反问"「今晚」大概是几点呀"，
状态机卡进等待态。根因是**没有一层回答"这句报了时间，是不是真要我到点做点什么"**：
模型读出"今晚"报 fuzzy，时间词就被直接绑成了提醒动作。

这份测试钉四件事：
① 闲聊里的时间词**不绑动作**（不反问几点、不进等待态、不建提醒、不开番茄钟）；
② 只有**明说**要专心才开番茄钟（"我要专注写代码"开，"我在写代码"不开）；
③ 显式提醒请求照旧走提醒，且**不顺手**把番茄钟开起来；
④ 一句话里多个意图信号**只出一个动作**（显式专属 > 闲聊）。

另外单钉一条红线：聊天开番茄钟走的是屏幕计时器那条**内存**线，
不能往 pet.db 落 `PomodoroSession`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, pump, section, summary, wait_for

from life.dialog import chat_intent

# 反问"几点"的那句（`_ask_time_text` 的普通话术）里必定带这个字眼，
# 拿它当"进没进提醒那条线"的探针
ASK_TIME_MARK = "几点"


def _sessions(app) -> int:
    """pet.db 里落了几行番茄钟会话记录（红线探针）。走 app 自己那个 DAO，不另开会话。"""
    return len(app.pomodoro_dao.recent(limit=200))


class Flow:
    """驱动一轮吐嘈，收集宠物说了什么。跟 test_chatter.py 的 Flow 同一套打法。"""

    def __init__(self, app, stub):
        self.app, self.stub = app, stub
        self.spoken: list[str] = []
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.pet_speak_proactive.connect(self.spoken.append)

    def say(self, text: str, *, confidence: str = "none", time_hint: str = "",
            time_iso: str = "", reply: str = "记住啦") -> list[str]:
        self.spoken.clear()
        self.stub.parse_result = {
            "scene": text[:12],
            "time_hint": time_hint,
            "time_iso": time_iso,
            "time_confidence": confidence,
            "emotion": "平静",
            "module": "其他",
            "command": "",
            "reply": reply,
        }
        self.app.chatter_service.handle(text)
        wait_for(lambda: bool(self.spoken))
        pump(0.05)
        return list(self.spoken)

    @property
    def state(self) -> str:
        return self.app.chatter_service.state.value

    def reminders(self) -> list:
        return self.app.reminder_dao.list_all(limit=100)


def main() -> int:
    box, stub, app = make_env()
    app.scheduler_engine.start()
    app.config.schedule.quiet_hours = ["23:59", "23:59"]
    flow = Flow(app, stub)

    # ---------------------------------------------------------------- ① 模块本身

    section("① 闲聊 vs 动作意图：词表这一层怎么判（不走 app）")
    check("闲聊带时间词 → 不是提醒请求",
          not chat_intent.wants_reminder("我今晚吃了红烧肉你呢", ["提醒我", "叫我", "闹钟"]),
          f"wants_reminder={chat_intent.wants_reminder('我今晚吃了红烧肉你呢', ['提醒我', '叫我', '闹钟'])}")
    check("闲聊里没有专注意图",
          not chat_intent.explicit_focus("我今晚吃了红烧肉你呢"),
          f"explicit_focus={chat_intent.explicit_focus('我今晚吃了红烧肉你呢')}")
    check("闲聊 → 归到 chat 档",
          chat_intent.action_of("我今晚吃了红烧肉你呢", ["提醒我"]) == chat_intent.ACTION_CHAT,
          chat_intent.action_of("我今晚吃了红烧肉你呢", ["提醒我"]))
    check("时间词本身不算请求：「晚上吃什么好呢」",
          not chat_intent.wants_reminder("晚上吃什么好呢", ["提醒我", "叫我"]),
          f"wants_reminder={chat_intent.wants_reminder('晚上吃什么好呢', ['提醒我', '叫我'])}")
    check("显式提醒词 → 是请求",
          chat_intent.wants_reminder("提醒我今晚8点吃药", ["提醒我", "叫我"]),
          f"wants_reminder={chat_intent.wants_reminder('提醒我今晚8点吃药', ['提醒我', '叫我'])}")
    check("明说专注 → 是专注意图",
          chat_intent.explicit_focus("我要专注写代码"),
          f"explicit_focus={chat_intent.explicit_focus('我要专注写代码')}")
    check("只是陈述在写代码 → 不算专注意图",
          not chat_intent.explicit_focus("我在写代码"),
          f"explicit_focus={chat_intent.explicit_focus('我在写代码')}")
    check("否定词挡得住：「别开始计时了」",
          not chat_intent.explicit_focus("别开始计时了"),
          f"explicit_focus={chat_intent.explicit_focus('别开始计时了')}")
    check("否定词挡得住：「不用陪我工作了」",
          not chat_intent.explicit_focus("不用陪我工作了"),
          f"explicit_focus={chat_intent.explicit_focus('不用陪我工作了')}")
    check("多意图取一个：专注 > 提醒",
          chat_intent.action_of("我要专注写代码，提醒我8点吃药", ["提醒我"])
          == chat_intent.ACTION_FOCUS,
          chat_intent.action_of("我要专注写代码，提醒我8点吃药", ["提醒我"]))
    check("多意图取一个：提醒 > 闲聊",
          chat_intent.action_of("提醒我吃药，今天好累", ["提醒我"]) == chat_intent.ACTION_REMIND,
          chat_intent.action_of("提醒我吃药，今天好累", ["提醒我"]))

    # ------------------------------------------------- ② S4-① 闲聊不绑时间动作

    section("② S4-①「我今晚吃了红烧肉你呢」→ 纯闲聊")
    said = flow.say("我今晚吃了红烧肉你呢", confidence="fuzzy", time_hint="今晚")
    check("不进等待态（状态仍是空闲）", flow.state == "idle", flow.state)
    check("没有反问「几点」", not any(ASK_TIME_MARK in t for t in said), str(said))
    check("宠物照常回了一句", bool(said), str(said))
    check("没建提醒", len(flow.reminders()) == 0, f"{len(flow.reminders())} 条")
    check("没自动开番茄钟", not app.screen_focus_timer.running,
          f"running={app.screen_focus_timer.running}")
    check("手动那套番茄钟也没被碰", not app.pomodoro_service.timer.is_active,
          f"is_active={app.pomodoro_service.timer.is_active}")

    section("②续：一串闲聊带时间词，一个都不该绑动作")
    for text, hint in [
        ("今天吃了火锅好撑", "今天"),
        ("晚上吃什么好呢", "晚上"),
        ("我们几点见面来着", "几点"),
        ("周末打算去爬山", "周末"),
        ("明天要下雨吧", "明天"),
    ]:
        before = len(flow.reminders())
        said = flow.say(text, confidence="fuzzy", time_hint=hint)
        ok = (flow.state == "idle" and len(flow.reminders()) == before
              and not any(ASK_TIME_MARK in t for t in said))
        check(f"「{text}」→ 只闲聊，不绑动作", ok,
              f"state={flow.state} 提醒={len(flow.reminders())} 说了={said}")

    # ------------------------------------------- ③ S4-③ 显式提醒照旧 + 不开番茄钟

    section("③ S4-③「提醒我今晚8点吃药」→ 设提醒，不强制开番茄钟")
    sessions_before = _sessions(app)
    said = flow.say("提醒我今晚8点吃药", confidence="fuzzy", time_hint="今晚8点")
    check("进入了等待补答状态（照旧反问）", flow.state == "awaiting_time", flow.state)
    check("反问里带上了原话的时间说法", any("今晚8点" in t for t in said), str(said))
    check("等待态里没顺手开番茄钟", not app.screen_focus_timer.running,
          f"running={app.screen_focus_timer.running}")
    # 补答一个具体时刻，确认提醒这条线整条是通的（不是被新闸拦死）
    flow.app.config.schedule.quiet_hours = ["23:59", "23:59"]
    stub.followup = {"intent": "answer_time", "time_iso": "2099-01-01 20:00"}
    flow.spoken.clear()
    app.chatter_service.handle("就八点吧")
    wait_for(lambda: bool(flow.spoken))
    pump(0.05)
    check("补答后建了提醒", len(flow.reminders()) == 1, f"{len(flow.reminders())} 条")
    check("回到空闲", flow.state == "idle", flow.state)
    check("这一路仍然没写番茄钟会话", _sessions(app) == sessions_before,
          f"{sessions_before} → {_sessions(app)}")

    # ------------------------------------------- ④ S4-② 明说专注才开番茄钟

    section("④ S4-②「我要专注写代码」→ 开番茄钟")
    app.screen_focus_timer.reset()
    pump(0.05)
    sessions_before = _sessions(app)
    said = flow.say("我要专注写代码")
    check("番茄钟起来了", app.screen_focus_timer.running,
          f"running={app.screen_focus_timer.running}")
    check("有剩余时间（真在计时，不是空壳）",
          app.screen_focus_timer.remaining > 0, f"{app.screen_focus_timer.remaining}s")
    # v2-focus-quiet 之后计时器进场不再自报开场白（原来是「看你进状态了，我陪你计时…」）。
    # 这条改成正向断言新行为：不复现那句旧文案，也不冒任何"进状态"的提示。
    check("计时器不再冒开场白（进专注静默）",
          not any("陪你计时" in t or "进状态" in t for t in said), str(said))
    check("这一句不建提醒", len(flow.reminders()) == 1, f"{len(flow.reminders())} 条")
    check("状态没被留在等待态", flow.state == "idle", flow.state)
    check("红线：走内存那条线，pet.db 没多番茄钟会话",
          _sessions(app) == sessions_before, f"{sessions_before} → {_sessions(app)}")

    section("④续：不是每一句「写代码」都算专注意图")
    app.screen_focus_timer.reset()
    pump(0.05)
    flow.say("我今天写代码写得眼睛疼", confidence="none")
    check("陈述状态 → 不开番茄钟", not app.screen_focus_timer.running,
          f"running={app.screen_focus_timer.running}")

    section("④续：已经在专注里，再喊一次不重复起一轮")
    app.screen_focus_timer.reset()
    pump(0.05)
    flow.say("我要专注写代码")
    planned = app.screen_focus_timer.planned
    remaining = app.screen_focus_timer.remaining
    pump(0.05)
    flow.say("我要专注写代码")
    check("还是同一轮（没重置计时）",
          app.screen_focus_timer.planned == planned
          and app.screen_focus_timer.remaining <= remaining,
          f"planned={app.screen_focus_timer.planned} remaining={app.screen_focus_timer.remaining}")

    # ------------------------------------------------- ⑤ S4-④ 多意图只出一个动作

    section("⑤ S4-④ 多意图只出一个动作（不并行输出互斥动作）")
    app.screen_focus_timer.reset()
    pump(0.05)
    before = len(flow.reminders())
    said = flow.say("我要专注写代码，顺便提醒我八点吃药", confidence="fuzzy", time_hint="八点")
    check("开了番茄钟（显式专注意图优先）", app.screen_focus_timer.running,
          f"running={app.screen_focus_timer.running}")
    check("没有同时再问「几点」（不并行动作）",
          not any(ASK_TIME_MARK in t for t in said), str(said))
    check("没有同时再建提醒", len(flow.reminders()) == before, f"{len(flow.reminders())} 条")
    check("状态没被留在等待态", flow.state == "idle", flow.state)

    app.screen_focus_timer.reset()
    pump(0.05)
    before = len(flow.reminders())
    flow.say("提醒我八点吃药，另外今天好累啊", confidence="fuzzy", time_hint="八点")
    check("纯提醒那句照旧进等待态（没被专注那条抢走）",
          flow.state == "awaiting_time", flow.state)
    check("也没顺手开番茄钟", not app.screen_focus_timer.running,
          f"running={app.screen_focus_timer.running}")
    app.chatter_service._reset_to_idle()
    pump(0.05)
    check("提醒没被新建（还在等补答）", len(flow.reminders()) == before,
          f"{len(flow.reminders())} 条")

    box.cleanup()          # 别的模块都收尾，就这个没收——沙箱目录一直留在 %TEMP% 里
    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
