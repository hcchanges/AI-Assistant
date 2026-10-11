"""帮聊那一则"上屏满 3 分钟绝对清除 + 连承载它的聊天窗一并收起"（v2-social-card-timeout-absolute）的自动测试。

口径（用户拍板，取代 v2-social-card-timeout 的老口径）：

1. **绝对 180 秒**：卡片**上屏那一拍**（`_start_card_watch`）就起一只 `CARD_CLEAR_S` 的
   单发表——数的是"这一则在屏幕上活多久"，跟人在不在跟前没关系；
2. **去抖动彻底**：人离开 / 回来（气泡失焦、被收起，微信不在前台 / 换了页，重新激活且
   仍在原单聊）**只记 `_card_away` 这个事实**——走开不提前清、回来不停表、抖动多少次都
   不重计（老口径正是在 `_note_card_back` 里 `stop()`，真机 37 次起表只等到 1 次清零）；
3. **到点收窗**：清掉那一则 + `close_bubble()`（淡出）把承载它的 `ChatBubble` 收起，不留
   空窗；这一拍框上要是挂着**别条线**的按钮，那一问不是"空窗"，框留着；
4. **唯一撤表的是「复制回复」**：内容取走了就不清。

`tests/test_social_card_timeout.py` 走的是同一套口径、外加"离开 / 回来那几条判据各自算不
算数"的边界；本模块专管"绝对"这一层：人在跟前也照清、抖动不重计、到点收窗、那三行日志。

计时那几节把 `CARD_CLEAR_S` 临时改小（0.3 / 1.2 秒）**跑真的 Qt 定时器**——不是"直接叫
一遍超时函数"那种假绿；真机上那一档 180 秒在红线一节里按真值读一遍。

红线：pet.db / mind.db 零写入；零新依赖；不模拟按键，**测试自身也不碰剪贴板**（写剪贴板
那只手用记录员顶掉）；不动时钟口径（这一整条线只读 `CARD_CLEAR_S` 这个秒数，不读任何
钟）；`config.yaml` / `.vscode` 一字不碰。
"""

from __future__ import annotations

import ast
import inspect
import logging
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, note, pump, section, summary

import core.bootstrap as bootstrap_mod
from ui.chat_bubble import ROLE_SOCIAL_DRAFT

DRAFT = "这周加班去不了。"
WHY = "直接说"

# `core/bootstrap.py` 顶层本来就 import 的那几个（v2-social-card-timeout-absolute 一个都没多要）。
_BASE_REFS = frozenset({
    "PySide6", "__future__", "brain", "core", "data", "focus", "life", "logging",
    "math", "memory", "monitor", "pet", "scheduler", "ui", "work",
})

# 卡片这条线上的那几条腿（红线那节整段读一遍源码）。
_WATCH_METHODS = (
    "_start_card_watch", "_note_card_left", "_note_card_back", "_on_card_window_left",
    "_on_card_window_back", "_in_original_chat", "_on_foreground_for_card",
    "_on_card_clear_due",
)


def _cards(bubble) -> list[str]:
    """记录区里眼下挂着哪几则帮聊（那一整则打的是 `ROLE_SOCIAL_DRAFT` 标记）。"""
    return [
        bubble._history.item(row).text()
        for row in range(bubble._history.count())
        if bubble._history.item(row).data(ROLE_SOCIAL_DRAFT)
    ]


def _show_card(app, body: str = DRAFT, why: str = WHY) -> None:
    """把那一则摆上屏：走装配层**真那条腿**（`_on_social_candidates` 用的就是它）。"""
    app._show_social_draft(body, why)
    pump(0.05)


def _shorten_clear(seconds: float) -> float:
    """把"3 分钟"临时改小，返回还原用的原值。"""
    original = bootstrap_mod.CARD_CLEAR_S
    bootstrap_mod.CARD_CLEAR_S = seconds
    return original


def _rows(db_path, table: str) -> int:
    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


