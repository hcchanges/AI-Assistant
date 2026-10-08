"""
专注静默只显闹钟（v2-focus-quiet）的自动测试。

用户真机报：专注中宠物反复弹「看你进状态了，我陪你计时 25 分钟」（两次）。查下来是
`ScreenFocusTimer.enter()` 里那句 `START_LINE`——进专注就冒一句状态词，而且 `_announced`
在 `reset()` 里被清掉，前台 FOCUS→非FOCUS→FOCUS 一抖，下一轮又冒一遍，等于"同一状态
重复推提示"。

这一轮把进专注的那句开场白**整个摘掉**：进专注只发 `changed`，浮窗亮倒计时（只显闹钟，
不说话）。完成那句 `DONE_LINE` 保留——它是"专注结束、恢复正常陪伴"的边界，不是专注中的
状态词。三条对应用户任务书 S4：

① 进专注 → 无状态词、只显示闹钟；
② 专注中 → 不重复弹提示；
③ 退出专注 → 恢复正常陪伴。

红线：pet.db / mind.db 零写入、零新依赖。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

from life.screen.companion import SCENE_LINES
from life.screen.scenes import SCENE_HORROR


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    try:
        pet = app.pet_window
        pet.show()
        pump(0.05)
        app.config.screen.enabled = True
        app.config.screen.focus_enabled = True
        app.config.screen.companion_enabled = True

        focus = app.screen_focus_timer
        bubble = app.pomodoro_bubble
        companion = app.screen_companion

        spoken: list[str] = []
        app.bus.pet_speak_proactive.connect(spoken.append)

        # ---------- ① 进专注：无状态词、只显示闹钟 ----------

        section("① 进专注 → 无状态词、只显示闹钟")

        focus.reset()
        bubble.hide_bubble()
        spoken.clear()
        focus.enter("chaoxing.exe")
        pump(0.05)
        check("专注跑起来了", focus.running, str(focus.running))
        check("闹钟浮窗挂上了（只显示倒计时）", bubble.isVisible(), str(bubble.isVisible()))
        check("进专注一个字都没冒（状态词已摘）",
              spoken == [], repr(spoken))

        # ---------- ② 专注中：不重复弹提示 ----------

        section("② 专注中 → 不重复弹提示")

        spoken.clear()
        focus.enter("chaoxing.exe")          # 已经跑着，再进一次
        pump(0.05)
        check("同一状态重复 enter → 一个字不冒", spoken == [], repr(spoken))

        # 前台 FOCUS→非FOCUS→FOCUS 一抖（原来 _announced 被清、下一轮又冒一遍）
        spoken.clear()
        focus.leave()                        # 切走 → 回空闲
        pump(0.05)
        focus.enter("chaoxing.exe")          # 切回 → 重新起一轮
        pump(0.05)
        check("切走再切回 → 也只亮闹钟、不重复冒状态词", spoken == [], repr(spoken))

        # ---------- ③ 退出专注：恢复正常陪伴 ----------

        section("③ 退出专注 → 恢复正常陪伴")

        focus.reset()
        pump(0.05)
        check("退出专注后门控解锁（_is_focusing 为假）",
              app._is_focusing() is False, str(app._is_focusing()))
        check("闹钟浮窗收掉了", not bubble.isVisible(), str(bubble.isVisible()))

        # 恢复正常陪伴：不在专注时，剧情反应照常冒（反向对照，证明静默没把说话线关死）
        pet._bubble.hide_bubble()
        pet._speech_until = 0.0
        companion._last_spoken.clear()
        companion._cursor.clear()
        companion.on_scene(SCENE_HORROR)
        pump(0.05)
        check("退出专注 → 陪伴照常开口（恐怖那句冒出来了）",
              pet._bubble.isVisible() and pet._bubble._label.text() == SCENE_LINES[SCENE_HORROR][0],
              repr(pet._bubble._label.text()))

        # 完成那句照旧（它是"专注结束"的边界，不是专注中的状态词）
        spoken.clear()
        focus.enter("chaoxing.exe")
        pump(0.05)
        focus._finish()
        pump(0.05)
        check("专注结束 → 完成那句照旧冒（DONE_LINE 没被误删）",
              len(spoken) == 1 and "抬头喝口水" in spoken[0], repr(spoken))

        # ---------- 红线 ----------

        section("红线：两个库一行不多、零新依赖")

        before_utt = _rows(app.config.db_path, "utterance")
        before_frag = app.mind_store.fragment_count()
        focus.enter("chaoxing.exe")
        pump(0.1)
        focus.leave()
        companion.on_scene(SCENE_HORROR)
        pump(0.2)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == before_utt,
              f"{before_utt} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == before_frag, "")

        focus.reset()
        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("专注静默只显闹钟、去状态词（v2-focus-quiet）")


if __name__ == "__main__":
    raise SystemExit(main())
