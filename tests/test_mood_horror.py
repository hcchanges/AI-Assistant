"""
真机"放恐怖片完全没反应"（v2-mood-horror）的自动测试。

**S0 查出来的实况**：原以为根因是"CV 判定太严"，实测不是。拿真机那张丧尸画面
（用户在浏览器里放的那一段）喂进判读链：

    CV    horror 0.9250 / angry 0.0262 / festive 0.0232 / neutral 0.0075
          阈值 screen.mood_min_score = 0.70 —— 高出 0.2250，其余各档加起来不到 0.06
          （当年实测值；幽默那一档 v2-mood-detect 已整档删除）
    OCR   字幕带认出「天啊」→ 关键词命中 0 → 不成立（按设计如此，没给它补惊叹词）
    融合  scene=horror / source=cv

再走真链路（真的 ScreenMonitor + 真的抓屏 + 真的完整应用）三档全部复现出 horror
与「我怕怕…」，`life/screen/monitor.py` 在那个会话前后一个字节都没改。
所以**阈值与提示词一个字没动**——0.70 是量出来的（真实节庆照片 0.990 / 纯红 0.658 /
白噪声 0.506，见 mood_cv.py 里那段标定表），往下挪到 0.60 反而贴着噪声带。

真机上量到的两个真问题，就是这一轮改的两处：

① **开场字幕跟"场景成立"抢跑**：宽限期 12 秒，而"场景成立"要等窗口里的票攒成多数、
   每拍 sample_sec(5s) + 识别(约 4s)，实测落在 12~18 秒。同一份代码、同一张图：
   全屏那轮 12.2 秒成立（开场字幕被正确撤掉），窗口化那轮 17.8 秒才成立（开场字幕已经
   先说出口了）。用户要的"开场即恐怖 → 直接怕怕、别说我陪你看吧"就成了抛硬币。
   改法：宽限期到点时若这一拍**已经判出场景、只差窗口没满**，就把这一句往后挪一次
   （OPENING_DEFER_MS），挪完还不成立照发——只挪一次，不会把这一句憋死。

② **"没反应"在日志里查不出来**：这条链上"没反应"有三种完全不同的成因（前台不是
   播放器 / OCR 没命中 / CV 分数没过），表现却一模一样（宠物安静），而原来只在
   logger.debug 上说话，真机日志是 INFO 级，等于什么都看不见。改法：换前台记一条、
   浏览器标题判出标签记一条、每一拍判读完记一条（场景/来源/多数票/两条腿各自的结论）。
   **只落结论不落内容**：字幕文字与画面一个字都不进日志。

这一轮验四节，外加一节"没动的那些"：
- ① 开场字幕不再抢跑（同一条路做修复前/后对照）+ 只挪一次、苗头不成立照发；
- ② 每拍一条 INFO，内容够定位；字幕原文不进日志；不看片的那拍不记；
- ③ 红线：pet.db 零写入、零新依赖、判据一个字没改。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    Recorder,
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
    use_scene_vote,
    wait_for,
)

from PySide6.QtGui import QImage

from life.screen import monitor as mon
from life.screen import mood_cv
from life.screen.companion import OPENING_DEFER_MS, OPENING_LINE, SCENE_LINES
from life.screen.monitor import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
    SCENE_KEYWORDS,
    SCENE_MIN_HITS,
)
from life.screen.mood_cv import MoodVerdict
from life.screen.mood_fuse import fuse

# 认出恐怖的那句字幕。挑的是"命中一个恐怖关键词就够"的一句（恐怖这档是 1 票）。
HORROR_SUBTITLE = "别过去那里有血"
# 什么关键词都不命中的一句。
QUIET_SUBTITLE = "今天天气不错我们出去走走"


class FakeReader:
    """假识别器：省掉一次 4 秒的真 OCR。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假截屏：只记自己被叫了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None) -> QImage:
        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


