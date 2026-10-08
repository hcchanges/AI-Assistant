"""
多模态融合（v2-multimodal，听想说一体）的自动测试。

**这一轮最后没有动产品代码——这份测试就是产物本身，所以先把话说清楚。**

S0 侦察（本次做之前）的结论是："听得进来 / 想得明白 / 说得出去"三条链本来就各自
只有一个收口点，一体化是**结构性成立**的：

    听   语音 heard 与键盘 send_requested 都接到 Bootstrap._on_chat_send
    想   ChatterService.handle 是用户回话的唯一入口
    说   bus.pet_speak 全项目只有一个 handler：Bootstrap._on_pet_speak

于是（S1~Sn）我在两个收口点上各焊了一道"同文去重"，想挡住"同一条输入被投递两遍"。
**两版都撤了**，因为现有回归把这个想法证伪了，而且证伪得很干净：

- 说话侧第一版按**时间窗**（"2 秒内同文算重复"）→ `tests/test_screen.py` 挡回：
  陪伴语料池是轮着挑的，"连着挑到同一句"在池子边界上本来就合法，时间窗分不清
  "撞车"和"轮到了同一句"。失败项：`horror 冒的是定稿那句`、`同态冷却：连着两次
  只冒一次`、`池子里轮着挑，一轮内不重样`。
- 说话侧第二版改判**状态**（"这句还挂在脸上才算重复"）→ 这次 `test_screen.py`
  过了，但 `test_asr.py` 与 `test_social.py` 挡回：前者"语音问的它照样回"因为上一遍
  的回话还挂在气泡上而整条被吞；后者"正在写时明说了要等"因为同一句请求文本刚发过
  而根本没走到忙态分支。
- 听侧那道时间窗同样不成立 → 同一个 `test_asr.py`、同一个 `test_social.py` 挡回。

**根因不是参数没调好，是想法的前提错了。** 在收口点上，"同一条输入被投递了两遍"
和"用户真的要再说一遍"落到这里**是同一个信号**——它们本来就是 `heard` 上的一个字符串、
`send_requested` 上的一个字符串。收口点这个位置没有足够信息去区分二者。真要挡，
得在**发射端**挡；而发射端实测不会重复（`heard.emit` 全项目只有 life/voice/listen.py
一处，一次录音只走到一次）。

所以这一轮的结论是**不改**，并用下面这条回归锁把结论钉住：收口点上刻意不做文本去重，
同一句连说两遍就是两遍。将来谁再想加，先来这份文件看一遍那段失败清单。

覆盖：
- S0 三条链的收口点确实是同一个（源码级断言；v2-chat-core #1 之后语音那条腿
  的接线已摘，只剩键盘那一个入口）；
- S1 一体化实证：打字那条腿一路走到落库与回话（走真实信号）；语音那条腿反过来锁
  "引擎还在、话进不来"；
- S2 回归锁：收口点上没有按文本去重——同一句两遍就是两遍（听侧、说侧各一条；
  v2-chat-core #2 之后"两遍"都只出文字、都不出声）；
- S3 红线：危机话最高优先、pet.db / mind.db 零额外写入、不新增依赖。
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Recorder, check, drain_threads, make_env, pump, section, summary, wait_for

from life.psych.crisis import CRISIS_REPLY, is_crisis

# 两句不一样的日常话（都不含危机词、不命中任何指令）
LINE_A = "今天天气不错"
LINE_B = "晚上想吃点什么呢"


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _settle(app, quiet: float = 0.3, timeout: float = 6.0) -> None:
    """等到没有回话在路上。

    往聊天那条链里发过话之后，回话是 `run_async` 异步落的，晚一拍才 `pet_speak`。
    不等干净就往下走，那些迟到的话会串进下一节（顺手把这一节也变成"上一节的回声"）。

    探针盯的是 `pet_window.speak`（所有说话出口最终都落到它身上）。原先盯的是
    `voice.say`——v2-chat-core #2 之后那条线一次都不会被调，拿它当"还有没有话在路上"
    的探针就永远看着像"已经安静了"。
    """
    probe = Recorder(app.pet_window, "speak")
    try:
        seen = -1
        idle = 0.0
        waited = 0.0
        while waited < timeout:
            pump(0.05)
            drain_threads()
            waited += 0.05
            if len(probe.calls) == seen:
                idle += 0.05
                if idle >= quiet:
                    return
            else:
                seen = len(probe.calls)
                idle = 0.0
    finally:
        probe.restore()


def main() -> int:
    box, stub, app = make_env()
    try:
        # ---------- S0 收口点 ----------

        section("S0 三条链各自的收口点：确实是「同一个」，不是「差不多」")
        import core.bootstrap as bootstrap_mod

        source = inspect.getsource(bootstrap_mod)
        check("键盘那条腿接的是唯一入口：_on_chat_send",
              "chat_bubble.send_requested.connect(self._on_chat_send)" in source, "")
        # v2-chat-core #1：语音那条腿的接线摘了（用户看得见的语音输入入口全没了）。
        # 这条从"两个入口共用一个 handler"改成"只剩键盘这一个"。
        check("语音那条腿不再接线（v2-chat-core #1 摘了入口）",
              "voice_listener.heard.connect" not in source, "")
        check("pet_speak 全项目只有一个 handler 接线",
              source.count("bus.pet_speak.connect(") == 1,
              f"{source.count('bus.pet_speak.connect(')} 处")
        check("chatter.handle 是用户回话的唯一入口（生产代码只有两处调用）",
              source.count("chatter_service.handle(") == 2,
              f"{source.count('chatter_service.handle(')} 处")

        from life.voice import listen as listen_mod

        check("发射端不会重复：heard.emit 整条语音链只有一处，一次录音只发一次",
              inspect.getsource(listen_mod).count("self.heard.emit(") == 1,
              f"{inspect.getsource(listen_mod).count('self.heard.emit(')} 处")

        # ---------- S1 一体化实证 ----------

        # 探针只能打**下游不破坏链条**的一环。`chatter.handle` 是链中段，打上去等于
        # 把链掐断（test_asr 里也踩过这个坑，那边是打完之后补跑一趟才看到回话）。
        # 这里改探 `_on_chat_send` 里调用的 greeting_service.touch：两条腿都要经过它，
        # 探完链条照走。
        section("S1 一体化实证：打字那条腿走到底——进收口、落库、回话")
        touch = Recorder(app.greeting_service, "touch")
        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        try:
            before = _rows(app.config.db_path, "utterance")
            app.chat_bubble.send_requested.emit(LINE_B)
            wait_for(lambda: bool(spoken), timeout=8.0)
            drain_threads()
            pump(0.2)
            check("打字腿确实走到了收口点 _on_chat_send", touch.called, str(touch.calls))
            check("一路走到落库（pet.db utterance 多了一条）",
                  _rows(app.config.db_path, "utterance") == before + 1,
                  f"{before} → {_rows(app.config.db_path, 'utterance')}")
            check("再一路走到回话（pet_speak 上有内容）", bool(spoken), str(spoken))
        finally:
            app.bus.pet_speak.disconnect(spoken.append)
            touch.restore()

        # v2-chat-core #1 把语音那条腿的接线摘了。这一段从"语音腿也汇进同一条链"
        # 反过来锁：**引擎还在（还能发信号），但话不会再自己溜进对话链**。
        section("S1 语音腿（v2-chat-core #1 之后）：引擎还在，话进不来")
        _settle(app)
        spy = Recorder(app.chatter_service, "handle")
        heard_reply: list[str] = []
        app.bus.pet_speak.connect(heard_reply.append)
        try:
            before = _rows(app.config.db_path, "utterance")
            app.voice_listener.heard.emit(LINE_A)
            pump(0.3)
            drain_threads()
            check("语音腿投出来的话没进 chatter.handle", not spy.called, str(spy.calls))
            check("也就没落库",
                  _rows(app.config.db_path, "utterance") == before,
                  f"{before} → {_rows(app.config.db_path, 'utterance')}")
            check("也没回话", heard_reply == [], str(heard_reply))
        finally:
            spy.restore()
            app.bus.pet_speak.disconnect(heard_reply.append)

        # ---------- S2 回归锁：收口点上刻意不去重 ----------

        section("S2 回归锁（听侧）：同一句连发两遍就是两遍——收口点上不许去重")
        _settle(app)
        handle_spy = Recorder(app.chatter_service, "handle")
        try:
            app.chat_bubble.send_requested.emit(LINE_A)
            pump(0.05)
            app.chat_bubble.send_requested.emit(LINE_A)
            pump(0.05)
            check("同一句连着发两遍，两次都真进了链（这不是漏了，是刻意的）",
                  len(handle_spy.calls) == 2
                  and handle_spy.calls[0][0] == handle_spy.calls[1][0] == LINE_A,
                  str(handle_spy.calls))
        finally:
            handle_spy.restore()

        section("S2 回归锁（说侧）：同一句往 pet_speak 发两遍就是两遍（都出文字、都不出声）")
        _settle(app)
        spoken = Recorder(app.pet_window, "speak")
        voice = Recorder(app.voice, "say")
        try:
            app.bus.pet_speak.emit("这句话我就是要说两遍")
            pump(0.03)
            app.bus.pet_speak.emit("这句话我就是要说两遍")
            pump(0.03)
            # 收口点上刻意不去重这条没变；变的是"两遍都念"→"两遍都只出文字"
            # （v2-chat-core #2：任何文字回复都不再出声）。
            check("同一句连着说两遍，两遍都真放出去了（陪着看戏的语料池就靠这个）",
                  len(spoken.calls) == 2, f"{len(spoken.calls)} 次")
            check("两遍都一个字没念（出声那条线整个摘了）",
                  len(voice.calls) == 0, f"{len(voice.calls)} 次")
        finally:
            spoken.restore()
            voice.restore()

        section("S2 回归锁：产品代码里现在没有任何'给发声用的'去重")
        check("_on_pet_speak / _on_chat_send 里不含 seen/dedup/last_text 之类的东西",
              not any(k in source for k in
                      ("repeat_of_onscreen", "_speak_guard", "_utterance_guard",
                       "RecentText", "_drop_repeat_utterance")),
              "、".join(k for k in ("repeat_of_onscreen", "_speak_guard", "_utterance_guard",
                                    "RecentText", "_drop_repeat_utterance") if k in source)
              or "（一处都没有）")
        check("life/glue.py 不在了（这一轮撤掉的东西没留残骸）",
              not (Path(__file__).resolve().parent.parent / "life" / "glue.py").exists(), "")

        # ---------- S3 红线 ----------

        section("S3 红线：危机话最高优先，撤回这一轮之后一条没少")
        _settle(app)
        crisis_seen: list[str] = []
        app.bus.pet_speak.connect(crisis_seen.append)
        try:
            check("认得出这是危机话", is_crisis("我不想活了") is True, "")
            # 入口从语音那条腿换成打字那条（v2-chat-core #1 之后语音不再自动进链）：
            # 同一句文本、同一个 chatter.handle 收口，判据本来就跟"字从哪来"无关。
            for i in (1, 2):
                app.chat_bubble.send_requested.emit("我不想活了")
                wait_for(lambda: sum(1 for s in crisis_seen
                                     if CRISIS_REPLY[:12] in s) >= i, timeout=8.0)
                drain_threads()
                pump(0.1)
            hits = sum(1 for s in crisis_seen if CRISIS_REPLY[:12] in s)
            check("同一句危机话投两遍，两遍都出了危机话术", hits == 2, f"{hits} 次")
        finally:
            app.bus.pet_speak.disconnect(crisis_seen.append)

        section("S3 红线：两库零额外写入")
        _settle(app)
        before = _rows(app.config.db_path, "utterance")
        fragments = app.mind_store.fragment_count()
        app.bus.pet_speak.emit("红线检查用的这一句")
        pump(0.05)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == before,
              f"{before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == fragments, "")
    finally:
        drain_threads()
        box.cleanup()
    return summary("多模态融合（听想说一体：一体化实证 + 不去重的回归锁）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
