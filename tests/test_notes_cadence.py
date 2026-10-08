"""
抓屏节奏 / 批量整理 / 询问频率（v2-notes-cadence）的自动测试。

真机模块⑤反馈：专注期间屏幕抓取过于频繁——**不滑动屏幕也连着问好几条"发笔记"**。
拆下来是三件事叠在一起：

① **静止也在抓**：周期拍（`notes.interval_min`，默认 5 分钟）到点就抓，不问人在不在动；
   10s 那条滑屏节奏本来就要求"最近 3 秒有操作"，可 5 分钟那条兜底没有任何闸。
② **抓一帧就整理一轮**：每抓必提炼、必落盘、必可能问一句——吵的根子在这儿。
③ **没有"上次问过"的记录**：`confirm()` 一答复就把等待态清掉，下一帧立刻能再问一遍。

这一轮的三条新规矩，逐条钉住（对应 S4 四项）：

- **静止停拍**：最近 60 秒没有键鼠操作 → 周期拍不抓；换屏 / 进专注那两个事件拍不受影响；
- **攒满 10 份才整理**：第 9 拍之前一个字都不提炼、不落盘、不询问；
- **一周只问一次**：询问时间戳落到存档，7 天之内不再问；一周里整理多轮只问第一轮。

时钟与空闲时长都是注进去的（`_now` / `_idle`），不看跑测试这个人手有没有动。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, settle_notes, summary, varied_image

from PySide6.QtGui import QColor, QImage, QPainter

from life.screen.notes import (
    ASK_INTERVAL_DAYS,
    BATCH_SIZE,
    CADENCE_MS,
    DUP_MAX_DIST,
    IDLE_EXIT_S,
    STILL_STOP_S,
    ScreenNotesService,
    _ahash,
    _hamming,
)


class FakeReader:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read(self, image) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    def __init__(self, image: QImage | None = None) -> None:
        self.calls = 0
        self._image = image if image is not None else QImage(40, 30, QImage.Format_RGB32)

    def __call__(self, pet_rect=None):
        self.calls += 1
        return self._image


class SlidingGrabber:
    """每拍回一张**内容不同**的图：滑屏时屏幕本来就在变（见 harness.varied_image）。

    **计数与出图序列分开两个计数器**：`calls` 会被各段测试随手清零（数"这一段抓了几拍"），
    要是拿它当出图种子，清零就等于把画面倒回放过的几屏——那几张的指纹还在
    `_recent_hashes` 窗口里，会被去重挡掉（第一版就是这么假红的）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seq = 0

    def __call__(self, pet_rect=None):
        self.calls += 1
        self.seq += 1
        return varied_image(self.seq)


class Clock:
    """可拨的假时钟：`notes._now` 与询问存档的时间戳都走它。"""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kwargs) -> None:
        self.at += timedelta(**kwargs)


def _ticks(notes, reader, count: int, prefix: str = "屏") -> None:
    """走 count 拍周期拍。每拍给一段不同的字，好断言"这一轮到底攒了几屏"。

    每拍都等后台认字回来再走下一拍（`settle_notes`）：14-A 起认字在线程池里，不等的话
    下一拍会被在飞闸挡掉，"走了十拍"就只攒下几屏。
    """
    for index in range(count):
        reader.text = f"{prefix}{index} 内容"
        notes.tick()
        settle_notes(notes)