class LogCapture(logging.Handler):
    """把 life.screen.monitor 的日志就地接一份（真机落的是 data/logs/pet.log）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def only(self, token: str) -> list[str]:
        return [line for line in self.lines if token in line]


def main() -> int:
    box, _stub, app = make_env()
    monitor = app.screen_monitor
    companion = app.screen_companion
    bubble = app.pet_window._bubble
    app.pet_window.show()
    pump(0.05)

    cfg = app.config.screen
    cfg.enabled = True
    cfg.ocr_enabled = True
    cfg.companion_enabled = True
    cfg.opening_grace_sec = 12
    # 场景判定现在是"一分钟多数票"（v2-mood-detect 再审·终）。这一节验的是开场字幕的
    # 抢跑，不是那 60 秒本身，所以塞个假钟：窗口 5 秒、每拍走 5 秒——两拍填满，
    # 正好复现旧用例里"第一拍露头、第二拍成立"的节奏。
    clock = use_scene_vote(monitor, window=5.0)

    # v2-chat-core #2 之后「任何一条说话线都不出声」是硬口径，这一节用真次数盯着：
    # 陪伴字幕那条通道（还有回话那条）都不该再走到 voice.say 上。
    spoken = Recorder(app.voice, "say")
    app.config.voice.enabled = True

    reader = FakeReader()
    grabber = FakeGrabber()
    mon.foreground.foreground_pid = lambda: 1
    mon.foreground.foreground_title = lambda: ""
    monitor._pids.name_of = lambda pid: "potplayer.exe"
    monitor._reader = reader
    monitor._grabber = grabber
    monitor._cv_grabber = grabber
    monitor._pet_rect = lambda: None
    mon.is_fullscreen_foreground = lambda: False   # v2-mood-screen ①：离屏默认非全屏

    captions: list[str] = []
    reactions: list[str] = []
    app.bus.screen_caption.connect(captions.append)
    app.bus.pet_speak_proactive.connect(reactions.append)

    capture = LogCapture()
    capture.setLevel(logging.INFO)
    log = logging.getLogger("life.screen.monitor")
    log.addHandler(capture)
    log.setLevel(logging.INFO)

    def sample(text: str, rounds: int = 1) -> None:
        reader.text = text
        for _ in range(rounds):
            clock.advance()          # 每拍之间走 5 秒（多数票窗口要靠它攒）
            monitor.sample()
            wait_for(lambda: not monitor._reading, timeout=10)
            pump(0.02)

    def reset(grace: int = 12) -> None:
        companion._end_watch()
        companion._last_spoken.clear()
        companion._cursor.clear()
        captions.clear()
        reactions.clear()
        capture.lines.clear()
        monitor._forget_scene()
        monitor._last_fusion = None
        monitor._proc = ""                 # 让下一拍的"换前台"那条日志真能再记一次
        bubble.hide_bubble()
        spoken.calls.clear()
        cfg.opening_grace_sec = grace
        pump(0.02)

    # ---------- ① 开场字幕不再抢跑 ----------

    section("① 开场字幕不再跟「场景成立」抢跑（v2-mood-horror）")
    reset()
    companion.on_foreground("potplayer.exe")
    pump(0.05)
    check("前台换成播放器 → 开了一场看片会话", companion._watch_open, str(companion._watch_open))

    sample(HORROR_SUBTITLE)          # 一拍：判出恐怖，但窗口只有 1 票（要 2 票才定案）
    check(
        "这一拍已判出恐怖，但还没定案（1/2 票，scene 仍是空串）",
        companion._scene_pending() == SCENE_HORROR and monitor.scene == "",
        f"苗头={companion._scene_pending()!r} scene={monitor.scene!r} 票数={len(monitor._voter)}",
    )

    # 修复前那条路：不看苗头，宽限期到点就发。把 _opening_deferred 先置 True 就等于退回它。
    companion._opening_deferred = True
    companion._show_opening()
    pump(0.05)
    check(
        "修复前：宽限期到点就把「我陪你看吧」发出去了（这就是抢跑）",
        captions == [OPENING_LINE],
        str(captions),
    )

    captions.clear()
    companion._opening_done = False
    companion._opening_deferred = False       # 走现在这条路
    companion._show_opening()
    pump(0.05)
    check(
        "修复后：有苗头就先不发，往后挪一次（不是不发，是等这一拍落地）",
        captions == [] and companion._opening_deferred and companion._opening_timer.isActive(),
        f"captions={captions} deferred={companion._opening_deferred}",
    )

    sample(HORROR_SUBTITLE)          # 第二拍：窗口填满、多数票是恐怖
    pump(0.2)
    check("第二拍窗口填满 → 场景成立", monitor.scene == SCENE_HORROR, repr(monitor.scene))
    # 进看片场会先飘一句「我陪你看电影」（v2-scene-state-fix ①，场景态那另一条线），
    # 所以这里只验恐怖那句在反应里、开场字幕（「我陪你看吧」）没走字幕通道抢跑。
    check(
        "直接出的是「怕怕」，不是「我陪你看吧」",
        SCENE_LINES[SCENE_HORROR][0] in reactions and captions == [],
        str(reactions),
    )
    check(
        "气泡里就是那句「我怕怕…」",
        bubble.isVisible() and bubble._label.text() == SCENE_LINES[SCENE_HORROR][0],
        f"可见={bubble.isVisible()} 文案={bubble._label.text()!r}",
    )
    companion._show_opening()                 # 推迟到点后计时器又叫了一次
    pump(0.05)
    check("推迟到点也不补发开场字幕（这一场已经交代过了）", captions == [], str(captions))

    section("① 推迟只挪一次：苗头一直不成立，开场字幕还得发（别把这一句憋死）")
    reset()
    companion.on_foreground("potplayer.exe")
    pump(0.02)
    sample(HORROR_SUBTITLE)                   # 有苗头，但只有 1/2 票
    companion._show_opening()
    pump(0.05)
    check(
        "第一次叫：有苗头 → 往后挪",
        companion._opening_deferred and captions == [],
        f"deferred={companion._opening_deferred} captions={captions}",
    )
    companion._show_opening()                 # 苗头还在，但不许再挪了
    pump(0.05)
    check(
        "第二次叫：不再挪，老老实实发开场陪伴字幕",
        captions == [OPENING_LINE],
        str(captions),
    )
    check("推迟只挪这一次，时长是量出来的（8 秒 ≈ 一拍 5s + 一次识别约 4s）",
          OPENING_DEFER_MS == 8000, str(OPENING_DEFER_MS))

    section("① 反面：没有苗头时开场字幕照旧发")
    reset()
    companion.on_foreground("potplayer.exe")
    pump(0.02)
    sample(QUIET_SUBTITLE)                    # 什么都判不出来
    check("这一拍没有苗头", companion._scene_pending() == "", repr(companion._scene_pending()))
    companion._show_opening()
    pump(0.05)
    check("没苗头 → 开场字幕照发（原文一字不改）", captions == [OPENING_LINE], str(captions))
    # v2-chat-core #2 之后，整条"要我念出来吗"的确认链下线了：现在不是"不挂询问"，
    # 而是**任何一条说话线都不出声**。这条跟着改成量 voice.say 的次数。
    check("还是只显示不出声（v2-chat-core 之后一个字都不念）",
          spoken.calls == [],
          str(spoken.calls))

    # ---------- ② 每拍一条 INFO ----------

    section("② 每拍一条 INFO：真机「没反应」时看得出断在哪一环")
    reset()
    companion.on_foreground("potplayer.exe")
    pump(0.02)
    sample(HORROR_SUBTITLE)
    check(
        "换前台记了一条，并写清这一类应用「会不会去看」",
        capture.only("前台换成 potplayer.exe") != [],
        str(capture.lines),
    )
    sample_lines = [line for line in capture.lines if "场景=" in line]
    check("这一拍记了一条判读结论", len(sample_lines) == 1, str(capture.lines))
    line = sample_lines[0] if sample_lines else ""
    check(
        "一行里齐了：场景 / 来源 / 多数票 / 两条腿各自的结论",
        all(
            part in line
            for part in ("场景=horror", "来源=ocr", "多数票 1/5s", "OCR horror", "CV 没参与")
        ),
        line,
    )
    check(
        "字幕原文一个字都没进日志（只落结论，不落内容）",
        all(HORROR_SUBTITLE not in text for text in capture.lines),
        str(capture.lines),
    )

    capture.lines.clear()
    monitor._pids.name_of = lambda pid: "notepad.exe"
    sample(HORROR_SUBTITLE)
    check(
        "前台不是播放器/浏览器 → 一条判读日志都不记（只记了这次换前台）",
        [line for line in capture.lines if "场景=" in line] == []
        and capture.only("前台换成 notepad.exe") != [],
        str(capture.lines),
    )
    monitor._pids.name_of = lambda pid: "potplayer.exe"
    monitor._proc = "potplayer.exe"

    section("② CV 那条腿在日志里的样子（直接喂一个判读结果：就用真机量到的那组数）")
    capture.lines.clear()
    verdict = MoodVerdict(
        SCENE_HORROR,
        0.9250,
        {
            SCENE_HORROR: 0.9250,
            SCENE_ANGRY: 0.0262,
            SCENE_FESTIVE: 0.0232,
            mood_cv.NEUTRAL_KEY: 0.0075,
        },
    )
    monitor._log_sample(None, verdict, fuse(None, verdict))
    cv_line = capture.only("CV horror")[-1] if capture.only("CV horror") else ""
    check(
        "CV 的结论与四档分数都落进同一行（真机那张图是 0.9250）",
        "场景=horror" in cv_line and "来源=cv" in cv_line and "horror 0.9250" in cv_line
        and "neutral 0.0075" in cv_line,
        cv_line,
    )

    capture.lines.clear()
    unsure = MoodVerdict(None, 0.6100, {SCENE_HORROR: 0.6100, mood_cv.NEUTRAL_KEY: 0.3900})
    monitor._log_sample(None, unsure, fuse(None, unsure))
    unsure_line = capture.only("场景=无")[-1] if capture.only("场景=无") else ""
    check(
        "CV 没过阈值时也照记（写「没把握」并带上分数——这正是原来查不出来的那种拍）",
        "CV 没把握" in unsure_line and "horror 0.6100" in unsure_line,
        unsure_line,
    )

    # ---------- ③ 口径：这一轮没动的那些 ----------

    section("③ 口径：v2-mood-horror 改抢跑与可观测；v2-mood-screen ② 补血腥锚")
    check(
        "恐怖阈值仍是 0.70（真机那张图 0.9250，缺的不是这 0.1；往下挪会贴着纯红 0.658 的噪声带）",
        mood_cv.DEFAULT_MIN_SCORE == 0.70,
        str(mood_cv.DEFAULT_MIN_SCORE),
    )
    check(
        "恐怖提示词现在补了血腥锚（v2-mood-screen ②：血腥/尸体/割喉画面也判恐怖）",
        mood_cv._PROMPT_HORROR == (
            "一张恐怖惊悚的画面",
            "恐怖片场景，阴森吓人",
            "血腥的尸体画面，血肉模糊",
            "割喉喷血的恐怖场面",
        ),
        str(mood_cv._PROMPT_HORROR),
    )
    check(
        "中立锚没动（写实血腥没被算成中性）",
        mood_cv._NEUTRAL_PROMPTS == ("一张平淡普通的画面", "日常平静的画面，没什么特别"),
        str(mood_cv._NEUTRAL_PROMPTS),
    )
    check(
        "恐怖这一档仍是 1 票（没跟着幽默抬到 2）",
        SCENE_MIN_HITS[SCENE_HORROR] == 1,
        str(SCENE_MIN_HITS[SCENE_HORROR]),
    )
    check(
        "恐怖关键词表没删字（漏判那条不能更漏）",
        set(SCENE_KEYWORDS[SCENE_HORROR]) >= {"鬼", "血", "杀", "尸体", "血腥", "割喉", "谋杀"},
        str(SCENE_KEYWORDS[SCENE_HORROR]),
    )
    check(
        "OCR 那条腿没补惊叹词（用户点名不许补）",
        all(word not in SCENE_KEYWORDS[SCENE_HORROR] for word in ("天啊", "啊", "救命", "快跑")),
        str(SCENE_KEYWORDS[SCENE_HORROR]),
    )

    drain_threads()
    box.cleanup()
    return summary("真机「放恐怖片没反应」的修复（v2-mood-horror）")


if __name__ == "__main__":
    raise SystemExit(main())
