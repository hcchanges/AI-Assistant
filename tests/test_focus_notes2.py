"""
专注暂停自动退出 + 抓屏笔记落盘（v2-focus-notes2）的自动测试。

真机模块⑤+⑩报的三件事，逐条钉住：

① **专注暂停要自动退出。** 原先两条路都会停在"暂停"这一态：自动那套切走
   `focus_timer.leave()` 只是把表停住（剩余留住、浮窗一直挂屏幕上），手动那套
   面板的「暂停」按钮同理。暂停态在界面上没有出口，只能退出程序。现在两条路都
   改成**暂停即结束这一轮**：回空闲、浮窗收掉。
② **右键菜单只留 工作/生活/设置/退出**，不因为"这会儿在专注"就多冒
   「结束专注」/「番茄钟」两条（桌宠与托盘两处一致）。
③ **抓屏笔记要真落到文件夹里。** 真机"笔记文件夹就是没生成笔记"的直接原因是
   本机 config.yaml 里 `notes.enabled: false`（总闸关着）——本测试验的是**闸打开后
   整条链路真的写文件**：进入专注即拍、换屏即拍、滑屏节奏拍、同屏去重。

红线：笔记只落沙箱里的笔记目录（harness 已把 notes.dir 指到临时目录），
pet.db / mind.db 一行不多；零新依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, settle_notes, summary, varied_image

from PySide6.QtWidgets import QPushButton

from life.screen.notes import BATCH_SIZE

FOUR = ["工作", "生活", "设置", "退出"]
FOCUS_ITEMS = ("开始专注", "结束专注", "番茄钟", "继续")


class FakeReader:
    def __init__(self, text: str = "专注 这一屏 抓屏笔记") -> None:
        self.text = text
        self.calls = 0

    def read(self, image) -> str:
        self.calls += 1
        return self.text


class PatternGrabber:
    """假抓屏：画面一直不变，`next()` 才换一屏——"换没换屏"正是去重判据要看的事。

    出图用 `harness.varied_image`（那里记了"纯色图 / 窄栏渐变图都会被 aHash 判成
    同一屏"这两个坑）。这一轮起还要能一口气出十屏不重样：一轮整理要攒十份。
    """

    def __init__(self) -> None:
        self.seed = 1
        self.image = varied_image(self.seed)
        self.calls = 0

    def next(self) -> None:
        """换一屏：从去重判据的角度看，这就是"用户滑到了下一屏"。"""
        self.seed += 1
        self.image = varied_image(self.seed)

    def __call__(self, pet_rect=None):
        self.calls += 1
        return self.image


def _menu_texts(widget_menu) -> list[str]:
    return [a.text() for a in widget_menu.actions() if a.text()]


def _buttons(panel) -> list[QPushButton]:
    return panel.findChildren(QPushButton)


def _click(panel, contains: str) -> None:
    matches = [b for b in _buttons(panel) if contains in b.text()]
    assert matches, f"面板上没有含「{contains}」的按钮：{[b.text() for b in _buttons(panel)]}"
    matches[0].click()
    pump(0.05)


def main() -> int:
    box, stub, app = make_env()
    try:
        pet = app.pet_window
        manual = app.pomodoro_service.timer
        auto = app.screen_focus_timer
        bubble = app.pomodoro_bubble
        panel = app.pomodoro_panel
        notes = app.screen_notes
        pet.show()
        pump(0.05)
        pet._settled = True

        # ---------- ① 自动那套：切走即结束 ----------

        section("① 自动番茄钟：切走不再停在「暂停」，直接结束这一轮")

        auto.reset()
        manual.reset()
        pump(0.05)
        auto.enter("chrome.exe")
        pump(1.2)
        running_remaining = auto.remaining
        check("先进了专注（表真在走）",
              auto.running and running_remaining < auto.planned,
              f"running={auto.running} {running_remaining}/{auto.planned}")
        check("浮窗挂上了", bubble.isVisible(), str(bubble.isVisible()))

        auto.leave()                    # = 前台切走
        pump(0.05)
        check("切走 → 不在跑", not auto.running, f"running={auto.running}")
        check("切走 → 也没有留下进度（不再有「暂停态」这个状态）",
              not auto.in_progress, f"in_progress={auto.in_progress}")
        check("切走 → 剩余回到整轮（修前是留住 {})".format(running_remaining),
              auto.remaining == auto.planned,
              f"{auto.remaining}/{auto.planned}")
        check("切走 → 浮窗当场收掉（修前会一直挂在屏幕上）",
              not bubble.isVisible(), str(bubble.isVisible()))
        check("切走 → 专注门控解锁", app._is_focusing() is False, str(app._is_focusing()))

        # ---------- ① 手动那套：面板主按钮 ──────────

        section("① 手动番茄钟：面板主按钮在跑的时候就是「结束专注」")

        manual.reset()
        pump(0.05)
        app.panel.close() if app.panel.isVisible() else None
        app.open_panel("pomodoro")
        pump(0.1)
        _click(panel, "开始专注")
        check("点「开始专注」→ 手动番茄钟进专注", manual.is_focusing, str(manual.phase))
        labels = [b.text() for b in _buttons(panel)]
        check("主按钮改口叫「结束专注」（不再写「暂停」）",
              "结束专注" in labels and "暂停" not in labels, str(labels))
        check("面板上没有「继续」这一态（暂停态整个拿掉了）",
              not any("继续" in t for t in labels), str(labels))

        _click(panel, "结束专注")
        check("点「结束专注」→ 回空闲、这一轮作废",
              manual.phase.value == "idle", manual.phase.value)
        check("浮窗收掉了", not bubble.isVisible(), str(bubble.isVisible()))
        check("回空闲后主按钮又写「开始专注」",
              "开始专注" in [b.text() for b in _buttons(panel)],
              str([b.text() for b in _buttons(panel)]))

        # ---------- ② 右键菜单四条固定 ----------

        section("② 右键菜单：只有 工作 / 生活 / 设置 / 退出（专注时也一样）")

        texts = _menu_texts(pet._build_menu())
        check("空闲态：正好四条", texts == FOUR, str(texts))

        auto.enter("chrome.exe")            # 专注跑起来
        pump(0.05)
        busy = _menu_texts(pet._build_menu())
        check("自动番茄钟跑着 → 菜单仍是四条（修前会多冒「结束专注」「番茄钟」）",
              busy == FOUR, str(busy))

        manual.start_focus()
        pump(0.05)
        both = _menu_texts(pet._build_menu())
        check("手动番茄钟在跑 → 菜单仍是四条", both == FOUR, str(both))
        manual.reset()
        auto.reset()
        pump(0.05)

        app.tray._menu.aboutToShow.emit()   # 模拟"用户点开托盘菜单"
        tray = _menu_texts(app.tray._menu)
        check("托盘同款：显示/隐藏 + 四条",
              tray == ["隐藏桌宠"] + FOUR, str(tray))
        check("两处菜单都没有「结束专注」/「番茄钟」（v2-focus-notes2 ③）",
              all(t not in texts + busy + both + tray for t in FOCUS_ITEMS),
              f"{texts} / {busy} / {tray}")

        # ---------- ③ 专注期抓屏真的落盘 ----------

        section("③ 笔记总闸打开后：进入专注即拍，攒满一轮才落进文件夹")

        app.config.notes.enabled = True
        reader, grabber = FakeReader(), PatternGrabber()
        notes._reader, notes._grabber = reader, grabber
        notes._pet_rect = lambda: None
        notes._idle = lambda: 0.0           # 见 test_notes：别让"静止停拍"跟着人的手走
        stub.notes_result = {"title": "专注记一笔", "key_points": ["这一屏是在专注里抓的"],
                             "todos": [], "questions": []}
        notes_dir = Path(app.config.notes_dir)
        check("落点是沙箱目录（绝不碰真实「笔记」文件夹）",
              str(box.dir) in str(notes_dir), str(notes_dir))

        manual.reset()
        settle_notes(notes)
        grabber.calls = 0
        manual.start_focus()                # 进专注 → snap_now
        pump(0.05)
        settle_notes(notes)
        check("进入专注瞬间立即抓了一拍（不等 timer 的下一拍）",
              grabber.calls >= 1, f"抓了 {grabber.calls} 次")
        check("只抓了这一拍，还没到一轮：笔记文件夹里还是空的（v2-notes-cadence ②）",
              not list(notes_dir.glob("*.md")), str([f.name for f in notes_dir.glob("*.md")]))

        # 再换几屏把这轮凑满（换屏那条事件路是现成的，测试里拿它当"用户继续看下去"）。
        # 每一屏等后台认字回来再补下一屏（settle_notes：14-A 起认字在线程池里）。
        for _ in range(BATCH_SIZE - notes.buffered):
            grabber.next()
            notes.snap_now()
            settle_notes(notes)
        files = sorted(notes_dir.glob("*.md"))
        check("凑满一轮 → 笔记真落进文件夹了（真生成了文件）",
              len(files) == 1, str([f.name for f in files]))
        body = files[0].read_text(encoding="utf-8") if files else ""
        check("文件里有那屏的整理结果", "专注记一笔" in body, body[:80].replace("\n", " / "))
        first_len = len(body)

        # ---------- ④ 换屏即拍 / 滑屏节奏 ----------

        section("④ 换屏即拍 + 滑屏 10s：专注期换一屏就再抓一屏")

        settle_notes(notes)
        grabber.calls = 0
        grabber.next()
        app.screen_monitor.foreground_changed.emit("explorer.exe")   # = 换屏
        pump(0.05)
        check("换屏 → 立刻抓了一拍", grabber.calls >= 1, f"抓了 {grabber.calls} 次")

        settle_notes(notes)                 # 上面那拍的认字回来，别把滑屏这拍挡在闸外
        grabber.calls = 0
        grabber.next()
        notes._idle = lambda: 0.5           # 最近有键鼠操作 = 在滑屏
        notes._on_cadence()
        check("滑屏节奏（10s 拍）→ 也抓了一拍", grabber.calls == 1, f"抓了 {grabber.calls} 次")

        settle_notes(notes)
        for _ in range(BATCH_SIZE - notes.buffered):    # 再凑满一轮
            grabber.next()
            notes.snap_now()
            settle_notes(notes)
        body = files[0].read_text(encoding="utf-8")
        check("又攒满一轮 → 文件里多了一段（不是覆盖）", len(body) > first_len,
              f"{first_len} → {len(body)}")

        # ---------- ⑤ 去重 / 停手不拍 / 静止停拍 ----------

        section("⑤ 去重与停手：同一屏不反复记，停手了不拍，静态屏也不再连拍")

        notes._idle = lambda: 0.5
        settle_notes(notes)                 # 先等上一拍的认字落地，这两条对照才不是空转
        reader.calls = 0
        kept = len(body)
        notes._on_cadence()                 # 同一张图（没有 next()）
        settle_notes(notes)
        check("同一屏再来一拍 → 连字都不去认（aHash 雷同，直接跳过）",
              reader.calls == 0, f"认了 {reader.calls} 次")
        check("文件没被重复追加", len(files[0].read_text(encoding="utf-8")) == kept,
              f"{kept} → {len(files[0].read_text(encoding='utf-8'))}")

        notes._idle = lambda: 30.0          # 停手 30 秒
        grabber.next()
        grabber.calls = 0
        settle_notes(notes)
        notes._on_cadence()
        check("停手超过 3 秒 → 滑屏节奏不拍（回到 5 分钟兜底）",
              grabber.calls == 0, f"抓了 {grabber.calls} 次")

        notes._idle = lambda: 300.0         # 五分钟没碰键鼠 = 人在看一屏静态内容
        grabber.next()
        grabber.calls = 0
        for _ in range(3):
            notes.tick()                    # 5 分钟兜底那条路
            pump(0.02)
        drain_threads()
        check("静止超过 60 秒 → 连 5 分钟兜底那一路也不拍了（v2-notes-cadence ①）",
              grabber.calls == 0, f"抓了 {grabber.calls} 次")
        notes._idle = lambda: 0.0

        section("⑤ 反向对照：不在专注态时一拍都不抓")

        manual.reset()
        auto.reset()
        pump(0.05)
        check("前置：确实不在专注态", not app._is_focusing(), str(app._is_focusing()))
        grabber.next()
        grabber.calls = 0
        notes.tick()
        notes._on_cadence()
        pump(0.05)
        check("非专注态：tick 与滑屏节奏都不抓屏",
              grabber.calls == 0, f"抓了 {grabber.calls} 次")

        section("⑤ 反向对照：总闸关着时也不抓（真机上就是它关着）")

        app.config.notes.enabled = False
        manual.start_focus()                # 进专注也不该抓
        pump(0.05)
        grabber.calls = 0
        notes.tick()
        pump(0.05)
        check("总闸关着 → 进了专注也不抓屏",
              grabber.calls == 0, f"抓了 {grabber.calls} 次")
        manual.reset()
        app.config.notes.enabled = True
        pump(0.05)

        # ---------- 红线 ----------

        section("红线：这一轮只动专注状态/菜单/笔记落盘，两个库一行不多")

        import sqlite3

        def _rows(db, table):
            con = sqlite3.connect(db)
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                con.close()

        utterance = _rows(app.config.db_path, "utterance")
        fragments = app.mind_store.fragment_count()
        auto.enter("chrome.exe")
        pump(0.3)
        auto.leave()
        manual.start_focus()
        pump(0.2)
        manual.reset()
        drain_threads()
        check("pet.db 的 utterance 一行没多",
              _rows(app.config.db_path, "utterance") == utterance,
              f"{utterance} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 一条碎片都没写",
              app.mind_store.fragment_count() == fragments, "")

        section("红线：零新依赖（笔记这条线还是 QImage + 标准库）")

        src = (Path(__file__).resolve().parents[1] / "life" / "screen" / "notes.py").read_text(encoding="utf-8")
        check("notes.py 没引图像库/第三方（aHash 用 QImage 算）",
              "import numpy" not in src and "import cv2" not in src and "PIL" not in src, "")
        check("focus_timer.py 还是只用 PySide6 + 标准库",
              "from PySide6" in (Path(__file__).resolve().parents[1] / "life" / "screen" / "focus_timer.py").read_text(encoding="utf-8"),
              "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("专注暂停自动退出 + 抓屏笔记落盘（v2-focus-notes2）")


if __name__ == "__main__":
    raise SystemExit(main())
