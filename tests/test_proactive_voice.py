"""
主动说话需确认（v2-proactive-voice）→ **v2-chat-core #2：一个字都不念**。

这份文件原先锁的是"宠物自己开口时先问一句「要我念出来吗」，点了才出声"（v2-proactive-voice
+ v2-speech-polish ①）。v2-chat-core 的用户指令 #2 把整条确认链整个撤了——**连那个询问
按钮一起**：任何文字回复都只显示，不再出声。所以这份文件跟着改口径，从"问了才念"变成
"**哪条说话线都不出声**"：原来那些"点了才播 1 次 / 点不用 0 次"的断言，现在统一塌成
"不管有没有可点的地方，一律 0 次"。

留下来的那半边仍然有效，也仍然值得盯——**它跟出不出声无关**，是结构事实：

1. **两个口子还在**：宠物**自己**开的口走 `bus.pet_speak_proactive`，用户发消息引出的
   回话走 `bus.pet_speak`。改道的名单是冻结的（v2-proactive-voice 当初挪了 `life/proactive.py`
   那三处，v2-speech-polish 又把"首启问候、久坐/分心提醒、屏幕陪伴、番茄钟到点、社交邀请、
   卡片自动归档"并了过来），多一处少一处都得先改下面那条断言——逼着人先想清楚"这条到底
   是谁开的口"。将来要按口子做分级、限流、静音，还得靠这道分界。
2. **一个字都不念**：主动话、回话、危机定稿话术、电影场景里的话，无论语音总闸开着还是
   关着、无论 `confirm_proactive` 取什么值，`voice.say` 都是 0 次。
3. **界面上没有可点的出声入口**：气泡里没有确认行，聊天框里没有那排轻按钮——不是
   "点了没反应"，是那些东西整块不在。
4. **不破既有红线**：pet.db / mind.db 零写入、语音引擎本体还在（只是不接线）、没新增依赖。
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Recorder, check, drain_threads, make_env, pump, section, summary

from life.psych.crisis import CRISIS_REPLY
from life.screen.scenes import SCENE_HORROR

ROOT = Path(__file__).resolve().parents[1]

LINE = "我刚想起你上次说的那件事了"
REPLY_LINE = "刚给你查好了"

# 宠物**自己**开口、因此走 pet_speak_proactive 的全部发点所在文件（v2-speech-polish 收口后的现状）。
EXPECTED_MIGRATED = sorted([
    "focus/pomodoro.py", "life/care.py", "life/greeting.py", "life/playmate.py",
    "life/screen/companion.py", "life/screen/focus_timer.py", "life/screen/social.py",
    "monitor/service.py", "life/cards/service.py",
])
# 回用户的话（用户刚发了消息 / 用户刚点了按钮）所在文件，必须继续留在 pet_speak 上。
EXPECTED_STILL_REPLY = sorted([
    "core/bootstrap.py", "focus/pomodoro.py", "life/cards/service.py",
    "life/screen/game.py", "life/screen/social.py",
])

# 出声那条线可能被接的地方（生产代码全量）。tts.py 自己是定义处，不在名单里。
PRODUCTION_DIRS = ("core", "ui", "life", "monitor", "pet", "brain", "memory")


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _fresh(app) -> None:
    """每个断言前把气泡清干净，免得上一句的残留混进这一句。"""
    app.pet_window._bubble.hide_bubble()
    app.pet_window._speech_until = 0.0
    pump(0.02)


def main() -> int:
    box, _stub, app = make_env()
    try:
        bubble = app.pet_window._bubble
        # 沙箱默认把语音总闸关了（免得测试真出声）。这一轮验的是"一个字都不念"，
        # 所以**故意把闸门打开**——开着都不念，才说明摘的是接线而不是靠总闸挡着。
        app.config.voice.enabled = True
        spoken = Recorder(app.voice, "say")

        # ---------- S1 出口：两个口子还在（跟出不出声无关的结构事实） ----------

        section("S1 出口：主动那条线跟回话那条线是两个口子")

        proactive_src = _src("life/proactive.py")
        check("主动搭话的三个发点全部在 pet_speak_proactive 上",
              proactive_src.count("pet_speak_proactive.emit") == 3
              and "pet_speak.emit" not in proactive_src,
              f"proactive={proactive_src.count('pet_speak_proactive.emit')} "
              f"old={proactive_src.count('pet_speak.emit') - proactive_src.count('pet_speak_proactive.emit')}")

        migrated = sorted(
            rel for rel in (
                "focus/pomodoro.py", "life/care.py", "life/greeting.py", "life/playmate.py",
                "life/screen/companion.py", "life/screen/focus_timer.py", "life/screen/social.py",
                "monitor/service.py", "life/cards/service.py",
            )
            if "pet_speak_proactive.emit" in _src(rel)
        )
        check("自己开口的改道名单冻结在这九处（多一处少一处先改这条断言）",
              migrated == EXPECTED_MIGRATED, str(migrated))

        still_reply = sorted(
            rel for rel in (
                "core/bootstrap.py", "focus/pomodoro.py", "life/cards/service.py",
                "life/screen/game.py", "life/screen/social.py",
            )
            if "pet_speak.emit" in _src(rel)
        )
        check("回用户的话还留在 pet_speak 上（没被顺手挪走）",
              still_reply == EXPECTED_STILL_REPLY, str(still_reply))

        boot_src = _src("core/bootstrap.py")
        check("新口子在装配层只接了一次", boot_src.count("bus.pet_speak_proactive.connect(") == 1,
              str(boot_src.count("bus.pet_speak_proactive.connect(")))
        check("回话那个口子也还在（没被换掉）", boot_src.count("bus.pet_speak.connect(") == 1,
              str(boot_src.count("bus.pet_speak.connect(")))

        # ---------- S2 硬口径：一个字都不念 ----------

        section("S2 硬口径：主动话只显示，一个字都不念")

        _fresh(app)
        spoken.calls.clear()
        app.bus.pet_speak_proactive.emit(LINE)
        pump(0.05)
        check("主动话没经过 voice.say（0 次）", len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
        check("文字照旧冒出来了", bubble.isVisible() and bubble._label.text() == LINE,
              repr(bubble._label.text()))
        check("气泡里没有可点的确认行（整块不在，不是点了没反应）",
              not hasattr(bubble, "_ask_row") and not hasattr(bubble, "_voice_row"),
              f"ask_row={hasattr(bubble, '_ask_row')} voice_row={hasattr(bubble, '_voice_row')}")

        section("S2 回话也是一样：不多念一个字")

        _fresh(app)
        spoken.calls.clear()
        app.bus.pet_speak.emit(REPLY_LINE)
        pump(0.05)
        check("回话也没经过 voice.say（0 次）", len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
        check("文字照旧", bubble.isVisible() and bubble._label.text() == REPLY_LINE,
              repr(bubble._label.text()))

        section("S2 红线：危机定稿话术两条线上都不念")

        for where, emit in (("主动线", app.bus.pet_speak_proactive.emit),
                            ("回话线", app.bus.pet_speak.emit)):
            _fresh(app)
            spoken.calls.clear()
            emit(CRISIS_REPLY)
            pump(0.05)
            check(f"危机话术（{where}）0 次", len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
            check(f"危机话术（{where}）文字照旧显示", bubble.isVisible(), "")

        section("S2 屏幕上正有戏在演（⑥）→ 也一样不念")

        _fresh(app)
        spoken.calls.clear()
        app.screen_monitor._scene = SCENE_HORROR
        try:
            app.bus.pet_speak_proactive.emit(LINE)
            pump(0.05)
            check("电影演着不念", len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
            check("文字/表情照旧", bubble.isVisible() and bubble._label.text() == LINE,
                  repr(bubble._label.text()))
        finally:
            app.screen_monitor._scene = ""

        section("S2 两个开关现在都不参与判断（开着关着都是 0 次）")

        for enabled in (False, True):
            _fresh(app)
            app.config.voice.enabled = enabled
            spoken.calls.clear()
            app.bus.pet_speak_proactive.emit(LINE)
            pump(0.05)
            check(f"语音总闸 enabled={enabled} → 0 次", len(spoken.calls) == 0,
                  f"{len(spoken.calls)} 次")
        app.config.voice.enabled = True

        # confirm_proactive 是"问了才念"那套的开关；那套整个撤了，这个字段现在
        # 只剩配置兼容（改它不影响任何行为）。用真值量，不看源码。
        for flag in (False, True):
            _fresh(app)
            app.config.voice.confirm_proactive = flag
            spoken.calls.clear()
            app.bus.pet_speak_proactive.emit(LINE)
            pump(0.05)
            check(f"confirm_proactive={flag} → 0 次（这个开关已经管不着任何事）",
                  len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
        app.config.voice.confirm_proactive = True

        check("装配层不再读 confirm_proactive（问了才念那套没留半根线）",
              "confirm_proactive" not in boot_src, "")

        # ---------- S3 源码级：生产代码里没有出声的调用点 ----------

        section("S3 源码级：全项目没有一处 voice.say( 的调用")

        # 走 ast 而不是 grep：`.say(` 在 docstring 与注释里也出现过（记的是"原先这行
        # 是无条件 voice.say(...)"），按行匹配会把这些说明文字当成调用点。
        call_sites: list[str] = []
        for folder in PRODUCTION_DIRS:
            for path in (ROOT / folder).rglob("*.py"):
                if path.name == "tts.py":
                    continue                    # 定义处，不算调用点
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "say"):
                        call_sites.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        check("一个调用点都没有（摘的是接线，不是把引擎删了）",
              call_sites == [], "、".join(call_sites) or "（一处都没有）")
        check("引擎本体还在（将来要接回来，入口是 VoiceEngine.say）",
              hasattr(app.voice, "say") and hasattr(app.voice, "synthesize")
              and "def say(" in _src("life/voice/tts.py"), "")

        # ---------- S4 红线 ----------

        section("S4 红线：零写入、没碰模型名、没新依赖")

        _fresh(app)
        before = _rows(app.config.db_path, "utterance")
        fragments = app.mind_store.fragment_count()
        app.bus.pet_speak_proactive.emit(LINE)
        pump(0.05)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == before,
              f"{before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == fragments, "")

        check("界面层没反向 import 语音引擎（气泡只管显示）",
              "life.voice" not in _src("ui/bubble.py")
              and "life.voice" not in _src("ui/chat_bubble.py"), "")

        req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        check("requirements 没新增语音依赖",
              not any(pkg in req for pkg in ("pyttsx3", "sounddevice", "pyaudio",
                                             "soundfile", "whisper")), "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("主动说话：一个字都不念（v2-chat-core #2 之后的回归锁）")


if __name__ == "__main__":
    raise SystemExit(main())
