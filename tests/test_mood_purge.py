"""
删除搞笑判别 + 观影/游戏/情绪全场景治理（v2-mood-detect 修正版）的自动测试。

上一条 v2-mood-detect 把"搞笑/幽默"只做了"两票制"的补强，没真的删——真机仍弹
「哈哈哈这个是真的逗」（办公待办界面、专注中）。这一轮按用户定稿**直接移除**幽默
这一档（搞笑判别押后迭代、由笑容替代喜庆），并把愤怒共情改成**只认字幕文本**（写实
游戏里压抑/紧张/打斗的气氛不再被画面腿误判成用户"愤怒"）。同时补一批写实电影感游戏
进库，前台命中即按游戏走。

七条（对应用户任务书 S4）：
① 全场景不再弹「哈哈哈真逗」等搞笑反应（幽默判别已删）；
② 写实游戏 → 识别为游戏、弹攻略不弹电影、不弹愤怒；
③ 办公待办界面 → 不弹任何观影反应；
④ 专注中 → 不出观影反应（含「看电影啦」与笑容/愤怒反应）；
⑤ 空白文件夹 → 不弹电影；
⑥ 真观影 → 笑容「好开心」/ 恐怖「我怕怕…」照旧（v2-mood-tri：哭泣那一池整池已删）；
⑦ 愤怒/恐怖 字幕判据照旧，哭泣字幕一个态都不判（关键词已拔干净）。

红线：pet.db / mind.db 零写入、零新依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

from PySide6.QtGui import QColor, QImage

from life.screen import monitor as mon
from life.screen import mood_cv
from life.screen.companion import SCENE_LINES
from life.screen.game import game_of
from life.screen.monitor import (
    SCENE_KEYWORDS,
    SCENE_MIN_HITS,
    WATCH_PROCS,
    classify_text,
    is_watchable,
)
from life.screen.scene_guard import KIND_FOCUS
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR


def _blank_image(w: int = 640, h: int = 200) -> QImage:
    """一张纯白画面：模拟"打开一个空文件夹"那种空白屏。"""
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    return img


def main() -> int:
    # ---------- ① 幽默判别已删 ----------

    section("① 幽默判别已删：全场景不再弹「哈哈哈真逗」")

    check("SCENE_ORDER 里没有 funny", "funny" not in mon.SCENE_ORDER, str(mon.SCENE_ORDER))
    check("SCENE_TITLES 里没有「幽默」", "幽默" not in mon.SCENE_TITLES.values(),
          str(mon.SCENE_TITLES))
    check("SCENE_KEYWORDS 里没有 funny", "funny" not in SCENE_KEYWORDS, str(SCENE_KEYWORDS))
    check("SCENE_MIN_HITS 里没有 funny", "funny" not in SCENE_MIN_HITS, str(SCENE_MIN_HITS))
    check("语句池里没有幽默那一池，也没有「逗」腔",
          "funny" not in SCENE_LINES
          and all("逗" not in line for pool in SCENE_LINES.values() for line in pool),
          str(list(SCENE_LINES)))
    check("「哈哈哈这个是真的逗」已从库里删干净",
          all("哈哈哈这个是真的逗" not in line for pool in SCENE_LINES.values() for line in pool), "")
    check("字幕「哈哈哈太好笑了」不再判幽默", classify_text("哈哈哈太好笑了") is None,
          str(classify_text("哈哈哈太好笑了")))
    check("字幕「笑死我了」也不再判幽默", classify_text("笑死我了") is None,
          str(classify_text("笑死我了")))
    check("画面腿文本零-shot 里没有幽默/愤怒（愤怒走样例原型，不是泛化文本句）",
          "funny" not in set(mood_cv.prompt_table()[1])
          and "angry" not in set(mood_cv.prompt_table()[1]),
          str(sorted(set(mood_cv.prompt_table()[1]))))
    check("愤怒/恐惧已加回样例原型（PROTO_EMOTIONS 三态含 angry 与 horror，哭泣已删）",
          SCENE_ANGRY in mood_cv.PROTO_EMOTIONS and SCENE_HORROR in mood_cv.PROTO_EMOTIONS
          and "cry" not in mood_cv.PROTO_EMOTIONS,
          str(mood_cv.PROTO_EMOTIONS))

    box, _stub, app = make_env()
    try:
        monitor = app.screen_monitor
        companion = app.screen_companion
        guide = app.game_guide
        pet = app.pet_window
        guard = app.screen_scene_guard
        pet.show()
        pump(0.05)

        cfg = app.config.screen
        cfg.enabled = True
        cfg.ocr_enabled = True
        cfg.companion_enabled = True

        # ---------- ② 写实电影感游戏 ----------

        section("② 写实电影感游戏：识别为游戏、弹攻略不弹电影、不弹愤怒")

        for proc, name in (("gow.exe", "战神"), ("ds.exe", "死亡搁浅"),
                           ("detroitbecomehuman.exe", "底特律：变人"),
                           ("re4.exe", "生化危机 4"), ("sottr.exe", "古墓丽影：暗影")):
            check(f"{proc} → {name}", game_of(proc) == name, str(game_of(proc)))

        check("写实游戏不在播放器名单（不会从电影那条腿进来）", "gow.exe" not in WATCH_PROCS, "")
        check("写实游戏前台 → 不算在看片", monitor.is_watch_foreground("gow.exe") is False, "")
        check("写实游戏前台 → 也不进采样那道门", monitor._watch_candidate("gow.exe") is False, "")

        said: list[str] = []
        app.bus.screen_caption.connect(lambda line: said.append(line))
        offers: list[str] = []
        app.bus.game_offer.connect(lambda game: offers.append(game))
        # v2-scene-state 之后"问攻略"默认要等开场白说完（30 秒）。这一节测的是场景互斥
        # （写实游戏按游戏算、不按电影算），不是那个等待，所以归零。
        app.config.screen.guide_ask_delay_sec = 0

        companion.on_foreground("gow.exe")
        guide.on_foreground("gow.exe")
        pump(0.05)
        check("写实游戏 → 不开看片会话", companion._watch_open is False, str(companion._watch_open))
        companion._show_opening()
        pump(0.02)
        check("写实游戏 → 不冒「看电影啦」", said == [], str(said))
        check("写实游戏 → 攻略照问（弹攻略不弹电影）", offers == ["战神"], str(offers))
        check("画面腿的愤怒只走样例原型、不走泛化文本句（压抑/打斗气氛没有愤怒脸，撞不上原型）",
              "angry" not in set(mood_cv.prompt_table()[1])
              and SCENE_ANGRY in mood_cv.PROTO_EMOTIONS, "")
        guide.on_foreground("explorer.exe")     # 退游戏，清掉守卫里的游戏态
        pump(0.02)

        # ---------- ③ 办公待办界面 ----------

        section("③ 办公待办界面：不弹任何观影反应")

        check("待办字幕不含任何情绪关键词 → 不触发",
              classify_text("今日待办：整理会议纪要") is None,
              str(classify_text("今日待办：整理会议纪要")))
        check("办公进程不在播放器/浏览器名单 → 不值得 OCR", not is_watchable("wps.exe"), "")
        check("幽默这一档没了，办公界面不会再被画面腿判成搞笑",
              "funny" not in set(mood_cv.prompt_table()[1]), "")

        # ---------- ④ 专注中不出观影反应（含笑容/愤怒） ----------

        section("④ 专注中：不出观影反应（含「看电影啦」与笑容/愤怒反应）")

        said.clear()
        app.screen_focus_timer.reset()
        app.pomodoro_service.timer.reset()
        pump(0.05)

        app.screen_focus_timer.enter("chaoxing.exe")
        pump(0.05)
        check("前置：确实进了专注、守卫也认",
              app._is_focusing() and guard.active == KIND_FOCUS,
              f"{app._is_focusing()} / {guard.active}")

        companion.on_foreground("potplayer.exe")
        pump(0.02)
        check("专注中切进播放器 → 不开看片会话", companion._watch_open is False,
              str(companion._watch_open))
        companion._show_opening()
        pump(0.02)
        check("专注中 → 「看电影啦」一个字不冒", said == [], str(said))

        pet._bubble.hide_bubble()
        pet._speech_until = 0.0
        companion._last_spoken.clear()
        companion._cursor.clear()
        companion.on_scene(SCENE_FESTIVE)
        pump(0.05)
        check("专注中 → 笑容反应「好开心」也不冒（互斥覆盖所有观影内容）",
              not pet._bubble.isVisible(), repr(pet._bubble._label.text()))
        companion.on_scene(SCENE_ANGRY)
        pump(0.05)
        check("专注中 → 愤怒反应「太气人，真想捶他几下」也不冒",
              not pet._bubble.isVisible(), repr(pet._bubble._label.text()))

        app.screen_focus_timer.reset()
        app.pomodoro_service.timer.reset()
        pump(0.05)

        # ---------- ⑤ 空白文件夹 ----------

        section("⑤ 空白文件夹：不弹电影")

        monitor._pids.name_of = lambda pid: "explorer.exe"
        monitor._grabber = lambda rect=None: _blank_image()
        monitor._pet_rect = lambda: None
        mon.is_fullscreen_foreground = lambda: True
        companion._end_watch()
        companion.on_foreground("explorer.exe")
        pump(0.02)
        check("全屏 + 空白画面 → 不开看片会话", companion._watch_open is False,
              str(companion._watch_open))
        mon.is_fullscreen_foreground = lambda: False
        companion._end_watch()

        # ---------- ⑥ 真观影：笑容照旧、哭泣整池已删 ----------

        section("⑥ 真观影：笑容「好开心」/ 恐怖「我怕怕…」照旧，哭泣那一池整池已删")

        check("笑容第一句仍是「好开心」", SCENE_LINES[SCENE_FESTIVE][0] == "好开心",
              str(SCENE_LINES[SCENE_FESTIVE][0]))
        check("恐怖第一句仍是「我怕怕…」", SCENE_LINES[SCENE_HORROR][0] == "我怕怕…",
              str(SCENE_LINES[SCENE_HORROR][0]))
        check("哭泣那一池整池没了（v2-mood-tri：用户定稿的四句也不留）",
              "cry" not in SCENE_LINES
              and all("我也好想哭" not in line and "别哭了" not in line
                      for pool in SCENE_LINES.values() for line in pool),
              str(sorted(SCENE_LINES)))

        # 不在专注时，笑容反应照常冒出来（反向对照，证明互斥不是把功能关死）
        pet._bubble.hide_bubble()
        # ② 里游戏攻略把聊天框弹开了（v2-scene-state-fix ③），得先关掉——不然这句笑容
        # 会走「聊天框开着就说进框里」那条路，浮动气泡验不到。
        app.chat_bubble.close_bubble(fade=False)
        companion._last_spoken.clear()
        companion._cursor.clear()
        companion.on_scene(SCENE_FESTIVE)
        pump(0.05)
        check("不在专注 → 笑容反应照常冒", pet._bubble.isVisible()
              and pet._bubble._label.text() == "好开心", repr(pet._bubble._label.text()))

        # ---------- ⑦ 愤怒/恐怖字幕判据（哭泣已断） ----------

        section("⑦ 愤怒（字幕）/ 恐怖 判据照旧，哭泣字幕一个态都不判")

        check("愤怒字幕只认明确愤怒词（v2-mood-anger：单字「怒/骂/战」已移出）",
              classify_text("你凭什么骂人") == SCENE_ANGRY, "")
        check("恐怖字幕照旧", classify_text("有鬼啊快跑") == SCENE_HORROR, "")
        check("哭泣字幕一个态都不判（关键词拔干净，连字幕信号一并断）",
              classify_text("她哭得停不下来") is None,
              str(classify_text("她哭得停不下来")))
        check("恐怖仍只要一票", SCENE_MIN_HITS[SCENE_HORROR] == 1, str(SCENE_MIN_HITS[SCENE_HORROR]))
        check("愤怒字幕仍只要一票", SCENE_MIN_HITS[SCENE_ANGRY] == 1,
              str(SCENE_MIN_HITS[SCENE_ANGRY]))

        # ---------- 红线 ----------

        section("红线：两个库一行不多")

        import sqlite3

        def _rows(db, table):
            con = sqlite3.connect(db)
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                con.close()

        utterance = _rows(app.config.db_path, "utterance")
        fragments = app.mind_store.fragment_count()
        companion.on_scene(SCENE_HORROR)
        companion.on_foreground("potplayer.exe")
        guide.on_foreground("gow.exe")
        pump(0.2)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == utterance,
              f"{utterance} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == fragments, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("删除搞笑判别 + 观影/游戏/情绪全场景治理（v2-mood-detect 修正版）")


if __name__ == "__main__":
    raise SystemExit(main())
