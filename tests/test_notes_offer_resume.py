"""专注结束把压下的笔记询问补出来（v2-focus-notes-offer-resume 14-F）的自动测试。

真机那条腿：笔记**只在专注里攒**，所以"要不要发你"那一问也就必然在专注里到点；
14-C 让专注期不弹（用户在看网课，眼前突然多一个要点的框，既打扰又容易点不着），
可服务侧是在 emit **之前**就落 `_awaiting = True` + `_remember_ask()` 的
（`life/screen/notes.py:447-449`，还带一个 7 天戳）——于是被 14-C 挡下的那一问是
**丢掉**，不是"顺延"：笔记一直挂着待发，7 天内不会再问。

14-F 补的就是这一下：专注**结束**（`_is_focusing` 由真转假的那条沿）时，若服务侧
那一问还等着答复（`awaiting_confirm`——判据看的就是它，不在装配层另记一本账），
就补一次 `show_offer()`。装配层单侧，服务侧一行不动。

红线：零新依赖；pet.db / mind.db 零写入；不碰 `life/screen/notes.py` 服务侧；
14-C 那道"专注期不弹"的闸一个字没松（① 就是它的钉子）。
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
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

from core.bootstrap import DesktopPetApp  # noqa: E402
from life.screen.notes import BATCH_SIZE, ScreenNotesService  # noqa: E402


class FakeReader:
    def __init__(self) -> None:
        self.text = ""
        self.calls = 0

    def read(self, image) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假抓屏：`next()` 才换一屏（抓屏笔记有均值哈希去重，见 harness.varied_image）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.seq = 0

    def next(self) -> None:
        self.seq += 1

    def __call__(self, pet_rect=None):
        self.calls += 1
        return varied_image(self.seq)


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    try:
        pet = app.pet_window
        manual = app.pomodoro_service.timer          # 手动那套（点「开始专注」）
        auto = app.screen_focus_timer                # 自动那套（前台工作软件）
        notes = app.screen_notes
        bubble = app.chat_bubble
        pet.show()
        pump(0.05)

        # 开抓屏笔记，取景与认字都换成假的（离屏跑不动真截图、真 OCR）
        app.config.notes.enabled = True
        reader, grabber = FakeReader(), FakeGrabber()
        notes._reader, notes._grabber = reader, grabber
        notes._pet_rect = lambda: None
        notes._idle = lambda: 0.0
        stub.notes_result = {"title": "14-F", "key_points": ["专注里攒的"],
                             "todos": [], "questions": []}

        # 判据只能看**气泡**（`show_offer` / `_offer_theme`），不能数 `bus.notes_offer`：
        # 那个信号 bootstrap 的槽早退了也照发——14-C 那一版就是这么看着全绿、界面上没问的。
        asked: list[str] = []
        spoken: list[str] = []
        offers: list[int] = []
        delivered: list[str] = []
        real_show = bubble.show_offer

        def spy_show(theme: str) -> None:
            asked.append(theme)
            real_show(theme)

        bubble.show_offer = spy_show
        app.bus.pet_speak.connect(spoken.append)
        app.bus.pet_speak_proactive.connect(spoken.append)
        app.bus.notes_offer.connect(offers.append)
        app.bus.notes_delivered.connect(delivered.append)

        def feed_until_offer(tag: str, limit: int = 40) -> bool:
            """一直喂屏（每拍等认字回来）直到服务真问出一句，最多喂 `limit` 拍。

            抓满 `BATCH_SIZE` 屏才整理一轮、才可能问，而"攒到第几屏"跟这一轮开始时
            缓冲区里剩几份有关——所以不写死拍数，问出来就停。
            """
            before = len(offers)
            for index in range(limit):
                reader.text = f"{tag}｜第{index}屏"
                grabber.next()
                notes.snap_now()
                settle_notes(notes)
                if len(offers) > before:
                    return True
            return False

        def pass_a_week() -> None:
            """把"问过了"的记录抹掉 = 假装距上次询问过了一周（只影响询问频率那道闸）。"""
            notes._last_ask_at = None
            try:
                notes._state_path().unlink()
            except OSError:
                pass

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        # ---------- ① 专注里到点：照旧不弹，但记成「欠着」 ----------

        section("① 专注里攒满一轮：14-C 照旧不弹，这一问记成「欠着」（不是丢掉）")

        manual.start_focus()
        pump(0.05)
        check("手动番茄钟进了专注", app._is_focusing(), str(manual.phase))
        check("★ 专注里喂满一轮 → 服务真问了（`bus.notes_offer` 到点）",
              feed_until_offer("14-F 专注"), f"{len(offers)} 次")
        check("★ 到点那一刻确实还在专注里（笔记只在专注里攒，这就是那一拍）",
              app._is_focusing(), f"_is_focusing={app._is_focusing()}")
        check("★ 专注期照旧不弹框（14-C 那道闸一个字没松）",
              asked == [] and bubble._offer_theme == "",
              f"asked={asked} theme={bubble._offer_theme!r}")
        check("服务侧照旧当「问过了」（7 天戳已落、笔记还挂着待发）",
              notes.awaiting_confirm and notes.pending > 0,
              f"awaiting={notes.awaiting_confirm} pending={notes.pending}")
        check("★ 但这一问没当作丢掉（14-F：还没补，等专注结束那条沿）",
              app._notes_offer_resumed is False, repr(app._notes_offer_resumed))

        # ---------- ② 专注结束那条沿：补出来 ----------

        section("② 专注一结束：把压下的那一问补出来（就是那一问，不另起一问）")

        count = notes.pending
        spoken.clear()
        manual.reset()                      # 点「结束专注」→ 阶段信号 → 专注结束那条沿
        pump(0.05)
        check("★ 专注结束那一下把那一问补出来了", asked == ["notes"], str(asked))
        check("★ 补的时候真问了那一句、条数就是攒着的（不是空框、不是一个 0）",
              f"我攒了 {count} 条笔记，要不要发你？" in spoken, " | ".join(spoken) or "（安静）")
        check("★ 补过这一笔就记上了（同一问不补第二遍）",
              app._notes_offer_resumed is True, repr(app._notes_offer_resumed))
        check("气泡上挂的确实是笔记那一问", bubble._offer_theme == "notes",
              repr(bubble._offer_theme))
        check("补的还是**同一问**：服务侧照样在等答复（没凭空多问一遍）",
              notes.awaiting_confirm and len(offers) == 1,
              f"awaiting={notes.awaiting_confirm} offers={len(offers)}")

        # ---------- ③ 判重：再结束一次不重复补 ----------

        section("③ 判重：同一条沿再走一遍，不重复补（防抖动多次触发）")

        asked.clear()
        spoken.clear()
        app._notes_offer_resumed = True     # ② 已经补过
        app._focus_quiet_on = True          # 再摆一次"刚才是专注态"，沿还会来
        manual.reset()                      # 再发一条专注结束的沿
        pump(0.05)
        check("★ 不重复补（欠的那一笔销了，就算沿再来也不补）",
              asked == [] and spoken == [], f"{asked} | {spoken}")

        # ---------- ④ 那一问已经了了：不补 ----------

        section("④ 那一问已经在别处了了（点头发出去过）→ 专注结束不补")

        app.chat_bubble.offer_reply.emit("好呀")     # 真轻按钮 → 装配层分发 → 笔记服务
        drain_threads()
        pump(0.05)
        check("点头后笔记真发出去了（那一问确实了了）",
              len(delivered) == 1 and not notes.awaiting_confirm,
              f"delivered={len(delivered)} awaiting={notes.awaiting_confirm}")

        app._notes_offer_resumed = False    # 摆出「还没补 + 刚到沿」，但那一问已经不等着了
        app._focus_quiet_on = True
        asked.clear()
        spoken.clear()
        app._sync_scene_guard()
        check("★ 那一问已经不等着答复了 → 不补（不凭空冒一个空框）",
              asked == [] and spoken == [], f"{asked} | {spoken}")

        # ---------- ⑤ 自动那套那条沿也补 ----------

        section("⑤ 自动番茄钟（前台工作软件）：它那条沿也补")

        manual.reset()
        auto.reset()
        pass_a_week()                       # 上一问已经了了，7 天闸别挡着这一段
        auto.enter("chrome.exe")            # 前台落回工作软件 → 自动那套开跑
        pump(0.05)
        check("自动那套进了专注", auto.running and app._is_focusing(),
              f"running={auto.running} focusing={app._is_focusing()}")
        check("★ 自动那套的专注期同样不弹、也没当作丢掉",
              feed_until_offer("14-F 自动") and asked == []
              and app._notes_offer_resumed is False,
              f"asked={asked} resumed={app._notes_offer_resumed}")
        count = notes.pending
        spoken.clear()
        auto.reset()                        # 自动那套结束那条沿（走 changed → _sync_scene_guard）
        pump(0.05)
        check("★ 自动那套的专注结束也补（那条沿挂在 _sync_scene_guard 上，不是手动那条信号）",
              asked == ["notes"], str(asked))
        check("补的那句条数也对", f"我攒了 {count} 条笔记，要不要发你？" in spoken,
              " | ".join(spoken) or "（安静）")

        # ---------- ⑤-2 当场露过面的那一问：专注进出一次也不再补 ----------

        section("⑤-2 那一问在非专注期当场露过面 → 专注进出一次也不再补")

        asked.clear()
        spoken.clear()
        app._notes_offer_resumed = False        # 摆出「还没补」，看第二道闸拦不拦得住
        app._on_notes_offer(3)                  # 非专注期到点：当场露面
        check("★ 非专注期到点就当场问（不经补发那条路）",
              asked == ["notes"] and app._notes_offer_resumed is True,
              f"{asked} resumed={app._notes_offer_resumed}")
        check("★ 那一问在服务侧还等着答复（第一道闸这时是放行的）",
              notes.awaiting_confirm is True, repr(notes.awaiting_confirm))
        asked.clear()
        spoken.clear()
        app._focus_quiet_on = True
        app._sync_scene_guard()                 # 摆出「专注刚结束」的那条沿
        check("★ 服务侧还等着答复也不重复补——拦它的是第二道闸（`_notes_offer_resumed`）",
              asked == [] and spoken == [], f"{asked} | {spoken}")

        # ---------- ⑥ 红线 ----------

        section("⑥ 红线：14-C 的闸没松 / 服务侧一行没动 / 零新依赖 / 两库零写入")

        src_offer = inspect.getsource(DesktopPetApp._on_notes_offer)
        guard = src_offer.index("if self._is_focusing():")
        check("★ 14-C 那道「专注期不弹」的闸原样还在（压下的那一支直接 return）",
              "return" in src_offer[guard:guard + 90]
              and "_show_notes_offer" not in src_offer[guard:guard + 90], "")

        src_notes = inspect.getsource(ScreenNotesService)
        call = src_notes.index("self._bus.notes_offer.emit(")
        before = src_notes[max(0, call - 400):call]
        check("服务侧照旧「先落状态、再 emit」（14-F 没碰它）",
              "self._awaiting = True" in before and "self._remember_ask()" in before, "")
        check("服务侧没有 14-F 的东西（改的全在装配层）",
              "offer_resumed" not in src_notes and "resume_notes" not in src_notes, "")

        src_resume = inspect.getsource(DesktopPetApp._resume_notes_offer)
        check("★ 判据看服务侧：认的是 `_awaiting_offer_kind` 那个 kind，不数装配层自己记的账",
              '_awaiting_offer_kind != "notes_offer"' in src_resume, "")
        body = src_resume.split('"""')[-1]   # 只看正文：说明文字里正好写着「不再 emit」
        check("★ 补的那一下复用现成的 show_offer 路径，**不重新 emit**、不重打 7 天戳",
              "_show_notes_offer(" in body
              and "notes_offer.emit(" not in body
              and "_remember_ask" not in body, "")
        check("★ 防重复补的标志就是 `_notes_offer_resumed`（补完置上）",
              "_notes_offer_resumed = True" in body
              and "if self._notes_offer_resumed:" in body, "")
        check("★ 那本账的真身是服务侧的 `awaiting_confirm`（装配层只当读数）",
              "awaiting_confirm" in inspect.getsource(DesktopPetApp._awaiting_offer_kind.fget), "")

        import ast

        refs: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(sys.modules["core.bootstrap"]))):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(a.name for a in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api", "fuzzywuzzy", "regex"}
        check("装配层没多一个第三方 import", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        check("红线：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("红线：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        # ---------- ⑦ 退出程序那一下不补 ----------

        section("⑦ 退出程序：同一摊状态，那条沿不补（程序都要关了，不该再冒一句）")

        # 先把"这条沿本来是**会**补的"钉住：同一摊状态，只差"不退出"
        app._notes_offer_resumed = False
        app._focus_quiet_on = True
        asked.clear()
        spoken.clear()
        app._sync_scene_guard()
        check("★ 同一摊状态：正常那条沿是会补的（下面那条才算数）",
              asked == ["notes"], f"{asked} | {spoken}")

        # 再摆回同一摊状态，改走"退出程序"那条路（shutdown 里 screen_focus_timer.reset()
        # 也会发这条沿）
        app._notes_offer_resumed = False
        app._focus_quiet_on = True
        asked.clear()
        spoken.clear()
        app.shutdown()
        drain_threads()
        pump(0.05)
        check("★ 退出程序那一下不补（牌子立着，那条沿不再往外说话）",
              asked == [] and spoken == [], f"{asked} | {spoken}")
        check("退出的牌子立起来了", app._closing is True, repr(app._closing))
    finally:
        drain_threads()
        box.cleanup()

    return summary("专注结束补上被压下的笔记询问（14-F）")


if __name__ == "__main__":
    raise SystemExit(main())
