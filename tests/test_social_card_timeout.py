"""帮聊那一则"上屏满 3 分钟就清掉"（v2-social-card-timeout → -absolute）的自动测试。

用户拍板的口径（v2-social-card-timeout-absolute 起，取代老口径）：

1. 卡片**上屏那一拍**起一只 `CARD_CLEAR_S = 180` 秒的单发表——**绝对**的，数的是
   "这一则在屏幕上活多久"，跟人在不在跟前没关系；
2. 期间点了「复制回复」→ 撤表（内容已经被取走，留着无害）；
3. 到点 → 清空那一则、回到初始聊天态（输入框还在、建议气泡收起）+ 把承载它的聊天窗
   淡出收起（不留空窗）；
4. 用户**离开 / 回到对话窗口**（宠物浮窗失焦 / 被收起，或微信对话窗口不在前台，**任一
   即可**）只记 `_card_away` 这个事实：走开不提前清、回来不停表、抖动多少次都不重计
   ——老口径（离开才计、回来停表）真机上 37 次起表只等到 1 次清零，这一版把它拆了。

本模块把上面四条一个一个走一遍，外加"边界：两种离开判定各自都算数"与"红线"两节；
"人在跟前也照清 / 抖动不重计 / 到点收窗 / 收窗那几行日志"另起一节在
`tests/test_social_card_timeout_absolute.py`。

计时那几节把 `CARD_CLEAR_S` 临时改小（0.3 秒）**跑真的 Qt 定时器**——不是"直接叫一遍
超时函数"那种假绿：起表那一刻现读的就是模块常量，改小之后 `pump()` 一小会儿就等于
"3 分钟到了"。真机上那一档 180 秒在红线一节里按真值读一遍。

红线：pet.db / mind.db 零写入；零新依赖；不模拟按键，**测试自身也不碰剪贴板**（写剪贴板
那只手用记录员顶掉）；`config.yaml` / `.vscode` 一字不碰。
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, note, pump, section, summary, wait_for

import core.bootstrap as bootstrap_mod
from life.screen.social import PAGE_GROUP, PAGE_PERSONAL
from ui.chat_bubble import ROLE_SOCIAL_DRAFT

DRAFT = "这周加班去不了。"
WHY = "直接说"
CHAT_LINE = "嗯，我记着了。"

# `core/bootstrap.py` 顶层本来就 import 的那几个（v2-social-card-timeout 一个都没多要）。
_BASE_REFS = frozenset({
    "PySide6", "__future__", "brain", "core", "data", "focus", "life", "logging",
    "math", "memory", "monitor", "pet", "scheduler", "ui", "work",
})


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


def main() -> int:
    box, stub, app = make_env()
    bubble = app.chat_bubble
    real_clear_s = bootstrap_mod.CARD_CLEAR_S

    # 写剪贴板那只手换成记录员：本模块**一次都不碰真剪贴板**（红线），而"点了复制"整条
    # 链（按钮 → `copy_requested` → `_on_social_copy`）一个字不改。
    copied: list[str] = []
    original_copy = app._copy_social_draft
    app._copy_social_draft = lambda draft: (copied.append(draft), True)[1]

    try:
        # ---------------------------------------------------------------- S0
        section("S0 卡片上屏那一刻：状态是初始那三样、**绝对表已经跑起来了**")
        bubble.append_pet(CHAT_LINE)          # 一句普通聊天，用来验"清卡片不动普通记录"
        _show_card(app)
        check("★ 帮聊那一则上屏了（记录区里就一条打标的）", len(_cards(bubble)) == 1,
              f"{len(_cards(bubble))} 条")
        check("手上按着的是这一版的正文（「复制回复」有得抄）",
              bubble._copy_text == DRAFT, repr(bubble._copy_text))
        check("★ 卡片在、还没被复制、用户也没离开",
              (app._card_open, app._card_copied, app._card_away) == (True, False, False),
              f"open={app._card_open} copied={app._card_copied} away={app._card_away}")
        check("★ 上屏这一拍表就起了（口径：绝对 180 秒，数的是卡片活多久）",
              app._card_clear_timer.isActive() is True
              and app._card_clear_timer.interval() == int(real_clear_s * 1000),
              f"active={app._card_clear_timer.isActive()} "
              f"interval={app._card_clear_timer.interval()}ms")
        check("单发定时器：真跑起来也只打一枪",
              app._card_clear_timer.isSingleShot() is True, "")

        section("S0b 真那条腿走一遍：'帮我回' → 拟稿 → 卡片自己上屏，状态一样是初始那三样")
        stub.candidates_result = {"candidates": [{"draft": DRAFT, "why": WHY}]}
        app._on_chat_send("帮我回一下，同事问我周末加不加班")
        wait_for(lambda: len(_cards(bubble)) == 1, timeout=5.0)
        drain_threads()
        pump(0.1)
        check("★ 拟稿那条腿走完，卡片自己上屏了（不是本模块硬摆上去的）",
              len(_cards(bubble)) == 1, f"{len(_cards(bubble))} 条")
        check("★ 真流程出来的卡片：三样状态与上一条一样，绝对表也起了",
              (app._card_open, app._card_copied, app._card_away,
               app._card_clear_timer.isActive()) == (True, False, False, True),
              f"open={app._card_open} copied={app._card_copied} away={app._card_away} "
              f"active={app._card_clear_timer.isActive()}")

        # 这一拍之后的库计数才是"卡片这条线自己写的"那部分（拟稿那一趟的记账不掺进来）。
        utterance_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        # ---------------------------------------------------------------- S1
        section("S1 不复制 + 到点 → 清空那一则、回初始聊天态、承载它的聊天窗一并收起")
        shortened = _shorten_clear(0.3)
        try:
            bubble.show_offer("social_card")  # 真机这一拍本来就挂着"要不要建攻略卡"那排按钮
            _show_card(app)                   # 上屏：绝对表就按这时候的 0.3 秒起
            check("★ 上屏这一拍起的表就是 `CARD_CLEAR_S`（本模块临时改成 0.3 秒）",
                  app._card_clear_timer.isActive() and app._card_clear_timer.interval() == 300,
                  f"active={app._card_clear_timer.isActive()} "
                  f"interval={app._card_clear_timer.interval()}ms")
            bubble.user_left.emit()           # 离开（气泡失焦）
            pump(0.05)
            check("★ 离开这一拍**不动那只表**（还是上屏那一刻起跑的那一只）",
                  app._card_clear_timer.isActive() is True
                  and app._card_clear_timer.interval() == 300,
                  f"active={app._card_clear_timer.isActive()} "
                  f"interval={app._card_clear_timer.interval()}ms")
            check("这一拍状态：卡片还在、没被复制、标记成'离开了'",
                  (app._card_open, app._card_copied, app._card_away) == (True, False, True),
                  f"open={app._card_open} copied={app._card_copied} away={app._card_away}")
            check("离开这一拍框还开着（离到点上还有一会儿）", bubble.isVisible() is True, "")
            pump(0.6)                         # "3 分钟到了"
            check("★ 满 3 分钟：那一则从记录区里没了", _cards(bubble) == [], repr(_cards(bubble)))
            check("★ 手上那三样跟着作废（正文 / 按钮 / 那一行）",
                  bubble._copy_text == "" and bubble._copy_button is None
                  and bubble._copy_row is None,
                  f"text={bubble._copy_text!r} button={bubble._copy_button}")
            check("★ 回初始聊天态：输入框还在、placeholder 照旧、能打字",
                  bubble._input.placeholderText() == "想跟我说点什么？"
                  and bubble._input.isEnabled(),
                  f"{bubble._input.placeholderText()!r} enabled={bubble._input.isEnabled()}")
            check("★ 建议气泡一并收起（口径：无建议气泡）",
                  bubble._offer_row.isVisible() is False and bubble.offer_theme == "",
                  f"visible={bubble._offer_row.isVisible()} theme={bubble.offer_theme!r}")
            check("★ 承载这一则的聊天窗跟着收起（淡出那条路走完，不留空窗）",
                  bubble.isVisible() is False, f"visible={bubble.isVisible()}")
            check("普通聊天一条没动（清的是打标的那一则，不是整框）",
                  CHAT_LINE in "\n".join(
                      bubble._history.item(r).text() for r in range(bubble._history.count())),
                  "")
            check("清完之后状态归位、表也停了",
                  (app._card_open, app._card_away, app._card_clear_timer.isActive())
                  == (False, False, False),
                  f"open={app._card_open} away={app._card_away} "
                  f"active={app._card_clear_timer.isActive()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- S2
        section("S2 点过「复制回复」的卡片：表撤了、到点也不清（内容已经取走了）")
        copied.clear()
        shortened = _shorten_clear(0.3)
        try:
            _show_card(app)
            check("上屏就把表起了（先摆成'在跑'这一档）",
                  app._card_clear_timer.isActive() is True, "")
            bubble._copy_button.click()       # 走真按钮 → `_on_social_copy`
            pump(0.05)
            check("★ 按钮那一下真走到了写剪贴板那只手（记录员收到正文）",
                  copied == [DRAFT], repr(copied))
            check("★ 复制这一拍就把表撤了、并且记下'已取走'",
                  app._card_copied is True and app._card_clear_timer.isActive() is False,
                  f"copied={app._card_copied} active={app._card_clear_timer.isActive()}")
            bubble.user_left.emit()           # 复制完之后离开
            pump(0.05)
            check("★ 已复制的卡片：离开也不会把表弄回来（撤了就是撤了）",
                  app._card_clear_timer.isActive() is False,
                  f"active={app._card_clear_timer.isActive()}")
            check("'离开'那一格压根没被置位（复制过的不算离开）",
                  app._card_away is False, f"away={app._card_away}")
            pump(0.6)
            check("★ 满 3 分钟也不清：那一则还在、正文还在按着",
                  len(_cards(bubble)) == 1 and bubble._copy_text == DRAFT,
                  f"{len(_cards(bubble))} 条 / {bubble._copy_text!r}")
            check("框也还开着（复制过的那一则不清、不收窗）",
                  bubble.isVisible() is True, f"visible={bubble.isVisible()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- S3
        section("S3 离开 → 回来抖动几轮：表一次都不重计（去抖动彻底），到点照样清")
        shortened = _shorten_clear(1.2)
        try:
            _show_card(app)
            first = app._card_clear_timer.remainingTime()
            check("上屏时剩余就是整只表（本模块临时改成 1.2 秒）",
                  0 < first <= 1200, f"{first}ms")
            pump(0.3)                          # 相当于"离开了一分钟"
            marks: list[bool] = []
            for _ in range(4):                 # 离开 / 回来来回抖四轮（真机 07:52 那几秒）
                bubble.user_left.emit()
                pump(0.04)
                marks.append(app._card_away)
                bubble.user_back.emit()
                pump(0.04)
                marks.append(app._card_away)
            check("★ 抖动 4 轮：`_card_away` 跟着起落（离开 True、回来 False）",
                  marks == [True, False] * 4, repr(marks))
            left = app._card_clear_timer.remainingTime()
            check("★ 抖动完剩下的时间只会更少、不会往回涨（一次都没重计过）",
                  0 <= left < first, f"{first}ms -> {left}ms")
            check("★ 回来之后表照旧在跑（老口径在这儿 stop() 了）",
                  app._card_clear_timer.isActive() is True
                  and app._card_clear_timer.interval() == 1200,
                  f"active={app._card_clear_timer.isActive()} "
                  f"interval={app._card_clear_timer.interval()}ms")
            pump(1.2)
            check("★ 到点算的是**上屏那一刻**：抖动多少轮都拦不住这一次清除",
                  _cards(bubble) == [], repr(_cards(bubble)))
            check("★ 到点那一下框也收起了", bubble.isVisible() is False,
                  f"visible={bubble.isVisible()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- S4
        section("S4 口径反转那一处：一直坐在对话窗口看着 —— 满 180 秒照样清、照样收窗")
        shortened = _shorten_clear(0.3)
        try:
            _show_card(app)
            check("★ 人一直在跟前（没离开过）：表照样跑着、这一格也没被误标成'离开了'",
                  app._card_clear_timer.isActive() is True and app._card_away is False,
                  f"active={app._card_clear_timer.isActive()} away={app._card_away}")
            pump(0.6)                          # "3 分钟到了"
            check("★ 老口径在这儿是'人在跟前不清'；这一版到点就清",
                  _cards(bubble) == [], repr(_cards(bubble)))
            check("★ 那一则的手上状态一并作废（正文 / 按钮）",
                  bubble._copy_text == "" and bubble._copy_button is None,
                  f"text={bubble._copy_text!r} button={bubble._copy_button}")
            check("★ 聊天窗也一并收起（人在跟前跟没人看着一样收）",
                  bubble.isVisible() is False, f"visible={bubble.isVisible()}")
            check("清完之后状态归位、表也停了",
                  (app._card_open, app._card_away, app._card_clear_timer.isActive())
                  == (False, False, False),
                  f"open={app._card_open} away={app._card_away} "
                  f"active={app._card_clear_timer.isActive()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- S5
        section("S5 边界：'离开'那两种判定各自都算数，'回来'得是原来那条单聊")
        shortened = _shorten_clear(30.0)       # 这一节反复进出，先给一只够长的表
        try:
            _show_card(app)
            bubble.user_back.emit()
            pump(0.05)
            check("先摆回'人在窗口里'这一档", app._card_away is False, "")
            bubble.user_left.emit()            # 甲：宠物浮窗失焦
            pump(0.05)
            check("★ 甲-1 宠物浮窗失焦 → 算离开（这一格立起来；表是上屏就起的，不归它管）",
                  app._card_away is True and app._card_clear_timer.isActive() is True, "")
            bubble.user_back.emit()
            pump(0.05)
            check("★ 甲-1b 回来只把这格抹掉、表照走（老口径在这儿 stop）",
                  app._card_away is False and app._card_clear_timer.isActive() is True, "")
            bubble.close_bubble(fade=False)    # 甲：把对话窗收起（按 × / Esc 那条腿）
            pump(0.05)
            check("★ 甲-2 对话窗被收起 → 也算离开（真收起那条路也报了那一声）",
                  app._card_away is True and app._card_clear_timer.isActive() is True, "")

            bubble.open_at(app.pet_window.frameGeometry())
            pump(0.05)
            check("甲-3 重新展开气泡 → 算回来（表照走，不重计）",
                  app._card_away is False and app._card_clear_timer.isActive() is True, "")

            app.social_reply.note_page_kind(PAGE_PERSONAL)   # 这一屏仍认得出是那条单聊
            app.screen_monitor.foreground_changed.emit("explorer.exe")
            pump(0.05)
            check("★ 乙-1 微信对话窗不在前台（换了别的程序）→ 算离开",
                  app._card_away is True and app._card_clear_timer.isActive() is True, "")
            app.screen_monitor.foreground_changed.emit("wechat.exe")
            pump(0.05)
            check("★ 乙-2 切回微信、而且仍是那条单聊 → 算回来（表照走，不重计）",
                  app._card_away is False and app._card_clear_timer.isActive() is True, "")

            app.screen_monitor.foreground_changed.emit("explorer.exe")
            pump(0.05)
            check("走出社交窗那一拍，服务自己把页型格清回空串（既有口径，"
                  "这条线不改它）——空串是**默认放行**，所以真机上'切回微信'多半落在这一档",
                  app.social_reply._page_kind == "", repr(app.social_reply._page_kind))
            app.social_reply.note_page_kind(PAGE_GROUP)       # 认出来的偏偏是群聊那一档
            app.screen_monitor.foreground_changed.emit("wechat.exe")
            pump(0.05)
            check("★ 乙-3 切回微信、可这一屏认出来**不是单聊** → 不算回来，还是算离开",
                  app._card_away is True and app._card_clear_timer.isActive() is True,
                  f"away={app._card_away} active={app._card_clear_timer.isActive()}")

            app.screen_monitor.foreground_changed.emit("")
            pump(0.05)
            check("乙-4 前台报回宠物自己（空进程名）→ 按'人在框跟前'处理",
                  app._card_away is False, f"away={app._card_away}")

            # 乙-5：换一则新的、表换成 0.3 秒，走一遍"走开 → 到点"这条整腿
            _shorten_clear(0.3)
            _show_card(app)
            app.social_reply.note_page_kind(PAGE_PERSONAL)
            app.screen_monitor.foreground_changed.emit("explorer.exe")   # 走开
            pump(0.05)
            check("走开这一拍就记上了'没人看着'", app._card_away is True, "")
            pump(0.6)
            check("★ 乙-5 这几种离开判定最后都能走到'到点就清 + 收窗'",
                  _cards(bubble) == [] and bubble.isVisible() is False,
                  f"{len(_cards(bubble))} 条 / visible={bubble.isVisible()}")
        finally:
            bootstrap_mod.CARD_CLEAR_S = shortened

        # ---------------------------------------------------------------- S6
        section("S6 红线：3 分钟就是 180 秒 / 零新依赖 / 不动既有那三条 / 两库零写入")
        check("★ `CARD_CLEAR_S` 的真值是 180 秒（红线：满 3 分钟）",
              real_clear_s == 180.0, repr(real_clear_s))
        check("清完之后常量还回真值（本模块只临时改小）",
              bootstrap_mod.CARD_CLEAR_S == 180.0, repr(bootstrap_mod.CARD_CLEAR_S))

        source = inspect.getsource(bootstrap_mod)
        refs = set()
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                refs.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                refs.add(node.module.split(".")[0])
        check("★ `bootstrap.py` 顶层 import 还是原来那几样（零新依赖）",
              refs <= _BASE_REFS, repr(sorted(refs - _BASE_REFS)))

        tree = ast.parse(source)
        clip = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "clipboard"]
        check("★ 碰剪贴板的手仍然只有一只（新增这几条腿一处都没伸手）",
              len(clip) == 1, f"{len(clip)} 处")

        new_src = textwrap.dedent("".join(
            inspect.getsource(getattr(bootstrap_mod.DesktopPetApp, name))
            for name in ("_start_card_watch", "_note_card_left", "_note_card_back",
                         "_on_card_window_left", "_on_card_window_back", "_in_original_chat",
                         "_on_foreground_for_card", "_on_card_clear_due")
        ))
        names = {n.id for n in ast.walk(ast.parse(new_src)) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(ast.parse(new_src)) if isinstance(n, ast.Attribute)}
        banned = {"keybd_event", "SendInput", "uiautomation", "pyautogui", "pynput",
                  "win32clipboard", "clipboard", "OcrReader", "grab_foreground_window"}
        hit = banned & (names | attrs)
        check("★ 新增那几条腿里没有模拟按键 / 碰剪贴板 / 抓屏认字那几样",
              not hit, "、".join(sorted(hit)) or "（一处都没有）")
        check("新代码一次截图、一趟后台任务都不起（这活儿只动界面状态）",
              not ({"grab_chat", "run_async", "_read_social_screen"} & attrs), "")

        # 去抖动那两处（v2-social-card-timeout-absolute）：回来不停表、离开不重计，是从
        # 源码里就砌死的，不是"看着像没触发"。
        back_src = inspect.getsource(bootstrap_mod.DesktopPetApp._note_card_back)
        check("★ `_note_card_back` 里再没有停表 / 重计那一步（去抖动彻底的落点）",
              ".stop(" not in back_src and ".start(" not in back_src, repr(back_src.strip()[-60:]))
        left_src = inspect.getsource(bootstrap_mod.DesktopPetApp._note_card_left)
        check("★ `_note_card_left` 也不再碰那只表了（人离开只记事实）",
              ".start(" not in left_src and ".stop(" not in left_src, "")
        due_src = inspect.getsource(bootstrap_mod.DesktopPetApp._on_card_clear_due)
        check("★ 到点那一下走的是 `close_bubble()`（淡出那条现成的路，不是硬 hide）",
              "close_bubble()" in due_src and "hide()" not in due_src, "")
        check("★ 到点那两道闸只剩两道（`_card_open` / `_card_copied`），不再看人在不在",
              "_card_away" in due_src and "not self._card_away" not in due_src, "")

        from life.screen import social as smod
        check("★ 触发闸门 / 按需抓拍的老口径一个没动",
              smod.PENDING_SNAP_MS == 500 and smod.OFFER_HANG_S == 90.0,
              f"PENDING_SNAP_MS={smod.PENDING_SNAP_MS} OFFER_HANG_S={smod.OFFER_HANG_S}")
        prop_src = inspect.getsource(smod.SocialReplyService.in_personal_chat.fget)
        check("★ 新加的那个只读属性只读页型：不发信号、不抓屏、不认字、不写库",
              "_page_kind" in prop_src
              and not any(word in prop_src for word in ("emit", "run_async", "grab", "db")), "")

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
        note(f"（红线这几条从真流程走完那一拍起算：utterance={utterance_before} "
             f"碎片={mind_before}）")

        drain_threads()
    finally:
        app._copy_social_draft = original_copy
        bootstrap_mod.CARD_CLEAR_S = real_clear_s

    box.cleanup()
    return summary("帮聊那一则上屏满 3 分钟自动清掉（v2-social-card-timeout → -absolute）")


if __name__ == "__main__":
    raise SystemExit(main())
