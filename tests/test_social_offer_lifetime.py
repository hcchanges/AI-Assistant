"""社交那条线「问句与按钮同寿」（v2-social-offer-ui-lifetime）的自动测试。

真机毛病（用户截图 `屏幕截图 2026-10-11 002842.png`）：桌宠在微信单聊旁冒了那句
`聊天么？要不要我帮你回呀？`，可底下的 [好呀] [不用了] **一个都没排出来**，看上去
是个"带输入框 + 发送按钮"的对话窗。用户于是自己往宠物输入框里打了"好呀"——那条路
根本不是入口（`_on_offer_reply` 找不到这一问，落到通用聊天兜底）。

查下来的根因**不是**渲染分支、**不是** e64700f 回归：按钮那一拍确实排出来了（真机
00:25:25.230 `show_offer` → `_offer_theme="social"`，00:25:46.539 那行
`dismiss_offer` 被 `_offer_visible()` 挡回去正是证据）。真正脱节的是**两条寿数**：

- 按钮：`OFFER_HANG_S`（90 秒）一到，`_drop_stale_offer` 把它收掉；
- 问句：`_pop_offer` 里用 `append_pet` 放进对话流，从此**是普通聊天记录**，没人管。

真机 00:28:11.706 用户切回微信、监听计时器起来，1.6 秒后 `_drop_stale_offer` 判
168 秒 ≥ 90 秒退掉按钮——问句留在原地，于是屏幕上就剩"一句孤零零的问话 + 一排输入框"。

修法（A）：`ui/chat_bubble.py` 新增 `ROLE_OFFER_ASK` 这一格标记，`_pop_offer` 只给
`social` 那条线打（`theme == "social"`，**不用 `startswith`**——`social_card` 也以
social 开头，但它没有超时腿，跟着打标记会被**别的线**的过期误删），问句改走
`append_offer_ask`；过期那一拍 `clear_offer_ask()` 把它整条摘掉（照 `clear_social_draft`
的倒序 `takeItem` 路数，认 item data 不认文本）。

修法（③）：摘完之后补一句灰字 `OFFER_EXPIRED_NOTE` 说明"这条邀请过期了"——问句凭空消失、
连句交代都没有，用户会以为自己点错了。

**§本文件里那两处钉「交代只在单聊页补」的断言已按 v2-social-offer-expiry-note 改掉**，
照实记在这儿：那一道闸（`in_personal_chat`）在过期那一拍**几乎必为假**（90 秒里人早切走
了），真机后果就是"问句凭空消失、一个字不交代"；`is_personal_kind("")` 又把空串当单聊，
判据两头自相矛盾。这一节点把闸拿掉、改成无条件补，所以本文件里"停在会话总列表上就不补"
那段反面对照与源码级那条 `"in_personal_chat" in expired_src` 都跟着翻转（新口径见
`tests/test_social_offer_expiry_note.py`）。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；`hide_offer` 的另外
五个调用点（场景换 / 用户打字 / 3 分钟绝对收窗 / 生活卡两处）一律不摘问句——尤其
`_on_card_clear_due` 那 3 分钟：那一拍用户可能正低头粘贴。
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
    summary,
    wait_for,
)

from core.bootstrap import OFFER_EXPIRED_NOTE  # noqa: E402
from life.screen.scene_state import ASK_NOTES  # noqa: E402
from life.screen.social import (  # noqa: E402
    ASK_CARD_LINE,
    ASK_LINE as SOCIAL_ASK_LINE,
    OFFER_HANG_S,
    PAGE_LIST,
    SocialReplyService,
)
from life.screen.social_window import SENDER_OTHER, SENDER_USER, ChatRead  # noqa: E402
from ui.chat_bubble import (  # noqa: E402
    ROLE_OFFER_ASK,
    ROLE_SOCIAL_DRAFT,
    ROLE_SPEAKER,
    SPEAKER_NOTE,
)

PEER_LINE = "明天下午三点老地方见？"
DRAFT = "好呀，三点我准到"
BECAUSE = "先应下时间，别的见面再说"


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def bubble_text(app) -> str:
    """气泡记录区现在写着什么（一条一行）。"""
    history = app.chat_bubble._history
    return "\n".join(history.item(i).text() for i in range(history.count()))


def _items_with(app, role: int) -> list[str]:
    """记录区里打了这一格标记的那几条文本。"""
    history = app.chat_bubble._history
    return [history.item(i).text()
            for i in range(history.count())
            if history.item(i).data(role)]


def _ask_items(app) -> list[str]:
    return _items_with(app, ROLE_OFFER_ASK)


def _note_items(app) -> list[str]:
    """记录区里"系统灰字"那条（`SPEAKER_NOTE`），按说话人那一格认。"""
    history = app.chat_bubble._history
    return [history.item(i).text()
            for i in range(history.count())
            if history.item(i).data(ROLE_SPEAKER) == SPEAKER_NOTE]


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    # 冒话那道冷却写死成 30 分钟，别跟着本机 config 走（用户调大了下面就冒不出来）。
    app.config.screen.social_cooldown_min = 30

    spoken: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)

    # 通用聊天那条兜底：**记一笔、不真跑**（它真跑会去写 pet.db，撞红线）。
    handled: list[str] = []
    app.chatter_service.handle = handled.append

    peer_read = ChatRead(
        lines=[(SENDER_OTHER, "在忙吗"), (SENDER_USER, "还好"), (SENDER_OTHER, PEER_LINE)],
        raw=" ".join(["在忙吗", "还好", PEER_LINE]),
        page_kind="personal",
    )

    grab = {"frame": "FRAME_CHAT"}
    clock = {"t": 10_000.0}

    def _read(array):
        return peer_read if array == "FRAME_CHAT" else ChatRead(lines=[])

    service._grab_chat = lambda: grab["frame"]
    service._read_chat = _read
    service._idle_seconds = lambda: 10.0
    service._examples_provider = lambda query: ""
    service._clock = lambda: clock["t"]

    stub.candidates_result = {"candidates": [{"draft": DRAFT, "why": BECAUSE}]}

    def _reset(*, text: str = "") -> None:
        """回到"刚切进微信、什么都没冒"的那一刻。"""
        service._social_foreground = False
        service._running = False
        service._awaiting = False
        service._card_awaiting = False
        service._chat_reading = False
        service._listen_reading = False
        service._pending_draft = None
        service._pending_draft_at = 0.0
        service._chat_text = text
        service._chat_text_at = 0.0 if not text else clock["t"]
        service._last_asked = None            # 出厂态：冷却哨兵还没烧过
        service._last_proc = ""
        service._page_kind = ""               # 页型回"没认过"，好让"非单聊 → 单聊"跃迁成立
        service._last_listen_at = 0.0
        service._last_listen_tail = ""
        service._last_listen_kind = ""
        service._last_fingerprint = None
        service._last_draft_source = ""
        service._pending_auto_source = ""
        service._want_situation_until = 0.0
        service._snap_timer.stop()
        service._listen_timer.stop()
        service.reset_done()
        app.chat_bubble.hide_offer()
        app.chat_bubble.clear_offer_ask()
        app.chat_bubble.clear_social_draft()
        app.chat_bubble.set_busy(False)
        for row in range(app.chat_bubble._history.count() - 1, -1, -1):
            item = app.chat_bubble._history.item(row)
            if item is not None and not item.data(ROLE_OFFER_ASK) \
                    and not item.data(ROLE_SOCIAL_DRAFT):
                app.chat_bubble._history.takeItem(row)   # 清台面，只留本次要看的
        grab["frame"] = "FRAME_CHAT"
        spoken.clear()
        handled.clear()
        stub.prompts.clear()
        stub.candidates_result = {"candidates": [{"draft": DRAFT, "why": BECAUSE}]}
        clock["t"] += 10.0

    def _raise_offer() -> bool:
        """切进微信、等那句邀请冒出来（`on_foreground` → 抓屏 → 认出单聊 → `_ask_offer`）。"""
        service.on_foreground("Weixin.exe")
        return wait_for(lambda: service.awaiting_confirm is True
                        and app.chat_bubble.offer_theme == "social")

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 冒话：问句在，按钮也在（两条寿数从同一个地方起步） ----------

        section("① 冒话那一拍：问句落了标记、按钮排出来了（同寿的起点）")

        _reset()
        check("前置：进微信冒了邀请，[好呀] [不用了] 排出来了",
              _raise_offer(),
              f"awaiting={service.awaiting_confirm} / theme={app.chat_bubble.offer_theme!r}")
        check("★ 那句问句在记录区里", SOCIAL_ASK_LINE in bubble_text(app),
              bubble_text(app).replace("\n", " / "))
        check("★ 它**打上了**「这一问」的标记（过期那一拍才有得摘）",
              _ask_items(app) == [SOCIAL_ASK_LINE], repr(_ask_items(app)))
        check("★ 没顺手把别的线也标上（只社交那条线有超时腿）",
              _items_with(app, ROLE_SOCIAL_DRAFT) == [], "")
        check("问句是宠物说的、外观跟普通气泡一致（不是灰字系统条）",
              app.chat_bubble._history.item(0).text() == SOCIAL_ASK_LINE, "")
        check("这一拍按钮真露着（不是收在关着的框里）",
              app.chat_bubble._offer_theme == "social"
              and not app.chat_bubble._offer_row.isHidden(),
              f"theme={app.chat_bubble._offer_theme!r} / "
              f"隐藏={app.chat_bubble._offer_row.isHidden()}")

        # ---------- ② 过期：按钮收 + 问句摘 + 留一句交代 ----------

        section("② 挂过 90 秒没人理 → 按钮收、问句跟着摘、补一句交代")

        # 人一直坐在那扇单聊窗里（`_page_kind` 是 `personal`）
        check("前置：这一屏还认着是单聊",
              service.in_personal_chat is True, repr(service._page_kind))

        clock["t"] += OFFER_HANG_S + 5.0
        service._poll_listen()          # 超时那一拍就是这儿（`_drop_stale_offer`）

        check("★ 挂过阈值 → 服务这边自己把它过期掉", service.awaiting_confirm is False,
              str(service.awaiting_confirm))
        check("★ 那排按钮收掉了", app.chat_bubble.offer_theme == "",
              repr(app.chat_bubble.offer_theme))
        check("★ 问句**跟着一起摘了**（修前它就孤零零挂在这儿，用户当成能打字的框）",
              SOCIAL_ASK_LINE not in bubble_text(app),
              bubble_text(app).replace("\n", " / "))
        check("★ 那一格标记也随条目一起走了（不留半截空壳）",
              _ask_items(app) == [], repr(_ask_items(app)))
        check("★ 补了一句交代（人还在单聊里，得说清为什么没了）",
              OFFER_EXPIRED_NOTE in bubble_text(app),
              bubble_text(app).replace("\n", " / "))
        check("★ 交代走的是**灰字系统条**（`append_note`，跟宠物说话分开）",
              _note_items(app) == [OFFER_EXPIRED_NOTE], repr(_note_items(app)))
        check("★ 交代排在问句原来的位置之后（先摘掉、再补上，不会条数错位）",
              app.chat_bubble._history.count() == 1,
              f"{app.chat_bubble._history.count()} 条："
              f"{bubble_text(app).replace(chr(10), ' / ')}")

        # **交代跟页型无关**（v2-social-offer-expiry-note）：人已经切走了（停在会话总列表上）
        # 也照补——这一句本来就是说给"还看着这一问的人"听的，而过期那一拍他早就切走了，
        # 旧口径（只在 `in_personal_chat` 时补）等于**从来不说**。三种页型一视同仁地钉。
        for kind, label in ((PAGE_LIST, "会话总列表"), ("login", "登录页"), ("", "空页型")):
            _reset()
            check(f"前置：又冒了一问（{label} 这一轮）",
                  _raise_offer(), str(service.awaiting_confirm))
            service._page_kind = kind                 # 人不在那扇单聊窗里了
            clock["t"] += OFFER_HANG_S + 5.0
            service._poll_listen()
            check(f"★ 停在{label} → 按钮照收、问句照摘、**照样补**那句交代",
                  app.chat_bubble.offer_theme == ""
                  and SOCIAL_ASK_LINE not in bubble_text(app)
                  and OFFER_EXPIRED_NOTE in bubble_text(app),
                  bubble_text(app).replace("\n", " / "))

        # ---------- ③ 点 [好呀]：问句**留着**（它是这则草稿的上下文） ----------

        section("③ 点了 [好呀] → 问句不摘（草稿的上下文）、卡片照出、不说过期")

        _reset()
        check("前置：冒了一问", _raise_offer(), str(service.awaiting_confirm))
        check("前置：这一场认到了对方原话",
              wait_for(lambda: PEER_LINE in service._chat_text), repr(service._chat_text))

        app.chat_bubble._reply_offer("好呀")
        check("★ 消息出去那一刻问句**还留着**（修得过头就会把它一起摘掉）",
              SOCIAL_ASK_LINE in bubble_text(app),
              bubble_text(app).replace("\n", " / "))
        check("★ 真走进了 confirm 那条路（候选出来了）",
              wait_for(lambda: _items_with(app, ROLE_SOCIAL_DRAFT) != []),
              bubble_text(app).replace("\n", " / "))
        drain_threads()
        pump(0.1)
        check("★ 卡片出来了、问句仍在它上面（同一段对话的上下文）",
              _ask_items(app) == [SOCIAL_ASK_LINE]
              and _items_with(app, ROLE_SOCIAL_DRAFT) != [],
              f"ask={_ask_items(app)} / card={len(_items_with(app, ROLE_SOCIAL_DRAFT))} 条")
        check("★ 没说过期那句（这一问是被接下的，不是过期的）",
              OFFER_EXPIRED_NOTE not in bubble_text(app), "")
        check("★ 也没落进通用聊天兜底（[好呀] 被社交那条线接走了，不是当普通聊天）",
              handled == [], repr(handled))
        app.chat_bubble.clear_social_draft()
        check("★ 再摘一次草稿，问句依旧不动（两格标记互不误伤）",
              _ask_items(app) == [SOCIAL_ASK_LINE], repr(_ask_items(app)))

        # ---------- ④ 其余三线：问句一律不打标记、过期也不误摘 ----------

        section("④ 笔记 / 攻略 / 场景 / 攻略卡：`append_pet` 老路一个字没动")

        _reset()
        for theme, text, label in (
            ("scene_work", ASK_NOTES, "场景"),
            ("game", "要不要我陪你打一把？", "小游戏"),
            ("social_card", ASK_CARD_LINE, "对象攻略卡"),
        ):
            app._pop_offer(theme, text)
            check(f"{label}：问句摆出来了（`{theme}`）", text in bubble_text(app),
                  bubble_text(app).replace("\n", " / "))
            check(f"{label}：**没打**「这一问」的标记（只有社交那条线打）",
                  _items_with(app, ROLE_OFFER_ASK) == [], repr(_ask_items(app)))

        # 别的线摆好之后，社交那条线过期一次：一声都不该误摘到它们头上
        app.chat_bubble.clear_offer_ask()
        for theme, text, label in (
            ("scene_work", ASK_NOTES, "场景"),
            ("game", "要不要我陪你打一把？", "小游戏"),
            ("social_card", ASK_CARD_LINE, "对象攻略卡"),
        ):
            check(f"{label}：社交过期那一下**没**误摘它（各条线的问句互不相干）",
                  text in bubble_text(app), bubble_text(app).replace("\n", " / "))
        app.chat_bubble.hide_offer()

        # 用户自己打的那句话，**文本跟问句一模一样**：摘的时候绝不能连坐。
        # 这是"认 item data、不认文本"这条口径的活体证据（`clear_social_draft` 的老
        # 教训：解析那句里也可能出现「」，按文本挑会误伤真聊天）。
        _reset()
        app.chat_bubble.append_user(SOCIAL_ASK_LINE)
        app.chat_bubble.append_offer_ask(SOCIAL_ASK_LINE)
        app.chat_bubble.clear_offer_ask()
        check("★ 文本与问句一模一样的**用户发言**不会被摘（认标记、不认文本）",
              _ask_items(app) == []
              and _items_with(app, ROLE_SPEAKER).count(SOCIAL_ASK_LINE) >= 1,
              bubble_text(app).replace("\n", " / "))

        # ---------- ⑤ 红线（源码层 / 两库） ----------

        section("⑤ 红线：只挂在过期那条路 / 别的收按钮路不摘 / 两库零写入")

        app_src = inspect.getsource(type(app))
        expired_src = inspect.getsource(type(app)._on_social_offer_expired)
        chat_src = inspect.getsource(type(app.chat_bubble))

        check("★ `clear_offer_ask` 全仓**只有一处**调用点（就是过期那一声）",
              app_src.count("clear_offer_ask") == 1,
              str(app_src.count("clear_offer_ask")))
        check("★ 那一处在 `_on_social_offer_expired` 里",
              "clear_offer_ask" in expired_src, "")
        check("★ 先摘问句、再补交代（顺序钉死）",
              expired_src.index("clear_offer_ask") < expired_src.index("OFFER_EXPIRED_NOTE"),
              "")
        # 掐掉 docstring 只看代码体：注释里为了说清来龙去脉会提到 `in_personal_chat`
        expired_code = expired_src.split('"""')[-1]
        check("★ 交代**不再挂在任何页型闸上**（v2-social-offer-expiry-note：无条件补）",
              "in_personal_chat" not in expired_code
              and "append_note(OFFER_EXPIRED_NOTE)" in expired_code, "")
        for name in ("_on_chat_send", "_on_card_clear_due"):
            src = inspect.getsource(getattr(type(app), name))
            check(f"★ `{name}` 不摘问句（用户打字那拍 / 3 分钟绝对收窗那拍绝不碰它）",
                  "clear_offer_ask" not in src, "")
        check("★ `_pop_offer` 判的是**恰好等于** `social`（`social_card` 别被连坐）",
              'theme == "social"' in inspect.getsource(type(app)._pop_offer), "")
        ask_src = inspect.getsource(type(app.chat_bubble).clear_offer_ask)
        check("★ 摘的是 item data（`data(ROLE_OFFER_ASK)`），不是按文本挑",
              "data(ROLE_OFFER_ASK)" in ask_src, "")
        check("★ 倒着走（`takeItem` 会挪行号，`clear_social_draft` 的老路数）",
              "range(self._history.count() - 1, -1, -1)" in ask_src, "")
        check("★ 没碰固定控件 `_offer_row` 的建法 / 完成态锁",
              "mark_done" not in chat_src and "_lock_verdict" not in chat_src, "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        service._snap_timer.stop()
        service._listen_timer.stop()
        drain_threads()
        box.cleanup()

    return summary("社交问句与按钮同寿（v2-social-offer-ui-lifetime）")


if __name__ == "__main__":
    raise SystemExit(main())
