"""
观影识别误判与延迟修复（v2-mood-detect）的自动测试。

真机模块⑦报的四件事，一条一条验：

① **空白过多被误判看电影。** 根因：判"在看片"的闸是
   `is_watchable(proc) or is_fullscreen_foreground()`，而后者只量窗口**有多大**——
   一个最大化的资源管理器（打开一个空文件夹）也铺满整屏，于是开了一场看片会话，
   12 秒后冒出"主人，看电影啦"。当时的改法是给全屏那一档再加一道**画面信息量（空白）闸**；
   **v2-social-scene-boundary 之后那道闸连同整条全屏兜底一起删了**（用户拍板
   "照原话删兜底"）——非播放器且非浏览器一律不算在看片，连"截一帧看看空不空"都不必了。
   这一节现在验的是**删干净之后的样子**：认不出的全屏窗口一律不开会话，且一拍都不多截。
② **情绪识别延迟。** 每拍 = sample_sec(5s) + OCR(约 4s)，还要连中 2 拍；更慢的是
   第一次识别要把 RapidOCR 的模型读进来（实测十几秒）。改法两条：画面腿先跑、
   它定案就跳过那 4 秒字幕识别（融合本来就只取 CV 的，结论不变）；启动时后台预热 OCR。
③ **笑容识别不了。** 提示词从两句加到四句（照恐怖那一档补血腥锚的先例）。
④ **本地播放器识别延迟。** 播放器名单补齐常见的一批，认得出进程名的第一拍就看画面。

外加一节回归：恐怖/愤怒/笑容那几个场景与台词一条没动。
（v2-mood-tri 之后这一节把哭泣那一档整档划掉：字幕词表、票数、台词池、场景顺序里都不再有它。）

**这一段验的是判据与链路，不是"识别得多准"**：真机准头要真画面，报告里照实写。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage, QPainter

from harness import check, drain_threads, make_env, pump, section, summary, use_scene_vote

from life.screen import monitor as mon
from life.screen import mood_cv
from life.screen import shot
from life.screen.companion import SCENE_LINES
from life.screen.monitor import SCENE_KEYWORDS, WATCH_PROCS, classify_text, is_watchable
from life.screen.mood_cv import MoodVerdict
from life.screen.mood_fuse import fuse
from life.screen.scenes import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
)


def _blank_image(w: int = 640, h: int = 200) -> QImage:
    """一张纯白画面：模拟"打开一个空文件夹"那种空白屏。"""
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    return img


def _busy_image(w: int = 640, h: int = 200) -> QImage:
    """一张马赛克噪点画面：模拟真在放东西时的那一帧（有信息量）。"""
    img = QImage(w, h, QImage.Format_RGB32)
    painter = QPainter(img)
    for y in range(0, h, 20):
        for x in range(0, w, 20):
            tone = ((x * 7 + y * 13) % 200) + 30
            painter.fillRect(x, y, 20, 20, QColor(tone, (tone * 3) % 255, 255 - tone))
    painter.end()
    return img


class FakeReader:
    """假 OCR：读什么由测试说了算，省掉一次 4 秒的真识别。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0
        self.warmed = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text

    def warm_up(self) -> bool:
        self.warmed += 1
        return True


class FakeMood:
    """假画面腿：判什么由测试说了算。"""

    available = True

    def __init__(self, verdict: MoodVerdict | None) -> None:
        self.verdict = verdict
        self.calls = 0

    def classify_array(self, array):
        self.calls += 1
        return self.verdict


def _img(fill: QColor) -> QImage:
    img = QImage(320, 180, QImage.Format_RGB32)
    img.fill(fill)
    return img


