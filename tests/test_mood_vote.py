"""
影视情绪「一分钟多数票 + 状态词显示≥1分钟 + 非视频闸死 + 愤怒笑容校准」
（v2-mood-detect 再审·终）的自动测试。

真机模块⑦第三轮报的三件事：

① **影视情绪不准**：笑容识别不出、愤怒被误判成笑容、非视频场景乱飘"太上老君"。
   判定从"连着两拍同一态"改成**一分钟滑动窗口取多数票**（`life/screen/vote.py`）：
   60 秒里出现最多的那一态才算数；不满窗口 / 平票**什么都不动**（维持上一次判定，不秒退）。
   **没把握的那一拍不投票**（v2-mood-recall-fix）：弃权只作废这一拍的时间，不进窗口
   稀释情绪票；**散场只由硬证据触发**（换前台 / 不是视频 / 空白屏 / 自己的窗口），
   不再靠"猜不出来的拍数过半"。
② **状态词秒退、闪烁**：状态词弹出后常驻头顶、不排自动淡出（`core/bootstrap.py` 的
   `SCENE_STATUS_PERSIST_MS = 0`，v2-scene-state-fix ④ 起），不秒退、不闪烁乱出。
   **三态**一视同仁（v2-mood-tri 砍掉哭泣之后 `_SCENE_STATUS` 就剩恐怖/愤怒/笑容三格；
   **v2-mood-fear-tune** 起恐惧摘出常驻，只剩愤怒/笑容两格，恐惧那句「我怕怕…」降为轻反应、
   随默认时长淡出）。
③ **"太上老君急急如律令"**：那是玄幻咒语腔，不是恐怖腔，从恐怖池里删掉。

外加两条治理：
- **非视频前台硬闸**（`monitor._known_non_video`）：桌面 / 文件夹 / 办公 / 聊天这类
  前台，就算铺满整屏也**不进采样**——既不认字，也不投任何票。
- **愤怒↔笑容校准**：`mood_cv.proto_separation()` 量两态质心距离，保证愤怒画面
  不往笑容上投。**阈值一个数没动**（PROTO_CONF / PROTO_MARGIN / PROTO_TEMP /
  mood_min_score 全照旧），降噪靠窗口投票，不靠调阈值。

v2-mood-face-crop 只动了 margin 一项：`PROTO_MARGIN` 这个默认值仍是 0.03，
但 `proto_verdict` 改成按**冠军态**取门（`PROTO_MARGIN_BY_STATE`），原因是各态
"离得多远"差很多——0.03 对笑容（最近邻 0.1029）形同虚设，对恐惧又刚好贴边。
PROTO_CONF 0.76 与温度 20.0 仍是一个数没动。

v2-mood-tri（砍悲伤、降到三态）在这一轮**没有再动任何阈值**：三态的分 margin
沿用原本那三个数（笑容 0.015 / 愤怒 0.025 / 恐惧 0.03），只是表里少了哭泣那一格。
⑧b 那一节加上的是**融合层双保险**——已删除的态连投票窗都进不去，绝不空转占窗。

红线：pet.db / mind.db 零写入、零新依赖。
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from PySide6.QtGui import QColor, QImage

from harness import check, drain_threads, make_env, pump, section, skip, summary, use_scene_vote, wait_for

from life.screen import monitor as mon
from life.screen import mood_cv
from life.screen.companion import SCENE_LINES
from life.screen.monitor import WATCH_PROCS, _known_non_video
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR, SCENE_TITLES
from life.screen.vote import (
    FIRST_MIN_SHARE,
    FIRST_MIN_VOTES,
    FIRST_MIN_VOTES_BY_STATE,
    SWITCH_HOLD_SEC,
    SceneVoter,
)

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "data" / "models"

# 非视频前台的代表：桌面/文件夹、记事本、Word、微信、编辑器
NON_VIDEO_PROCS = ("explorer.exe", "notepad.exe", "winword.exe", "wechat.exe", "code.exe")


class FakeReader:
    """假 OCR：读什么由测试说了算，省掉一次 4 秒的真识别。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假截屏：回一张**拼块**图——半屏一个色、半屏另一个色。

    必须真铺成块：`shot.is_blank` 数的是"最大单色占比 ≥ 0.90 就算空白"，
    点几个像素上去（哪怕几十个）主色占比还是 99%，照样撞空白闸、
    `sample()` 直接 `_forget_scene()` 走人，一张票都投不进来。
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None) -> QImage:
        self.calls += 1
        img = QImage(64, 64, QImage.Format_RGB32)
        img.fill(QColor(20, 40, 60))
        for y in range(0, 64, 8):
            for x in range(0, 64, 8):
                if (x + y) % 16 == 0:
                    for dy in range(8):
                        for dx in range(8):
                            img.setPixelColor(x + dx, y + dy, QColor(220, 180, 90))
        return img


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _imports(path: Path) -> list[str]:
    """文件里 import 了哪些顶层包（AST 静态扫，不执行）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return names


def main() -> int:
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

        db_path = app.config.db_path
        mind_path = app.config.mind.db_path
        before_utt = _rows(db_path, "utterance")
        before_mind = _rows(mind_path, "fragment")

        def _sample() -> None:
            monitor.sample()
            wait_for(lambda: not monitor._reading, timeout=5.0)
            pump(0.02)

        # ---------- ① 多数票本身 ----------

        section("① 多数票：不满窗口 / 平票 / 全弃权，三种都不乱动")

        def voter(window: float, start: float = 0.0):
            """这一节验的是**多数票本身**（满窗 / 平票 / 弃权 / 剪票），所以把
            v2-mood-window 那两道闸（首弹门槛、切换去抖）**摘掉**——闸本身在
            `test_mood_window.py` 里用生产参数单独验，混在一起两件事都验不清。
            摘闸就是把三档取值传 0（`switch_hold_sec=0` 直接跳过整个去抖分支）。"""
            box_ = {"t": start}
            return SceneVoter(window, now=lambda: box_["t"],
                              first_min_votes=0, first_min_share=0.0,
                              switch_hold_sec=0.0), box_

        plain = SceneVoter(60.0)
        check("★ 生产默认带着 v2-mood-window 的两道闸（首弹 ≥8 票且 ≥60%、切换 ≥10 秒）",
              (plain.first_min_votes, plain.first_min_share, plain.switch_hold_sec)
              == (FIRST_MIN_VOTES, FIRST_MIN_SHARE, SWITCH_HOLD_SEC)
              == (8, 0.60, 10.0),
              f"{plain.first_min_votes} / {plain.first_min_share} / {plain.switch_hold_sec}")
        check("★ 生产默认还带着按态分票（怒 6 / 恐 8，笑容回落 8，v2-mood-fear-tune 抬回恐）",
              plain.first_min_votes_by_state == FIRST_MIN_VOTES_BY_STATE == {SCENE_ANGRY: 6,
                                                                            SCENE_HORROR: 8},
              str(plain.first_min_votes_by_state))

        v, clk = voter(60.0)
        check("一票都没有 → 不定案（不是散场）", v.decide() is None, repr(v.decide()))
        check("第一票：不满窗口 → 不定案", v.note(SCENE_HORROR) is None, "")
        clk["t"] = 30.0
        check("半分钟两票：还是不满 → 不定案", v.note(SCENE_HORROR) is None, "")
        clk["t"] = 60.0
        got = v.note(SCENE_HORROR)
        check("满一分钟、三票同态 → 定案恐怖", got == SCENE_HORROR, repr(got))

        v2, c2 = voter(60.0)
        v2.note(SCENE_ANGRY)
        c2["t"] = 60.0
        got = v2.note(SCENE_FESTIVE)
        check("两票打平 → 不定案（不是散场）", got is None, repr(got))

        v3, c3 = voter(60.0)
        v3.note(None)
        c3["t"] = 30.0
        v3.note(None)
        c3["t"] = 60.0
        got = v3.note(None)
        check("整窗全弃权 → 不定案（维持上次判定，**不散场**）",
              got is None and len(v3) == 0, f"{got!r} / {len(v3)} 票")

        v6, c6 = voter(60.0)
        v6.note(SCENE_ANGRY)                     # t=0 一票有定论
        for t in (10.0, 20.0, 30.0, 40.0, 50.0):
            c6["t"] = t
            got = v6.note(None)                  # 中间五拍全弃权
        check("弃权的拍不投票：五拍弃权之后窗口里还是只有那一票",
              len(v6) == 1, f"{len(v6)} 票")
        c6["t"] = 60.0
        got = v6.note(SCENE_ANGRY)
        check("★ 弃权不稀释：两票愤怒就定案愤怒（旧口径下这五张空票会把定案推成散场）",
              got == SCENE_ANGRY, repr(got))

        v4, c4 = voter(60.0)
        v4.note(SCENE_HORROR)    # t=0
        c4["t"] = 30.0           # 半分钟：老票还在 60 秒窗里
        v4.note(SCENE_HORROR)
        check("窗口内的票留着（两票都在）", len(v4) == 2, str(len(v4)))
        c4["t"] = 130.0
        got = v4.note(None)      # 弃权不投票；进来时顺手把 0/30 那两票一起剪掉
        check("时间往前走，窗口外的老票剪光 → 一票不剩、不定案",
              got is None and len(v4) == 0, f"{got!r} / {len(v4)}")

        v5, _c5 = voter(0.0, start=5.0)
        check("窗口 0 秒 → 一拍即定（单测/即时反应用的那档）",
              v5.note(SCENE_FESTIVE) == SCENE_FESTIVE, "")
        v5.reset()
        check("reset 之后旧票作废", len(v5) == 0 and v5.decide() is None, str(len(v5)))

        # ---------- ② 非视频前台硬闸 ----------

        section("② 非视频前台：硬闸挡在采样之前（打开桌面/办公/聊天都不认字）")

        mon.is_fullscreen_foreground = lambda: True    # 就算铺满整屏也不放行
        for proc in NON_VIDEO_PROCS:
            check(f"{proc} → 不算在看片", monitor.is_watch_foreground(proc) is False, "")
            check(f"{proc} → 也不进采样门（不截屏、不认字）",
                  monitor._watch_candidate(proc) is False, "")
        check("真播放器照旧放行（硬闸没把功能一起关掉）",
              monitor.is_watch_foreground("potplayer.exe") is True, "")
        # v2-social-scene-boundary：全屏兜底整条删掉了，"认不出名字但铺满屏"这一类
        # **不再算候选**——用户拍板的原话是"非播放器且非浏览器则不放行"。
        check("认不出名字、铺满屏 → **也不算候选**（全屏兜底已删）",
              monitor._watch_candidate("mystery_player.exe") is False, "")
        check("播放器名单一个都没被这道闸误伤",
              [p for p in WATCH_PROCS if _known_non_video(p)] == [],
              str([p for p in WATCH_PROCS if _known_non_video(p)]))

        # ---------- ③ 一分钟多数票接进采样链 ----------

        section("③ 采样链：满一分钟才定案，定案之后不重复报")

        monitor._reader = FakeReader("别过去那里有血")
        monitor._grabber = FakeGrabber()
        mon.foreground.foreground_pid = lambda: 1
        monitor._pids.name_of = lambda pid: "potplayer.exe"
        mon.is_fullscreen_foreground = lambda: False
        monitor._forget_scene()
        monitor._scene = ""
        clock = use_scene_vote(monitor, window=60.0)     # 真·默认窗口宽度
        seen: list[str] = []
        monitor.scene_changed.connect(seen.append)

        _sample()                                  # t=0，第一票
        check("第 1 拍（0 秒）：窗口没满 → 一声不吭", seen == [], str(seen))
        for _ in range(11):
            clock.advance(5)
            _sample()
        check("第 12 拍（55 秒）：还差 5 秒才满 → 照旧不报", seen == [], str(seen))
        clock.advance(5)
        _sample()
        check("第 13 拍（60 秒）：窗口满、票全一致 → 定案恐怖",
              seen == [SCENE_HORROR] and monitor.scene == SCENE_HORROR, str(seen))
        clock.advance(5)
        _sample()
        check("再拍一次：定案没变 → 不重复报（不闪烁）", seen == [SCENE_HORROR], str(seen))

        monitor._reader.text = ""                 # 字幕没了、画面也没判出什么 = 全程弃权
        for _ in range(13):
            clock.advance(5)
            _sample()
        check("整整一分钟全是弃权 → 一样不动：场景还是恐怖、不散场（不秒退）",
              seen == [SCENE_HORROR] and monitor.scene == SCENE_HORROR, str(seen))
        check("弃权的那一分钟一张票都没进窗口（老票也早被窗口剪光了）",
              len(monitor._voter) == 0, f"{len(monitor._voter)} 票")

        monitor._pids.name_of = lambda pid: "explorer.exe"      # 真散场：硬证据
        clock.advance(5)
        _sample()
        check("★ 换前台（硬闸命中）→ 立刻散场，不用等一分钟空票攒够",
              seen[-1] == "" and monitor.scene == "", str(seen))

        # ---------- ④ 非视频前台端到端 ----------

        section("④ 非视频前台端到端：一整分钟也不飘任何影视状态词")

        monitor._reader = FakeReader("别过去那里有血")     # 字幕里明明有恐怖词
        monitor._grabber = FakeGrabber()
        monitor._pids.name_of = lambda pid: "explorer.exe"
        mon.is_fullscreen_foreground = lambda: True
        monitor._forget_scene()
        companion._last_spoken.clear()
        lines: list[str] = []
        app.bus.pet_speak_proactive.connect(lines.append)
        try:
            for _ in range(13):
                clock.advance(5)
                _sample()
        finally:
            app.bus.pet_speak_proactive.disconnect(lines.append)
        check("资源管理器前台：一分钟一票都没投（硬闸挡在采样之前）",
              len(monitor._voter) == 0, str(len(monitor._voter)))
        check("一句影视状态词都没冒", lines == [], str(lines))
        check("场景也一直是空的", monitor.scene == "", repr(monitor.scene))

        # ---------- ⑤ 状态词常驻头顶 ----------

        section("⑤ 状态词：怒/笑常驻头顶，恐惧轻反应（v2-mood-fear-tune）")

        from core import bootstrap as boot_mod
        from ui.bubble import SHOW_MS

        check("状态词时长常量 = 0（常驻）",
              boot_mod.SCENE_STATUS_PERSIST_MS == 0, str(boot_mod.SCENE_STATUS_PERSIST_MS))
        check("常驻只剩怒/笑两态（恐惧已摘出去，v2-mood-fear-tune）",
              set(boot_mod._SCENE_STATUS) == {SCENE_ANGRY, SCENE_FESTIVE},
              str(sorted(boot_mod._SCENE_STATUS)))

        bubble.hide_bubble()
        app.pet_window._speech_until = 0.0
        monitor._scene = ""
        app.bus.pet_speak_proactive.emit("记得喝水")
        pump(0.05)
        check("非场景的话照旧用默认时长（只给影视状态词加常驻）",
              bubble._hide_timer.interval() == SHOW_MS, str(bubble._hide_timer.interval()))

        for scene, line in (
            (SCENE_ANGRY, "太气人，真想捶他几下"),
            (SCENE_FESTIVE, "好开心"),
        ):
            bubble.hide_bubble()
            app.pet_window._speech_until = 0.0
            monitor._scene = scene
            app.bus.pet_speak_proactive.emit(line)
            pump(0.05)
            check(f"{scene} 的状态词「{line}」→ 常驻头顶（不排自动淡出）",
                  bubble.isVisible() and not bubble._hide_timer.isActive(),
                  f"visible={bubble.isVisible()} timer={bubble._hide_timer.isActive()}")

        # 恐惧摘出常驻（v2-mood-fear-tune）：那句「我怕怕…」照说，但排自动淡出（轻反应）
        bubble.hide_bubble()
        app.pet_window._speech_until = 0.0
        monitor._scene = SCENE_HORROR
        app.bus.pet_speak_proactive.emit("我怕怕…")
        pump(0.05)
        check("horror 的状态词「我怕怕…」→ 轻反应，排自动淡出（不常驻，v2-mood-fear-tune）",
              bubble.isVisible() and bubble._hide_timer.isActive(),
              f"visible={bubble.isVisible()} timer={bubble._hide_timer.isActive()}")
        monitor._scene = ""

        # ---------- ⑥ 愤怒↔笑容校准 ----------

        section("⑥ 愤怒↔笑容校准：质心距离量得出来，阈值一个数没动")

        check("没原型时返回 -1（当没量到，不瞎报）", mood_cv.proto_separation(None) == -1.0, "")
        check("缺某一态也返回 -1",
              mood_cv.proto_separation({SCENE_ANGRY: np.ones(3)}) == -1.0, "")
        orth = {SCENE_ANGRY: np.array([1.0, 0.0, 0.0]),
                SCENE_FESTIVE: np.array([0.0, 1.0, 0.0])}
        check("正交两态 → 距离 1.0（完全分得开）",
              abs(mood_cv.proto_separation(orth) - 1.0) < 1e-6,
              f"{mood_cv.proto_separation(orth):.4f}")
        same = {SCENE_ANGRY: np.array([1.0, 2.0, 3.0]),
                SCENE_FESTIVE: np.array([2.0, 4.0, 6.0])}
        check("同向两态 → 距离 0.0（贴在一起，这就是要报警的那种）",
              abs(mood_cv.proto_separation(same)) < 1e-6,
              f"{mood_cv.proto_separation(same):.4f}")

        check("阈值一个数没动：PROTO_CONF 0.76 / PROTO_MARGIN 0.03 / 温度 20.0",
              (mood_cv.PROTO_CONF, mood_cv.PROTO_MARGIN, mood_cv.PROTO_TEMP) == (0.76, 0.03, 20.0),
              f"{mood_cv.PROTO_CONF} / {mood_cv.PROTO_MARGIN} / {mood_cv.PROTO_TEMP}")
        check("零-shot 那条腿的阈值也没动（0.70）",
              mood_cv.DEFAULT_MIN_SCORE == 0.70, str(mood_cv.DEFAULT_MIN_SCORE))

        # ---------- ⑥b 按态分 margin（v2-mood-face-crop） ----------

        section("⑥b 按态分 margin：门按冠军态取，不是三态一刀切")

        check("分态表就是 273 张留一法扫出来那三个数（哭泣那一格随整态删除，一个数没动）",
              mood_cv.PROTO_MARGIN_BY_STATE == {SCENE_FESTIVE: 0.015,
                                                SCENE_ANGRY: 0.025, SCENE_HORROR: 0.03},
              str(mood_cv.PROTO_MARGIN_BY_STATE))
        check("分态表里没有哭泣那一格（留着它就是一个永远当不上冠军的挂起配置）",
              "cry" not in mood_cv.PROTO_MARGIN_BY_STATE, str(sorted(mood_cv.PROTO_MARGIN_BY_STATE)))
        check("没列出来的态回落默认 PROTO_MARGIN（不留窟窿）",
              mood_cv.margin_for("没这一态") == mood_cv.PROTO_MARGIN, str(mood_cv.margin_for("没这一态")))
        check("笑容的门比恐惧松（它的最近邻在 0.1029 外，0.03 对它形同虚设）",
              mood_cv.margin_for(SCENE_FESTIVE) < mood_cv.margin_for(SCENE_HORROR),
              f"笑{mood_cv.margin_for(SCENE_FESTIVE)} / 恐{mood_cv.margin_for(SCENE_HORROR)}")
        check("指令原拟的「恐 0.04」没被采纳（实测把恐惧弃权率顶高 15pp）",
              mood_cv.margin_for(SCENE_HORROR) < 0.04, str(mood_cv.margin_for(SCENE_HORROR)))

        import math as _math

        def _pair(top, second, lead):
            """造一组原型：`top` 指向 e0，`second` 与它余弦差正好 lead，剩下那一态正交。"""
            th = _math.acos(1.0 - lead)
            protos = {top: np.array([1.0, 0.0, 0.0, 0.0]),
                      second: np.array([_math.cos(th), _math.sin(th), 0.0, 0.0])}
            for s in (SCENE_FESTIVE, SCENE_ANGRY, SCENE_HORROR):
                protos.setdefault(s, np.array([0.0, 0.0, 0.0, 1.0]))
            return protos

        _look = np.array([1.0, 0.0, 0.0, 0.0])
        same_lead = 0.02
        got_festive = mood_cv.proto_verdict(_look, _pair(SCENE_FESTIVE, SCENE_HORROR, same_lead))
        got_horror = mood_cv.proto_verdict(_look, _pair(SCENE_HORROR, SCENE_FESTIVE, same_lead))
        check("同样领先 0.02：冠军是笑容 → 开口（0.02 ≥ 0.015）",
              got_festive is not None and got_festive.scene == SCENE_FESTIVE,
              str(got_festive.scene if got_festive else None))
        check("同样领先 0.02：冠军是恐惧 → 不开口（0.02 < 0.03）",
              got_horror is None, str(got_horror))
        check("领先 0.026 的愤怒开口（0.026 ≥ 0.025）、0.02 的愤怒不开口",
              mood_cv.proto_verdict(_look, _pair(SCENE_ANGRY, SCENE_HORROR, 0.026)) is not None
              and mood_cv.proto_verdict(_look, _pair(SCENE_ANGRY, SCENE_HORROR, 0.02)) is None, "")
        check("够像但谁都不领先的帧照样弃权（门再松也不放行）",
              mood_cv.proto_verdict(_look, _pair(SCENE_FESTIVE, SCENE_HORROR, 0.005)) is None, "")


        real = mood_cv.load_prototypes(MODELS_DIR)
        if real is None:
            skip("愤怒↔笑容质心距离实测",
                 f"{MODELS_DIR} 下没建过 {mood_cv.PROTO_FILENAME}（gitignore，不进仓库）"
                 "——本机用样例图建的留一法实测值写在收尾报告第八十五节")
        else:
            sep = mood_cv.proto_separation(real)
            check(f"愤怒↔笑容质心距离 {sep:.4f} 大于 PROTO_MARGIN {mood_cv.PROTO_MARGIN}"
                  "（小了就该重挑样例，而不是去动阈值）",
                  sep > mood_cv.PROTO_MARGIN, f"{sep:.4f}")

        # ---------- ⑦ 台词池 ----------

        section("⑦ 台词池：玄幻咒语清掉，三态定稿第一句没动（哭泣整池已删）")

        banned = ("太上老君", "急急如律令", "剑来", "天地无极", "乾坤")
        leftovers = [line for pool in SCENE_LINES.values() for line in pool
                     if any(word in line for word in banned)]
        check("所有池子里都没有玄幻咒语", leftovers == [], str(leftovers))
        check("恐怖池第一句仍是定稿那句「我怕怕…」",
              SCENE_LINES[SCENE_HORROR][0] == "我怕怕…", SCENE_LINES[SCENE_HORROR][0])
        check("愤怒/笑容的状态词就是用户定稿那两句",
              SCENE_LINES[SCENE_ANGRY][0] == "太气人，真想捶他几下"
              and SCENE_LINES[SCENE_FESTIVE][0] == "好开心", "")
        check("三态池子都在、一态都没少（哭泣那一池随整态删除）",
              set(SCENE_LINES) == {SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE},
              str(sorted(SCENE_LINES)))

        # ---------- ⑧ 三态定案率（按阶段一实测分布） ----------

        section("⑧ 三态定案率回归锁：按阶段一实测的逐帧分布，窗口该定得下来")

        # 分布是**实测**的（收尾报告第八十六节·阶段一，留一法 217 张）：
        # 每态一张帧出一张票的概率 = 判对自己 / 判成别人 / 弃权。这里把它当成
        # "一段戏里随便挑 N 拍"的抽样分布，跑真窗口规则，看有没有被弃权票拖垮。
        # v2-mood-tri：哭泣那一行整行删除（它不再是窗口里可能出现的票）。
        #
        # v2-mood-window：**这段的尺度从一个窗口（13 拍）改成一整段戏（36 拍 / 3 分钟）**。
        # 不是把标准放松，是原来那个尺度在加了首弹门槛之后**不再是要验的东西**了：
        # 门槛的要求就是"一个窗口攒不够 8 张有定论的票、再多攒一趟"，所以"一个窗口内
        # 必弹"跟门槛本身是矛盾的。换成一段戏正是用户要的口径——三分钟的戏总得定得
        # 下来，而 13 拍那个尺度下愤怒只有 72.9%、恐怖只有 55.4%（实测仿真），
        # 那正是"门槛没够，还在攒"，不是"判不出来"。**生产参数一条没改**（不传任何
        # 门槛参数 = 用默认值），所以这条锁盯的仍是真窗口规则。
        SEGMENT_TICKS = 36           # 0/5/…/175 秒：3 分钟的一段戏
        measured = {
            SCENE_FESTIVE: {SCENE_FESTIVE: 81, None: 18},
            SCENE_ANGRY: {SCENE_ANGRY: 28, SCENE_HORROR: 3, None: 16},
            SCENE_HORROR: {SCENE_HORROR: 19, None: 13},
        }
        import random as _random

        rng = _random.Random(20261004)
        for scene, bucket in measured.items():
            pool = [k for k, n in bucket.items() for _ in range(n)]
            right = neither = wrong = 0
            firsts: list[float] = []
            trials = 2000
            for _ in range(trials):
                clock_box = [0.0]
                vv = SceneVoter(60.0, now=lambda clock_box=clock_box: clock_box[0])
                out = None
                first = None
                for i in range(SEGMENT_TICKS):      # 一段戏：每拍 5 秒
                    clock_box[0] = i * 5.0
                    out = vv.note(rng.choice(pool))
                    if first is None and out is not None:
                        first = clock_box[0]
                if first is not None:
                    firsts.append(first)
                if out is None:
                    neither += 1
                elif out == scene:
                    right += 1
                else:
                    wrong += 1
            check(f"{SCENE_TITLES.get(scene, scene)}：一段戏定案率 {right / trials:.2%}"
                  f"（弃权票不再稀释）",
                  right / trials >= 0.95, f"定案 {right} / 不定案 {neither} / 定成别的 {wrong}")
            check(f"{SCENE_TITLES.get(scene, scene)}：定成别的情绪 ≈ 0",
                  wrong / trials <= 0.01, f"{wrong}/{trials}")
            avg_first = sum(firsts) / len(firsts) if firsts else 0.0
            check(f"{SCENE_TITLES.get(scene, scene)}：首弹不早于一个满窗口（{avg_first:.0f} 秒）"
                  f"——窗口那份 45~60 秒的耐心没被门槛弄丢",
                  firsts and min(firsts) >= 60.0, f"最早 {min(firsts) if firsts else '-'} 秒")

        # 反向对照：把同样的分布放回**旧口径**（弃权也投一张空票）会怎样——
        # 断言它确实差得多，免得这条回归锁变成恒真的。
        from collections import Counter as _Counter

        old_style = {}
        for scene, bucket in measured.items():
            pool = [("" if k is None else k) for k, n in bucket.items() for _ in range(n)]
            rng_old = _random.Random(7)
            right = 0
            for _ in range(2000):
                c = _Counter(rng_old.choice(pool) for _ in range(13))
                top = max(c.values())
                win = [k for k, n in c.items() if n == top]
                right += 1 if len(win) == 1 and win[0] == scene else 0
            old_style[scene] = right / 2000
        check("旧口径（空票进窗口）在两个态上明显更低——这才说明这一轮改动真在起作用",
              old_style[SCENE_ANGRY] < 0.95 and old_style[SCENE_HORROR] < 0.95,
              " / ".join(f"{SCENE_TITLES.get(k, k)} {v:.0%}" for k, v in old_style.items()))

        # ---------- ⑧b 融合层双保险：已删除的态连投票窗都进不去 ----------

        section("⑧b 空转占窗：已删除的态在进投票窗前就被丢掉（v2-mood-tri 融合层双保险）")

        use_scene_vote(monitor, window=0.0)   # 一拍即定：有毒的票要是进了票箱，立刻会定案
        monitor._forget_scene()
        for _ in range(13):
            monitor._count_scene("cry")
        check("13 拍哭泣票 → 票箱里一张都没进（不空转占窗）",
              len(monitor._voter) == 0, f"票箱里有 {len(monitor._voter)} 票")
        check("也一次都没定案、没广播（散场不该被它搅出来）",
              monitor.scene == "", repr(monitor.scene))
        # 反向对照：同一条路上喂一个**真态**，票立刻就进、立刻就定案——
        # 证明上一条不是"这条路整个坏了"。
        monitor._count_scene(SCENE_FESTIVE)
        check("反向对照：喂一个真态照旧一票即定",
              len(monitor._voter) == 1 and monitor.scene == SCENE_FESTIVE,
              f"{len(monitor._voter)} 票 / {monitor.scene!r}")
        monitor._forget_scene()

        # ---------- ⑨ 红线 ----------

        section("⑨ 红线：两个库零写入 + 零新依赖")

        check("pet.db 的 utterance 一行没多",
              _rows(db_path, "utterance") == before_utt,
              f"{before_utt} → {_rows(db_path, 'utterance')}")
        check("mind.db 一条碎片都没写",
              _rows(mind_path, "fragment") == before_mind,
              f"{before_mind} → {_rows(mind_path, 'fragment')}")
        vote_imports = set(_imports(ROOT / "life" / "screen" / "vote.py"))
        # 「零新依赖」= 不新增第三方包；`life` 是同库的 scenes 叶子常量模块，不算新依赖。
        check("vote.py 不 import 第三方包（零新依赖；只多了同库 scenes 叶子）",
              vote_imports <= {"__future__", "time", "collections", "typing", "life"},
              str(sorted(vote_imports)))
        check("多数票纯内存：vote.py 里没有任何落盘入口（open/write/sqlite）",
              not any(word in (ROOT / "life" / "screen" / "vote.py").read_text(encoding="utf-8")
                      for word in ("open(", "sqlite3", "write_bytes", "write_text")), "")

        drain_threads()
    finally:
        box.cleanup()
    return summary("影视情绪一分钟多数票（弃权票不稀释）+ 状态词≥1分钟 + 非视频闸死"
                   " + 散场靠硬闸（v2-mood-detect 再审·终 / v2-mood-recall-fix）")


if __name__ == "__main__":
    raise SystemExit(main())
