"""
邀请的等待态与冷启动的在场判定（v2-social-await-prime-fix）的自动测试。

两条真机毛病，一在"点下去之后"，一在"启动那一刻"：

**改动一 · 点 [好呀] 无下文。** 真机（2026-10-07 17:59）现象：邀请冒出来了（材料也认到
了），用户点 [好呀] 却只回一句「好的呀」，`DRAFT` / `_on_social_candidates` /
`_show_social_draft` 一条日志都没有——起草这条线**一次都没进**。日志坐实的经过是：冒话
之后前台切到 Notepad.exe / 宠物自己，`on_foreground` 里那句 `dismiss_offer()` 把
`_awaiting` 清了，而**气泡上那排 [好呀] [不用了] 一个动作都没有、照样挂着**；用户回来点
下去，`_on_offer_reply` 先拿 `awaiting_confirm` 问一句，判 False → 跳过 `confirm()` →
落到通用聊天兜底（`chatter_service.handle`，真机回的就是那句「好的呀」）。
`confirm()` 的出口是**当场照对方末句拟一段**（`v2-social-chat-ui-copy` 批次11 之前是那句
长问句 `NEED_SITUATION_LINE`），绝不是「好的呀」——所以"confirm 真没走到"这条判据在这一轮
可以当场验：走到了就**这一拍就开始拟**（`service.busy` 立起来），没走到就只有兜底那句话。

根因是**状态与按钮不同寿**：服务这边清了 `_awaiting`，界面那排按钮还显示着。修的是错位，
不是把老语义拆了——`dismiss_offer()` 清之前先问一句界面（`_offer_visible`）：按钮还归
社交这一条线就不清（用户只是走开一下，邀请还有效）；按钮真收掉（`hide_offer` / 被别的
线顶替 / 关气泡）才清。老口径防的那件事（`_awaiting` 一挂就把 `_poll_listen` 堵死）一分
没松：超时那一声（`_drop_stale_offer`）走 `force=True`，照清，而且**顺手把按钮一起收掉**
（新信号 `social_offer_expired`）——过期之后点下去同样不会落进通用聊天兜底。

**改动二 · 首启不触发。** 真机（用户 18:06 口述 + 17:57 日志）：微信个人窗先在桌面上
开着（**不是焦点**）→ 再开宠物 → 17:57:18 冷启动探一次前台，探到的是 `explorer.exe`，
社交那条线据此把 `_social_foreground` 定成 False；直到用户移动鼠标切了一次窗口，
`foreground_changed(Weixin)` 发出来，才认到微信、才冒话。根因是冷启动那一问只问"此刻
**焦点**是谁"（`monitor.foreground_now()`），而"微信开着但不在焦点"这条路上焦点那条腿
一个字都答不出来。修的是判据：焦点那条腿答不出来时，**退一步枚举当前所有可见（非最小化）
顶层窗口**，看有没有社交进程在场——只看进程号，**窗口标题一个字都不读**（隐私口径不动）。

红线：零新依赖（枚举走 `monitor/foreground.py` 既有的 ctypes 那一层）；pet.db / mind.db
零写入；不模拟按键 / 不碰剪贴板 / 不做 UI 自动化。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    section,
    summary,
    wait_for,
)

from life.screen.social import (  # noqa: E402
    OFFER_HANG_S,
    SocialReplyService,
)
from life.screen.social_window import SENDER_OTHER, ChatRead  # noqa: E402

PEER_LINE = "今天玩得开心吗"
DRAFT = "挺开心的，你呢？"
BECAUSE = "寒暄回寒暄，不升级"


def _peer_read(lines: list[str]) -> ChatRead:
    """按"对方白气泡"拼一份 `ChatRead`，末尾是对方最新那条。"""
    rows = [(SENDER_OTHER, text) for text in lines]
    return ChatRead(lines=rows, raw=" ".join(text for _s, text in rows))


def draft_bubble_text(app) -> str:
    """宠物那个非模态气泡里现在写着什么（候选 + 解析都在这儿）。"""
    history = app.chat_bubble._history
    return "\n".join(history.item(i).text() for i in range(history.count()))


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    # 冒话那道冷却写死成 30 分钟（跟 config 里的默认一致），别跟着本机配置走——
    # 用户要是把 `social_cooldown_min` 调大了，下面几节"又冒一次邀请"就冒不出来。
    app.config.screen.social_cooldown_min = 30

    spoken: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)

    grab = {"frame": "FRAME_A"}

    def _read(array):
        return _peer_read([PEER_LINE])

    # 通用聊天那条兜底：**记一笔、不真跑**。这一轮验的正是"点击有没有落到它身上"，
    # 让它真跑起来只会去写 pet.db（红线）并把断言搅浑。
    handled: list[str] = []
    app.chatter_service.handle = handled.append

    clock = {"t": 10_000.0}          # 冷却是 30 分钟，别让上一次邀请挡住这一次
    service._grab_chat = lambda: grab["frame"]
    service._read_chat = _read
    service._idle_seconds = lambda: 10.0
    service._examples_provider = lambda query: ""
    service._clock = lambda: clock["t"]

    stub.candidates_result = {"candidates": [{"draft": DRAFT, "why": BECAUSE}]}

    def _reset_offer_state() -> None:
        """回到"刚切进微信、什么都没冒"的那一刻。"""
        service._social_foreground = False
        service._listen_timer.stop()
        service._awaiting = False
        service._running = False
        service._last_asked = 0.0
        service._last_listen_at = 0.0
        service._last_listen_tail = ""
        service._last_listen_kind = ""
        service._last_fingerprint = None
        service._chat_reading = False
        service._listen_reading = False
        service._pending_draft = None
        service._chat_text = ""
        service._chat_text_at = 0.0
        # 页型那格也清回「还没认出来」（v2-social-trigger-window-gate）：邀请现在要等
        # 这一屏认回来、认出是**单聊**才冒（`on_foreground` → 抓屏 → `_on_screen_read` →
        # `note_page_kind`）。不清这一格，上一节留下的 `personal` 会让"非单聊 → 单聊"
        # 那一下跃迁不成立，邀请就冒不出来了。
        service._page_kind = ""
        service._want_situation_until = 0.0
        service._card_awaiting = False
        service._last_offered = 0.0
        service._last_draft_source = ""
        service._pending_auto_source = ""
        # v2-social-helping-window：帮聊窗口也翻篇——docstring 说的就是"刚切进微信、什么都
        # 没冒"的那一刻，那一刻当然还没点过 [好呀]。① 点过那一下之后这一格会立起来
        # （那一场从此不再冒问），不在这儿翻掉的话，③④⑤⑥ 这几节要验的邀请一个都冒不出来。
        service._forget_helping()
        service.reset_done()
        app.chat_bubble.hide_offer()
        app.chat_bubble.set_busy(False)
        spoken.clear()
        handled.clear()

    try:
        # ---------- ① 点 [好呀] 那条链：按钮还挂着时前台离场，点击必须走进 confirm ----------

        section("① 按钮还挂着、前台离场 → 点 [好呀] 仍要走进 confirm（起草那条链的入口）")

        _reset_offer_state()
        service.on_foreground("Weixin.exe")
        # 邀请**不再在 `on_foreground` 那一刻当场冒**（v2-social-trigger-window-gate）：
        # 先抓一屏、后台认字（约 4 秒），认出这一屏是**单聊**才冒。所以这儿得等它认回来。
        check("前置：进微信冒了邀请，那排 [好呀] [不用了] 排出来了",
              wait_for(lambda: service.awaiting_confirm is True
                       and app.chat_bubble.offer_theme == "social"),
              f"awaiting={service.awaiting_confirm} / theme={app.chat_bubble.offer_theme!r}")
        # 冒话那一拍顺手抓的那一屏认回来了（`on_foreground` → `_read_social_screen` 是
        # 后台 OCR，得等它落地；这一场"对方原话"就是它）
        check("前置：这一场认到了对方原话（冒话那一拍顺手抓的一屏）",
              wait_for(lambda: PEER_LINE in service._chat_text), repr(service._chat_text))
        service._last_listen_tail = PEER_LINE

        # 用户切去 Notepad 看了一眼（真机 17:59:09 那一下）——**气泡上那排按钮没动**
        service.on_foreground("Notepad.exe")
        check("★ 前台离场、但按钮还挂在气泡上 → 这一档**不**作废（修前这里就清了）",
              service.awaiting_confirm is True, str(service.awaiting_confirm))
        check("离场那条路的其余老动作照旧（出了社交前台，轻量轮询计时器停掉）",
              service._listen_timer.isActive() is False, str(service._listen_timer.isActive()))

        # 用户回来点 [好呀]（走真实轻按钮出口）
        app.chat_bubble._reply_offer("好呀")
        # 批次11：confirm() 的出口从"再问一句是什么情况"换成"当场照对方末句拟一段"，
        # 所以"真走到了"的凭据也跟着换——这一拍就该开拟（`busy` 立起来）。
        check("★ 点下去 → confirm() 真走到了（这一拍当场就开始拟）",
              service.busy is True, f"忙态 {service.busy}")
        check("★ 不再回那句长问句了（批次11 把「是什么情况」这一步拆了）",
              not any("什么情况" in s for s in spoken), repr(spoken))
        check("★ 也没落到通用聊天兜底（那正是真机那句「好的呀」的口子）",
              handled == [], repr(handled))

        # ---------- ② 接着走完起草：候选亮在气泡里 ----------

        section("② 点完 [好呀] 当场就拟完：候选亮进宠物自己的气泡（不用再讲一遍处境）")

        check("★ 起草真的被调了（候选亮进气泡）",
              wait_for(lambda: DRAFT in draft_bubble_text(app)), repr(draft_bubble_text(app)))
        check("★ 候选连着那句解析一起在（起草链两个末端都在）",
              BECAUSE in draft_bubble_text(app), repr(draft_bubble_text(app)))
        check("★ 起草走的是社交那条链，不是通用聊天兜底",
              handled == [], repr(handled))
        check("起草吃的还是这一场认回来的对方原话（材料没丢）",
              any(PEER_LINE in str(p) for p in stub.prompts) or stub.prompts == [],
              f"{len(stub.prompts)} 条提示词")
        drain_threads()

        # ---------- ③ 反向对照：修前那条老路（按钮还挂着也照清）必红 ----------

        section("③ 反向对照：老口径（`_offer_visible` 不接）下，同一条路真的落进兜底")

        _reset_offer_state()
        service._offer_visible = None        # 老口径：来一声就清，不看界面
        service.on_foreground("Weixin.exe")
        check("前置：邀请又冒出来了",
              wait_for(lambda: service.awaiting_confirm is True),
              str(service.awaiting_confirm))
        service.on_foreground("Notepad.exe")
        check("老口径：前台一离场就把这一档清了（这正是错位的那一半）",
              service.awaiting_confirm is False, str(service.awaiting_confirm))
        check("★ 按钮却还挂在气泡上（错位的另一半——按钮还显示、旗子已经没了）",
              app.chat_bubble.offer_theme == "social", repr(app.chat_bubble.offer_theme))
        app.chat_bubble._reply_offer("好呀")
        check("★ 于是点下去落进通用聊天兜底（真机那句「好的呀」就是从这儿出去的）",
              handled == ["好呀"], repr(handled))
        check("★ 反向也钉住了：confirm() 一次都没走（这一趟没被社交那条线接走）",
              service.busy is False and service._want_situation_until == 0.0,
              f"忙态 {service.busy} / until={service._want_situation_until}")
        service._offer_visible = lambda: app.chat_bubble.offer_theme == "social"

        # ---------- ④ 超时那一声：状态与按钮同寿（过期之后点下去也不落兜底） ----------

        section("④ 邀请挂过期（`OFFER_HANG_S`）→ 状态与按钮一起收，点下去不落兜底")

        _reset_offer_state()
        service.on_foreground("Weixin.exe")
        check("前置：邀请挂着、按钮排着",
              wait_for(lambda: service.awaiting_confirm is True
                       and app.chat_bubble.offer_theme == "social"),
              f"awaiting={service.awaiting_confirm} / theme={app.chat_bubble.offer_theme!r}")
        hidden: list[str] = []
        app.chat_bubble.offer_hidden.connect(hidden.append)
        grab["frame"] = None                 # 这一节不验抓屏，别让轮询顺带起草
        clock["t"] += OFFER_HANG_S + 5.0
        service._poll_listen()
        check("★ 挂过阈值 → 服务这边自己把它过期掉（老语义一分没松）",
              service.awaiting_confirm is False, str(service.awaiting_confirm))
        check("★ 界面那排按钮跟着一起收了（新信号 `social_offer_expired` 那条链）",
              app.chat_bubble.offer_theme == "", repr(app.chat_bubble.offer_theme))
        check("★ 收按钮那一下也顺手喊了声退休（状态与界面不会各说各话）",
              "social" in hidden, repr(hidden))
        app.chat_bubble._reply_offer("好呀")
        check("★ 过期之后那排按钮已经不在，点不出任何东西（尤其不落通用聊天兜底）",
              handled == [] and service.busy is False, repr(handled))
        grab["frame"] = "FRAME_A"
        app.chat_bubble.offer_hidden.disconnect(hidden.append)

        # ---------- ⑤ 冷启动：焦点答不出社交软件时，退一步看桌面上有没有 ----------

        section("⑤ 冷启动：微信开着但不在焦点 → 照样认（不用等用户切一次窗）")

        real_now = app.screen_monitor.foreground_now
        real_visible = app.screen_monitor.visible_procs
        calls: list[int] = []

        # 真机 17:57 那一拍：焦点是 explorer（桌面上），微信个人窗开着但不在焦点
        app.screen_monitor.foreground_now = lambda: "explorer.exe"
        app.screen_monitor.visible_procs = lambda: (
            calls.append(1), ["explorer.exe", "Weixin.exe"]
        )[1]
        _reset_offer_state()
        app._prime_social_foreground()
        check("★ 冷启动问不出社交焦点，但桌面上有可见的微信窗口 → 社交那条线认了它",
              service._social_foreground is True, str(service._social_foreground))
        check("★ 认了就算数：这一趟的邀请冒得出来，不用用户先切一次窗",
              wait_for(lambda: service.awaiting_confirm is True
                       and app.chat_bubble.offer_theme == "social"),
              f"awaiting={service.awaiting_confirm} / theme={app.chat_bubble.offer_theme!r}")
        check("这一问走的是枚举那条腿（真的问过一次桌面）", calls == [1], repr(calls))

        # ---------- ⑥ 反向对照：桌面上一个可见的社交窗口都没有 → 不许瞎认 ----------

        section("⑥ 反向对照：桌面上没有可见的社交窗口 → 不认，照旧等切窗")

        app.screen_monitor.visible_procs = lambda: ["explorer.exe", "chrome.exe", "Code.exe"]
        _reset_offer_state()
        app._prime_social_foreground()
        check("★ 一个可见的社交窗口都没有 → 社交那条线**不**被置成可触发态",
              service._social_foreground is False, str(service._social_foreground))
        check("★ 也没冒话（没人可回，冒了就是瞎认）",
              service.awaiting_confirm is False and app.chat_bubble.offer_theme == "",
              f"awaiting={service.awaiting_confirm} / theme={app.chat_bubble.offer_theme!r}")
        # 宠物先开、微信后开这条老路一个字没坏：真的一次切换过来，信号照旧管用
        clock["t"] += 5.0
        service.on_foreground("Weixin.exe")
        check("宠物先开、微信后开：真的切过来照样冒话（老路没被这一轮改动碰坏）",
              wait_for(lambda: service.awaiting_confirm is True
                       and app.chat_bubble.offer_theme == "social"),
              f"awaiting={service.awaiting_confirm} / theme={app.chat_bubble.offer_theme!r}")

        # ---------- ⑦ 保底：焦点就是社交软件时，老口径一个字不变 ----------

        section("⑦ 保底：焦点就是社交软件 → 走老口径，枚举那一趟一次都不跑")

        app.screen_monitor.foreground_now = lambda: "Weixin.exe"
        calls.clear()
        _reset_offer_state()
        app._prime_social_foreground()
        check("★ 焦点就是社交软件 → 照老口径认它",
              service._social_foreground is True, str(service._social_foreground))
        # 这一拍顺手抓的那一屏认回来再往下走（v2-social-trigger-window-gate：邀请是它
        # 认完页型之后才冒的）。不等它的话，那一趟会飘到下面几节里落地。
        wait_for(lambda: not service._chat_reading)
        check("★ 焦点已经答出来了，就**不去**枚举桌面（那一趟一次都没跑）",
              calls == [], repr(calls))
        app.screen_monitor.foreground_now = real_now
        app.screen_monitor.visible_procs = real_visible

        # ---------- ⑧ 红线：零新依赖 / 两库零写入 / 不模拟按键 ----------

        section("⑧ 红线：零新依赖 / 两库零写入 / 不模拟按键 / 枚举不读标题")

        import sqlite3

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        import ast
        import inspect

        from core import events as events_mod
        from life.screen import social as social_mod

        refs: set[str] = set()
        for mod in (social_mod, events_mod):
            for node in ast.walk(ast.parse(inspect.getsource(mod))):
                if isinstance(node, ast.Import):
                    refs.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    refs.update(a.name for a in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api"}
        check("服务与事件总线都没多一个第三方 import",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")

        src = inspect.getsource(social_mod)
        check("没有模拟按键 / 不做 UI 自动化那几条腿",
              not any(word in src for word in ("keybd_event", "SendInput", "SetCursorPos")),
              "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        # 枚举那一趟（改动二）的红线：只看进程号，窗口标题 / 类名一个字都不读。
        # 这一条非有不可——`EnumWindows` 一到手就递过来每一个窗口的句柄，读标题只要
        # 再加一行 `GetWindowTextW` 的事，而模块开头那条隐私口径就是这么被磨掉的。
        from monitor import foreground as fg_mod

        enum_src = inspect.getsource(fg_mod.visible_pids)
        titled = [word for word in ("GetWindowTextW", "GetWindowTextLengthW",
                                    "GetClassNameW", "foreground_title")
                  if word in enum_src]
        check("枚举可见窗口那一趟：窗口标题 / 类名一个字都不读",
              not titled, "、".join(titled) or "（只看进程号）")

        mon_src = inspect.getsource(type(app.screen_monitor).visible_procs)
        check("`visible_procs` 也是只翻进程名，不读标题",
              not any(word in mon_src for word in ("title", "GetWindowTextW")),
              "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("邀请的等待态与冷启动的在场判定（v2-social-await-prime-fix）")


if __name__ == "__main__":
    raise SystemExit(main())
