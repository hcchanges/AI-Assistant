"""帮聊「随时能退」的那个出口（v2-social-exit-easy）的自动测试。

**补的是哪一处**：进过帮聊窗口之后（点过 [好呀] / 喊过"帮我回"），对方每来一句都自动出
候选（`_auto_generate` 头一道闸就是 `_is_helping()`）。可**收手的路只有两条，两条都不好使**：

- 那排轻按钮里的 [不用了]——它只有"还没进窗口"时才摆得出来（`_ask_offer` 头一道闸也是
  `_is_helping()`，进了就不再问），按过一次 [好呀] 之后**再也见不着**；
- 关气泡——`close_bubble()` 只把窗口藏起来，服务那一格一个字不动，下一句照样自动出候选。

真机上于是成了"帮起来就停不下来"。这一批在**那一则上**加一个出口：「复制回复」旁边一个
「不用帮了」，什么时候按、什么时候这一场就从帮聊窗口里退出来。

**出口为什么必须长在那一则上**：那一则（正文 + 解析 + 按钮的那一整个框）就是"正在帮"的
物证——出口跟它同处一屏，才叫"随时能退"。

四段：① 界面那一层（那一则上真多了一只按钮，按下去只发一声）；② 服务那一层
（`exit_helping()` 只清**这一场那一格**，别的场次、别的状态一概不动）；③ 退出之后那两条腿
（自动出候选停下、那一问能再冒）——本批的红线；④ 装配层那条真路（摘卡片 + 补交代 +
**不碰库、不碰卡**）。

**口径如实记**：

- 这一下退的是"接着帮不帮"，**不是"记没记过"**——三记忆库、按对端分格的对象档案、对象攻略
  卡那四格一概不动（④ 逐条验过）。出口叫「不用帮了」，不叫「忘掉」。
- **已经出门在路上的那一趟挡不住**：按下去那一刻若正有一趟起草在跑（上一则有正文、下一趟
  已在路上），那一趟到点照样上屏。`_running` / `_awaiting` 是服务的闸门，界面伸手去改就会
  弄坏"一次只拟一段"，所以这一批**不碰它**（② 明写这两格退出前后一字不变）——如实记账，
  不是缺陷：退掉的是"往后每一句都自动出候选"这条腿和那一问。
- **冷却那一格没动**：退出不清 `_last_asked`（"这一场半小时内不再冒话"照旧），③ 明写。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    Recorder,
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
)

from PySide6.QtWidgets import QPushButton  # noqa: E402

import core.bootstrap as bootstrap_mod  # noqa: E402
from core.bootstrap import HELP_EXIT_NOTE  # noqa: E402
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
    SocialReplyService,
)
from ui.chat_bubble import (  # noqa: E402
    ROLE_SOCIAL_DRAFT,
    ChatBubble,
)

# 两个**不同的人**：验"在甲那一场按了退出，乙那一场该帮还帮"。
JIA, YI = "甲", "乙"
# 对方最近那几句话（跟别的社交模块同一份材料口径）。
PEER_TALK = "他问我周末去不去爬山\n今天上班好累啊，开会开到九点"
BODY, WHY = "这周加班去不了，改天我请你。", "直接说，别拖着"
# 这一批一共就动了这几个文件（零新依赖那一节照着扫）。
_TOUCHED = ("ui/chat_bubble.py", "life/screen/social.py", "core/bootstrap.py",
            "tests/test_social_exit_easy.py")
_BANNED = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
           "pyautogui", "win32api", "fuzzywuzzy", "regex", "chromadb",
           "keyboard", "pynput", "mouse", "pywin32"}


def _cards(bubble) -> list[str]:
    """记录区里眼下挂着哪几则帮聊（那一整则打的是 `ROLE_SOCIAL_DRAFT` 标记）。"""
    return [
        bubble._history.item(row).text()
        for row in range(bubble._history.count())
        if bubble._history.item(row).data(ROLE_SOCIAL_DRAFT)
    ]


def _texts(bubble) -> list[str]:
    """记录区里眼下**每一条**的字（含灰字的系统提示）。"""
    return [
        bubble._history.item(row).text() for row in range(bubble._history.count())
    ]


def _notes(bubble) -> list[str]:
    """记录区里的灰字（装配层补交代走的就是 `append_note` 这条）。"""
    return [text for text in _texts(bubble) if "（" in text and "）" in text]


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _calls(func) -> set[str]:
    """这个函数**真调用**了哪些 `self.xxx` / `xxx.yyy`（只看调用，注释与说明不算数）。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _current_card(bubble):
    """记录区里**眼下挂着的那一则**（控件），没有就是 None。

    为什么要"眼下那一则"而不是整框 `findChildren`：摘掉的那几则控件要等事件循环才真删
    （`takeItem` 只是把它从表里摘下来），整框找会把**上一则的旧按钮**一起捞出来——那样点
    到的可能是已经摘走的那一则上的按钮，验的就不是眼前这一只了。
    """
    for row in range(bubble._history.count() - 1, -1, -1):
        item = bubble._history.item(row)
        if item is not None and item.data(ROLE_SOCIAL_DRAFT):
            return bubble._history.itemWidget(item)
    return None


