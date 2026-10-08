"""
抓屏笔记（C 多模态 · 笔记先行版）的自动测试。

覆盖：
- S1 取景：原图临时落盘 → 用完即删；取窗口矩形不抛异常（非 Windows / 没窗口返回 None）；
- S2/S3 触发与提炼：只在专注态抓（复用 S4 判定）、认字→chat_json→笔记落配置的那个文件夹；
- S4 问一句再发：生成笔记只问不自动发；用户点头才 notes_delivered；
- 红线：pet.db / mind.db 零写入、危机帧跳过不笔记、原图不长期落盘、屏内容不进画像库。

取景与 OCR 换成了假的（离屏跑不动真截图、真 OCR 太慢），但**信号接线走真实链路**
（bus.notes_offer → bootstrap → 气泡 → offer_reply → 服务），端到端验到。
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
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

import yaml
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

from life.screen import shot
from life.screen.notes import BATCH_SIZE


def _enable_notes(box) -> None:
    """在构造应用之前把 notes 打开（默认关着），落点已经是沙箱临时目录。"""
    raw = yaml.safe_load(box.config_path.read_text(encoding="utf-8")) or {}
    raw.setdefault("notes", {})
    raw["notes"]["enabled"] = True
    box.config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


class FakeReader:
    """假 OCR：read() 回预先摆好的字。"""

    def __init__(self) -> None:
        self.text = ""
        self.calls = 0

    def read(self, image) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假取景：每次回一张**内容不同**的图（真机每拍屏幕内容都在变）。

    计数与出图序列分成两个计数器：`calls` 会被各段测试随手清零，拿它当出图种子的话
    清零就等于把画面倒回放过的几屏，会被均值哈希去重挡掉（见 harness.varied_image）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seq = 0

    def __call__(self, pet_rect=None):
        self.calls += 1
        self.seq += 1
        return varied_image(self.seq)


def _round(notes, reader, text: str) -> None:
    """抓满一轮（`BATCH_SIZE` 屏）：前九拍只攒着，第十拍才提炼、才落盘、才可能问。

    v2-notes-cadence 之后"抓一拍"和"出一条笔记"不再是一回事了，所以凡是关心
    笔记产出的地方都得走满一轮。每一拍都等后台认字回来再走下一拍
    （`settle_notes`：14-A 起认字在线程池里，没回来时下一拍会被在飞闸挡掉）。
    """
    for index in range(BATCH_SIZE):
        reader.text = f"{text}｜第{index}屏"
        notes.tick()
        settle_notes(notes)


def _pass_a_week(notes) -> None:
    """把"问过了"的记录抹掉 = 假装距上次询问已经过了一周。

    真实的 7 天闸由 test_notes_cadence 用假时钟验；这个文件里只是别让它挡着后面几段
    ——它们要验的是"点头才发 / 说不用不发"，不是询问频率。
    """
    notes._last_ask_at = None
    try:
        notes._state_path().unlink()
    except OSError:
        pass


class ShotWatch:
    """盯住**这一趟测试自己**落过的临时原图：落一张记一张，最后看还有谁留在盘上。

    **为什么不数临时目录里 `pet_notes_*.png` 的个数**：那个目录（`%TEMP%`）是全机器共用的，
    真机上正跑着的那只桌宠（`pythonw.exe`，`notes.enabled: true`）也在往里落同名文件、
    落完自己删——它一拍就能让"前后两个集合"对不上，把这条红线判成假红（实测十次里红一次，
    抓到的那个文件在本进程里从头到尾没有 save/discard 记录）。改成只看**自己落的那几张**，
    别的进程怎么进出都不影响，判据还更硬：不是"目录里没多东西"，是"我落的每一张都删了"。
    """

    def __init__(self) -> None:
        self.paths: list[Path] = []
        self._real = shot.save_temp

    def install(self) -> None:
        shot.save_temp = self._save

    def restore(self) -> None:
        shot.save_temp = self._real

    def _save(self, image):
        path = self._real(image)
        if path is not None:
            self.paths.append(Path(path))
        return path

    def forget(self) -> None:
        """清掉已记的名单（"从这儿往后落的那几张"）。"""
        self.paths.clear()

    def leftovers(self) -> list[str]:
        return [p.name for p in self.paths if p.exists()]


def _rows(db_path) -> int:
    """直接数 pet.db 某张表的行数（真读盘，不是看对象属性）。"""
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("select count(*) from utterance").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    section("S1 取景：原图临时落盘 → 用完即删")
    path = shot.save_temp(QImage(20, 10, QImage.Format_RGB32))
    check("原图能落一次临时文件", path is not None and Path(path).exists(), str(path))
    shot.discard(path)
    check("discard 之后原图没了", path is not None and not Path(path).exists(), str(path))
    shot.discard(None)                        # 空路径不炸
    check("窗口矩形在没窗口时优雅返回", shot.foreground_window_rect() is None or
          isinstance(shot.foreground_window_rect(), QRect), "")

    box, stub, app = make_env(prepare=_enable_notes)
    # 从这儿起记下"这一趟自己落过的临时原图"（见 ShotWatch：别去数共用的 %TEMP%）
    watch = ShotWatch()
    watch.install()
    # 红线基线：跑之前先把两个库的真实行数记下来，最后拿前后比
    pet_utterance_before = _rows(app.config.db_path)
    pet_memory_before = len(app.memory_dao.recent(100))
    mind_frag_before = app.mind_store.fragment_count()
    notes = app.screen_notes
    reader, grabber = FakeReader(), FakeGrabber()
    notes._reader, notes._grabber = reader, grabber
    # "多久没键鼠操作"注成常量：不然跑测试这个人要是六十秒没碰键盘，周期拍就全被
    # 静止停拍那道闸拦下，断言会随人手而红（真机上那条闸由 test_notes_cadence 验）。
    notes._idle = lambda: 0.0

    delivered: list[str] = []
    offers: list[int] = []
    app.bus.notes_delivered.connect(delivered.append)
    app.bus.notes_offer.connect(offers.append)

    notes_dir = Path(app.config.notes_dir)
    check("笔记落点在临时沙箱里（不碰用户真实的「笔记」文件夹）", "pet_test_" in str(notes_dir), str(notes_dir))

    # 专注态由 ScreenFocusTimer.running 决定：测试里直接改它的开关来"进/出网课"
    def set_focus(on: bool) -> None:
        app.screen_focus_timer._running = on

    section("S2 触发：只在专注态抓")
    set_focus(False)
    reader.text = "第三章 线性回归 作业：周五前提交"
    grabber.calls = 0
    notes.tick()
    drain_threads()
    check("不在专注态（刷剧/闲逛）→ 一拍都不抓", grabber.calls == 0, f"抓了 {grabber.calls} 次")
    check("不专注 → 没有笔记", notes.pending == 0, f"pending={notes.pending}")

    section("S2/S3 专注态 → 抓满一轮 → 认字 → 提炼 → 落笔记文件夹")
    set_focus(True)
    stub.notes_result = {
        "title": "线性回归",
        "key_points": ["最小二乘法的目标是最小化残差平方和"],
        "todos": ["周五前交作业"],
        "questions": ["正则化怎么选 lambda"],
    }
    watch.forget()                              # 从这一轮起，只盯这一轮自己落的图
    for index in range(BATCH_SIZE - 1):         # 前九拍：只攒着，不提炼
        reader.text = f"第三章 线性回归｜第{index}屏"
        notes.tick()
        settle_notes(notes)
    drain_threads()
    check("前九拍只攒着：没笔记、没落盘、没问（v2-notes-cadence ②）",
          notes.pending == 0 and not list(notes_dir.glob("*.md")) and offers == [],
          f"pending={notes.pending} 文件={[f.name for f in notes_dir.glob('*.md')]} offers={offers}")

    reader.text = "第三章 线性回归 最小二乘 残差平方和 作业周五前交 正则化 lambda"
    notes.tick()                                # 第十拍：这才提炼这一轮
    settle_notes(notes)
    wait_for(lambda: notes.pending > 0, timeout=5.0)

    check("专注态 → 真的抓满一轮（十屏）", grabber.calls == BATCH_SIZE, f"抓了 {grabber.calls} 次")
    check("每一屏都认了字", reader.calls == BATCH_SIZE, f"read {reader.calls} 次")
    check("十屏合成一条笔记（不是十条）", notes.pending == 1, f"pending={notes.pending}")
    check("原图不长期落盘（临时图没留下）", not watch.leftovers(), str(watch.leftovers()))

    files = list(notes_dir.glob("*.md"))
    check("配置的那个文件夹里落了一个 md", len(files) == 1, str([f.name for f in files]))
    body = files[0].read_text(encoding="utf-8") if files else ""
    check("笔记里有要点", "残差平方和" in body, body[:80])
    check("笔记里有待办", "周五前交作业" in body, "")
    check("笔记里有疑问", "lambda" in body, "")

    check("S4 问了一句要不要发", offers == [1], str(offers))
    check("S4 没确认之前不自动发", delivered == [], str(delivered))
    check("服务标记为等确认", notes.awaiting_confirm, "")

    section("S4 用户点头 → 才发")
    app.chat_bubble.offer_reply.emit("好呀")     # 走真实轻按钮 → bootstrap 分发 → 服务
    drain_threads()
    pump(0.05)
    check("点头后发了笔记正文", len(delivered) == 1 and "残差平方和" in delivered[0], str(delivered)[:80])
    check("发完清空待发", notes.pending == 0, f"pending={notes.pending}")

    section("S4 用户说不用 → 不发")
    stub.notes_result = {"title": "第二节", "key_points": ["梯度下降"], "todos": [], "questions": []}
    _pass_a_week(notes)                         # 一周只问一次那条闸别挡着这一段
    _round(notes, reader, "第二节 梯度下降")
    app.chat_bubble.offer_reply.emit("不用了")
    drain_threads()
    pump(0.05)
    check("说不用 → 一条都没发出去", len(delivered) == 1, str(len(delivered)))

    section("14-C 笔记询问尊重专注：专注期不弹框（v2-focus-mode-freeze-fix）")
    # 这一段的判据只能看**气泡**，不能看 `offers`：那个列表接的是 `bus.notes_offer`
    # 这个信号本身，bootstrap 的槽早退了它也一样记一笔——上一版就是因此看着全绿，
    # 而界面上压根没问（实测 `_offer_theme` 是空的）。
    asked: list[str] = []
    real_show = app.chat_bubble.show_offer

    def spy_show(theme: str) -> None:
        asked.append(theme)
        real_show(theme)

    app.chat_bubble.show_offer = spy_show
    try:
        _pass_a_week(notes)                     # 一周一问那道闸别挡着这一段
        stub.notes_result = {"title": "14-C", "key_points": ["专注里攒的"],
                             "todos": [], "questions": []}
        offers_before = len(offers)
        _round(notes, reader, "14-C 专注帧")
        wait_for(lambda: len(offers) > offers_before, timeout=5.0)
        check("★ 询问到点那一刻确实还在专注里（笔记只在专注里攒，这就是那一拍）",
              app._is_focusing(), str(app.screen_focus_timer.running))
        check("专注期：气泡不问（不弹要点的框）",
              asked == [] and app.chat_bubble._offer_theme == "",
              f"asked={asked} theme={app.chat_bubble._offer_theme!r}")
        # 服务侧照旧：它是在 emit **之前**就落好 `_awaiting` 与时间戳的，
        # 所以"专注期不问"落下去就是**丢掉**这一问，不是留到专注结束再问。
        check("专注期：服务侧照旧当「问过了」（7 天闸已落、待发还挂着）",
              notes.awaiting_confirm and notes.pending > 0,
              f"awaiting={notes.awaiting_confirm} pending={notes.pending}")
        set_focus(False)                        # 出了专注：同一问该照常弹
        app.bus.notes_offer.emit(2)
        pump(0.05)
        check("出了专注：气泡照常问", asked == ["notes"], str(asked))
    finally:
        app.chat_bubble.show_offer = real_show
        set_focus(True)

    section("S2 手动番茄钟专注 → 也抓（v2-notes-focus：原来只认自动那套会漏掉）")
    # 自动那套不跑（前台判定没触发），单靠手动番茄钟进专注——这正是真机反馈的场景。
    set_focus(False)
    manual = app.pomodoro_service.timer
    manual.start_focus()
    pump(0.05)
    check("手动番茄钟专注态成立", manual.is_focusing, str(manual.phase))
    check("装配层 _is_focusing 把手动专注也算进去", app._is_focusing(), "")
    stub.notes_result = {"title": "手动专注", "key_points": ["这一屏是在手动专注里抓的"],
                         "todos": [], "questions": []}
    _pass_a_week(notes)
    grabber.calls = 0
    _round(notes, reader, "手动专注 这一屏 抓屏")
    check("手动专注 → 真的抓了", grabber.calls == BATCH_SIZE, f"抓了 {grabber.calls} 次")
    check("手动专注 → 笔记也攒下了", notes.pending > 0, f"pending={notes.pending}")
    manual.reset()                          # 退出，别把专注态带到后面的红线测试
    pump(0.05)
    check("手动番茄钟退出后 _is_focusing 回假", not app._is_focusing(), "")

    section("红线：危机帧跳过不笔记")
    stub.notes_result = {"title": "危险", "key_points": ["不该出现"], "todos": [], "questions": []}
    reader.text = "我不想活了"
    pending_before = notes.pending
    watch.forget()                              # 只管这一拍落的图
    notes.tick()
    settle_notes(notes)
    check("危机内容这一拍不产出笔记", notes.pending == pending_before, f"{pending_before} → {notes.pending}")
    check("危机帧原图也删了", not watch.leftovers(), str(watch.leftovers()))

    section("14-A 认字丢后台：tick 不等 OCR（v2-focus-mode-freeze-fix）")

    class BlockingReader:
        """认字卡在事件上不返回，用来钉住"tick 有没有被认字按住"。

        修前 `_capture` 是同步调的：一个 5 秒不返回的 read 会把 tick 一起按 5 秒
        （真机上就是 RapidOCR 那一次约 4 秒）。修后认字在线程池里，tick 立刻返回，
        read 在别的线程上等着。
        """

        def __init__(self) -> None:
            self.calls = 0
            self.thread = None
            self.release = threading.Event()

        def read(self, image) -> str:
            self.calls += 1
            self.thread = threading.current_thread()
            self.release.wait(5.0)
            return "阻塞帧 内容"

    blocking = BlockingReader()
    saved_reader, saved_grabber = notes._reader, notes._grabber
    notes._reader, notes._grabber = blocking, FakeGrabber()
    notes._buffer.clear()
    notes._recent_hashes.clear()                # 别让上一段留下的指纹把这一拍去重掉
    settle_notes(notes)
    set_focus(True)
    started = time.monotonic()
    notes.tick()
    elapsed = time.monotonic() - started
    wait_for(lambda: blocking.calls == 1, timeout=5.0)
    check("认字真的跑起来了（后台那一趟）", blocking.calls == 1, f"认了 {blocking.calls} 次")
    check("★ tick 没被认字按住（修前这一个 tick 要等满 5 秒）", elapsed < 1.0, f"{elapsed:.2f}s")
    check("★ 认字跑在别的线程上（不是界面线程）",
          blocking.thread is not None and blocking.thread is not threading.main_thread(),
          str(blocking.thread))
    blocking.release.set()                      # 放行：异步不该把这一拍的结果弄丢
    settle_notes(notes)
    check("放行后这一拍的字照常攒下", notes.buffered == 1, f"buffered={notes.buffered}")
    set_focus(False)
    notes._reader, notes._grabber = saved_reader, saved_grabber
    notes._buffer.clear()

    section("红线：切走 → 抓屏暂停")
    set_focus(False)
    reader.text = "写点什么"
    grabber.calls = 0
    notes.tick()
    drain_threads()
    check("切到别的应用 → 不抓", grabber.calls == 0, f"抓了 {grabber.calls} 次")

    section("红线：pet.db / mind.db 零写入、屏内容不进画像库")
    pet_utterance_after = _rows(app.config.db_path)
    pet_memory_after = len(app.memory_dao.recent(100))
    mind_frag_after = app.mind_store.fragment_count()
    check("pet.db 的 utterance 表一行没多", pet_utterance_after == pet_utterance_before,
          f"{pet_utterance_before} → {pet_utterance_after}")
    check("pet.db 的 memory 表一行没多", pet_memory_after == pet_memory_before,
          f"{pet_memory_before} → {pet_memory_after}")
    check("mind.db 一条碎片都没写（屏内容不进画像库）", mind_frag_after == mind_frag_before,
          f"{mind_frag_before} → {mind_frag_after}")
    ep = app.mind_store.ep()
    check("mind.db 情绪快照是空的（屏内容没混进去）", not ep.get("emotion_snapshot"), str(ep.get("emotion_snapshot")))

    section("gitignore 里钉住了老落点 data/notes（历史遗留，留着无害）")
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    check("data/notes/ 在忽略清单里", "data/notes/" in ignore, "")

    drain_threads()
    watch.restore()                             # 别把打过补丁的 shot.save_temp 留给别人用
    box.cleanup()
    return summary("抓屏笔记")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
