"""
吐嘈闭环的自动测试（打桩模型）。

覆盖：明确时间建提醒 / 模糊时间反问 / 等待态的四个出口 / 长期记忆不再进 pet.db / 提醒到点的静音规则。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.psych.persona_loader import DEFAULT_CHAT_PERSONA
from scheduler.jobs import STATUS_FIRED, STATUS_MUTED, STATUS_PENDING


def hhmm(when: datetime) -> str:
    return when.strftime("%H:%M")


class Flow:
    """驱动一轮或多轮对话，并收集宠物说了什么。"""

    def __init__(self, app, stub):
        self.app, self.stub = app, stub
        self.spoken: list[str] = []
        self.toasts: list[str] = []
        self.memory_changed = 0
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.toast.connect(self.toasts.append)
        app.bus.memory_changed.connect(self._on_memory_changed)

    def _on_memory_changed(self) -> None:
        self.memory_changed += 1

    def say(
        self,
        text: str,
        *,
        confidence: str = "none",
        time_iso: str = "",
        time_hint: str = "",
        reply: str = "记住啦",
        memories: tuple = (),
        command: str = "",
    ) -> list[str]:
        self.spoken.clear()
        self.stub.parse_result = {
            "scene": text[:12],
            "time_hint": time_hint,
            "time_iso": time_iso,
            "time_confidence": confidence,
            "emotion": "平静",
            "module": "其他",
            "command": command,
            "reply": reply,
        }
        self.stub.memories = list(memories)
        self.app.chatter_service.handle(text)
        wait_for(lambda: bool(self.spoken) or bool(self.toasts))
        return list(self.spoken)

    def answer(self, text: str, intent: str, time_iso: str = "") -> list[str]:
        """等待态下补一句话（补答时间 / 取消 / 换话题 / 说不清）。"""
        self.spoken.clear()
        self.stub.followup = {"intent": intent, "time_iso": time_iso}
        # 换话题时这句话会被当成新的吐嘈处理，所以给个中性的解析结果，
        # 免得沿用上一轮的"模糊时间"，又把它带进等待态
        self.stub.parse_result = {
            "scene": text[:12],
            "time_hint": "",
            "time_iso": "",
            "time_confidence": "none",
            "emotion": "平静",
            "module": "其他",
            "command": "",
            "reply": "嗯嗯",
        }
        self.app.chatter_service.handle(text)
        wait_for(lambda: bool(self.spoken) or bool(self.toasts))
        return list(self.spoken)

    @property
    def state(self) -> str:
        return self.app.chatter_service.state.value

    def reminders(self):
        return self.app.reminder_dao.list_all(limit=100)

    def contents(self) -> list[str]:
        return [row.content for row in self.reminders()]


def main() -> int:
    box, stub, app = make_env()
    app.scheduler_engine.start()      # 建提醒要往调度器里登记
    flow = Flow(app, stub)

    # 免打扰时段会影响"提醒到点"的判定，先把测试期间的时段钉死
    app.config.schedule.quiet_hours = ["23:59", "23:59"]   # 永远不在免打扰里

    future = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    later = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")

    section("明确时间 → 建提醒")
    said = flow.say("明天下午三点提醒我开会", confidence="explicit", time_iso=future)
    rows = flow.reminders()
    check("库里多了一条提醒", len(rows) == 1, f"{len(rows)} 条")
    if rows:
        check("提醒状态是待触发", rows[0].status == STATUS_PENDING, rows[0].status)
        check("提醒内容记的是这件事", "开会" in rows[0].content, rows[0].content)
    check("回话里带上了具体时刻", any(future[11:] in text for text in said), str(said))

    section("模糊时间 → 反问，进等待态")
    said = flow.say("下班后提醒我买菜", confidence="fuzzy", time_hint="下班后")
    check("进入了等待补答状态", flow.state == "awaiting_time", flow.state)
    check("反问里带上了原话里的时间说法", any("下班后" in text for text in said), str(said))
    check("这轮不建提醒", len(flow.reminders()) == 1, f"{len(flow.reminders())} 条")

    section("等待态的四个出口")
    flow.answer("那七点吧", "answer_time", later)
    check("补答成功 → 建提醒", len(flow.reminders()) == 2, f"{len(flow.reminders())} 条")
    check("补答成功 → 回到空闲", flow.state == "idle", flow.state)

    flow.say("等下提醒我倒垃圾", confidence="fuzzy", time_hint="等下")
    said = flow.answer("算了", "unclear")     # 规则词优先，模型怎么回都不影响
    check("说算了 → 不建提醒", len(flow.reminders()) == 2, f"{len(flow.reminders())} 条")
    check("说算了 → 回到空闲", flow.state == "idle", flow.state)
    check("说算了 → 明确回一句不提醒了", any("不提醒" in text for text in said), str(said))

    flow.say("待会儿提醒我喝水", confidence="fuzzy", time_hint="待会儿")
    flow.answer("不用麻烦了", "cancel")       # 规则没匹配到，靠模型语义兜底
    check("语义取消 → 回到空闲", flow.state == "idle", flow.state)
    check("语义取消 → 不建提醒", len(flow.reminders()) == 2, f"{len(flow.reminders())} 条")

    flow.say("过会儿提醒我吃药", confidence="fuzzy", time_hint="过会儿")
    flow.answer("今天好冷啊", "new_topic")
    check("换话题 → 回到空闲", flow.state == "idle", flow.state)
    check(
        "换话题 → 那句模糊的话没被建成提醒",
        not any("吃药" in text for text in flow.contents()),
        str(flow.contents()),
    )

    flow.say("晚点提醒我收衣服", confidence="fuzzy", time_hint="晚点")
    flow.answer("再晚点", "unclear")
    check("第一次没听清 → 还在等", flow.state == "awaiting_time", flow.state)
    flow.answer("还是晚点", "unclear")
    check("第二次没听清 → 还在等", flow.state == "awaiting_time", flow.state)
    said = flow.answer("就晚点嘛", "unclear")
    check("第三次没听清 → 放弃等待", flow.state == "idle", flow.state)
    check("放弃时说清楚这次不提醒了", any("先不提醒" in text for text in said), str(said))

    section("长期记忆改走 life/mind 加密库，不再写 pet.db")
    # 原先这一句会问模型抽一遍"值得记住的事"、写进 pet.db 的 memory 表。
    # 本轮把那条链路掐了（情感记忆改走加密库，见 life/mind/），这里钉住新行为：
    # pet.db 不新增，那句话改由情感记忆提炼器攒着，等这轮聊完再统一压进加密库。
    before = len(app.memory_dao.recent(100))
    flow.say(
        "我不吃香菜",
        memories=({"kind": "preference", "content": "不吃香菜", "importance": 0.8},),
    )
    wait_for(lambda: app.mind_distiller is not None and app.mind_distiller.pending > 0, timeout=3.0)
    drain_threads()
    check(
        "pet.db 的 memory 表一句话都没多",
        len(app.memory_dao.recent(100)) == before,
        f"{before} → {len(app.memory_dao.recent(100))}",
    )
    check(
        "这句话进了情感记忆提炼器的队列",
        app.mind_distiller is not None and app.mind_distiller.pending > 0,
        f"pending={getattr(app.mind_distiller, 'pending', 'None')}",
    )

    section("提醒到点的静音规则")
    now = datetime.now()

    due = app.reminder_dao.add(app.user.id, now - timedelta(seconds=1), "该喝水了", None)
    app.jobs.fire(due.id)
    check("正常到点 → 标为已提醒", app.reminder_dao.get(due.id).status == STATUS_FIRED)

    app.jobs.focus_mode = True
    focused = app.reminder_dao.add(app.user.id, now - timedelta(seconds=1), "专注期间的提醒", None)
    app.jobs.fire(focused.id)
    check("专注期间 → 静音", app.reminder_dao.get(focused.id).status == STATUS_MUTED)
    app.jobs.focus_mode = False

    app.config.schedule.quiet_hours = [hhmm(now), hhmm(now + timedelta(minutes=1))]
    quiet = app.reminder_dao.add(app.user.id, now - timedelta(seconds=1), "免打扰期间的提醒", None)
    app.jobs.fire(quiet.id)
    check("免打扰期间 → 静音", app.reminder_dao.get(quiet.id).status == STATUS_MUTED)

    app.config.schedule.quiet_hours = ["23:59", "23:59"]
    app.config.schedule.max_notify_per_hour = 2      # 上面已经触发过 1 条
    first = app.reminder_dao.add(app.user.id, now - timedelta(seconds=1), "第二条", None)
    app.jobs.fire(first.id)
    check("没超上限 → 正常提醒", app.reminder_dao.get(first.id).status == STATUS_FIRED)
    over = app.reminder_dao.add(app.user.id, now - timedelta(seconds=1), "超上限的第三条", None)
    app.jobs.fire(over.id)
    check("超过每小时上限 → 静音", app.reminder_dao.get(over.id).status == STATUS_MUTED)

    section("本体只有一个人格（和谁聊已删，指令 24）")
    # 前面几轮的回话是排到主线程执行的，先把它们派发完，免得混进这一节收集到的 spoken 里
    drain_threads()
    pump(0.4)
    check("本体永远是林医生", app.psych_service.persona_id == DEFAULT_CHAT_PERSONA, app.psych_service.persona_id)
    flow.say("让玄机先生来", command="")
    check("这话不会再被当成换身份", app.psych_service.persona_id == DEFAULT_CHAT_PERSONA, app.psych_service.persona_id)
    check("而是当普通一句话回掉", bool(flow.spoken), str(flow.spoken[:1]))

    drain_threads()
    box.cleanup()
    return summary("吐嘈闭环")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
