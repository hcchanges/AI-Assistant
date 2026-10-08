"""
愤怒反应不乱出 + 改文案（v2-mood-anger）的自动测试。

用户真机报的两个问题：**聊着红烧肉/手艺被弹了一句愤怒共情**（那句"这也能气着？来，
我陪你骂两句"），以及那句文案本身听着像撺掇。

查下来是观影那条腿漏过来的，两个入口：

1. **愤怒关键词太松**：`SCENE_KEYWORDS[SCENE_ANGRY]` 原来只有三个单字 `("怒","骂","战")`，
   一票就判。"战"在"挑战/观战/备战"里到处都是，"怒"在"怒放"里也有——全屏的办公页、
   网页、聊天窗里随手撞上一个字就冒愤怒共情。
2. **宠物认自己的字**：宠物自己的窗口（气泡里写着它以前说过的话）被全屏兜底那条腿
   当成了播放器，OCR 起自家文本来，判出场景再冒，越冒越像。

三条对应用户任务书 S4：
① 聊天说红烧肉/手艺 → 不弹愤怒；
② 明确愤怒文本 → 弹定稿那句「太气人，真想捶他几下」；
③ 非观影场景不乱出观影/愤怒反应（游戏/专注/空白屏/自家窗口都不出）。

观影场景仍按 v2-mood-detect（真观影才触发，不回归）——恐怖/哭泣/笑容三档判据与
连击一字未动，只动了愤怒这一档的**关键词表**与那句文案。

红线：pet.db / mind.db 零写入、零新依赖。
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QImage

from harness import check, drain_threads, make_env, pump, section, summary, use_scene_vote, wait_for

from life.screen import mood_cv
from life.screen import monitor as mon
from life.screen.companion import SCENE_LINES
from life.screen.monitor import SCENE_KEYWORDS, SCENE_MIN_HITS, classify_text
from life.screen.scene_guard import KIND_FOCUS
from life.screen.scenes import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
    SCENE_ORDER,
)

ROOT = Path(__file__).resolve().parent.parent

# 用户真机那一幕：在跟宠物聊红烧肉/手艺，没在看片、没在打游戏
CHATTY_TEXTS = (
    "我今晚做红烧肉，手艺还行吧",
    "这家的红烧肉炖得烂，我照着菜谱学的",
    "今天在家练手艺，做了三个菜",
    "今日待办：整理会议纪要",
)

# 原来单字一票就判，现在一个都不该过
OLD_SINGLE_CHARS = (
    "挑战全网最快通关",
    "先观战两局再说",
    "备战双十一，先把购物车清一清",
    "怒放的生命，这歌真上头",
    "这画骂得有点狠",
    "讨论一下下一步怎么走",
)

# 明确的愤怒表达：词面上就是发火
ANGRY_TEXTS = (
    "气死我了这人也太气人了",
    "你凭什么骂人",
    "我真的很生气，火大",
    "这人怎么这么恼火，气炸了",
)


def _blank_image(w: int = 640, h: int = 200) -> QImage:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    return img


class _TextReader:
    """假 OCR：永远读回同一句。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假截屏：记自己被叫了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None) -> QImage:
        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


def _fake_foreground(monitor, proc_holder, pid=1) -> None:
    from monitor import foreground

    foreground.foreground_pid = lambda: pid
    monitor._pids.name_of = lambda value: proc_holder["proc"]


