"""
真机模块②四项反馈（v2-speech-polish）的自动测试。

四条反馈各一节，外加一节红线：

① **打开宠物不自动出声**：开机问候那条线原来发 `pet_speak`，那是"回用户话"的口子，
   直接出声，于是每次打开宠物它自己先念上了。这一节锁的就是"打开不自动说话"。
   **v2-chat-core #2 之后口径收得更死**：任何文字回复都不再出声，所以本节里的
   "要我念出来吗"确认链断言已经换成"一个字都不念"（见下面 ① 的正文）。
② **语音输入入口（已删）**：v2-speech-polish ② 当初给两条菜单各加了一条「语音输入」
   与全局快捷键；**v2-chat-core #1 把用户看得见的入口全摘了**（聊天框底部那个
   「按住说话」按钮也一起），聊天输入区只剩文字框 + 发送。所以本节从"入口看得见"
   反过来锁成"入口已摘、引擎还在"。
③ **回复不机械**：闲聊（指令/闲聊/确认，且情绪分低于阈值）改走 `life/psych/casual.py`
   那条轻路，不套林医生卡的「共情 → 正常化 → 探索 → 赋能」；倾诉/求助/高情绪/危机
   一律照旧走专业框架。
④ **看电影陪伴字幕**：认出在看片先发一句开场陪伴字幕（只显示、不出声、停 6 秒）；
   开场就认出剧情的，跳过开场字幕直接出对应表情反应。
⑤ 红线：⑥看电影静音、主动确认、pet.db 零写入、零新依赖都不回退。

第④条那几项只驱动 ScreenCompanion 自己的状态机（跟前台信号同一条入口），
不去跑真的截屏 + OCR——那条链在 test_screen.py 里已经独门验过。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Recorder, check, drain_threads, make_env, pump, section, summary, wait_for

# PySide6 必须排在 harness 之后：harness 头一行就把 QT_QPA_PLATFORM 定成 offscreen，
# 早于它导入 Qt 会真去连显示器（沙箱里连不上就炸）。
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QHBoxLayout, QLabel, QStyleOptionViewItem

from core.bootstrap import SCREEN_CAPTION_SHOW_MS
from life.dialog.router import (
    INTENT_CHAT,
    INTENT_COMMAND,
    INTENT_CONFIRM,
    INTENT_HELP,
    INTENT_VENT,
    DialogRoute,
)
from life.psych import casual
from life.psych.persona_loader import DEFAULT_CHAT_PERSONA
from life.psych.service import PsychService
from life.screen.browser import TITLE_WATCH
from life.screen.companion import OPENING_LINE, SCENE_LINES
from life.screen.monitor import SCENE_HORROR
import life.screen.monitor as monitor_mod
from life.voice.tts import VoiceEngine
from pet.persona import split_kaomoji, strip_action_marks, strip_kaomoji
from ui.bubble import SHOW_MS
from ui.chat_bubble import BUBBLE_PAD_X, BUBBLE_PAD_Y, SPEAKER_PET, SPEAKER_USER
from ui.theme import TEXT, TEXT_MUTED
import ui.pet_window as pet_window_mod

ROOT = Path(__file__).resolve().parents[1]

# 用户真机反馈里那条职能卡式的例句不在这儿——①验的是"能读出来的话"，
# 所以用一句带颜文字的问候原文，颜文字挑的是 greeting.KAOMOJI 里的那只猫脸。
GREETING = "早上好呀 (=^･ω･^=)"


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def menu_texts(menu) -> list[str]:
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def _sys_text(messages: list[dict[str, str]]) -> str:
    """一次模型调用里所有 system 段拼起来——两条回话路的差别只在这几段里。"""
    return "\n".join(m["content"] for m in messages if m.get("role") == "system")


def _census(image: QImage) -> dict[str, int]:
    """数一张渲染结果里，正文色和淡化色各占多少像素（只认同色号，别的不看）。

    "颜文字灰一档"这件事，光看源码文本是验不出来的——它是个**画出来长什么样**的
    性质。所以这里真把控件画到白底图上，再按像素点色号数。两个色号差得远
    （#4a3a2f 与 #8c7a6b 每个通道差 60 上下），不会互相误认。
    """
    counts = {TEXT: 0, TEXT_MUTED: 0}
    for y in range(image.height()):
        for x in range(image.width()):
            name = image.pixelColor(x, y).name()
            if name in counts:
                counts[name] += 1
    return counts


def _render(widget) -> QImage:
    """把一个控件画到白底图上。画布按控件自己的尺寸来，不写死——写小了会把
    下半截（往往正是颜文字所在的那几行）裁掉，数出来就成了"没淡化"的假象。"""
    image = QImage(max(1, widget.width()), max(1, widget.height()), QImage.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    widget.render(image)
    return image


def _fragments(doc) -> list[tuple[str, str]]:
    """把一份 QTextDocument 拆成 [(文字, 前景色号)]，按排版顺序。"""
    out: list[tuple[str, str]] = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                out.append((frag.text(), frag.charFormat().foreground().color().name()))
            it += 1
        block = block.next()
    return out


# 一段话里夹一只颜文字（跟真机反馈里那条陪伴台词同一形状）与一段没有颜文字的原文
KAO_LINE = "这段我不敢看了，捂眼睛的那种(>_<)"
PLAIN_LINE = "主人，看电影啦，我陪你看吧！"


def main() -> int:
    box, stub, app = make_env()
    pet = app.pet_window
    bubble = pet._bubble
    companion = app.screen_companion
    pet.show()
    pump(0.05)
    # v2-mood-screen ①：全屏检测在离屏环境会拿到真前台窗口（或桌面）矩形，钉成 False
    # 让"切走就收场"这些老断言照旧成立；全屏触发那条由 test_mood_screen 单独验。
    monitor_mod.is_fullscreen_foreground = lambda: False

    # 沙箱默认把语音总闸关着（免得测试真出声）。这一轮验的是"问了才念"，得在**配置**
    # 上把闸打开；声音本身仍被 Recorder 截住，不会真念出来。
    app.config.voice.enabled = True
    say = Recorder(app.voice, "say")
    proactive: list[str] = []
    app.bus.pet_speak_proactive.connect(proactive.append)

    # ---------- ① 打开宠物不自动出声 + 颜文字不读 ----------

    section("① 主动出声：开机问候只文字，一个字都不念")

    app.greeting_service._greet("day", startup=True)
    pump(0.05)
    check("开机问候走的是主动线（它自己起的话头）", len(proactive) == 1, str(proactive))
    check(
        "打开宠物不自动出声（voice.say 0 次）",
        say.calls == [],
        str(say.calls),
    )
    check(
        "话照说：气泡里显示出来了",
        bubble.isVisible() and bubble._label.text() == strip_action_marks(proactive[0]),
        repr(bubble._label.text()),
    )
    # v2-chat-core #2：原先这里挂着一行「要我念出来吗」，点了才出声。用户要的是
    # **连那个询问按钮一起去掉**——文字回复就是纯文字。这两条量的是"整条确认链
    # 已经不在界面上"：气泡里没有询问行，也不存在任何"待念"的状态可挂。
    check(
        "气泡里没有「要我念出来吗」那行（确认链整条下线）",
        not hasattr(bubble, "_ask_row") and not hasattr(app, "_voice_pending"),
        f"ask_row={hasattr(bubble, '_ask_row')} pending={hasattr(app, '_voice_pending')}",
    )
    check(
        "没有任何会出声的交互（气泡上不存在 answer_ask 这类入口）",
        not hasattr(bubble, "answer_ask") and not hasattr(bubble, "show_text_ask"),
        f"answer_ask={hasattr(bubble, 'answer_ask')} show_text_ask={hasattr(bubble, 'show_text_ask')}",
    )

    section("① 颜文字：UI 上还看得见，念的时候剥掉")
    check("颜文字从话里剥掉", strip_kaomoji(GREETING) == "早上好呀", repr(strip_kaomoji(GREETING)))
    check(
        "整句只有一只猫的脸 → 没有能念的话",
        VoiceEngine._speakable("(=^･ω･^=)") == "",
        repr(VoiceEngine._speakable("(=^･ω･^=)")),
    )
    check(
        "中文括号里的字不算颜文字（「（笑）」别顺手剥了）",
        strip_kaomoji("好呀（笑）") == "好呀（笑）",
        repr(strip_kaomoji("好呀（笑）")),
    )
    check(
        "带数字的括号内容不算颜文字（「（2026）」别剥）",
        strip_kaomoji("明年（2026）再说") == "明年（2026）再说",
        repr(strip_kaomoji("明年（2026）再说")),
    )

    # 真跑一次唱歌那条链：把合成那步截住，看**送进合成器**的到底是哪一份文本。
    # 收口在 VoiceEngine.say 上，所以这一条同时代表所有说话线（回话/主动/提醒）。
    #
    # v2-chat-core #2 之后**没有任何地方再调 say**（引擎留着、接线摘了），所以这一节
    # 从"线上行为"降级成"引擎自己的契约"：将来谁要把出声接回来，颜文字不许跟着念
    # 出来这条得仍然成立。上面那几条（一个字都不念）才是现在的线上口径。
    synth = Recorder(app.voice, "_synthesize")
    # 引擎是按**构造时**的配置决定开不开的（沙箱那一刻是关的），得在引擎上也打开；
    # 说话线那头看的是 config.voice.enabled（上面已开），跟这一处不是同一个开关。
    app.voice._enabled = True
    app.voice._voice_name = "测试嗓子"          # 跳过真去探系统嗓子的那一步
    say.restore()                               # 上面那节验完了，这次要真进 say 的肚子
    app.voice.say(GREETING, "default")
    wait_for(lambda: synth.calls, timeout=3.0)
    check(
        "真走一遍 say：颜文字没被送进合成器",
        bool(synth.calls) and synth.calls[-1][0] == "早上好呀",
        str(synth.calls[-1][0] if synth.calls else "（合成器一次没被喊）"),
    )
    check(
        "上面这一句是**真的**被送去念了（不是 say 提前返回）",
        bool(synth.calls),
        f"合成 {len(synth.calls)} 次",
    )
    synth.restore()
    say = Recorder(app.voice, "say")
    app.voice._enabled = False
    app.voice._voice_name = None

    # ---------- ② 按键说话：入口看得见、点得动 ----------

    section("② 语音输入：用户看得见的入口全摘了，引擎还留着")

    check(
        "桌宠右键菜单里没有「语音输入」",
        not any("语音输入" in text for text in menu_texts(pet._build_menu())),
        str(menu_texts(pet._build_menu())),
    )
    check(
        "托盘菜单里也没有",
        not any("语音输入" in text for text in menu_texts(app.tray._menu)),
        str(menu_texts(app.tray._menu)),
    )
    check(
        "桌宠右键菜单不再按快捷键拼文案（talk_menu_text 这条口子没了）",
        not hasattr(pet_window_mod, "talk_menu_text"),
        "",
    )

    # 聊天框底部那一行也得干净：只剩文字框 + 发送。
    chat = app.chat_bubble
    check(
        "聊天框里没有麦克风按钮（_mic 整个属性都不在了）",
        not hasattr(chat, "_mic") and not hasattr(chat, "set_mic_enabled"),
        f"mic={hasattr(chat, '_mic')} set_mic_enabled={hasattr(chat, 'set_mic_enabled')}",
    )
    check(
        "也没有那排「轻按钮」（_voice_row / 念出来 / 不用）",
        not hasattr(chat, "_voice_row") and not hasattr(chat, "_voice_yes")
        and not hasattr(chat, "_voice_no"),
        f"row={hasattr(chat, '_voice_row')} yes={hasattr(chat, '_voice_yes')}",
    )
    # 底部那一行 = 装着「发送」的那个 QHBoxLayout，量它最准：只剩文字框 + 发送。
    row_layout = next(
        lay for lay in chat.findChildren(QHBoxLayout) if lay.indexOf(chat._send_button) >= 0
    )
    row_widgets = [row_layout.itemAt(i).widget() for i in range(row_layout.count())]
    check(
        "底部那一行只剩文字框 + 发送（麦克风按钮整块没了）",
        [type(w).__name__ for w in row_widgets] == ["QLineEdit", "QPushButton"]
        and row_widgets[0] is chat._input
        and row_widgets[1] is chat._send_button,
        f"{[type(w).__name__ for w in row_widgets]}",
    )

    # 摘的是入口，不是引擎：语音这条路的两台引擎都还在手上，只是没接线。
    check(
        "引擎还在：VoiceListener 与 VoiceEngine 都留着（life/voice/ 一个字没删）",
        app.voice_listener is not None and app.voice is not None,
        f"listener={type(app.voice_listener).__name__} engine={type(app.voice).__name__}",
    )
    boot_src = _src("core/bootstrap.py")
    check(
        "但装配层一根本都没接（heard / talk_started / hotkey 全无接线）",
        "voice_listener.heard.connect" not in boot_src
        and "voice_hotkey" not in boot_src
        and "talk_started.connect" not in boot_src,
        f"heard={boot_src.count('voice_listener.heard.connect')} "
        f"hotkey={boot_src.count('voice_hotkey')}",
    )
    check(
        "快捷键不注册了（全仓没有 GlobalHotkey 的实例化）",
        all("GlobalHotkey(" not in _src(rel)
            for rel in ("core/bootstrap.py", "ui/pet_window.py", "ui/tray.py")),
        "",
    )

    # ---------- ③ 回复自然：闲聊不套四步流程 ----------

    section("③ 回话：闲聊走轻路，求助照旧走专业框架")

    sys_prompt = casual.system_prompt(app.config.pet, app.config.pet.name)
    check(
        "闲聊的 system 里没有那套四步流程的骨架（「回应准则」这一节是林医生卡独有的）",
        "【回应准则" not in sys_prompt,
        f"含回应准则={'【回应准则' in sys_prompt}",
    )
    check(
        "闲聊的 system 点名拆掉了「共情→正常化→探索→赋能」",
        "不要" in sys_prompt and "流程" in sys_prompt,
        "",
    )
    check("安全底线没丢（软表达走这条轻路也得被接住）", "12356" in sys_prompt, "")

    svc = app.psych_service
    # 两组的差别就在 system 那一段和生成参数上，得真跑一遍 _compose 才看得见。
    svc._compose("今天天气不错，刚好出去走走", 0.0, DialogRoute(INTENT_CHAT, "平静"))
    casual_sys = _sys_text(stub.messages[-1])
    check(
        "闲聊这一轮真走了轻路（模型收到的 system 里没有四步流程）",
        "回应准则" not in casual_sys and "不要" in casual_sys,
        f"system {len(casual_sys)} 字，含回应准则={'回应准则' in casual_sys}",
    )
    casual_msgs = casual.messages(app.config.pet, app.config.pet.name, "你好", "（暂无）")
    # v2-chat-style 反其道：摆 6 段**日常陪伴**范本（不是咨询话术），让模型照着"像熟人聊"学。
    check(
        "闲聊那条路摆 6 段日常陪伴范本（v2-chat-style，不是咨询 few-shot）",
        [m["role"] for m in casual_msgs].count("assistant") == 6,
        str([m["role"] for m in casual_msgs]),
    )

    stub.messages.clear()
    svc._compose("我好累，有点撑不住了", 0.6, DialogRoute(INTENT_VENT, "低落"))
    vent_sys = _sys_text(stub.messages[-1])
    check(
        "倾诉照旧走林医生那张卡（没被顺手改掉）",
        "回应准则" in vent_sys,
        f"含回应准则={'回应准则' in vent_sys}",
    )

    stub.messages.clear()
    svc._compose("我该怎么办才好", 0.5, DialogRoute(INTENT_HELP, "焦虑"))
    help_sys = _sys_text(stub.messages[-1])
    check("求助也照旧走专业框架", "回应准则" in help_sys, "")

    check(
        "生成参数真的换了：闲聊那条路用 casual 的档位（更放开）",
        casual.CASUAL_TEMPERATURE > 0.8 and casual.CASUAL_TOP_P >= 0.9,
        f"temperature={casual.CASUAL_TEMPERATURE} top_p={casual.CASUAL_TOP_P}",
    )

    # 分岔口的判据本身：越界一律退回专业框架（保守那一侧是"照旧"）。
    cases = [
        ("闲聊 / 平静 / 情绪 0.0 → 轻路", (DialogRoute(INTENT_CHAT, "平静"), 0.0), True),
        ("指令 / 平静 → 轻路", (DialogRoute(INTENT_COMMAND, "平静"), 0.0), True),
        ("确认 / 平静 → 轻路", (DialogRoute(INTENT_CONFIRM, "平静"), 0.0), True),
        ("倾诉 / 低落 → 专业", (DialogRoute(INTENT_VENT, "低落"), 0.6), False),
        ("求助 / 焦虑 → 专业", (DialogRoute(INTENT_HELP, "焦虑"), 0.5), False),
        (
            "闲聊但情绪分很高 → 专业（别把沉的话接轻了）",
            (DialogRoute(INTENT_CHAT, "低落"), 0.8),
            False,
        ),
        (
            "危机短路那一支 → 专业",
            (DialogRoute(INTENT_CHAT, "平静", crisis=True), 0.0),
            False,
        ),
        ("没接路由（route=None）→ 专业", (None, 0.0), False),
    ]
    for label, (route, score), want in cases:
        got = svc._is_casual(score, route)
        check(label, got is want, f"got={got} want={want}")

    other_card = PsychService(
        app.config, app.bus, app.memory_store, persona_id="some_other_card"
    )
    check(
        "换了角色卡就不走轻路（陪执行那张卡的腔调没被本体这条盖掉）",
        svc.persona_id == DEFAULT_CHAT_PERSONA
        and other_card._is_casual(0.0, DialogRoute(INTENT_CHAT, "平静")) is False,
        f"本体={svc.persona_id} 别的卡={other_card._is_casual(0.0, DialogRoute(INTENT_CHAT, '平静'))}",
    )

    # ---------- ④ 看电影：开场陪伴字幕 ----------

    section("④ 看电影：开场陪伴字幕只显示、不出声、停 6 秒")
    app.config.screen.enabled = True
    app.config.screen.companion_enabled = True
    captions: list[str] = []
    app.bus.screen_caption.connect(captions.append)
    reactions: list[str] = []
    app.bus.pet_speak_proactive.connect(reactions.append)

    def _reset_watch(grace: int = 12) -> None:
        companion._end_watch()
        companion._last_spoken.clear()
        companion._cursor.clear()
        captions.clear()
        reactions.clear()
        bubble.hide_bubble()
        pet._speech_until = 0.0
        say.calls.clear()
        app.config.screen.opening_grace_sec = grace
        pump(0.02)

    _reset_watch()
    companion.on_foreground("potplayer.exe")
    pump(0.05)
    check("前台换成播放器 → 开了一场看片会话", companion._watch_open, str(companion._watch_open))
    check(
        "开场字幕不是立刻冒的（先给剧情一个认出来的机会）",
        captions == [],
        str(captions),
    )

    companion._show_opening()          # 宽限期到点（真机由 QTimer 叫这一下）
    pump(0.05)
    check("宽限期过了 → 发开场陪伴字幕（原文一字不改）", captions == [OPENING_LINE], str(captions))
    check(
        "字幕显示出来了",
        bubble.isVisible() and bubble._label.text() == OPENING_LINE,
        repr(bubble._label.text()),
    )
    check("字幕**不出声**（voice.say 0 次）", say.calls == [], str(say.calls))
    check(
        "字幕一个字都不念（v2-chat-core #2：出声那条线整个不在界面上了）",
        say.calls == [],
        str(say.calls),
    )
    remaining = pet._speech_until - time.monotonic()
    check(
        f"停 {SCREEN_CAPTION_SHOW_MS / 1000:.0f} 秒（不是默认那 9 秒）",
        SCREEN_CAPTION_SHOW_MS == 6000 and 4.5 < remaining <= 6.0 and SHOW_MS != SCREEN_CAPTION_SHOW_MS,
        f"还剩 {remaining:.2f} 秒 / 默认 {SHOW_MS / 1000:.0f} 秒",
    )

    companion.on_scene(SCENE_HORROR)
    pump(0.05)
    # 进看片场会先飘「我陪你看电影」（v2-scene-state-fix ①，场景态那另一条线），
    # 反应里多这一句不算错；验的是恐怖表情反应在。
    check(
        "剧情认出来了 → 出对应表情反应（怕怕）",
        SCENE_LINES[SCENE_HORROR][0] in reactions,
        str(reactions),
    )
    check("开场字幕后不再重复发开场字幕", captions == [OPENING_LINE], str(captions))

    section("④ 开场即剧情：跳过开场字幕，直接出表情")
    _reset_watch(grace=12)
    companion.on_foreground("potplayer.exe")
    companion.on_scene(SCENE_HORROR)        # 宽限期还没到，剧情先认出来了
    pump(0.05)
    check(
        "开场字幕被剧情抢掉（不再说「我陪你看吧」）",
        companion._opening_skipped and captions == [],
        f"skipped={companion._opening_skipped} captions={captions}",
    )
    # 同上：反应里先有「我陪你看电影」（场景态开场），再有「怕怕」。
    check(
        "直接出的是「怕怕」",
        SCENE_LINES[SCENE_HORROR][0] in reactions,
        str(reactions),
    )
    companion._show_opening()               # 就算计时器还是叫了，也不许再冒
    pump(0.05)
    check("宽限期到点也不补发（这一场已经交代过了）", captions == [], str(captions))

    section("④ 浏览器：光开着不算看片，标题判成 watch 才算")
    _reset_watch(grace=12)
    companion.on_foreground("chrome.exe")
    pump(0.02)
    check(
        "刚打开浏览器不算在看片（不然随手一开就冒「看电影啦」）",
        not companion._watch_open and captions == [],
        f"watch_open={companion._watch_open}",
    )
    companion.on_title(TITLE_WATCH)
    pump(0.02)
    check("标题判成 watch → 这才算看片", companion._watch_open, str(companion._watch_open))
    companion.on_foreground("explorer.exe")
    pump(0.02)
    companion._show_opening()
    pump(0.05)
    check(
        "切走就收场，计时不再补发（不会在别的窗口上冒出看电影字幕）",
        not companion._watch_open and captions == [],
        f"watch_open={companion._watch_open} captions={captions}",
    )

    # ---------- ① 补：颜文字 UI 淡化（§43.6 记着"只做了一半"的那一半） ----------
    #
    # 上一轮只做到"不读出来"，"显示的时候灰一档"没做。这一节把它补上，验的是**画出来
    # 的样子**：真渲染成图，数两种色号各多少像素——源码里有没有 `TEXT_MUTED` 这几个
    # 字符，说明不了任何事。

    section("① 补：颜文字在界面里灰一档（桌宠气泡 + 聊天记录）")

    check(
        "判据认得出哪一段是颜文字、哪一段不是",
        split_kaomoji(KAO_LINE) == [("这段我不敢看了，捂眼睛的那种", False), ("(>_<)", True)],
        str(split_kaomoji(KAO_LINE)),
    )
    check(
        "没有颜文字的话原样一段返回（调用方照旧走原文那条路）",
        split_kaomoji(PLAIN_LINE) == [(PLAIN_LINE, False)],
        str(split_kaomoji(PLAIN_LINE)),
    )
    check(
        "正经的括号内容不会被当成颜文字淡化（有汉字 / 有数字 / 有字母的都放过）",
        split_kaomoji("以你当地为准（以你当地为准）") == [("以你当地为准（以你当地为准）", False)]
        and split_kaomoji("明年（2026）再说") == [("明年（2026）再说", False)]
        # (PDF) 这种最容易被漏：没汉字、没数字、还短，光靠前两条筛不掉它，
        # 得靠"里面得有颜文字专用的那些符号"这一条才拦得住。
        and split_kaomoji("导出成 (PDF) 就行") == [("导出成 (PDF) 就行", False)],
        f"(PDF) 那句 -> {split_kaomoji('导出成 (PDF) 就行')}",
    )

    # ---- 浮口气泡（桌宠头顶那个）----
    bubble.show_text(KAO_LINE, pet.geometry())
    pump(0.05)
    check(
        "浮口气泡的 .text() 还是原文（读它的地方一个字都不用改）",
        bubble._label.text() == KAO_LINE,
        repr(bubble._label.text()),
    )
    after = _census(_render(bubble._label))
    check(
        "浮口气泡画出来两种色：正文一档、颜文字另一档",
        after[TEXT] > 0 and after[TEXT_MUTED] > 0,
        f"正文色 {after[TEXT]} 像素 / 淡化色 {after[TEXT_MUTED]} 像素",
    )

    # 修复前的样子：同一个位置放一个普通 QLabel，整段一个色。同一个文本、同一个样式表，
    # 只差"有没有分段上色"这一个变量。
    plain = QLabel(bubble)
    plain.setObjectName("BubbleText")
    plain.setWordWrap(True)
    plain.setFixedWidth(bubble._label.width())
    plain.setText(KAO_LINE)
    plain.ensurePolished()
    before = _census(_render(plain))
    plain.deleteLater()
    check(
        "修复前那个写法：整句一个色，淡化色像素是 0（所以这确实是新做的）",
        before[TEXT] > 0 and before[TEXT_MUTED] == 0,
        f"正文色 {before[TEXT]} 像素 / 淡化色 {before[TEXT_MUTED]} 像素",
    )

    bubble.show_text(PLAIN_LINE, pet.geometry())
    pump(0.05)
    plain_after = _census(_render(bubble._label))
    check(
        "没有颜文字的话一个字都不动（普通气泡跟改造前长得一样）",
        plain_after[TEXT_MUTED] == 0 and plain_after[TEXT] > 0,
        f"正文色 {plain_after[TEXT]} 像素 / 淡化色 {plain_after[TEXT_MUTED]} 像素",
    )

    # 富文本会"粘"在 label 上：先放一句带颜文字的，再放一句没有的，
    # 后一句要是还按富文本渲染，正文里的尖括号就会被当成标签吃掉。
    bubble.show_text(PLAIN_LINE, pet.geometry())
    pump(0.05)
    check(
        "上一句的富文本不会粘到下一句（换回纯文本渲染）",
        bubble._label.text() == PLAIN_LINE and bubble._label.textFormat() == Qt.PlainText,
        f"format={bubble._label.textFormat()}",
    )
    bubble.hide_bubble()
    pump(0.02)

    # ---- 聊天记录（它说的那些话）----
    app.open_chat()
    pump(0.05)
    chat = app.chat_bubble
    chat.append_pet(KAO_LINE)
    pump(0.05)
    delegate = chat._history.itemDelegate()
    opt = QStyleOptionViewItem()
    opt.widget = chat._history
    opt.font = chat._history.font()
    opt.rect = QRect(0, 0, chat._history.width(), 0)
    doc = delegate._document(
        opt, KAO_LINE, delegate._text_width(opt), delegate._color_for(SPEAKER_PET),
        delegate._dim_for(SPEAKER_PET),
    )
    frags = _fragments(doc)
    check(
        "聊天记录里拆成了两段：正文一档、颜文字另一档",
        frags == [("这段我不敢看了，捂眼睛的那种", TEXT), ("(>_<)", TEXT_MUTED)],
        str(frags),
    )
    check(
        "量高和画图用的是同一份文档（不然气泡会叠字或者露白）",
        delegate.sizeHint(opt, chat._history.model().index(0, 0)).width()
        == int(doc.idealWidth()) + BUBBLE_PAD_X * 2
        and delegate.sizeHint(opt, chat._history.model().index(0, 0)).height()
        == int(doc.size().height()) + BUBBLE_PAD_Y * 2,
        f"sizeHint={delegate.sizeHint(opt, chat._history.model().index(0, 0))}",
    )
    check(
        "只淡它说的：我说的（主色底白字）不淡",
        delegate._dim_for(SPEAKER_PET) is True and delegate._dim_for(SPEAKER_USER) is False,
        f"pet={delegate._dim_for(SPEAKER_PET)} user={delegate._dim_for(SPEAKER_USER)}",
    )
    check(
        "聊天记录真的把它这条画在看得见的地方了（不然下面数出来的 0 像素是假象）",
        chat._history.visualItemRect(chat._history.item(0)).top() >= 0,
        str(chat._history.visualItemRect(chat._history.item(0))),
    )
    record = _census(_render(chat._history.viewport()))
    check(
        "聊天记录整块画出来，淡化色真的落在屏幕上（不是只活在文档对象里）",
        record[TEXT_MUTED] > 0 and record[TEXT] > 0,
        f"正文色 {record[TEXT]} 像素 / 淡化色 {record[TEXT_MUTED]} 像素",
    )

    chat.append_user("你在干嘛呀 (>_<)")
    pump(0.05)
    user_doc = delegate._document(
        opt, "你在干嘛呀 (>_<)", delegate._text_width(opt), delegate._color_for(SPEAKER_USER),
        delegate._dim_for(SPEAKER_USER),
    )
    check(
        "我说的那句整段一个色（颜文字照样是白字，不掺灰）",
        [color for _t, color in _fragments(user_doc)] == ["#ffffff"],
        str(_fragments(user_doc)),
    )
    chat.close_bubble(fade=False)
    pump(0.05)

    # 判据是同一套：语音剥掉的与界面淡化的必须正好是同一批字符，不能各认各的。
    # 把 split 出来的非颜文字段拼回去，应该跟 strip 的结果一字不差（strip 另外收了下空格）。
    KAOMOJI_CASES = (
        "(=^･ω･^=)",
        "(T_T)",
        "好呀（笑）",
        "明年（2026）再说",
        "导出成 (PDF) 就行",
        "以你当地为准（以你当地为准）",
        "这段我不敢看了，捂眼睛的那种(>_<)",
        "(=^･ω･^=) 人家回来啦",
        "捂眼睛的那种(>_<) 呀 (T_T)",
        "（研发这边，跨部门要盯紧接口人。）",
    )
    mismatched = [
        s
        for s in KAOMOJI_CASES
        if strip_kaomoji(s)
        != re.sub(r"[ \t]{2,}", " ", "".join(p for p, k in split_kaomoji(s) if not k)).strip()
    ]
    check(
        "界面淡化用的判据 == 语音剥除用的判据（剥掉的和淡化的是同一批字符）",
        mismatched == [],
        str(mismatched),
    )

    # ---------- ⑤ 红线 ----------

    section("⑤ 红线：静音/确认/pet.db/依赖都不回退")

    say.calls.clear()
    app.screen_monitor._scene = SCENE_HORROR      # 模拟"屏幕上正有戏"
    app.bus.pet_speak.emit("陪你看会儿")
    pump(0.05)
    check(
        "⑥看电影静音优先：场景成立时回话也不出声",
        say.calls == [],
        str(say.calls),
    )
    app.screen_monitor._scene = ""

    caption_body = _src("core/bootstrap.py").split("def _on_screen_caption")[1].split("\n    def ")[0]
    check(
        "陪伴字幕那条通道上一个 self.voice.say 都没有（结构上就没接，不是念的时候判了一下）",
        "self.voice.say" not in caption_body,
        f"正文 {len(caption_body)} 字",
    )
    # v2-chat-core #2 把口径从"这条通道不念"推到"哪条通道都不念"：装配层里
    # `self.voice.say(` 一处都不该再有（引擎留着，但没人调它）。
    check(
        "装配层里 self.voice.say( 一处不剩（整条出声线摘干净了，不是逐条判）",
        "self.voice.say(" not in _src("core/bootstrap.py"),
        f"{_src('core/bootstrap.py').count('self.voice.say(')} 处",
    )
    casual_src = _src("life/psych/casual.py")
    check(
        "新增的闲聊路不碰数据库（没有 open/sqlite/db 落盘口子）",
        all(token not in casual_src for token in ("sqlite3", "open(", ".db")),
        "",
    )
    casual_imports = [line.strip() for line in casual_src.splitlines() if line.startswith(("import ", "from "))]
    # 红线是"零新依赖"，不是"一个 import 都不许加"：标准库随便用，仓库自己的包也随便用，
    # 要拦的只有第三方。原来这里写死成两行清单，v2-chat-style-fix 给 casual.py 加了
    # logging / re / dataclasses / pathlib（全是标准库）它当场变红——断言改成按"是不是
    # 标准库 / 是不是本仓的包"判，红线照旧拦得住，往后再加 stdlib 也不会无故报警。
    third_party = sorted(
        {
            name
            for line in casual_imports
            if (module := line.split()[1]) != "__future__"
            for name in (module.split(".")[0],)
            if name not in sys.stdlib_module_names and not (ROOT / name).is_dir()
        }
    )
    check(
        "闲聊路只用标准库 + 宠物自己的模块（零新依赖，第三方一个没有）",
        not third_party,
        third_party or f"{len(casual_imports)} 条 import 全是标准库/本仓模块",
    )

    drain_threads()
    box.cleanup()
    return summary("真机模块②四项反馈（v2-speech-polish）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
