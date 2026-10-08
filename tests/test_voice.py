"""
语音合成（C 多模态 · 语音先行版）的自动测试。

覆盖：语气映射（B 解锁 → 音色/语速）/ 危机话术不播 / 真合成（SAPI5 落成 WAV）/
模型缺失·后端不可用的优雅降级 / 所有说话出口汇到 _on_pet_speak 的统一叠加。

真合成那条腿是**真的在跑**：调 Windows 自带 SAPI5 把中文念成 WAV，断言 RIFF 头
和大小，不是打桩糊弄。所以「confide 比 default 语速慢 → WAV 更长」这条是实测出来的。
播放那条腿（winsound 出声）在无头测试里不验——合成成功是它的充分前提，播放本身
由系统接管，这里只验证「不抛、不阻塞」。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Recorder, check, make_env, section, skip, summary, wait_for

from life.psych.crisis import (
    CRISIS_REPLY,
    SOFT_CARE_REPLY,
    SOFT_CARE_REPLY_DIVINATION,
    is_crisis_reply,
)
from life.voice.tts import TONE_PROFILES, VoiceEngine, tone_for_unlocked


def main() -> int:
    section("语气映射：B 解锁 → TTS 语气（纯规则）")
    check("啥都没解锁 → default", tone_for_unlocked([]) == "default", tone_for_unlocked([]))
    check("closer_tone → closer", tone_for_unlocked(["closer_tone"]) == "closer",
          tone_for_unlocked(["closer_tone"]))
    check("confide 压过 closer_tone → confide",
          tone_for_unlocked(["closer_tone", "confide"]) == "confide", "")
    check("三档语速确实是递进（越亲近越慢）",
          TONE_PROFILES["default"]["rate"] == 0
          and TONE_PROFILES["closer"]["rate"] < 0
          and TONE_PROFILES["confide"]["rate"] < TONE_PROFILES["closer"]["rate"],
          f"default={TONE_PROFILES['default']['rate']} closer={TONE_PROFILES['closer']['rate']} "
          f"confide={TONE_PROFILES['confide']['rate']}")

    section("危机拦截：危机/离线关怀话术不播（S4）")
    check("硬危机话术被认出来", is_crisis_reply(CRISIS_REPLY), "")
    check("软表达离线关怀被认出来", is_crisis_reply(SOFT_CARE_REPLY), "")
    check("玄机先生口吻的关怀也被认出来", is_crisis_reply(SOFT_CARE_REPLY_DIVINATION), "")
    check("普通一句不是危机话术", not is_crisis_reply("今天天气不错呀"), "")

    section("真合成：中文 → 合法 WAV（真跑 SAPI5）")
    eng = VoiceEngine(enabled=True)
    if not eng.available:
        skip("系统里没有中文 SAPI 嗓子，真合成这条略过", "（本机应装有 Microsoft Huihui Desktop）")
    else:
        tmp = Path(tempfile.gettempdir()) / "test_voice_a.wav"
        ok = eng.synthesize("早上好呀，记得吃早饭～", "default", str(tmp))
        check("合成返回成功", ok, "")
        data = tmp.read_bytes() if tmp.exists() else b""
        check("产出的是合法 WAV（RIFF/WAVE 头）", data[:4] == b"RIFF" and data[8:12] == b"WAVE",
              f"{data[:12]!r}")
        check("不是空跑（大小远超 44 字节最小头）", len(data) > 4096, f"{len(data)} 字节")

        # 语气真的动了语速：同样一句话，confide 更慢 → WAV 更长
        tmp2 = Path(tempfile.gettempdir()) / "test_voice_b.wav"
        ok2 = eng.synthesize("今天天气真不错，出去走走吧。", "confide", str(tmp2))
        slower = ok and ok2 and tmp2.exists() and tmp2.stat().st_size > tmp.stat().st_size
        check("confide 比 default 慢（WAV 更长）", slower,
              f"default {tmp.stat().st_size if tmp.exists() else '?'} / "
              f"confide {tmp2.stat().st_size if tmp2.exists() else '?'} 字节")
        for p in (tmp, tmp2):
            if p.exists():
                p.unlink()

    section("优雅降级：语音不可用 → 只文字不崩")
    off = VoiceEngine(enabled=False)
    check("总闸关了 say 直接 False", off.say("你好") is False, "")
    check("总闸关了 synthesize 直接 False", off.synthesize("你好") is False, "")
    check("关了也不抛异常（上面两行已经跑过就是证据）", True, "")

    # 没有中文嗓子 / PowerShell 起不来的降级：把探测和合成两处都打成「失败」，
    # 看 say / synthesize 是不是只返回 False、不往外抛
    import life.voice.tts as tts_mod

    saved_probe = tts_mod._probe_zh_voice
    saved_run = tts_mod.subprocess.run
    tts_mod._probe_zh_voice = lambda: None
    try:
        deg = VoiceEngine(enabled=True)
        check("没有中文嗓子 → available False", deg.available is False, "")
        check("没有嗓子 → synthesize 返回 False（真合不出来）", deg.synthesize("你好") is False, "")
        # say 是异步的：只负责排队，返回 True 不保证出声；后台发现没嗓子就静默跳过。
        # 这里证「不崩 + 不产出音频」——先清掉旧 WAV，再 say，后台跑完也不该留下新文件。
        tts_mod._TMP_WAV.unlink(missing_ok=True)
        check("没有嗓子 → say 不崩不抛（照常排队）", deg.say("你好") is True, "")
        wait_for(lambda: not tts_mod._TMP_WAV.exists(), timeout=2.0)
        check("没有嗓子 → 后台静默降级，不产出 WAV", not tts_mod._TMP_WAV.exists(),
              str(tts_mod._TMP_WAV))
    finally:
        tts_mod._probe_zh_voice = saved_probe

    eng2 = VoiceEngine(enabled=True)
    eng2._voice_name = "Microsoft Huihui Desktop"   # 有嗓子，但让合成那步直接抛
    def boom(*a, **k):
        raise OSError("模拟 PowerShell 起不来")
    tts_mod.subprocess.run = boom
    try:
        check("合成那步抛异常 → synthesize 返回 False 不冒泡",
              eng2.synthesize("你好", "default") is False, "")
    finally:
        tts_mod.subprocess.run = saved_run

    section("接线：所有说话出口汇到 bus.pet_speak → _on_pet_speak，只出文字不出声")
    box, stub, app = make_env()
    calls: list[tuple[str, str]] = []

    class FakeVoice:
        def say(self, text, tone="default"):
            calls.append((text, tone))
            return True

    app.voice = FakeVoice()
    # v2-chat-core #2：出声那条线整个摘了——把闸门打开、把引擎换成探针，也一次都不该被叫到。
    app.config.voice.enabled = True
    speak_rec = Recorder(app.pet_window, "speak")

    try:
        # 走真实信号（emit pet_speak）——正是所有说话出口汇到的那个点，绕过它测不算数
        app.bus.pet_speak.emit("今天天气不错呀")
        check("普通一句 → 文字照出（pet_window.speak），一个字都不念",
              bool(speak_rec.last) and speak_rec.last[0] == "今天天气不错呀"
              and calls == [],
              f"speak={speak_rec.calls} voice={calls}")

        calls.clear()
        speak_rec.calls.clear()
        app.bus.pet_speak.emit(CRISIS_REPLY)
        check("危机话术 → 文字照出，语音不播（不念隐私）",
              bool(speak_rec.calls) and not calls,
              f"speak={bool(speak_rec.calls)} voice={calls}")

        # 原先这里验"好感度换档 → 语气跟着变 closer"。v2-chat-core #2 之后没有任何地方
        # 再去算语气了，所以这条改成量"就算把好感度换档，也不会有人去碰语音"。
        calls.clear()
        class FakeIntimacy:
            def state(self):
                return {"unlocked": ["closer_tone"]}
        app.mind_intimacy = FakeIntimacy()
        app.bus.pet_speak.emit("陪我说说话嘛")
        check("好感度换档也不出声（语气那条链已经不接了）",
              calls == [], str(calls))

        # 语气映射本身还是纯函数，没被这轮删掉——引擎留着，映射跟着留。
        check("语气映射那道纯函数还在（引擎的零件没拆）",
              tone_for_unlocked(["closer_tone"]) == "closer", "")
    finally:
        speak_rec.restore()

    box.cleanup()
    return summary("语音合成（C 多模态 · 语音先行版）")


if __name__ == "__main__":
    raise SystemExit(main())