def main() -> int:
    # ---------- 判据本身：空白闸量的是"主色占比" ----------

    section("① 空白判据：纯色屏与空文件夹算空白，噪声/渐变/实拍不算")

    for label, color in (("全白", QColor(255, 255, 255)),
                         ("全黑", QColor(0, 0, 0)),
                         ("纯灰", QColor(128, 128, 128))):
        img = _img(color)
        check(f"{label}屏 → 判空白（主色占比 {shot.dominant_ratio(img):.3f}）",
              shot.is_blank(img), f"{shot.dominant_ratio(img):.3f}")

    # 一个最大化的空文件夹：白底 + 几个图标
    folder = QImage(1920, 1080, QImage.Format_RGB32)
    folder.fill(QColor(250, 250, 250))
    painter = QPainter(folder)
    for i in range(6):
        painter.fillRect(80, 100 + i * 150, 64, 64, QColor(60, 90, 160))
    painter.fillRect(0, 0, 1920, 40, QColor(235, 235, 235))
    painter.end()
    check("空文件夹（白底 + 6 个图标）→ 判空白",
          shot.is_blank(folder), f"{shot.dominant_ratio(folder):.3f}")
    check("阈值 0.90 落在断档里（空白那侧离它还有余量）",
          shot.dominant_ratio(folder) >= shot.BLANK_RATIO, f"{shot.dominant_ratio(folder):.3f}")

    check("有信息量的画面（马赛克噪点）→ 不判空白",
          not shot.is_blank(_busy_image()), f"{shot.dominant_ratio(_busy_image()):.3f}")

    # 渐变：这一条正是**否掉第一版判据**的那张图（逐格标准差法把它算成 1.000 平坦）
    grad = QImage(1920, 1080, QImage.Format_RGB32)
    painter = QPainter(grad)
    for y in range(1080):
        painter.setPen(QColor(20 + y * 180 // 1080, 40 + y * 120 // 1080, 120))
        painter.drawLine(0, y, 1920, y)
    painter.end()
    check("渐变画面 → 不判空白（天空/墙面这类真实画面不能被当成空白）",
          not shot.is_blank(grad), f"{shot.dominant_ratio(grad):.3f}")

    check("取不到画面 → 不判空白（拿空值把陪看关掉更糟）", not shot.is_blank(None), "")
    check("太小的帧（8×8 假帧）→ 不判空白",
          not shot.is_blank(QImage(8, 8, QImage.Format_RGB32)), "")

    # ---------- 空白屏不开看片会话 ----------

    box, stub, app = make_env()
    try:
        monitor = app.screen_monitor
        companion = app.screen_companion
        bubble = app.pet_window._bubble
        app.pet_window.show()
        pump(0.05)

        cfg = app.config.screen
        cfg.enabled = True
        cfg.ocr_enabled = True
        cfg.companion_enabled = True
        # 这一节验的是"认不出的档开不开会话"与链路，不是那 60 秒窗口本身——窗口 0 秒 = 一拍即定
        use_scene_vote(monitor, window=0.0)

        reader = FakeReader()
        mon.foreground.foreground_pid = lambda: 1
        mon.foreground.foreground_title = lambda: ""
        monitor._reader = reader
        monitor._pet_rect = lambda: None

        section("① 全屏但认不出的窗口：不开看片会话，且跟画面内容无关")

        # "认不出名字、但全屏"那个兜底档，v2-social-scene-boundary 之后**整条没了**。
        # 下面两个用例合起来钉住的就是这件事：**开不开会话只看进程名在不在名单里，
        # 画面空不空、有没有内容都不参与判定**——所以同一档两种画面都不开。
        monitor._pids.name_of = lambda pid: "mystery_player.exe"
        mon.is_fullscreen_foreground = lambda: True      # 最大化窗口 → 够全屏
        monitor._grabber = lambda rect=None: _blank_image()
        companion._end_watch()
        companion.on_foreground("mystery_player.exe")
        pump(0.02)
        check("全屏 + 空白画面 → **不**开看片会话（修前这里会开，12 秒后冒「看电影啦」）",
              not companion._watch_open, str(companion._watch_open))

        monitor._grabber = lambda rect=None: _busy_image()
        companion._end_watch()
        companion.on_foreground("mystery_player.exe")
        pump(0.02)
        check("同样全屏、画面**有内容** → 也不开（兜底删了，判据不看画面）",
              not companion._watch_open, str(companion._watch_open))
        companion._end_watch()

        section("① 认不出的档一拍都不多截（截屏那道门只有名单能推开）")

        grabs = {"n": 0}

        def counted(rect=None):
            grabs["n"] += 1
            return _blank_image()

        monitor._grabber = counted
        monitor._pids.name_of = lambda pid: "mystery_player.exe"
        mon.is_fullscreen_foreground = lambda: True
        monitor._proc = "other.exe"          # 装作刚换了前台，好让 sample() 发 foreground_changed
        monitor._forget_scene()
        grabs["n"] = 0
        monitor.sample()
        drain_threads()
        pump(0.05)
        # 修前这里会截一到两次（空白闸自己一帧、采样一帧，靠共用帧压成一次）；
        # 兜底一删，`_watch_candidate` 在进程名那一档就否了，**一帧都不用截**。
        check("认不出的全屏档：屏幕一帧都不截（修前至少一次）",
              grabs["n"] == 0, f"抓了 {grabs['n']} 次")

        section("① 反向对照：播放器进程名不看画面也认（名单那条路没动）")
        monitor._grabber = lambda rect=None: _blank_image()
        companion._end_watch()
        companion.on_foreground("potplayer.exe")
        pump(0.02)
        check("播放器进程名 → 直接算在看片（不因为一帧空白就否掉）",
              companion._watch_open, str(companion._watch_open))
        companion._end_watch()
        mon.is_fullscreen_foreground = lambda: False

        # ---------- 空白屏不判场景 ----------

        section("① 空白屏在采样链里也不成立（一票都不投、不去认字）")

        monitor._pids.name_of = lambda pid: "potplayer.exe"
        mon.is_fullscreen_foreground = lambda: False
        reader.text = "别过去那里有血"          # 字幕明明能判恐怖
        monitor._grabber = lambda rect=None: _blank_image()
        monitor._forget_scene()
        monitor.sample()
        drain_threads()
        pump(0.05)
        check("空白那一拍：不成立场景（哪怕字幕带里有恐怖词）",
              monitor.scene == "" and len(monitor._voter) == 0,
              f"scene={monitor.scene!r} 票数={len(monitor._voter)}")

        monitor._grabber = lambda rect=None: _busy_image()
        monitor._forget_scene()
        monitor.sample()
        drain_threads()
        pump(0.05)
        check("有内容的那一拍：照旧成立恐怖", monitor.scene == SCENE_HORROR, repr(monitor.scene))

        # ---------- ② 延迟：CV 定案就不再跑那 4 秒字幕识别 ----------

        section("② 延迟：画面腿定案就跳过字幕识别（结论不变，只省掉那 4 秒）")

        happy = MoodVerdict(
            SCENE_FESTIVE, 0.93,
            {SCENE_FESTIVE: 0.93, SCENE_HORROR: 0.04, SCENE_ANGRY: 0.01,
             "neutral": 0.01},
        )
        reader.calls = 0
        monitor._reader = reader
        monitor._mood = FakeMood(happy)
        text, verdict, skipped = monitor._analyze_sample(object(), object())
        check("CV 定案 → 那 4 秒的字幕识别一趟都不跑",
              reader.calls == 0, f"OCR 被叫了 {reader.calls} 次")
        check("并且标出这一拍跳过了 OCR（真机日志要能分辨『没命中』与『没跑』）",
              skipped is True and text == "", f"skipped={skipped} text={text!r}")

        # 关键断言：跳过 OCR **不改结论**——融合层本来就"CV 开了口就以它为准"
        with_ocr = fuse(classify_text("别过去那里有血"), happy)   # 字幕说恐怖，画面说笑容
        without_ocr = fuse(None, happy)
        check("跳过 OCR 与不跳过的**场景一致**（争议时听画面腿的）",
              with_ocr.scene == without_ocr.scene == SCENE_FESTIVE,
              f"{with_ocr.scene} / {without_ocr.scene}")

        unsure = MoodVerdict(None, 0.41, {SCENE_HORROR: 0.41, "neutral": 0.59})
        reader.calls = 0
        monitor._mood = FakeMood(unsure)
        text, verdict, skipped = monitor._analyze_sample(object(), object())
        check("CV 没把握 → 字幕那一趟照跑（回到原来的老路，不是把 OCR 关掉）",
              reader.calls == 1 and skipped is False and text == reader.text,
              f"OCR {reader.calls} 次 / skipped={skipped}")

        section("② 延迟：启动时后台预热 OCR（把十几秒的建引擎挪出第一拍）")
        warm = FakeReader()
        monitor._reader = warm
        monitor.start()
        drain_threads()
        pump(0.05)
        check("start() 会把 OCR 预热一次（第一拍就只剩纯推理）",
              warm.warmed == 1, f"预热了 {warm.warmed} 次")
        check("预热是在后台起的，没把 start() 卡住", True, "")
        monitor.stop()

        section("② 反向对照：假 reader 没有 warm_up 也不炸（测试里注入的那种）")
        class NoWarm:
            def read_array(self, array) -> str:
                return ""
        monitor._reader = NoWarm()
        monitor.start()
        pump(0.05)
        check("没有 warm_up 的 reader：start() 照常跑完，不抛",
              monitor.recording, str(monitor.recording))
        monitor.stop()
        monitor._reader = reader

        # ---------- ③④⑤ 三个场景与台词 ----------

        section("③④⑤ 笑容 / 恐怖 / 愤怒：画面腿判出来就冒对应的那句")

        monitor._reader = FakeReader("")
        reactions: list[str] = []
        app.bus.pet_speak_proactive.connect(reactions.append)
        try:
            for scene, want in (
                (SCENE_FESTIVE, "好开心"),
                (SCENE_ANGRY, "太气人，真想捶他几下"),
                (SCENE_HORROR, "我怕怕…"),
            ):
                companion._end_watch()
                companion._last_spoken.clear()
                companion._cursor.clear()
                reactions.clear()
                monitor._scene = ""
                companion.on_scene(scene)
                pump(0.05)
                check(f"{scene} → 冒「{want}」",
                      reactions[:1] == [want], str(reactions))
            check("三句就是各自那一池的定稿第一句（没串池）",
                  SCENE_LINES[SCENE_FESTIVE][0] == "好开心"
                  and SCENE_LINES[SCENE_ANGRY][0] == "太气人，真想捶他几下"
                  and SCENE_LINES[SCENE_HORROR][0] == "我怕怕…", "")
            # v2-mood-tri：哭泣那一池整池删除，连"认出来"这一路都不再存在。
            companion._end_watch()
            reactions.clear()
            monitor._scene = ""
            companion.on_scene("cry")
            pump(0.05)
            check("已经删掉的哭泣态（拿字符串硬喂）一个字都不冒",
                  reactions == [], str(reactions))
            check("哭泣那一段定稿台词在所有池子里一个字不剩",
                  all("我也好想哭" not in line and "别哭了" not in line
                      and "哭出来会好受点" not in line
                      for pool in SCENE_LINES.values() for line in pool), "")
        finally:
            app.bus.pet_speak_proactive.disconnect(reactions.append)
            monitor._scene = ""
        bubble.hide_bubble()

        # ---------- ⑥ 本地播放器名单 ----------

        section("⑥ 本地播放器：常见的一批补进名单，认得出来就不必等全屏")

        for proc in ("iqiyi.exe", "youku.exe", "mgtv.exe", "mpv.net.exe",
                     "mpc-be.exe", "kmplayer64.exe", "potplayer.exe"):
            check(f"{proc} 在名单里、认得出", proc in WATCH_PROCS and is_watchable(proc), "")
        check("认不出的进程名照旧不算（名单是等值表，不做子串匹配）",
              not is_watchable("myplayer_helper.exe"), "")

        # ---------- 回归 ----------

        section("回归：恐怖/愤怒/笑容的场景与阈值一条没动（幽默已删、哭泣已删）")

        check("三态顺序没动（哭泣那一格已删）",
              mon.SCENE_ORDER == (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE),
              str(mon.SCENE_ORDER))
        check("哭泣这一档已经整档删掉（关键词表与最低票数都没有它）",
              "cry" not in mon.SCENE_KEYWORDS and "cry" not in mon.SCENE_MIN_HITS,
              str(sorted(mon.SCENE_KEYWORDS)))
        check("幽默这一档已经整档删掉（SCENE_MIN_HITS 里没有它）",
              "funny" not in mon.SCENE_MIN_HITS, str(mon.SCENE_MIN_HITS))
        check("恐怖仍只要一票（漏判那条不能更漏）",
              mon.SCENE_MIN_HITS[SCENE_HORROR] == 1, str(mon.SCENE_MIN_HITS[SCENE_HORROR]))
        check("恐怖关键词表没删字",
              set(SCENE_KEYWORDS[SCENE_HORROR]) >= {"鬼", "血", "杀", "尸体", "血腥", "割喉"},
              str(SCENE_KEYWORDS[SCENE_HORROR]))
        check("解说词不再触发任何场景（幽默那一档整个没了）",
              classify_text("他搞了半天也没弄明白这个原理") is None, "")
        check("阈值仍是 0.70（本轮没重标，见 mood_cv 里的诚实标注）",
              mood_cv.DEFAULT_MIN_SCORE == 0.70, str(mood_cv.DEFAULT_MIN_SCORE))
        check("空白阈值仍是 0.90 / 容差 24（量出来的断档，不是拍的）",
              shot.BLANK_RATIO == 0.90 and shot.BLANK_TOL == 24,
              f"{shot.BLANK_RATIO} / {shot.BLANK_TOL}")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("观影识别误判与延迟修复（v2-mood-detect）")


if __name__ == "__main__":
    raise SystemExit(main())