class _Grab(logging.Handler):
    """把装配层那条线的日志抄一份（只为验"上屏 / 清除 / 收窗"那三行真的打了）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage() % record.args
                              if record.args else record.getMessage())
        except Exception:      # noqa: BLE001 —— 抄日志失败不该把用例带崩
            self.lines.append(record.getMessage())


def main() -> int:
    box, stub, app = make_env()
    bubble = app.chat_bubble
    real_clear_s = bootstrap_mod.CARD_CLEAR_S

    # 写剪贴板那只手换成记录员：本模块**一次都不碰真剪贴板**（红线），而"点了复制"整条链
    # （按钮 → `copy_requested` → `_on_social_copy`）一个字不改。
    copied: list[str] = []
    original_copy = app._copy_social_draft
    app._copy_social_draft = lambda draft: (copied.append(draft), True)[1]

    grab = _Grab()
    log_boot = logging.getLogger("core.bootstrap")
    _old_level = log_boot.level
    log_boot.setLevel(logging.DEBUG)        # 那三行是 debug，用例自己把闸放开
    log_boot.addHandler(grab)

    utterance_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------------------------------------------------------------- A0
        section("A0 上屏那一拍就起绝对表（真值：180 秒的单发；离开 / 回来都不动它）")
        _show_card(app)
        check("★ 表在上屏那一刻就是活的", app._card_clear_timer.isActive() is True, "")
        check("★ 表长就是 `CARD_CLEAR_S` 的真值（180 秒 = 180000 毫秒）",
              app._card_clear_timer.interval() == int(real_clear_s * 1000) == 180_000,
              f"interval={app._card_clear_timer.interval()}ms，常量={real_clear_s}")
        check("单发定时器：真到点也只打一枪", app._card_clear_timer.isSingleShot() is True, "")
        check("★ 上屏这一行日志打出来了（一眼看得见绝对表起了）",
              any("上屏：起一只绝对 180 秒的表" in line for line in grab.lines),
              repr(grab.lines[-2:]))
        check("帮聊那一则的状态：卡片在、没被复制、没人离开",
              (app._card_open, app._card_copied, app._card_away) == (True, False, False),
              f"open={app._card_open} copied={app._card_copied} away={app._card_away}")
        check("`_card_away` 只是记事实那一格：没离开就是 False，表不归它管",
              app._card_away is False, "")

        before = app._card_clear_timer.remainingTime()
        pump(0.1)
        after = app._card_clear_timer.remainingTime()
        check("★ 什么都没发生也一直在倒数（不是等谁发话才起表）",
              0 <= after < before, f"{before}ms -> {after}ms")

        # ---------------------------------------------------------------- A1
        section("A1 口径反转的正面用例：人一直坐着看着，满 180 秒照样清 + 照样收窗")
        shortened = _shorten_clear(0.3)
        try:
            grab.lines.clear()
            _show_card(app)
            check("表按改小后的数起（本模块临时改成 0.3 秒）",
                  app._card_clear_timer.interval() == 300,
                  f"{app._card_clear_timer.interval()}ms")
            spots: list[bool] = []
            for _ in range(3):                 # 全程采样，"离开"这一格一次都不该立起来
                pump(0.04)
                spots.append(app._card_away)
            check("★ 全程人都在跟前（采样三次都是 False）",
                  not any(spots) and app._card_away is False, repr(spots))
            check("采样这会儿还没到点：卡片、框都在",
                  len(_cards(bubble)) == 1 and bubble.isVisible() is True,
                  f"{len(_cards(bubble))} 条 / visible={bubble.isVisible()}")
            pump(0.5)                          # "3 分钟到了"
            check("★ 到点：那一则清掉了（老口径在这儿是'人在跟前不清'）",
                  _cards(bubble) == [], repr(_cards(bubble)))
            check("★ 到点：承载它的聊天窗也收起了（淡出走完，`isVisible()` 变 False）",
                  bubble.isVisible() is False, f"visible={bubble.isVisible()}")
            check("★ 清除那一行日志写明了这 180 秒里人在不在",
                  any("放了绝对" in line and "清掉它" in line and "人就在跟前" in line
                      for line in grab.lines), repr(grab.lines[-4:]))
            check("★ 收窗那一行日志也打了",
                  any("聊天窗跟着收起" in line for line in grab.lines),
                  repr(grab.lines[-2:]))
            check("清完之后状态归位、表也停了",
                  (app._card_open, app._card_away, app._card_clear_timer.isActive())
                  == (False, False, False),
                  f"open={app._card_open} away={app._card_away} "
                  f"active={app._card_clear_timer.isActive()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- A2
        section("A2 去抖动彻底：离开 / 回来抖四轮，表一次没重计，到点照样清 + 收窗")
        shortened = _shorten_clear(1.2)
        try:
            _show_card(app)
            first = app._card_clear_timer.remainingTime()
            check("上屏时剩余就是整只表（本模块临时改成 1.2 秒）",
                  0 < first <= 1200, f"{first}ms")
            pump(0.3)
            marks: list[bool] = []
            for _ in range(4):                 # 真机 07:52:39 / :41 / :42 / :43 那几秒就是这个样子
                bubble.user_left.emit()
                pump(0.04)
                marks.append(app._card_away)
                bubble.user_back.emit()
                pump(0.04)
                marks.append(app._card_away)
            check("★ `_card_away` 跟着抖（离开 True、回来 False），四轮一模一样",
                  marks == [True, False] * 4, repr(marks))
            left = app._card_clear_timer.remainingTime()
            check("★ 抖完剩下的时间只减不增（一次都没重计：老口径每轮离开都把它拉回满格）",
                  0 <= left < first, f"{first}ms -> {left}ms")
            check("★ 表还是上屏那一只：长没变、还在跑",
                  app._card_clear_timer.isActive() is True
                  and app._card_clear_timer.interval() == 1200,
                  f"active={app._card_clear_timer.isActive()} "
                  f"interval={app._card_clear_timer.interval()}ms")
            check("抖动完了卡片还在（还没到点）", len(_cards(bubble)) == 1, "")
            pump(1.2)
            check("★ 到点算的是**上屏那一刻**：抖多少轮都拦不住这一次清除",
                  _cards(bubble) == [], repr(_cards(bubble)))
            check("★ 到点那一下框也收起了", bubble.isVisible() is False,
                  f"visible={bubble.isVisible()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- A3
        section("A3 唯一能撤表的那条路：点了「复制回复」→ 表撤了、到点也不清、框不收")
        copied.clear()
        shortened = _shorten_clear(0.3)
        try:
            _show_card(app)
            check("上屏就起着表（先摆成'在跑'这一档）",
                  app._card_clear_timer.isActive() is True, "")
            bubble._copy_button.click()        # 走真按钮 → `_on_social_copy`
            pump(0.05)
            check("★ 正文真进了写剪贴板那只手（本模块用记录员顶掉，不碰真剪贴板）",
                  copied == [DRAFT], repr(copied))
            check("★ 表当场就撤了", app._card_clear_timer.isActive() is False,
                  f"active={app._card_clear_timer.isActive()}")
            pump(0.6)
            check("★ 到点也不清：那一则与框原样留着",
                  len(_cards(bubble)) == 1 and bubble.isVisible() is True,
                  f"{len(_cards(bubble))} 条 / visible={bubble.isVisible()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- A4
        section("A4 收窗是'先清卡、后收框'：那一声 `user_left` 不把'人离开了'立回来")
        shortened = _shorten_clear(0.3)
        try:
            _show_card(app)
            bubble.user_back.emit()            # 先摆成"人在跟前"
            pump(0.05)
            check("先摆成人在跟前（`_card_away` = False）", app._card_away is False, "")
            seen: list[tuple[int, bool]] = []

            def _spy() -> None:
                """`close_bubble` 报 `user_left` 那一刻的现场（几张卡、卡还在不在手上）。"""
                seen.append((len(_cards(bubble)), app._card_open))

            bubble.user_left.connect(_spy)
            try:
                pump(0.6)
            finally:
                try:
                    bubble.user_left.disconnect(_spy)
                except Exception:          # noqa: BLE001 —— 断开失败不该把用例带崩
                    pass
            check("★ 那一声响的时候：卡已经清了、`_card_open` 也已经放倒（顺序就是先清卡后收框）",
                  seen and seen[-1] == (0, False), repr(seen))
            check("★ 那一声也没把 `_card_away` 立回来（门口那道闸挡住了）",
                  app._card_away is False, f"away={app._card_away}")
            check("清完之后状态归位、框是收着的",
                  (app._card_open, app._card_clear_timer.isActive(), bubble.isVisible())
                  == (False, False, False),
                  f"open={app._card_open} active={app._card_clear_timer.isActive()} "
                  f"visible={bubble.isVisible()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- A5
        section("A5 到点那拍框上挂着别条线的按钮：只清卡片，框不收（那一问不是空窗）")
        shortened = _shorten_clear(0.3)
        try:
            _show_card(app)
            pump(0.05)
            bubble.show_offer("notes")         # 笔记那条线的按钮排上来了（主题不带 social 前缀）
            pump(0.05)
            check("框上挂着笔记那一问（`offer_theme` = 'notes'）",
                  bubble.offer_theme == "notes", repr(bubble.offer_theme))
            pump(0.6)
            check("★ 帮聊那一则照清（'到点就清'这一条不打折）",
                  _cards(bubble) == [], repr(_cards(bubble)))
            check("★ 但框不收：别条线那一问还挂着（收掉等于替用户把它作废）",
                  bubble.isVisible() is True and bubble.offer_theme == "notes",
                  f"visible={bubble.isVisible()} theme={bubble.offer_theme!r}")
            check("★ 日志里写明了'框不收'这一档",
                  any("别条线的按钮" in line for line in grab.lines),
                  repr(grab.lines[-3:]))
            bubble.hide_offer()                # 收拾干净再进下一节（只收那排按钮）
            pump(0.05)
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- A6
        section("A6 红线：180 秒真值 / 零新依赖 / 不动时钟口径 / 两库零写入")
        check("★ `CARD_CLEAR_S` 的真值是 180 秒（红线：满 3 分钟）",
              real_clear_s == 180.0, repr(real_clear_s))
        check("清完之后常量还回真值（本模块只在节里临时改小）",
              bootstrap_mod.CARD_CLEAR_S == 180.0, repr(bootstrap_mod.CARD_CLEAR_S))

        source = inspect.getsource(bootstrap_mod)
        refs: set[str] = set()
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                refs.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                refs.add(node.module.split(".")[0])
        check("★ `bootstrap.py` 顶层 import 还是原来那几样（零新依赖）",
              refs <= _BASE_REFS, repr(sorted(refs - _BASE_REFS)))

        watch_src = textwrap.dedent("".join(
            inspect.getsource(getattr(bootstrap_mod.DesktopPetApp, name))
            for name in _WATCH_METHODS
        ))
        tree = ast.parse(watch_src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        check("★ 不动时钟口径：这一整条线只读 `CARD_CLEAR_S` 这个秒数，不读任何钟、"
              "也不自己算时长",
              not ({"monotonic", "time", "now", "QElapsedTimer", "startTimer", "elapsed"}
                   & (names | attrs)),
              "、".join(sorted({"monotonic", "time", "now", "QElapsedTimer", "startTimer",
                                "elapsed"} & (names | attrs))) or "（一处都没有）")
        ui_src = inspect.getsource(bootstrap_mod.DesktopPetApp._init_ui)
        check("★ 量的那只手还是 Qt 的单发表（没另起线程、没自己轮询）",
              "self._card_clear_timer = QTimer()" in ui_src
              and "self._card_clear_timer.setSingleShot(True)" in ui_src
              and "QThread" not in watch_src and "threading" not in watch_src,
              "")
        check("★ 去抖动那两处是从源码里砌死的：`_note_card_back` 里没有停表 / 重计",
              ".stop(" not in inspect.getsource(bootstrap_mod.DesktopPetApp._note_card_back)
              and ".start(" not in inspect.getsource(
                  bootstrap_mod.DesktopPetApp._note_card_back),
              "")
        check("★ 到点那一下走的是 `close_bubble()`（淡出那条现成的路，不是硬藏）",
              "close_bubble()" in inspect.getsource(
                  bootstrap_mod.DesktopPetApp._on_card_clear_due)
              and "hide()" not in inspect.getsource(
                  bootstrap_mod.DesktopPetApp._on_card_clear_due),
              "")

        clip = [n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Attribute) and n.attr == "clipboard"]
        check("★ 碰剪贴板的手仍然只有一只（新增这几条腿一处都没伸手）",
              len(clip) == 1, f"{len(clip)} 处")

        banned = {"keybd_event", "SendInput", "uiautomation", "pyautogui", "pynput",
                  "win32clipboard", "clipboard", "OcrReader", "grab_foreground_window",
                  "grab_chat", "run_async", "_read_social_screen"}
        check("★ 这一整条线里没有模拟按键 / 碰剪贴板 / 抓屏认字 / 后台任务那几样",
              not (banned & (names | attrs)),
              "、".join(sorted(banned & (names | attrs))) or "（一处都没有）")

        from life.screen import social as smod
        check("★ 触发闸门 / 按需抓拍的老口径一个没动",
              smod.PENDING_SNAP_MS == 500 and smod.OFFER_HANG_S == 90.0,
              f"PENDING_SNAP_MS={smod.PENDING_SNAP_MS} OFFER_HANG_S={smod.OFFER_HANG_S}")
        from life.screen import social_window as swmod
        check("第⑦条那条锚（输入框上沿 0.838 + 边框线判定）原样在",
              swmod.INPUT_TOP_FRAC == 0.838 and hasattr(swmod, "_detect_input_top_by_edge"),
              f"INPUT_TOP_FRAC={getattr(swmod, 'INPUT_TOP_FRAC', None)}")

        check("★ 两库零写入（utterance 条数没变）",
              _rows(app.config.db_path, "utterance") == utterance_before,
              f"{utterance_before} -> {_rows(app.config.db_path, 'utterance')}")
        check("★ 两库零写入（记忆碎片数没变）",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} -> {app.mind_store.fragment_count()}")
        note(f"（红线这两条从构造完那一刻起算：utterance={utterance_before} "
             f"碎片={mind_before}）")

        drain_threads()
    finally:
        app._copy_social_draft = original_copy
        bootstrap_mod.CARD_CLEAR_S = real_clear_s
        log_boot.removeHandler(grab)
        log_boot.setLevel(_old_level)

    box.cleanup()
    return summary("帮聊那一则上屏满 3 分钟绝对清除并收起聊天窗"
                   "（v2-social-card-timeout-absolute）")


if __name__ == "__main__":
    raise SystemExit(main())