def _sample(monitor, rounds: int = 1, timeout: float = 5.0) -> None:
    for _ in range(rounds):
        monitor.sample()
        wait_for(lambda: not monitor._reading, timeout=timeout)
        pump(0.02)


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    # ---------- ① 单字误判已堵 ----------

    section("① 非观影场景：聊红烧肉/手艺不再弹愤怒")

    for line in CHATTY_TEXTS:
        check(f"{line!r} → 不触发任何场景", classify_text(line) is None,
              str(classify_text(line)))

    check("「战」这个单字已经移出愤怒表",
          "战" not in SCENE_KEYWORDS[SCENE_ANGRY], str(SCENE_KEYWORDS[SCENE_ANGRY]))
    check("「怒」「骂」两个单字也移出了",
          "怒" not in SCENE_KEYWORDS[SCENE_ANGRY] and "骂" not in SCENE_KEYWORDS[SCENE_ANGRY],
          str(SCENE_KEYWORDS[SCENE_ANGRY]))
    check("剩下的全是**整词**（长度都 > 1）",
          all(len(word) > 1 for word in SCENE_KEYWORDS[SCENE_ANGRY]),
          str([w for w in SCENE_KEYWORDS[SCENE_ANGRY] if len(w) <= 1]))
    for line in OLD_SINGLE_CHARS:
        check(f"{line!r} → 不触发（原来单字一票就中）", classify_text(line) is None,
              str(classify_text(line)))

    check("恐怖那一档的单字照旧保留（两档风险不对等，见注释）",
          set(SCENE_KEYWORDS[SCENE_HORROR]) >= {"鬼", "血", "杀"},
          str(SCENE_KEYWORDS[SCENE_HORROR]))

    # ---------- ② 明确愤怒照旧触发 + 文案已改 ----------

    section("② 明确的愤怒表达照旧触发")

    for line in ANGRY_TEXTS:
        check(f"{line!r} → 愤怒", classify_text(line) == SCENE_ANGRY,
              str(classify_text(line)))
    check("「你凭什么骂人」仍认（整词「骂人」保住了）",
          classify_text("你凭什么骂人") == SCENE_ANGRY, "")
    check("愤怒仍只要一票（阈值没动）", SCENE_MIN_HITS[SCENE_ANGRY] == 1,
          str(SCENE_MIN_HITS[SCENE_ANGRY]))

    section("② 文案换成定稿那句")

    check("愤怒池第一句就是「太气人，真想捶他几下」",
          SCENE_LINES[SCENE_ANGRY][0] == "太气人，真想捶他几下",
          str(SCENE_LINES[SCENE_ANGRY][0]))
    check("旧那句「这也能气着？来，我陪你骂两句」已从所有池子里删干净",
          all("这也能气着" not in line and "陪你骂" not in line
              for pool in SCENE_LINES.values() for line in pool), "")
    check("三态池子都还在、一态都没少（v2-mood-tri 之后哭泣那一池整池删除）",
          set(SCENE_LINES) == {SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE},
          str(sorted(SCENE_LINES)))

    box, stub, app = make_env()
    try:
        monitor = app.screen_monitor
        companion = app.screen_companion
        pet = app.pet_window
        guard = app.screen_scene_guard
        pet.show()
        pump(0.05)

        app.config.screen.enabled = True
        app.config.screen.ocr_enabled = True
        app.config.screen.companion_enabled = True
        # 这一节验的是"非视频不冒反应"，不看窗口投票——窗口设成 0 秒 = 一拍即定
        use_scene_vote(monitor, window=0.0)

        section("② 真跑一遍：愤怒那一池冒的就是定稿那句")

        pet._bubble.hide_bubble()
        pet._speech_until = 0.0
        companion._last_spoken.clear()
        companion._cursor.clear()
        companion.on_scene(SCENE_ANGRY)
        pump(0.05)
        check("气泡里就是「太气人，真想捶他几下」",
              pet._bubble.isVisible() and pet._bubble._label.text() == "太气人，真想捶他几下",
              repr(pet._bubble._label.text()))

        # ---------- ③ 非观影场景不乱出 ----------

        section("③ 非观影：非视频前台直接闸死，聊红烧肉的那一屏连字都不认")

        proc = {"proc": "explorer.exe"}
        _fake_foreground(monitor, proc)
        reader = _TextReader("我今晚做红烧肉，手艺还行吧")
        monitor._reader = reader
        monitor._grabber = FakeGrabber()
        monitor._cv_grabber = FakeGrabber()
        monitor._mood = None
        mon.is_fullscreen_foreground = lambda: True     # 就算铺满整屏，非视频这一档也闸死

        seen: list[str] = []
        monitor.scene_changed.connect(seen.append)
        pet._bubble.hide_bubble()
        companion._last_spoken.clear()
        _sample(monitor, rounds=2)
        check("非视频前台（资源管理器）→ 一次 OCR 都不跑（硬闸，v2-mood-detect 再审·终）",
              reader.calls == 0, f"{reader.calls} 次")
        check("一个场景都没成立（愤怒没冒出来）",
              SCENE_ANGRY not in seen and monitor.scene == "",
              f"{seen} / {monitor.scene!r}")
        check("气泡里也没冒出愤怒那句",
              not pet._bubble.isVisible(), repr(pet._bubble._label.text()))

        # 反向对照：换成真播放器前台 → 字照认（闸只挡非视频那一类），红烧肉照样不判愤怒
        proc["proc"] = "potplayer.exe"
        reader.calls = 0
        seen.clear()
        _sample(monitor, rounds=2)
        check("播放器前台 → 字照认（非视频闸没把功能一起关掉）", reader.calls >= 1,
              f"{reader.calls} 次")
        check("认了字也不冒愤怒（词表不误判）",
              SCENE_ANGRY not in seen and monitor.scene == "",
              f"{seen} / {monitor.scene!r}")

        section("③ 宠物不认自己的字：前台是自己的窗口 → 整拍不采样")

        pet._bubble.hide_bubble()
        companion._last_spoken.clear()
        self_reader = _TextReader("太气人，真想捶他几下")   # 就算写着愤怒那句
        monitor._reader = self_reader
        monitor._grabber = FakeGrabber()
        seen.clear()
        proc["proc"] = "python.exe"
        _fake_foreground(monitor, proc, pid=os.getpid())        # 前台就是我们自己
        monitor.sample()
        pump(0.05)
        check("认出来了：这一拍前台就是我们自己的 pid",
              monitor._fg_pid == os.getpid() and monitor._is_self_foreground() is True,
              f"{monitor._fg_pid} / {os.getpid()}")
        check("自家窗口不留字：一次 OCR 都没发生", self_reader.calls == 0,
              f"{self_reader.calls} 次")
        check("自家窗口也不冒场景", seen == [] and monitor.scene == "", f"{seen}")
        check("pid 取不到（0）时当'不是自己'，不误关采样",
              (setattr(monitor, "_fg_pid", 0), monitor._is_self_foreground() is False)[1], "")

        section("③ 专注中：愤怒反应也不冒（场景互斥）")

        app.screen_focus_timer.reset()
        app.pomodoro_service.timer.reset()
        pump(0.05)
        app.screen_focus_timer.enter("chaoxing.exe")
        pump(0.05)
        check("前置：确实进了专注、守卫也认",
              app._is_focusing() and guard.active == KIND_FOCUS,
              f"{app._is_focusing()} / {guard.active}")
        pet._bubble.hide_bubble()
        companion._last_spoken.clear()
        companion._cursor.clear()
        companion.on_scene(SCENE_ANGRY)
        pump(0.05)
        check("专注中 → 愤怒那句一个字不冒", not pet._bubble.isVisible(),
              repr(pet._bubble._label.text()))
        app.screen_focus_timer.reset()
        app.pomodoro_service.timer.reset()
        pump(0.05)

        # ---------- ④ 回归：观影那条腿一条没动 ----------

        section("④ 回归：恐怖/笑容的判据一字未动，哭泣整态连字幕一并断")

        check("恐怖字幕照旧", classify_text("有鬼啊快跑") == SCENE_HORROR, "")
        check("笑容字幕照旧", classify_text("恭喜恭喜新婚快乐") == SCENE_FESTIVE, "")
        check("哭了半天的字幕一个态都不判（v2-mood-tri：连字幕信号一并断）",
              classify_text("她哭得停不下来") is None,
              str(classify_text("她哭得停不下来")))
        check("连'呜呜咽咽抽泣'也认不出任何态",
              classify_text("他呜咽着抽泣，泪流满面") is None,
              str(classify_text("他呜咽着抽泣，泪流满面")))
        check("三态顺序没动（哭泣那一格已删）",
              SCENE_ORDER == (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE),
              str(SCENE_ORDER))
        check("三态阈值都没动（全是一票）",
              all(SCENE_MIN_HITS[s] == 1 for s in SCENE_ORDER), str(SCENE_MIN_HITS))
        check("关键词表里没有哭泣那一格",
              "cry" not in SCENE_KEYWORDS and "cry" not in SCENE_MIN_HITS,
              str(sorted(SCENE_KEYWORDS)))
        check("愤怒/恐惧的画面原型照旧在（v2-mood-fear 的成果没被这次回归冲掉）",
              SCENE_ANGRY in mood_cv.PROTO_EMOTIONS and SCENE_HORROR in mood_cv.PROTO_EMOTIONS
              and "cry" not in mood_cv.PROTO_EMOTIONS,
              str(mood_cv.PROTO_EMOTIONS))
        check("恐怖/笑容两态台词一条没动",
              SCENE_LINES[SCENE_HORROR][0] == "我怕怕…"
              and SCENE_LINES[SCENE_FESTIVE][0] == "好开心", "")

        # ---------- 红线 ----------

        section("红线：两个库一行不多、零新依赖")

        before_utt = _rows(app.config.db_path, "utterance")
        before_frag = app.mind_store.fragment_count()
        monitor._reader = _TextReader("气死我了这人也")
        proc["proc"] = "potplayer.exe"
        _fake_foreground(monitor, proc)
        _sample(monitor, rounds=2)
        companion.on_scene(SCENE_ANGRY)
        pump(0.2)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == before_utt,
              f"{before_utt} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == before_frag, "")

        allowed = {"__future__", "PySide6", "core", "life", "monitor", "brain", "pet"}
        for name in ("monitor.py", "companion.py"):
            tree = ast.parse((ROOT / "life" / "screen" / name).read_text(encoding="utf-8"))
            mods: set[str] = set()
            # 只看**模块层**的 import：函数体里那些懒加载（OCR/ONNX 引擎）是早就有的，
            # 这一轮新加的只有一个模块层的 `import os`。
            for node in tree.body:
                if isinstance(node, ast.Import):
                    mods |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods.add(node.module.split(".")[0])
            foreign = {m for m in mods
                       if m not in sys.stdlib_module_names and m not in allowed}
            check(f"{name} 没引第三方库（新加的 import 只有标准库 os）",
                  not foreign, str(sorted(foreign)))

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("愤怒反应仅明确文本触发 + 改文案（v2-mood-anger）")


if __name__ == "__main__":
    raise SystemExit(main())
