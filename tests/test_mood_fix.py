"""
精细情绪修正（v2-mood-fix）的自动测试：用户实测报的三个问题，一条一条验。

1. **说明/解说视频被判成搞笑。** 根因：搞笑那三个词（笑/搞/逗）全是日常单字，解说词里
   随手一句"他搞了半天也没弄明白"就一个"搞"，一票就够判。v2-mood-detect 修正版按
   定稿把幽默这一档**整档删除**（搞笑判别押后迭代、由笑容替代），这一段跟着改成验
   "幽默已删、解说词一个字都不冒"；恐怖那一档照旧保住一票（用户同时报了漏判）。
2. **恐怖血腥漏判。** 两条腿各有一处：OCR 那边"尸体""谋杀"这种整句不带单字关键词的
   原来整条漏掉（补了无歧义多字词）；CV 那边更狠 —— 挑屏看的是**光标**在哪，
   "电影放在左边那块屏、鼠标搁在主屏"就把整帧丢成 None，那条腿等于没开。
   现在两边都改成按**窗口在哪块屏**挑。
3. **看电影场景关语音只表情。** 原来 `_on_pet_speak` 唯一的静音条件是危机话术，
   电影演着宠物照样念。现在屏幕上真有场景成立时只显示不出声，换掉前台自动恢复。

三处都能在离线环境验：选屏规则抽成了纯函数（`pick_screen_index`），
用合成屏幕几何跑，不依赖本机真有几块屏。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor

from harness import Recorder, check, drain_threads, make_env, pump, section, summary

from life.psych.crisis import CRISIS_REPLY
from life.screen.monitor import SCENE_KEYWORDS, SCENE_MIN_HITS, classify_text
from life.screen.scenes import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
    SCENE_ORDER,
)
from life.screen.shot import pick_screen_index

# 合成两台显示器：主屏在 (0,0)，副屏挂在它左边
PRIMARY = QRect(0, 0, 1920, 1080)
LEFT = QRect(-1920, 0, 1920, 1080)
GEO = [PRIMARY, LEFT]

# 用户在说明/解说视频里随手能撞上的句子（都带过"搞笑单字"，原来一票就判幽默）
NARRATION = [
    "他搞了半天也没弄明白这个原理",
    "这个方法我一说你就笑了",
    "上面这段讲解我搞了好久才明白",
    "这个搞法我没见过，你先讲讲思路",
]


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    try:
        # ---------- 问题一：说明视频误判搞笑（v2-mood-detect 之后幽默整档删除） ----------

        section("S1 问题一：幽默这一档已删，说明/解说视频不再触发任何场景")
        for line in NARRATION:
            check(f"{line!r} → 不触发场景", classify_text(line) is None,
                  str(classify_text(line)))

        section("S1 幽默已从关键词表和票数表里移除")
        check("SCENE_MIN_HITS 里没有幽默", "funny" not in SCENE_MIN_HITS, str(SCENE_MIN_HITS))
        check("SCENE_KEYWORDS 里没有幽默", "funny" not in SCENE_KEYWORDS, str(SCENE_KEYWORDS))
        check("恐怖保住一票（用户同时报了漏判，这档绝不能收紧）",
              SCENE_MIN_HITS[SCENE_HORROR] == 1, str(SCENE_MIN_HITS[SCENE_HORROR]))
        check("愤怒字幕一票", SCENE_MIN_HITS[SCENE_ANGRY] == 1, "")
        check("笑容一票", SCENE_MIN_HITS[SCENE_FESTIVE] == 1, "")
        # v2-mood-tri：哭泣整档删除之后，这里原来那条"哭泣一票"跟着没了
        # （不是改成别的数，是那一格不存在了）。
        check("哭泣已经整档删除（票数表与关键词表里都没有它）",
              "cry" not in SCENE_MIN_HITS and "cry" not in SCENE_KEYWORDS,
              str(sorted(SCENE_MIN_HITS)))

        section("S1 原来那张表里剩的四条判例一条不落，并列恐怖优先也没变")
        for line, want in (
            ("别过去那里有血", SCENE_HORROR),
            ("有鬼啊快跑", SCENE_HORROR),
            ("你凭什么骂人", SCENE_ANGRY),
            ("恭喜恭喜新婚快乐", SCENE_FESTIVE),
        ):
            check(f"{line!r} → {want}", classify_text(line) == want, str(classify_text(line)))
        check("并列时恐怖优先（恐怖一票、笑容一票 → 恐怖）",
              classify_text("有鬼还恭喜") == SCENE_HORROR,
              str(classify_text("有鬼还恭喜")))
        check("没有命中仍然不触发",
              classify_text("今天天气不错我们出去走走") is None, "")
        check("空串仍然不触发", classify_text("") is None, "")

        # ---------- 问题二之一：OCR 补无歧义恐怖词 ----------

        section("S2 问题二（OCR 腿）：整句不带单字关键词的恐怖句，现在也认得出")
        check("「尸体」「谋杀」这类词真的进了表",
              {"尸体", "谋杀", "血腥", "割喉"} <= set(SCENE_KEYWORDS[SCENE_HORROR]),
              str(SCENE_KEYWORDS[SCENE_HORROR]))
        for line in ("地上躺着一具尸体", "这是一起谋杀案", "现场一片血腥",
                     "他被割喉了", "画面里全是残肢"):
            check(f"{line!r} → 恐怖", classify_text(line) == SCENE_HORROR,
                  str(classify_text(line)))
        check("「死」这个字没进表（会误伤「笑死我了」「死机」）",
              "死" not in SCENE_KEYWORDS[SCENE_HORROR], "")
        check('所以「笑死我了」不会被判成恐怖', classify_text("笑死我了他") != SCENE_HORROR,
              str(classify_text("笑死我了他")))

        # ---------- 问题二之二：挑屏按窗口，不按光标 ----------

        section("S2 问题二（CV 腿）：挑屏看窗口在哪块屏，不看光标在哪块屏")
        check("窗口在主屏 → 用主屏", pick_screen_index(QRect(100, 100, 1280, 720), GEO) == 0, "")
        check("窗口在左边那块屏、光标在主屏 → 用左屏（修前这里整帧丢掉）",
              pick_screen_index(QRect(-1800, 100, 1280, 720), GEO) == 1, "")
        check("窗口横跨两屏 → 取交叠大的那块（中心落哪算哪）",
              pick_screen_index(QRect(-400, 100, 1280, 720), GEO) == 0, "")
        check("窗口完全在屏外 → 给 0，不抛异常",
              pick_screen_index(QRect(9000, 9000, 100, 100), GEO) == 0, "")
        check("一块屏都没有 → 给 0，不抛异常", pick_screen_index(QRect(0, 0, 10, 10), []) == 0, "")

        section("S2 挑屏只有一处规则：字幕带那条腿也走同一个函数")
        from life.screen import monitor as monitor_mod
        from life.screen import shot as shot_mod

        src_shot = inspect.getsource(shot_mod)
        # 光看源码文本会误判：改动的**说明**里就写着当年那句
        # `QGuiApplication.screenAt(QCursor.pos())`，所以查名字必须走 AST。
        used = {n.id for n in ast.walk(ast.parse(src_shot)) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(ast.parse(src_shot)) if isinstance(n, ast.Attribute)}
        check("shot 里抓屏统一走 grab_screen_for", src_shot.count("def grab_screen_for(") == 1, "")
        check("shot 的代码里不再出现光标（只在注释里留着当年为什么改）",
              "QCursor" not in used, str(sorted(n for n in used if "Cursor" in n)))
        check("shot 里不再按光标挑屏（screenAt 也没了）",
              "screenAt" not in used, "")
        # v2-social-ocr-isolate 起，涂白这块由"只涂 pet_rect"扩成"pet_rect + extra_masks
        # 走同一条换算"（助手窗 / 气泡 / 候选框也悬在聊天窗上，一样得涂）。断言的**不变量**
        # 没变：减的是 `rect` 的左上角（窗口原点），不是已经减过屏幕原点的 local——
        # 所以这里按新的那句话验，并且回头确认 pet_rect 确实进了同一个循环。
        check("宠物涂白的坐标换算以窗口左上角为原点（副屏上不再偏一个屏幕原点）",
              "mask.translated(-rect.left(), -rect.top())" in src_shot
              and "masks.append(pet_rect)" in src_shot, "")

        src_mon = inspect.getsource(monitor_mod)
        check("字幕带那条腿的主路径按前台窗口挑屏",
              "rect = shot.foreground_window_rect()" in src_mon
              and "shot.grab_screen_for(" in src_mon, "")
        check("光标只剩在取不到前台窗口时的兜底分支里（排在主路径后面）",
              src_mon.index("rect = shot.foreground_window_rect()")
              < src_mon.index("QCursor.pos()"), "")

        section("S2 同一条规则真跑一遍：窗口多大就裁多大，宠物那块涂白对得上")
        img = shot_mod.grab_foreground_window(None, QRect(100, 100, 400, 300))
        check("抓得到，且尺寸就是窗口尺寸（整帧不会再丢）",
              img is not None and img.width() == 400 and img.height() == 300,
              "None" if img is None else f"{img.width()}x{img.height()}")
        img = shot_mod.grab_foreground_window(QRect(110, 110, 50, 50), QRect(100, 100, 400, 300))
        check("宠物在裁图里的位置换算对了：全局 (110,110) → 裁图 (10,10) 被涂白",
              img is not None and img.pixelColor(10, 10) == QColor(255, 255, 255),
              "None" if img is None else str(img.pixelColor(10, 10).name()))
        check("宠物整只在窗口外也不炸（涂白区域会被裁掉）",
              shot_mod.grab_foreground_window(QRect(5000, 5000, 50, 50),
                                              QRect(100, 100, 400, 300)) is not None, "")
        check("窗口矩形为空时给 None", shot_mod.grab_foreground_window(None, QRect()) is None, "")
        band = monitor_mod.grab_subtitle_band()
        check("字幕带那条腿同样跑得通", band is not None and band.width() > 0,
              "None" if band is None else f"{band.width()}x{band.height()}")

        # ---------- 问题三：看电影只表情不出声 ----------

        # v2-chat-core #2 之后口径变了：这三条原来量的是"什么时候**不**念"（对照是
        # "没戏在演时照旧念 1 次"）。现在任何一条说话线都不出声，"看片静音"这条闸
        # 已经没有可挡的东西了——断言跟着塌成"不管在不在看片，一律 0 次"。留着它们
        # 是为了将来谁把出声接回来时，这几条会立刻变红提醒他这里还有一道静音闸。
        section("S3 问题三：屏幕上正有戏在演 → 只表情、不出声（现在一律不出声）")
        spoken = Recorder(app.voice, "say")
        line = "这段我不敢看了，捂眼睛的那种(>_<)"
        try:
            app.screen_monitor._scene = ""
            app.pet_window._bubble.hide_bubble()
            app.pet_window._speech_until = 0.0
            app.bus.pet_speak.emit(line)
            pump(0.05)
            check("没在放视频时也不念（v2-chat-core #2：文字回复一律不出声）",
                  len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
            check("文字照旧显示",
                  app.pet_window._bubble.isVisible()
                  and app.pet_window._bubble._label.text() == line,
                  repr(app.pet_window._bubble._label.text()))

            app.screen_monitor._scene = SCENE_HORROR
            spoken.calls.clear()
            app.pet_window._bubble.hide_bubble()
            app.pet_window._speech_until = 0.0
            app.bus.pet_speak.emit(line)
            pump(0.05)
            check("电影演着也不念", len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
            check("但表情还在：气泡照样显示",
                  app.pet_window._bubble.isVisible()
                  and app.pet_window._bubble._label.text() == line,
                  repr(app.pet_window._bubble._label.text()))
        finally:
            spoken.restore()

        section("S3 换个前台就自动散场：出的还只是文字，不是声音")
        spoken = Recorder(app.voice, "say")
        try:
            app.screen_monitor._forget_scene()      # 用户切走 / 切回宠物窗口
            app.pet_window._bubble.hide_bubble()
            app.pet_window._speech_until = 0.0
            app.bus.pet_speak.emit(line)
            pump(0.05)
            check("场景散掉之后也没出声", len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
            check("文字照旧显示", app.pet_window._bubble._label.text() == line, "")
        finally:
            spoken.restore()

        section("S3 开关关掉 → 那个开关已经管不到出声了")
        app.screen_monitor._scene = SCENE_HORROR
        app.config.screen.mute_voice_while_watching = False
        spoken = Recorder(app.voice, "say")
        try:
            app.pet_window._bubble.hide_bubble()
            app.pet_window._speech_until = 0.0
            app.bus.pet_speak.emit(line)
            pump(0.05)
            check("开关关掉也一样不出声", len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
        finally:
            spoken.restore()
            app.config.screen.mute_voice_while_watching = True
            app.screen_monitor._scene = ""

        section("S3 红线：危机话术照旧不念，且这道新静音没能把它绕过去")
        app.screen_monitor._scene = SCENE_HORROR
        spoken = Recorder(app.voice, "say")
        try:
            app.pet_window._bubble.hide_bubble()
            app.pet_window._speech_until = 0.0
            app.bus.pet_speak.emit(CRISIS_REPLY)
            pump(0.05)
            check("危机话术 0 次（危机那条判据先于新条件生效）",
                  len(spoken.calls) == 0, f"{len(spoken.calls)} 次")
            check("文字照旧显示出来", app.pet_window._bubble.isVisible(), "")
        finally:
            spoken.restore()
            app.screen_monitor._scene = ""

        # ---------- 红线 ----------

        section("S4 红线：这一轮的改动没往库里写东西、没新增依赖")
        before = _rows(app.config.db_path, "utterance")
        fragments = app.mind_store.fragment_count()
        app.screen_monitor._scene = SCENE_HORROR
        app.bus.pet_speak.emit("红线检查用的这一句")
        pump(0.05)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == before,
              f"{before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == fragments, "")
        app.screen_monitor._scene = ""
        check("判场景这条线是三态、顺序没动（幽默已删、哭泣 v2-mood-tri 已删）",
              SCENE_ORDER == (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE),
              str(SCENE_ORDER))
    finally:
        drain_threads()
        box.cleanup()
    return summary("精细情绪修正（说明视频误判 / 恐怖漏判 / 看电影只表情）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