def _card_buttons(bubble, label: str) -> list[QPushButton]:
    """眼下这一则上字面是 `label` 的那只按钮（整框里别处的同款按钮不算）。"""
    card = _current_card(bubble)
    if card is None:
        return []
    return [b for b in card.findChildren(QPushButton, "ChatOffer")
            if b.text() == label]


def _exit_buttons(bubble) -> list[QPushButton]:
    return _card_buttons(bubble, ChatBubble.EXIT_LABEL)


def _seed(app) -> None:
    """往沙箱库（真 sqlite + 真加密）里按对端分格写两个人，用来验"退出不碰记过的东西"。"""
    store = app.mind_store
    store.save_profile(peer=JIA, name="小明", intro="最近在准备考研，压力大")
    store.save_ep(rel_state="刚加了微信，聊了三天", peer=JIA)
    store.add_fragment("他爱吃辣，上次说想吃火锅", peer=JIA)
    store.add_fragment("他养的猫叫豆豆", peer=JIA)
    store.save_profile(peer=YI, name="小红", intro="喜欢看展，做设计的")
    store.add_fragment("主人自己那一档的碎片", peer="")


def main() -> int:
    # ---------- ① 出口长在那一则上（界面那一层） ----------

    section("① 那一则上多了一只「不用帮了」，按下去只发一声")

    box, stub, app = make_env()
    try:
        service: SocialReplyService = app.social_reply
        bubble: ChatBubble = app.chat_bubble

        check("前置：这一批的出口字面是「不用帮了」",
              ChatBubble.EXIT_LABEL == "不用帮了", ChatBubble.EXIT_LABEL)
        check("前置：它跟那排轻按钮的 [不用了] **不是同一个字面**"
              "（一个退帮聊、一个答那一问，混起来两头都是坑）",
              ChatBubble.EXIT_LABEL != ChatBubble.OFFER_NO, ChatBubble.OFFER_NO)
        check("前置：这一声是一条独立信号（不是借 `offer_reply` / `copy_requested`）",
              hasattr(ChatBubble, "help_exit_requested"), "")

        bubble.append_social_card(BODY, WHY, BODY)
        pump(0.05)
        exits = _exit_buttons(bubble)
        copies = _card_buttons(bubble, ChatBubble.COPY_LABEL)
        check("★ 那一则上真多了一只「不用帮了」（这一则里就一只）",
              len(exits) == 1, f"{[b.text() for b in exits]}")
        check("「复制回复」也还在（那一则的老功能一条没少）",
              len(copies) == 1 and copies[0] is bubble._copy_button,
              f"{[b.text() for b in copies]}")
        check("★ 出口跟「复制回复」同一行、样式同一款（都是轻轻一点那档）",
              bool(exits and copies) and exits[0].parentWidget() is copies[0].parentWidget()
              and exits[0].objectName() == "ChatOffer"
              and copies[0].objectName() == "ChatOffer", "")
        row = bubble._copy_row
        order = (row.layout().indexOf(exits[0]), row.layout().indexOf(copies[0]))
        check("★ 「复制回复」仍在最右（主操作的位置没动），出口挨在它左边",
              row is not None and order[0] < order[1], f"出口 {order[0]} / 复制 {order[1]}")
        check("★ 出口**一直可按**（不因为「这一则没正文」变灰——那正是最想退的时候）",
              exits[0].isEnabled() is True, "")

        # 空正文那一则：复制是灰的，出口照旧可按
        bubble.append_social_card("（没拟出正文）", WHY, "")
        pump(0.05)
        exits = _exit_buttons(bubble)
        check("★ 空正文那一则：复制变灰、出口照旧可按",
              bubble._copy_button.isEnabled() is False
              and bool(exits) and exits[0].isEnabled() is True, "")
        bubble.clear_social_draft()
        pump(0.05)

        # 按下去只发一声（不顺手抄走正文、不冒充那一问的答复）
        bubble.append_social_card(BODY, WHY, BODY)
        pump(0.05)
        recorder = Recorder(app, "_copy_social_draft")
        exits_seen: list[int] = []
        bubble.help_exit_requested.connect(lambda: exits_seen.append(1))
        exits = _exit_buttons(bubble)
        check("前置：这一则的正文就是那只按钮要抄的那一版",
              bubble._copy_text == BODY, repr(bubble._copy_text))
        exits[0].click()
        pump(0.05)
        check("★ 点出口 → `help_exit_requested` 发了一声",
              len(exits_seen) == 1, f"{len(exits_seen)} 声")
        check("★ 点出口**没有**顺手把正文抄进剪贴板（一声都不该多发）",
              recorder.calls == [], str(recorder.calls))
        check("★ 界面自己不摘那一则（摘不摘由装配层按「服务原来在不在帮」定）",
              len(_cards(bubble)) == 1, f"{len(_cards(bubble))} 则")
        bubble._copy_button.click()          # 反面对照：复制那只按钮照旧只发它自己那一声
        pump(0.05)
        check("反面对照：点「复制回复」→ 抄一手、出口那一声不多发",
              len(recorder.calls) == 1 and len(exits_seen) == 1,
              f"抄 {len(recorder.calls)} 手 / 出口 {len(exits_seen)} 声")
        recorder.restore()
        bubble.clear_social_draft()
        pump(0.05)
        check("前置：收干净了（记录区里没有残留的这一则）", _cards(bubble) == [], "")

        exit_src = inspect.getsource(ChatBubble._exit_clicked)
        check("★ 那一只手只报一声：不碰剪贴板、不冒充答复、不自己摘卡片",
              "help_exit_requested.emit()" in exit_src
              and "clipboard" not in exit_src.lower()
              and "offer_reply" not in exit_src
              and "clear_social_draft" not in exit_src, "")

        # ---------- ② 服务那一层：只清这一场那一格 ----------

        section("② `exit_helping()` 只清**这一场那一格**，别的场次与别的状态一概不动")

        service._session_id, service._peer_name = "S1", JIA
        check("前置：这一场本来没在帮", service._is_helping() is False, "")
        check("★ 没在帮时按下去 = 什么都不做（返回 False，界面照返回值不动）",
              service.exit_helping() is False, "")
        check("…也没顺手往那一格里塞东西", service._helping == {}, str(service._helping))

        service._mark_helping()
        check("前置：进过帮聊窗口了（`_auto_generate` 那道闸放开）",
              service._is_helping() is True, "")
        key_jia = service._cooldown_key()
        check("★ 退出：返回 True，这一格真的没了",
              service.exit_helping() is True and service._is_helping() is False, "")
        check("★ 那一格是**摘掉**了，不是塞了个假值进去（`in` 判据才靠得住）",
              key_jia not in service._helping and service._helping == {},
              str(service._helping))
        check("★ 幂等：再按一下什么都不做（连点两下不该冒出两句交代）",
              service.exit_helping() is False, "")

        # 分格：甲退出不带累乙
        service._session_id, service._peer_name = "S1", JIA
        service._mark_helping()
        service._session_id, service._peer_name = "S1", YI
        service._mark_helping()
        key_yi = service._cooldown_key()
        check("前置：两场都在帮（两个格）",
              len(service._helping) == 2 and key_jia in service._helping
              and key_yi in service._helping, str(sorted(service._helping)))
        check("★ 在乙那一场按退出：乙退了",
              service.exit_helping() is True and service._is_helping() is False, "")
        service._session_id, service._peer_name = "S1", JIA
        check("★ 甲那一场**没被牵连**（该帮还帮）", service._is_helping() is True, "")
        check("★ 那一本上只少了乙那一格（不是整本清空）",
              len(service._helping) == 1 and key_jia in service._helping,
              str(sorted(service._helping)))

        # 认不出对象名（裸会话标识）也是一格
        service._session_id, service._peer_name = "S2", ""
        check("前置：认不出对象名 → 键退回裸会话标识（老行为）",
              service._cooldown_key() == "S2", service._cooldown_key())
        service._mark_helping()
        check("★ 认不出对象名那一格照样退得出来",
              service.exit_helping() is True and service._is_helping() is False, "")

        # 退出顺手作废"正等处境"那个窗口，但**不碰** `_running` / `_awaiting`
        service._session_id, service._peer_name = "S1", JIA
        service._want_situation_until = service._clock() + 600
        service._awaiting = True
        service._running = True
        service._mark_helping()
        check("★ 退出把「正等处境」那个窗口也作废了（跟 [不用了] 那一支同一个口径）",
              service.exit_helping() is True
              and service._want_situation_until == 0.0, "")
        check("★ 但**不动** `_awaiting`：那排按钮还在屏上，用户手里那一问不该被悄悄作废",
              service._awaiting is True, "")
        check("★ 也**不动** `_running`：那是「一次只拟一段」的闸门，界面伸手去改就坏",
              service._running is True, "")
        service._awaiting = False
        service._running = False

        service._want_situation_until = service._clock() + 600
        check("★ 没在帮时那一趟不碰任何状态（连「正等处境」都原样留着）",
              service.exit_helping() is False
              and service._want_situation_until > 0, "")
        service._want_situation_until = 0.0

        # ---------- ③ 退出之后：那两条腿 ----------

        section("③ 退出之后：自动那条腿停下、那一问能再冒（本批红线）")

        service._session_id, service._peer_name = "S1", JIA
        probe = Recorder(service, "_write")
        service._mark_helping()
        service._running = False
        service._last_listen_at = 0.0
        service._auto_generate(PEER_TALK)
        drain_threads()
        check("反面对照：还在帮的时候，这条腿真去拟（`_write` 被叫到）",
              len(probe.calls) == 1, f"{len(probe.calls)} 次")
        check("反面对照：…并且推了那一格冷却戳（说明真走到底了）",
              service._last_listen_at > 0, f"{service._last_listen_at}")

        probe.calls.clear()
        service._running = False
        service._last_listen_at = 0.0
        check("★ 按了退出", service.exit_helping() is True, "")
        service._auto_generate(PEER_TALK)
        drain_threads()
        check("★ 退出之后：自动那条腿**一步都不走**（连 `_write` 都不进）",
              probe.calls == [], str(probe.calls))
        check("★ 也**不推**冷却戳（那道闸摆在冷却之前，空转的拍子不占冷却）",
              service._last_listen_at == 0.0, f"{service._last_listen_at}")
        probe.restore()

        stub.prompts.clear()
        service._running = False
        service._last_listen_at = 0.0
        service._auto_generate(PEER_TALK)     # 这回走真 `_write`
        drain_threads()
        check("★ 退出之后**连模型都不调**（不是调完把结果丢掉）",
              stub.prompts == [], f"{len(stub.prompts)} 趟")

        # 那一问能不能再冒
        offers: list[str] = []
        app.bus.social_offer.connect(offers.append)
        key = service._cooldown_key()
        service._chat_text = PEER_TALK

        def _offer_case() -> bool:
            service._awaiting = False
            service._running = False
            service._last_asked_by_window.pop(key, None)   # 冷却那一格先空出来
            bubble.hide_offer()
            offers.clear()
            fired = service._ask_offer("Weixin.exe")
            pump(0.05)
            return fired

        check("反面对照：没在帮 → 那一问照常冒、照样发 `social_offer`",
              _offer_case() is True and len(offers) == 1, str(offers))
        service._awaiting = False
        bubble.hide_offer()
        pump(0.05)
        service._mark_helping()
        check("★ 在帮聊窗口里 → 那一问**根本不冒**"
              "（这就是「按过 [好呀] 之后再也见不着那排按钮」的根）",
              _offer_case() is False and offers == [], str(offers))
        check("★ 退出之后 → 那一问**又能再冒**（`_is_helping()` 那道闸放开了）",
              service.exit_helping() is True and _offer_case() is True and len(offers) == 1,
              str(offers))
        service._awaiting = False
        bubble.hide_offer()
        pump(0.05)

        # 冷却那一格：退出不清（"这一场半小时内不再冒话"照旧）
        service._last_asked_by_window.pop(key, None)
        service._last_asked = service._clock()
        stamp = service._last_asked
        service._mark_helping()
        service.exit_helping()
        check("★ 退出**不清**冷却那一格（半小时的冒话口径一个字没动）",
              service._last_asked == stamp and service._due() is False,
              f"{service._last_asked} / 该冒={service._due()}")
        service._last_asked_by_window.pop(key, None)

        # ---------- ④ 装配层那条真路 ----------

        section("④ 装配层那条真路：摘卡片 + 补交代 + 不碰库、不碰卡")

        _seed(app)
        card = app.card_service.create_manual_card(
            CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL
        )
        mind_before = app.mind_store.fragment_count()
        jia_profile = app.mind_store.profile(peer=JIA)
        jia_frags = app.mind_store.fragments(peer=JIA)
        pet_before = _rows(app.config.db_path, "utterance")

        service._session_id, service._peer_name = "S1", JIA
        service._mark_helping()
        app._show_social_draft(BODY, WHY)
        pump(0.05)
        check("前置：那一则上了屏、框也开着",
              len(_cards(bubble)) == 1 and bubble.isVisible() is True,
              f"{len(_cards(bubble))} 则 / visible={bubble.isVisible()}")
        check("前置：那只绝对 180 秒的表也起上了", app._card_open is True, "")

        app._on_social_help_exit()
        pump(0.05)
        check("★ 这一场退出来了", service._is_helping() is False, "")
        check("★ 那一则摘掉了（退了还摆在屏上，用户下一句照样以为在替他写）",
              _cards(bubble) == [], f"{len(_cards(bubble))} 则")
        check("★ 那只 180 秒的表也撤了（不留一颗到点空响的哑炮）",
              app._card_open is False and app._card_clear_timer.isActive() is False,
              f"open={app._card_open} / 表在跑={app._card_clear_timer.isActive()}")
        check("★ 补了一句交代（不交代就成了「宠物把知道的吐出来了、按个按钮什么都不剩」）",
              HELP_EXIT_NOTE in _texts(bubble), str(_notes(bubble)))
        check("★ 那一句把话说全了：退的是「接着帮」，不是「记没记过」",
              "一条没删" in HELP_EXIT_NOTE and "再要一段" in HELP_EXIT_NOTE,
              HELP_EXIT_NOTE)
        check("★ 框**不关**（用户正坐在框跟前按的这一个按钮，框是他的对话窗）",
              bubble.isVisible() is True, f"visible={bubble.isVisible()}")
        exit_src = inspect.getsource(bootstrap_mod.DesktopPetApp._on_social_help_exit)
        check("★ 这一处刻意跟 3 分钟那条收尾路不同：它调 `close_bubble()`，这一条不调",
              "close_bubble" not in _calls(bootstrap_mod.DesktopPetApp._on_social_help_exit)
              and "close_bubble" in _calls(bootstrap_mod.DesktopPetApp._on_card_clear_due),
              f"退出那趟调的：{sorted(_calls(bootstrap_mod.DesktopPetApp._on_social_help_exit))}")

        # 幂等：连按两下只有一句交代
        app._on_social_help_exit()
        pump(0.05)
        check("★ 幂等：再按一下不会再补第二句（服务那边返回 False 就什么都不做）",
              _texts(bubble).count(HELP_EXIT_NOTE) == 1,
              f"{_texts(bubble).count(HELP_EXIT_NOTE)} 句")

        # 真按钮那一条路：从界面一路走到服务（不能只靠直调装配层那只手来验）
        service._mark_helping()
        app._show_social_draft(BODY, WHY)
        pump(0.05)
        notes_before = _texts(bubble).count(HELP_EXIT_NOTE)
        button = _exit_buttons(bubble)
        check("前置：那一则又上屏了、那只出口也在",
              len(_cards(bubble)) == 1 and len(button) == 1, "")
        button[0].click()
        pump(0.05)
        check("★ 真按钮那条路整条通：点下去 → 这一场退了、那一则摘了、交代补上了",
              service._is_helping() is False and _cards(bubble) == []
              and _texts(bubble).count(HELP_EXIT_NOTE) == notes_before + 1
              and app._card_open is False,
              f"在帮={service._is_helping()} / {len(_cards(bubble))} 则 / "
              f"交代 {_texts(bubble).count(HELP_EXIT_NOTE)} 句")

        # 红线：不碰记过的东西
        check("★ 红线：三记忆库**一条没删**（按对端分格的那两格原样在）",
              app.mind_store.profile(peer=JIA) == jia_profile
              and app.mind_store.fragments(peer=JIA) == jia_frags
              and len(app.mind_store.fragments(peer=JIA)) == 2,
              f"{len(app.mind_store.fragments(peer=JIA))} 条碎片")
        check("★ 红线：别人那一格（乙）与主人自己那一档**也一条没动**",
              app.mind_store.profile(peer=YI).get("name") == "小红"
              and any("主人自己那一档" in str(f.get("text") or "")
                      for f in app.mind_store.fragments(peer="")), "")
        check("★ 红线：零写入（mind.db 碎片数跑完整条路一条没多）",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")
        check("★ 红线：对象攻略卡**没被清掉**（退的是「接着帮」，不是「那张卡不算了」）",
              app.card_service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE) is not None
              and app.card_service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE).id
              == getattr(card, "id", None), "")
        check("★ 红线：pet.db 零写入（utterance 一行没多）",
              _rows(app.config.db_path, "utterance") == pet_before, "")

        # 没在帮的时候按下：一点动静都没有
        cards_now = len(_cards(bubble))
        notes_now = _texts(bubble).count(HELP_EXIT_NOTE)
        app._on_social_help_exit()
        pump(0.05)
        check("★ 没在帮时按下：不补交代、不动界面（不会凭空冒半句「好，不替你写了」）",
              _texts(bubble).count(HELP_EXIT_NOTE) == notes_now
              and len(_cards(bubble)) == cards_now, "")

        # 别的线摆在上面的按钮不动；帮聊自己那排跟着退
        service._mark_helping()
        bubble.show_offer("notes")
        pump(0.05)
        app._on_social_help_exit()
        pump(0.05)
        check("★ 别的线（笔记）摆在上面的那排按钮**一个字不动**"
              "（替用户作废掉，那几条线就再也点不着了）",
              bubble.offer_theme == "notes" and bubble._offer_row.isVisible() is True,
              f"theme={bubble.offer_theme!r} 露着={bubble._offer_row.isVisible()}")
        bubble.hide_offer()
        pump(0.05)

        service._mark_helping()
        bubble.show_offer("social_card")
        pump(0.05)
        app._on_social_help_exit()
        pump(0.05)
        check("★ 帮聊自己那排按钮跟着退（不留「按钮还挂着、服务那一格已经空了」的错位）",
              bubble.offer_theme == "" and bubble._offer_row.isVisible() is False,
              f"theme={bubble.offer_theme!r} 露着={bubble._offer_row.isVisible()}")

        # 接线与红线（源码那一层）
        bsrc = inspect.getsource(bootstrap_mod)
        check("★ 接线：装配层真把这一声接上了",
              "help_exit_requested.connect(self._on_social_help_exit)" in bsrc, "")
        check("★ 装配层**不自己碰那一格**（进出都只走服务那个方法）",
              "_helping[" not in bsrc and "_helping.pop" not in bsrc
              and "service.exit_helping()" in bsrc, "")
        social_src = inspect.getsource(smod)
        check("★ 也没有新开第二条出口：全仓只有 `exit_helping` 这一个退帮聊的入口",
              "def exit_helping" in social_src
              and social_src.count("_forget_helping()") == 3, "")
        check("★ 社交这条线还是**一个库都不碰**（退出这条路也没多一只手）",
              "mind_store" not in exit_src and "sqlite3" not in exit_src, "")

        refs: set[str] = set()
        for relative in _TOUCHED:
            source = (Path(__file__).resolve().parents[1] / relative).read_text(
                encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    refs.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    refs.update(alias.name for alias in node.names)
        check("★ 零新依赖：这一批动的几个文件没多一个第三方 import",
              not (_BANNED & refs), "、".join(sorted(_BANNED & refs)) or "（一处都没有）")

        check("★ 那个出口不是「危险操作」的样子：样式跟复制回复同一款",
              'setObjectName("ChatOffer")' in inspect.getsource(
                  ChatBubble._build_social_card), "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("帮聊「随时能退」（v2-social-exit-easy）")


if __name__ == "__main__":
    raise SystemExit(main())
