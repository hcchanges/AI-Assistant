"""
意图 + 情绪路由骨架（C 多模态）的自动测试。

覆盖：
- S1 classify：五意图五情绪的纯规则分类（含优先级与兜底）；
- S2 mood：情绪 → 语气修饰（负向软化、开心欢快），意图 → 回应分寸；
- S3 接线：危机词最高优先短路（不路由、不调模型），路由提示真注入回话（倾诉不硬给方案）；
- S4 接线：会话情绪经 on_emotion 报到关怀服务，低落/焦虑/愤怒 → 关怀由头；
- 红线：pet.db / mind.db 零写入、路由与语气不调模型。

路由与语气是纯规则（只读词表），重点验"接线真的接上了、危机真的压在最上面"。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.dialog import mood
from life.dialog.router import (
    EMOTION_ANGRY,
    EMOTION_ANXIOUS,
    EMOTION_CALM,
    EMOTION_HAPPY,
    EMOTION_LOW,
    INTENT_CHAT,
    INTENT_COMMAND,
    INTENT_CONFIRM,
    INTENT_HELP,
    INTENT_VENT,
    classify,
)


def _parse_result(text: str, intent: str = "", emotion: str = "平静") -> dict:
    """一句普通话的桩解析结果（time_confidence=none → 走正常回话那支）。

    ``intent`` / ``emotion`` 是**模型这一趟会怎么答**：A+++ 之后解析链的结论会回过头
    校正规则路由（``router.refine``，越界回落规则值），所以桩要是自相矛盾——嘴上说
    "这句很平静"，断言却等一个"低落"——那是桩写错了，不是被验的那条线错了。
    默认「平静」只适用于真的没什么情绪的那类句子。
    """
    return {
        "scene": text[:12],
        "time_hint": "",
        "time_iso": "",
        "time_confidence": "none",
        "emotion": emotion,
        "module": "其他",
        "command": "",
        "intent": intent,
        "reply": "嗯嗯",
    }


def main() -> int:
    section("S1 路由：classify 五意图五情绪")
    cases = [
        ("今天好累好崩溃", INTENT_VENT, EMOTION_LOW),
        ("怎么缓解焦虑", INTENT_HELP, EMOTION_ANXIOUS),
        ("哈哈天气真好", INTENT_CHAT, EMOTION_HAPPY),
        ("关掉宠物", INTENT_COMMAND, EMOTION_CALM),
        ("嗯好的", INTENT_CONFIRM, EMOTION_CALM),
        ("好烦，怎么办", INTENT_VENT, EMOTION_LOW),
    ]
    for text, intent, emotion in cases:
        r = classify(text)
        check(
            f"{text!r} → {intent}+{emotion}",
            r.intent == intent and r.emotion == emotion,
            f"{r.intent}/{r.emotion}",
        )

    r = classify("")
    check(
        "空串 → 确认+平静（兜底）",
        r.intent == INTENT_CONFIRM and r.emotion == EMOTION_CALM and not r.matched,
        f"{r.intent}/{r.emotion}/matched={r.matched}",
    )
    check(
        "倾诉优先于求助（好烦压过怎么办）",
        classify("好烦，怎么办").intent == INTENT_VENT,
        classify("好烦，怎么办").intent,
    )
    check(
        "「焦虑」点题不泄进倾诉（要办法的是求助）",
        classify("怎么缓解焦虑").intent == INTENT_HELP,
        classify("怎么缓解焦虑").intent,
    )

    section("S2 情绪 → 语气修饰（负向软化、开心欢快）")
    check("低落 → 软化", "放软" in mood.tone_for_emotion(EMOTION_LOW), mood.tone_for_emotion(EMOTION_LOW))
    check("焦虑 → 安心", "安心" in mood.tone_for_emotion(EMOTION_ANXIOUS), mood.tone_for_emotion(EMOTION_ANXIOUS))
    check("愤怒 → 克制", "克制" in mood.tone_for_emotion(EMOTION_ANGRY), mood.tone_for_emotion(EMOTION_ANGRY))
    check("开心 → 欢快", "轻快" in mood.tone_for_emotion(EMOTION_HAPPY), mood.tone_for_emotion(EMOTION_HAPPY))
    check("平静 → 不加戏", mood.tone_for_emotion(EMOTION_CALM) == "", repr(mood.tone_for_emotion(EMOTION_CALM)))

    check(
        "倾诉提示：先接情绪、别急着给方案",
        "别急着给方案" in mood.hint_for_route(classify("今天好累好崩溃")),
        mood.hint_for_route(classify("今天好累好崩溃")),
    )
    check(
        "求助提示：给具体可行建议",
        "具体" in mood.hint_for_route(classify("怎么缓解焦虑")),
        mood.hint_for_route(classify("怎么缓解焦虑")),
    )
    check(
        "确认提示：一句话轻轻回掉",
        "轻轻回掉" in mood.hint_for_route(classify("嗯好的")),
        mood.hint_for_route(classify("嗯好的")),
    )
    check("None 路由 → 空提示（危机/未接路由）", mood.hint_for_route(None) == "", repr(mood.hint_for_route(None)))

    box, stub, app = make_env()

    section("S3 危机词最高优先：不路由、不调模型")
    spoken: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    p0 = len(stub.prompts)
    m0 = len(stub.messages)
    app.chatter_service.handle("我不想活了")
    wait_for(lambda: bool(spoken))
    drain_threads()
    check("危机话术（含热线 12356）", any("12356" in s for s in spoken), (spoken[0][:40] + "…") if spoken else "（没说话）")
    check("危机不跑解析链", len(stub.prompts) == p0, f"{p0} → {len(stub.prompts)}")
    check("危机不调模型拼消息", len(stub.messages) == m0, f"{m0} → {len(stub.messages)}")

    # 对照组：普通一句话确实会跑解析——证明上面那个"不跑"不是探测器坏了
    spoken.clear()
    app.chatter_service.handle("嗯好的")
    wait_for(lambda: bool(spoken))
    drain_threads()
    check("对照组：普通话会跑解析（探测器有效）", len(stub.prompts) > p0, f"{p0} → {len(stub.prompts)}")

    section("S3 路由提示注入回话（倾诉不被硬给方案）")
    spoken.clear()
    # 桩按"模型真会怎么答"给：这句它该判倾诉 + 疲惫。疲惫是解析链那套自由词，
    # 经 router 的别名表归到「低落」——顺带把这条翻译路也验了。
    stub.parse_result = _parse_result("今天好累好崩溃", intent=INTENT_VENT, emotion="疲惫")
    app.chatter_service.handle("今天好累好崩溃")
    wait_for(lambda: bool(spoken))
    drain_threads()
    pump(0.1)
    fed = " ".join(str(m) for m in stub.messages[-1]) if stub.messages else ""
    check("回话 messages 里带上了路由提示", "意图=倾诉" in fed, fed[:120] + "…" if fed else "（没拼消息）")
    check("倾诉提示：别急着给方案", "别急着给方案" in fed, "")
    check("情绪语气：放软", "放软" in fed, "")
    if app.mind_proactive is not None:
        check(
            "会话情绪经 on_emotion 报到关怀服务",
            app.mind_proactive._live_emotion == EMOTION_LOW,
            app.mind_proactive._live_emotion,
        )

    section("S4 情绪驱动关怀由头")
    if app.mind_proactive is not None:
        app.mind_proactive.note_emotion(EMOTION_LOW)
        topic, line = app.mind_proactive._emotion_care()
        check("低落 → 关怀由头", topic == "live:emotion" and "低落" in line, f"{topic}: {line}")
        app.mind_proactive.note_emotion(EMOTION_HAPPY)
        topic, line = app.mind_proactive._emotion_care()
        check("开心 → 不触发负向关怀", topic != "live:emotion", f"{topic or '（空）'}")
        app.mind_proactive.note_emotion("")
        check("清空 → 回到快照路径（无快照即空）", app.mind_proactive._emotion_care() == ("", ""), "")
    else:
        check("关怀服务已装配（mind 开启）", False, "app.mind_proactive is None")

    drain_threads()
    box.cleanup()
    return summary("意图+情绪路由")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
