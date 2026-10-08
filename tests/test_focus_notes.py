"""
番茄钟-抓屏联动（v2-focus-notes）的自动测试。

模块⑤+⑩联动：专注态变化驱动抓屏笔记。三件事——
① 退出专注 → 专注时钟隐藏、宠物切休闲态（非安静）；
② 重回专注 → 专注时钟从 0（整轮）重新计时；
③ 进入/重回专注瞬间 → 立即抓屏 + AI 整理当前屏幕为笔记，写入笔记文件夹。

外加回归：f7 专注期点击无反应、f8 右键菜单干净、笔记路径可改仍生效。

手动番茄钟那套（点「开始专注」）是主力场景；自动那套（前台工作软件）的切回也验一条。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    check,
    drain_threads,
    make_env,
    pump,
    section,
    settle_notes,
    summary,
    varied_image,
    wait_for,
)

from life.screen.notes import BATCH_SIZE


class FakeReader:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read(self, image) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假抓屏：画面一直不变，`next()` 才换一屏（抓屏笔记有均值哈希去重，见 harness）。

    计数与出图序列分开：`calls` 会被各段测试随手清零，拿它当种子的话清零就等于把画面
    倒回放过的几屏，会被去重挡掉。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seq = 0

    def next(self) -> None:
        self.seq += 1

    def __call__(self, pet_rect=None):
        self.calls += 1
        return varied_image(self.seq)


def _menu_texts(menu) -> list[str]:
    return [a.text() for a in menu.actions() if a.text()]


def main() -> int:
    box, stub, app = make_env()
    try:
        pet = app.pet_window
        manual = app.pomodoro_service.timer
        notes = app.screen_notes
        bubble = app.pomodoro_bubble
        pet.show()
        pump(0.05)
        pet._settled = True

        # 开抓屏笔记、换假抓屏/假 OCR（离屏跑不动真截图、真 OCR）
        app.config.notes.enabled = True
        reader, grabber = FakeReader(), FakeGrabber()
        notes._reader, notes._grabber = reader, grabber
        notes._pet_rect = lambda: None
        notes._idle = lambda: 0.0               # 别让"静止停拍"跟着跑测试这个人的手走
        stub.notes_result = {"title": "专注记一笔", "key_points": ["这一屏是在专注里抓的"],
                             "todos": [], "questions": []}
        reader.text = "专注 这一屏 抓屏笔记"

        notes_dir = Path(app.config.notes_dir)
        notes_dir.mkdir(parents=True, exist_ok=True)

        # ---------- ③ 进入专注立即抓屏 ----------

        section("③ 进入专注的瞬间：立即抓屏 + AI 整理成笔记入文件夹")

        grabber.calls = 0
        manual.start_focus()                    # 点「开始专注」→ 进专注 → snap_now
        pump(0.05)
        check("手动番茄钟进了专注", manual.is_focusing, str(manual.phase))
        check("进入专注瞬间立即抓了一拍（不等 timer 的下一拍）",
              grabber.calls >= 1, f"抓了 {grabber.calls} 次")
        # 进专注抓的只是这一轮的第一屏；v2-notes-cadence 之后要攒满一轮才整理成笔记，
        # 这里把剩下几屏补上（真机上是用户接着往下看/换屏）。**每一屏都等后台认字回来
        # 再补下一屏**（settle_notes）：14-A 起认字在线程池里，不等的话下一拍会被在飞闸
        # 挡掉，数出来的屏数就少了。
        settle_notes(notes)
        for _ in range(BATCH_SIZE - notes.buffered):
            grabber.next()
            notes.snap_now()
            settle_notes(notes)
        wait_for(lambda: notes.pending > 0, timeout=5.0)
        check("攒满一轮 → AI 整理出一条笔记", notes.pending > 0, f"pending={notes.pending}")
        files = list(notes_dir.glob("*.md"))
        check("笔记落进了笔记文件夹", len(files) >= 1, str([f.name for f in files]))
        body = files[0].read_text(encoding="utf-8") if files else ""
        check("笔记内容就是那屏的整理结果", "专注记一笔" in body, body[:60])

        # ---------- ① 退出专注：时钟隐藏 + 休闲态 ----------

        section("① 退出专注：专注时钟隐藏、宠物切休闲态")

        check("专注时时钟显示着", bubble.isVisible(), f"可见={bubble.isVisible()}")
        manual.reset()                          # 点「结束专注」→ 回空闲
        pump(0.05)
        check("退出专注 → 专注时钟收起", not bubble.isVisible(), f"可见={bubble.isVisible()}")
        check("退出专注 → 不再算专注态", not app._is_focusing(), "")
        check("宠物切回休闲态（不再安静陪着）",
              not pet._scheduler._quiet, f"_quiet={pet._scheduler._quiet}")

        # ---------- ② 重回专注：时钟从 0 计时 ----------

        section("② 重回专注：专注时钟从 0（整轮）重新计时")

        manual.start_focus()
        pump(0.05)
        check("重回专注 → 时钟从整轮开始（不是接着上次的剩余）",
              manual.remaining == manual.planned,
              f"{manual.remaining}/{manual.planned}")
        check("时钟又显示出来了", bubble.isVisible(), f"可见={bubble.isVisible()}")

        # ---------- ③ 重回专注也立即抓屏 ----------

        section("③ 重回专注的瞬间：同样立即抓屏")

        grabber.calls = 0
        manual.reset()
        settle_notes(notes)                     # 上一拍的认字回来，别把这一拍挡在闸外
        manual.start_focus()
        pump(0.05)
        check("重回专注瞬间又立即抓了一拍", grabber.calls >= 1, f"抓了 {grabber.calls} 次")

        # ---------- 自动那套：切回也立即抓屏 ----------

        section("③ 自动番茄钟：切回专注瞬间也立即抓屏")
        manual.reset()
        settle_notes(notes)
        focus = app.screen_focus_timer
        focus.reset()
        pump(0.02)
        grabber.calls = 0
        focus.enter("chrome.exe")               # 前台落回工作软件 → 自动番茄钟开跑
        pump(0.05)
        check("自动番茄钟进了专注", focus.running, "")
        check("自动那套切回瞬间也立即抓了一拍", grabber.calls >= 1, f"抓了 {grabber.calls} 次")
        focus.reset()
        manual.reset()

        # ---------- ④ 回归 ----------

        section("④ 回归：f7 专注期点击无反应 / f8 右键菜单干净 / 路径可改")

        manual.start_focus()
        pump(0.05)
        check("专注态确实是专注态（f7 的判据：点击屏蔽靠它）",
              app._is_focusing() and pet._focus_silenced(),
              f"_is_focusing={app._is_focusing()} silenced={pet._focus_silenced()}")
        manual.reset()
        pump(0.05)
        menu = _menu_texts(pet._build_menu())
        check("空闲态右键菜单干净（f8：没有专注那两条）",
              all(t not in menu for t in ("开始专注", "结束专注", "番茄钟")), str(menu))

        app.config.notes.dir = str(box.dir / "notes_moved")     # 路径可改
        check("笔记路径可改仍生效（v2-notes-path）",
              str(Path(app.config.notes_dir)) == str(box.dir / "notes_moved"),
              str(app.config.notes_dir))

        # ---------- 红线 ----------

        section("红线：抓屏这条线不写 pet.db 的会话表")
        import sqlite3

        def _rows(db, table):
            con = sqlite3.connect(db)
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                con.close()

        before = _rows(app.config.db_path, "utterance")
        manual.start_focus()
        pump(0.05)
        manual.reset()
        pump(0.05)
        drain_threads()
        check("进入/退出专注抓屏，pet.db 的 utterance 一行没多",
              _rows(app.config.db_path, "utterance") == before, f"{before} 行")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("番茄钟-抓屏联动（v2-focus-notes）")


if __name__ == "__main__":
    raise SystemExit(main())