def main() -> int:
    # ---------- 模块级哈希 + 定稿常量 ----------

    section("定稿常量：10s 一拍 / 停 3s 退出滚动 / 静止 60s 停拍 / 10 份一轮 / 7 天一问")

    a = QImage(64, 64, QImage.Format_RGB32)
    a.fill(0xFFFFFFFF)
    b = QImage(64, 64, QImage.Format_RGB32)
    b.fill(0xFFFFFFFF)
    c = QImage(64, 64, QImage.Format_RGB32)
    c.fill(0xFF000000)          # 全黑，再把左半涂白 → 左白右黑
    painter = QPainter(c)
    painter.fillRect(0, 0, 32, 64, QColor(255, 255, 255))
    painter.end()
    check("同一张图 → 同一个指纹", _ahash(a) == _ahash(b), f"{_ahash(a):064b}")
    check("同一张图 → 汉明距离 0", _hamming(_ahash(a), _ahash(b)) == 0, "")
    check("左白右黑 vs 全白 → 汉明距离大（不雷同）",
          _hamming(_ahash(a), _ahash(c)) > DUP_MAX_DIST, f"{_hamming(_ahash(a), _ahash(c))}")
    check("滑屏节奏还是 10s 一拍 / 停 3s 退出",
          CADENCE_MS == 10000 and IDLE_EXIT_S == 3, f"{CADENCE_MS}/{IDLE_EXIT_S}")
    check("静止停拍阈值 60 秒", STILL_STOP_S == 60, str(STILL_STOP_S))
    check("一轮攒 10 份（不是 1 份就整理）", BATCH_SIZE == 10, str(BATCH_SIZE))
    check("询问最短间隔 7 天", ASK_INTERVAL_DAYS == 7, str(ASK_INTERVAL_DAYS))

    box, stub, app = make_env()
    try:
        notes = app.screen_notes
        app.config.notes.enabled = True
        reader, grabber = FakeReader(), SlidingGrabber()
        notes._reader, notes._grabber = reader, grabber
        notes._pet_rect = lambda: None
        clock = Clock(datetime(2026, 10, 4, 10, 0, 0))
        notes._now = clock
        notes._idle = lambda: 0.5                   # 默认"正在动"，单看节奏那条线
        stub.notes_result = {"title": "节奏", "key_points": ["抓了一屏"], "todos": [], "questions": []}

        notes_dir = Path(app.config.notes_dir)
        notes_dir.mkdir(parents=True, exist_ok=True)
        offers: list[int] = []
        app.bus.notes_offer.connect(offers.append)

        manual = app.pomodoro_service.timer
        manual.start_focus()                        # 进专注（触发进专注立即抓）
        settle_notes(notes)
        manual.reset()
        settle_notes(notes)
        # 进专注那一拍不该算进后面的计数
        notes._buffer.clear()
        grabber.calls, reader.calls, offers[:] = 0, 0, []

        # ---------- S4-① 静止不滑动：不抓、不问 ----------

        section("S4-① 专注中静止不滑动 → 一拍不抓、一句不问")

        manual.start_focus()
        settle_notes(notes)
        notes._buffer.clear()
        grabber.calls, reader.calls, offers[:] = 0, 0, []
        notes._idle = lambda: 300.0                 # 五分钟没碰键鼠 = 人在看静态的一屏
        for _ in range(5):
            notes.tick()
            pump(0.02)
        drain_threads()
        pump(0.1)
        check("静止五分钟里，周期拍一次都没抓（修前 5 分钟到点就抓）",
              grabber.calls == 0, f"抓了 {grabber.calls} 次")
        check("一个字都没认", reader.calls == 0, f"认了 {reader.calls} 次")
        check("没有笔记、没有询问（不会连问好几条）",
              notes.pending == 0 and offers == [], f"pending={notes.pending} offers={offers}")

        # **静止态照旧**（上面刚设的 300 秒不动它）：这条断言的整个意义就是"人在没动
        # 的时候换了一屏，事件拍该抓还是抓"。把 _idle 改回"有操作"再调 snap_now 的话，
        # 它跟周期拍没区别，这条对照就白写了（变异验证时正是这么发现它空转的）。
        grabber.calls = 0
        notes.snap_now()                            # 换屏那条事件路
        check("对照：静止态下换屏事件拍不受闸管（该抓还是抓）",
              grabber.calls == 1, f"抓了 {grabber.calls} 次")
        settle_notes(notes)
        notes._buffer.clear()
        manual.reset()
        settle_notes(notes)

        # ---------- S4-② 滑屏仍约 10s 一拍 ----------

        section("S4-② 滑屏仍约 10 秒一拍：停手即停")

        manual.start_focus()
        settle_notes(notes)
        notes._buffer.clear()
        notes._idle = lambda: 0.5                   # 正在滚动
        grabber.calls = 0
        notes._on_cadence()
        check("滚动（最近有操作）→ 抓一帧", grabber.calls == 1, f"抓了 {grabber.calls} 次")
        settle_notes(notes)                         # 这一拍的认字回来再试下一拍

        notes._idle = lambda: 10.0                  # 停滚 10 秒
        grabber.calls = 0
        notes._on_cadence()
        check("停滚超过 3 秒 → 不抓（退出滚动节奏）", grabber.calls == 0, f"抓了 {grabber.calls} 次")
        settle_notes(notes)
        notes._buffer.clear()

        # ---------- S4-③ 攒满 10 份才整理一轮 ----------

        section("S4-③ 累计抓满 10 份才整理一轮（不是每抓一帧就整理）")

        notes._idle = lambda: 0.5
        grabber.calls, reader.calls, offers[:] = 0, 0, []
        files_before = sorted(notes_dir.glob("*.md"))
        stub.prompts.clear()
        _ticks(notes, reader, BATCH_SIZE - 1)       # 9 拍
        check("第 9 拍为止：攒着，没提炼（模型一次没被叫）",
              not stub.prompts, f"模型被叫了 {len(stub.prompts)} 次")
        check("第 9 拍为止：一条笔记都没产出", notes.pending == 0, f"pending={notes.pending}")
        check("第 9 拍为止：笔记文件夹里没有新文件",
              sorted(notes_dir.glob("*.md")) == files_before, str(sorted(notes_dir.glob("*.md"))))
        check("第 9 拍为止：一句都没问", offers == [], str(offers))
        check("攒着 9 份，差 1 份到一轮", notes.buffered == BATCH_SIZE - 1, f"buffered={notes.buffered}")

        reader.text = "屏9 内容"
        notes.tick()                                # 第 10 拍
        settle_notes(notes)
        check("第 10 拍：才提炼这一轮", len(stub.prompts) == 1, f"模型被叫了 {len(stub.prompts)} 次")
        check("整理出来的是 1 条笔记（10 屏合成一轮，不是 10 条）",
              notes.pending == 1, f"pending={notes.pending}")
        new_files = sorted(notes_dir.glob("*.md"))
        check("这一轮落成了一个新文件", len(new_files) == len(files_before) + 1, str([p.name for p in new_files]))
        check("攒的 10 屏全进了这一轮（头尾都在提示词里）",
              "屏0 内容" in stub.prompts[0] and "屏9 内容" in stub.prompts[0], "")
        check("整理完才第一次问「要不要发」", offers == [1], str(offers))

        # ---------- S4-④ 一周内多次整理只问首轮 ----------

        section("S4-④ 一周内多次整理，只问第一轮")

        app.chat_bubble.offer_reply.emit("好呀")     # 走真实轻按钮 → bootstrap → confirm
        drain_threads()
        pump(0.05)
        check("前置：答复完不再等确认", notes.awaiting_confirm is False, "")

        state = Path(box.dir) / "notes_state.json"
        check("询问时间戳落了存档（下次开程序也记得）", state.exists(), str(state))
        check("存档里就是这一轮问的时刻",
              clock.at.isoformat(timespec="seconds") in state.read_text(encoding="utf-8"),
              state.read_text(encoding="utf-8") if state.exists() else "（没有这个文件）")

        for round_index in range(3):                # 一周内再整理三轮
            _ticks(notes, reader, BATCH_SIZE, prefix=f"第{round_index}轮屏")
        check("一周内又整理了三轮，一次都没再问", offers == [1], str(offers))
        check("但笔记照记（不问≠不干）", notes.pending >= 3, f"pending={notes.pending}")

        fresh = ScreenNotesService(
            config=app.config, bus=app.bus, is_focusing=lambda: True, now_provider=clock
        )
        fresh._on_notes({"title": "重启后这一轮", "key_points": ["一条"], "todos": [], "questions": []})
        pump(0.05)
        check("重启（新建一个服务）后仍然记得「问过了」（存档真被读回去）",
              offers == [1] and not fresh.awaiting_confirm, str(offers))

        clock.advance(days=ASK_INTERVAL_DAYS, seconds=1)     # 过了 7 天零 1 秒
        _ticks(notes, reader, BATCH_SIZE, prefix="下周屏")
        # 问句里报的是**这一刻攒了几条**（第 5 轮时是 4 条），不是"第几次问"，
        # 所以这里只数问了几次。
        check("过了 7 天 → 新一轮整理又问了（凑够一周才问第二次）",
              len(offers) == 2, f"问了 {len(offers)} 次：{offers}")

        # ---------- 回归：换屏即拍 / 去重 / 进专注即拍 / f8 ----------

        section("回归：换屏即拍、同屏去重、进专注即拍、右键菜单不动")

        same = FakeGrabber()                        # 每次返回同一张图
        notes._grabber = same
        notes._buffer.clear()
        settle_notes(notes)
        reader.calls = 0
        notes.tick()
        settle_notes(notes)                         # 第一拍认完字再走第二拍
        after_first = reader.calls
        notes.tick()                                # 同一张图，应该被去重跳过
        settle_notes(notes)
        check("相同帧第二拍不去 OCR（被去重）", reader.calls == after_first,
              f"{after_first} → {reader.calls} 次")

        notes._grabber = grabber
        notes._buffer.clear()
        settle_notes(notes)
        manual.reset()
        settle_notes(notes)
        grabber.calls = 0
        manual.start_focus()
        pump(0.05)
        # `>= 1` 不是放水：进专注那一刻装配层会有两条信号都走到 snap_now（番茄钟那条
        # 与前台专注那条），真机上第二拍被均值哈希去重挡掉，测试里假取景器每拍都换图
        # 所以两拍都算。这里要钉的是"进专注会立刻抓"，不是"恰好抓一次"。
        check("进专注立即抓（v2-focus-notes 保留）", grabber.calls >= 1, f"抓了 {grabber.calls} 次")
        check("专注态判据还在（f7 靠它）", app._is_focusing(), "")
        manual.reset()
        pump(0.05)
        menu = [x.text() for x in app.pet_window._build_menu().actions() if x.text()]
        check("空闲态右键菜单干净（f8）",
              all(t not in menu for t in ("开始专注", "结束专注", "番茄钟")), str(menu))
        check("笔记仍落 NOTES_DIR（沙箱临时目录）", "pet_test_" in str(notes_dir), str(notes_dir))

        section("回归：静止停拍不写库（pet.db / mind.db 一行不多）")

        import sqlite3

        def _rows(db, table):
            con = sqlite3.connect(db)
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                con.close()

        before_utt = _rows(app.config.db_path, "utterance")
        before_frag = app.mind_store.fragment_count()
        notes._idle = lambda: 300.0
        for _ in range(5):
            notes.tick()
        drain_threads()
        check("pet.db utterance 一行没多", _rows(app.config.db_path, "utterance") == before_utt,
              f"{before_utt} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == before_frag, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("抓屏节奏 / 批量整理 / 询问频率（v2-notes-cadence）")


if __name__ == "__main__":
    raise SystemExit(main())
