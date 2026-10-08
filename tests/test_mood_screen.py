"""
观影陪伴修正（v2-mood-screen）的自动测试。

真机模块⑦这一轮改四件事：
① 本地播放器无反应 → 全屏视频窗口也触发陪看（不限于浏览器进程）。
   **v2-social-scene-boundary 之后这一条翻了个面**：全屏兜底被用户拍板删掉，
   "非播放器且非浏览器一律不算在看片"，所以①这一节现在验的是**反过来的那件事**——
   认不出的全屏窗口不再触发，而 `shot.is_fullscreen` 本身（纯尺寸判据）照旧逐条验；
② 恐怖有启动但血腥无害怕 → horror 视觉补血腥/尸体/割喉锚；
③ 愤怒显示时长过短 → 愤怒台词气泡停一倍（v2-scene-state-fix ④ 起改为**三态常驻头顶**、
   不排自动淡出）；
④ 新增笑容识别（替代原"喜庆"）→ 影视人脸笑容 → 气泡「好开心」。

外加一节回归：s2 解说不误判、s9 观影只表情不出声、愤怒/恐怖台词照旧。
（v2-mood-tri：⑤ 那一节从「哭泣识别」改成「哭泣整态删除」，四条腿逐个断言已经关掉。）

与 v2-mood-horror（532109e）共存：那一轮改的是开场字幕抢跑与日志可观测，
这一轮改的是触发范围（全屏）、horror 判据（血腥锚）、愤怒时长、笑容识别。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QRect

from harness import (
    Recorder,
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
    use_scene_vote,
)

from life.screen import monitor as mon
from life.screen import shot
from life.screen import mood_cv
from life.screen.companion import SCENE_LINES
from life.screen.monitor import classify_text
from life.screen.scenes import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
    SCENE_TITLES,
)
from ui.bubble import SHOW_MS

# 合成两块屏：主屏 (0,0)、副屏在左边
PRIMARY = QRect(0, 0, 1920, 1080)
LEFT = QRect(-1920, 0, 1920, 1080)
GEO = [PRIMARY, LEFT]


class FakeReader:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None):
        self.calls += 1
        from PySide6.QtGui import QImage
        return QImage(8, 8, QImage.Format_RGB32)


def main() -> int:
    # ---------- ① 全屏兜底：v2-social-scene-boundary 之后只剩尺寸判据本身 ----------

    section("① 全屏兜底已删：认不出的全屏窗口不再触发；尺寸判据本身照旧")

    check("全屏窗口（铺满主屏）→ 认", shot.is_fullscreen(QRect(0, 0, 1920, 1080), GEO), "")
    check("全屏窗口（铺满副屏）→ 认", shot.is_fullscreen(QRect(-1920, 0, 1920, 1080), GEO), "")
    check("窗口化（占一半）→ 不认", not shot.is_fullscreen(QRect(100, 100, 960, 540), GEO), "")
    check("空矩形 → 不认", not shot.is_fullscreen(QRect(), GEO), "")
    check("没有屏幕信息 → 不认", not shot.is_fullscreen(QRect(0, 0, 1920, 1080), []), "")

    box, _stub, app = make_env()
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
        # 这一节验的是触发范围与判据，不是那 60 秒窗口本身——窗口 0 秒 = 一拍即定
        use_scene_vote(monitor, window=0.0)

        reader = FakeReader()
        grabber = FakeGrabber()
        mon.foreground.foreground_pid = lambda: 1
        mon.foreground.foreground_title = lambda: ""
        monitor._reader = reader
        monitor._grabber = grabber
        monitor._cv_grabber = grabber
        monitor._pet_rect = lambda: None

        # 不在名单里的本地播放器（全屏放视频）→ **不再采样**（v2-social-scene-boundary）
        monitor._pids.name_of = lambda pid: "mystery_player.exe"
        mon.is_fullscreen_foreground = lambda: True
        reader.text = "别过去那里有血"
        monitor._forget_scene()
        grabber.calls = 0
        monitor.sample()
        drain_threads()
        pump(0.05)
        check("进程名不在名单、就算全屏 → **也不**截屏采样（全屏兜底已删）",
              grabber.calls == 0, f"抓了 {grabber.calls} 次")

        # 反向对照：**在名单里**的播放器，窗口化也照旧采样（判据是名字，不是全屏）
        monitor._pids.name_of = lambda pid: "potplayer.exe"
        mon.is_fullscreen_foreground = lambda: False
        monitor._forget_scene()
        grabber.calls = 0
        monitor.sample()
        drain_threads()
        pump(0.05)
        check("反向对照：名单里的播放器，窗口化也照旧采样",
              grabber.calls >= 1, f"抓了 {grabber.calls} 次")

        # companion 开场会话同一条口径：认不出的全屏窗口不开，名单里的开
        monitor._pids.name_of = lambda pid: "mystery_player.exe"
        mon.is_fullscreen_foreground = lambda: True
        companion._end_watch()
        companion.on_foreground("mystery_player.exe")
        pump(0.02)
        check("认不出的全屏窗口 → **不**开看片会话", not companion._watch_open,
              str(companion._watch_open))
        companion._end_watch()
        companion.on_foreground("potplayer.exe")
        pump(0.02)
        check("反向对照：名单里的播放器 → 开一场看片会话", companion._watch_open,
              str(companion._watch_open))
        companion._end_watch()
        mon.is_fullscreen_foreground = lambda: False

        # ---------- ② 恐怖血腥画面有害怕反应、不出声 ----------

        section("② 恐怖血腥：horror 视觉补血腥/尸体/割喉锚，反应不出声")

        check("horror 提示词里补了血腥锚",
              mood_cv._PROMPT_HORROR == (
                  "一张恐怖惊悚的画面",
                  "恐怖片场景，阴森吓人",
                  "血腥的尸体画面，血肉模糊",
                  "割喉喷血的恐怖场面",
              ),
              str(mood_cv._PROMPT_HORROR))
        check("血腥关键词 OCR 那条腿也在（血腥/尸体/割喉）",
              {"血腥", "尸体", "割喉", "谋杀"} <= set(mon.SCENE_KEYWORDS[SCENE_HORROR]),
              str(mon.SCENE_KEYWORDS[SCENE_HORROR]))

        spoken = Recorder(app.voice, "say")
        app.config.voice.enabled = True
        reactions: list[str] = []
        app.bus.pet_speak_proactive.connect(reactions.append)
        try:
            monitor._pids.name_of = lambda pid: "potplayer.exe"
            mon.is_fullscreen_foreground = lambda: False
            monitor._forget_scene()
            companion._last_spoken.clear()   # 清掉上一节采样时说的话，别让同态冷却挡了这拍
            companion._cursor.clear()        # 轮换指针也归零，好让这一拍还是从第一句开始
            reactions.clear()
            spoken.calls.clear()
            reader.text = "别过去那里有血"
            monitor.sample()
            drain_threads()
            pump(0.05)
            check("恐怖画面 → 场景成立", monitor.scene == SCENE_HORROR, repr(monitor.scene))
            # 进看片场会先飘一句「我陪你看电影」（v2-scene-state-fix ①），所以这里
            # 只验恐怖那句情绪词**在**反应里，不再要求它是第一条。
            check("反应里有那句「我怕怕…」（不是开场字幕那条通道）",
                  SCENE_LINES[SCENE_HORROR][0] in reactions, str(reactions))
            check("反应只显示不出声", spoken.calls == [], f"{len(spoken.calls)} 次")
        finally:
            spoken.restore()
            app.bus.pet_speak_proactive.disconnect(reactions.append)

        # ---------- ③ 情绪词常驻头顶（不再 60 秒秒退） ----------

        section("③ 状态词：怒/笑常驻头顶，恐惧轻反应（v2-mood-fear-tune）")

        from core import bootstrap as boot_mod
        check("状态词时长常量 = 0（常驻，不再按 60 秒秒退）",
              boot_mod.SCENE_STATUS_PERSIST_MS == 0,
              str(boot_mod.SCENE_STATUS_PERSIST_MS))
        check("常驻只剩怒/笑两态（恐惧已摘出去，v2-mood-fear-tune）",
              set(boot_mod._SCENE_STATUS) == {SCENE_ANGRY, SCENE_FESTIVE},
              str(sorted(boot_mod._SCENE_STATUS)))

        bubble.hide_bubble()
        app.pet_window._speech_until = 0.0
        monitor._scene = ""
        app.bus.pet_speak_proactive.emit("普通一句")
        pump(0.05)
        check("非场景的话 → 气泡照旧用默认时长",
              bubble._hide_timer.interval() == SHOW_MS, str(bubble._hide_timer.interval()))

        for scene, line in ((SCENE_ANGRY, "太气人，真想捶他几下"),
                            (SCENE_FESTIVE, "好开心")):
            bubble.hide_bubble()
            app.pet_window._speech_until = 0.0
            monitor._scene = scene
            app.bus.pet_speak_proactive.emit(line)
            pump(0.05)
            check(f"{scene} 的状态词 → 常驻头顶（不排自动淡出）",
                  bubble.isVisible() and not bubble._hide_timer.isActive(),
                  f"visible={bubble.isVisible()} timer={bubble._hide_timer.isActive()}")

        # 恐惧摘出常驻（v2-mood-fear-tune）：那句「我怕怕…」照说，但排自动淡出（轻反应）
        bubble.hide_bubble()
        app.pet_window._speech_until = 0.0
        monitor._scene = SCENE_HORROR
        app.bus.pet_speak_proactive.emit("我怕怕…")
        pump(0.05)
        check("horror 的状态词 → 轻反应，排自动淡出（不常驻，v2-mood-fear-tune）",
              bubble.isVisible() and bubble._hide_timer.isActive(),
              f"visible={bubble.isVisible()} timer={bubble._hide_timer.isActive()}")
        monitor._scene = ""

        # ---------- ④ 影视人脸笑容 → 「好开心」 ----------

        section("④ 笑容识别（替代原喜庆）：人脸笑容 → 「好开心」")

        check("festive 提示词改成了人脸微笑/大笑（v2-mood-detect 加到四句）",
              mood_cv._PROMPT_FESTIVE == (
                  "一张人脸上带着微笑的画面",
                  "人们开怀大笑的场面",
                  "一个人笑得很开心的特写",
                  "面带笑容、神情愉悦的人物",
              ),
              str(mood_cv._PROMPT_FESTIVE))
        check("这一档的中文名从「喜庆」改成「笑容」",
              SCENE_TITLES[SCENE_FESTIVE] == "笑容", str(SCENE_TITLES[SCENE_FESTIVE]))
        check("笑容那池第一句就是定稿原文「好开心」",
              SCENE_LINES[SCENE_FESTIVE][0] == "好开心", str(SCENE_LINES[SCENE_FESTIVE][0]))
        check("笑容那池整池都是笑容/开心腔（不再沾喜气）",
              all(w not in SCENE_LINES[SCENE_FESTIVE][1] for w in ("喜气", "沾沾光")),
              str(SCENE_LINES[SCENE_FESTIVE]))
        check("其余两态台词没动（恐怖照旧；愤怒 v2-mood-anger 换了定稿文案）",
              SCENE_LINES[SCENE_ANGRY][0] == "太气人，真想捶他几下"
              and SCENE_LINES[SCENE_HORROR][0] == "我怕怕…",
              "")

        # ---------- ⑤ 哭泣整态删除（v2-mood-tri） ----------

        section("⑤ 哭泣整态删除：提示词 / 中文名 / 台词池 / 字幕四条腿一起关")

        check("提示词常量整块没了（`mood_cv._PROMPT_CRY` 已不存在）",
              not hasattr(mood_cv, "_PROMPT_CRY"), "居然还在")
        check("文本零-shot 表里也没有哭泣那一格",
              "cry" not in set(mood_cv.prompt_table()[1]), str(sorted(set(mood_cv.prompt_table()[1]))))
        check("中文名表里没有哭泣",
              "cry" not in SCENE_TITLES, str(sorted(SCENE_TITLES)))
        check("台词池里没有哭泣（用户定稿那四句一并删，原文抄进报告第九十六节）",
              "cry" not in SCENE_LINES
              and all("我也好想哭" not in line for pool in SCENE_LINES.values() for line in pool),
              str(sorted(SCENE_LINES)))
        check("字幕里「哭」一个字都不判（OCR 那条腿整串关键词拔了）",
              classify_text("她哭得停不下来") is None, str(classify_text("她哭得停不下来")))
        check("字幕里「泪/抽泣/呜咽/大哭」也都不判",
              all(classify_text(t) is None for t in
                  ("她眼泪止不住", "他抽泣着", "传来呜咽声", "孩子大哭起来")),
              str([classify_text(t) for t in
                   ("她眼泪止不住", "他抽泣着", "传来呜咽声", "孩子大哭起来")]))
        check("场景顺序里没有哭泣那一格",
              "cry" not in mon.SCENE_ORDER, str(mon.SCENE_ORDER))

        # ---------- ⑥ 回归 ----------

        section("⑥ 回归：s2 解说不误判、s9 观影只表情、愤怒/恐怖仍反应")

        check("解说词不再触发任何场景（幽默这一档已删，s2 那条线没了）",
              classify_text("他搞了半天也没弄明白这个原理") is None, "")
        check("「笑死我了」也不再判幽默（那一档整个没了）",
              classify_text("笑死我了太搞笑了") is None,
              str(classify_text("笑死我了太搞笑了")))
        check("愤怒字幕关键词只有明确愤怒词能过（v2-mood-anger）",
              classify_text("你凭什么骂人") == SCENE_ANGRY, "")
        check("恐怖字幕关键词照旧（血/鬼/杀）",
              classify_text("有鬼啊快跑") == SCENE_HORROR, "")
        check("三态顺序没动（哭泣那一格已删）",
              mon.SCENE_ORDER == (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE),
              str(mon.SCENE_ORDER))

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("观影陪伴修正（v2-mood-screen）")


if __name__ == "__main__":
    raise SystemExit(main())
