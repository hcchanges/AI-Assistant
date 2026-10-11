"""「还在加载」的占位屏不再被当成单聊（v2-social-list-leak-fix）的自动测试。

**真机坐实**（2026-10-11 09:23:06，`data/logs/pet.log`）：登录微信后打开「公众号」，
那一屏内容还在加载，聊天区里认出来**只有一个「正在加载」**（`社交窗口认到 4 个字
（对方 6 行 / 自己 0 行）`）。这一屏把 `classify_chat_page` 的每条判据**全躲过去了**：

    · 纯时间行 0 条（`looks_like_list_page` 时间腿要 5 条）；
    · 列表专有字面 0 类（字面腿要 2 类）；
    · 没有右对齐时间列（几何腿要 6 个短框）；
    · 表头那几行没有固定页名。

于是它落到兜底的 `return PAGE_PERSONAL`，被当成"跟某人一对一"——`note_page_kind` 当
「非单聊 → 单聊」跃迁冒了 offer（Bug X）；用户点了 [好呀] 之后，这屏的字又被当了材料。
那一戳还把这一场（`_cooldown_key()`）永久记进 `_helping`（无 TTL），之后切回真正的
个人单聊被 `_ask_offer` 开局的 `_is_helping()` **静默**挡下（Bug Y）。

本测试钉四件事：

**① 判据**：整屏只有「正在加载」这类占位、无一真话、无 `SENDER_USER` → 判为非单聊
（`looks_like_list_page` 的第三条腿，复用 16-β 的"清成空 read"那条路，落 `PAGE_LIST`）。

**② 不冒话、不落戳**：这一屏 `_ask_offer` 不冒 offer，`_last_asked_by_window` 里也不留
这一场的冷却戳。这是**双保险**——`on_foreground` 那条"没接 OCR / 取帧失败"的兜底路
压根不走页型判定，只有这一道拦得住。

**③ 从根切断**：走真实回调 `_on_screen_read`，加载屏的页型是非单聊 → 不冒 offer → 这一场
**进不了** `_helping`（用户没有可点的 [好呀]，Bug Y 的根就此断掉）。

**④ 黏连态出口**：`confirm_card` 的 [不用了] 清掉当前那一格的 `_helping`（§179-己.3 点明
的那一行），且**不碰** `_last_asked_by_window`。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；不模拟按键；两道危机闸 /
三白名单判定点 / 列表免 OCR 一分没动。
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary

from life.screen import social as smod
from life.screen.social import (
    PAGE_LIST,
    PAGE_PERSONAL,
    SocialReplyService,
    _is_loading_line,
    _looks_like_loading_material,
    classify_chat_page,
    looks_like_list_page,
    looks_like_loading_page,
)
from life.screen.social_window import (
    SENDER_OTHER,
    SENDER_USER,
    ChatRead,
)


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _loading_read() -> ChatRead:
    """加载屏那一拍：只有「正在加载」，色卡一律判"对方"（真机 6 行 / 自己 0 行同款）。"""
    return ChatRead(lines=[(SENDER_OTHER, "正在加载")])


def _chat_read() -> ChatRead:
    """一场真单聊：对方刚回一句（末条是对方发的）。"""
    return ChatRead(lines=[(SENDER_OTHER, "周末一起吃个饭？")])


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    app.config.screen.social_cooldown_min = 30

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()
    src = inspect.getsource(smod)

    offers: list[str] = []
    app.bus.social_offer.connect(offers.append)

    # 社交软件在前台、这一场认到"甲"（`_cooldown_key()` 就是"甲"）。
    service._social_foreground = True
    service._last_proc = "Weixin.exe"
    service._session_provider = lambda: "甲"
    service._sync_session()
    service._examples_provider = lambda query: ""
    service._running = False
    service._awaiting = False
    key = service._cooldown_key()
    check("前置：这一场的键就认到「甲」（`_cooldown_key()` = `_session_id`）",
          key == "甲", repr(key))

    try:
        # ---------- ① 判据 ----------

        section("① 判据：整屏只有加载占位 → 非单聊")

        lr = _loading_read()
        check("前提：色卡把「正在加载」判成对方说的（真机 6 行 / 自己 0 行 同款）",
              lr.lines == [(SENDER_OTHER, "正在加载")], repr(lr.lines))
        check("★ `looks_like_loading_page` 命中", looks_like_loading_page(lr) is True, "")
        check("★ `looks_like_list_page` 也判它非单聊（第三条腿）",
              looks_like_list_page(lr) is True, "")
        check("★ `classify_chat_page` 不再回 `PAGE_PERSONAL`（这就是 Bug X 的根）",
              classify_chat_page(lr) != PAGE_PERSONAL, classify_chat_page(lr))
        check("★ 落的是 `PAGE_LIST`（复用 16-β「清成空 read」那条路）",
              classify_chat_page(lr) == PAGE_LIST, classify_chat_page(lr))

        # 反面对照：不是"把单聊整条闷死"
        check("反面对照：一句真话 → `PAGE_PERSONAL`",
              classify_chat_page(_chat_read()) == PAGE_PERSONAL,
              classify_chat_page(_chat_read()))
        check("反面对照：空 read 仍当单聊（口径①默认放行，一个字都没认到不算加载）",
              classify_chat_page(ChatRead()) == PAGE_PERSONAL, "")
        check("反面对照：自己发过话的屏不判加载（绿气泡是聊天窗的硬证据）",
              looks_like_loading_page(ChatRead(lines=[(SENDER_USER, "正在加载")])) is False, "")
        check("反面对照：「加载中别急」这种夹着别的话的行不算占位",
              looks_like_loading_page(ChatRead(lines=[(SENDER_OTHER, "加载中别急")])) is False, "")
        check("反面对照：真单聊 + 一行加载字混在一起 → 不判加载（要「每一行都是」）",
              looks_like_loading_page(ChatRead(
                  lines=[(SENDER_OTHER, "正在加载"), (SENDER_OTHER, "在吗")])) is False, "")

        # 单行 / 材料那一层
        check("`_is_loading_line`：整行就是加载字 → True", _is_loading_line("正在加载") is True, "")
        check("`_is_loading_line`：带省略号 / 空格的 OCR 变形照认",
              _is_loading_line("加载中...") is True and _is_loading_line("正在 加载") is True, "")
        check("★ `_looks_like_loading_material` 对「正在加载」= True",
              _looks_like_loading_material("正在加载") is True, "")
        check("反面对照：空材料不算占位（「没认到」是另一种情形）",
              _looks_like_loading_material("") is False, "")
        check("反面对照：真话材料不算占位",
              _looks_like_loading_material("周末一起吃个饭？") is False, "")

        # ---------- ② 不冒话、不落戳 ----------

        section("② 加载屏不冒话、不落冷却戳（双保险，挡的是不走页型判定的那条兜底路）")

        def _offer_case(material: str) -> tuple[bool, bool]:
            service._last_asked_by_window.pop(key, None)
            service._helping.pop(key, None)
            service._awaiting = False
            service._running = False
            service._chat_text = material
            offers.clear()
            fired = service._ask_offer("Weixin.exe")
            return fired, key in service._last_asked_by_window

        fired, stamped = _offer_case("正在加载")
        check("★ 加载屏不冒 offer", fired is False, f"fired={fired} offers={offers!r}")
        check("★ 也不落这一场的冷却戳（别把冷却按在没聊起来的屏上）",
              stamped is False, f"stamped={stamped}")

        fired, stamped = _offer_case("周末一起吃个饭？")
        check("反面对照：一句真话照样冒、照样落戳",
              fired is True and stamped is True, f"fired={fired} stamped={stamped}")
        check("反面对照：真冒了才发 `social_offer`", len(offers) == 1, repr(offers))
        app.chat_bubble.hide_offer()
        service._awaiting = False

        # ---------- ③ 从根切断 ----------

        section("③ 从根切断：加载屏这一拍不进帮聊窗口（Bug Y 的根）")

        service._helping.pop(key, None)
        service._last_asked_by_window.pop(key, None)
        service._page_kind = ""
        service._awaiting = False
        service._running = False
        offers.clear()
        # 走真实回调：装配层认完字把这一屏交给服务（`_on_screen_read` 就是那一步）。
        service._on_screen_read(_loading_read())
        check("★ 加载屏这一拍没冒 offer（用户没有可点的 [好呀]）",
              offers == [], repr(offers))
        check("★ 这一场没被记进 `_helping`（Bug Y 的根被切断了）",
              key not in service._helping, repr(service._helping))
        check("★ 页型记成非单聊 `PAGE_LIST`（事实照记）",
              service._page_kind == PAGE_LIST, repr(service._page_kind))

        # 反面对照：真单聊那一拍照样冒 offer；点了 [好呀] 才进窗口
        service._page_kind = ""
        service._last_asked_by_window.pop(key, None)
        service._awaiting = False
        service._running = False
        offers.clear()
        service._on_screen_read(_chat_read())
        check("反面对照：真单聊照样冒 offer", offers != [], repr(offers))
        # `confirm("好呀")` 的落戳排在 `_draft_now` 之前；把 `_running` 立起来只为让
        # `_draft_now` 当场收手（"上一段我还在写"），免得这一节去起起草线程——验的是
        # "点了 [好呀] 就进窗口"那一行，跟起草无关。
        service._running = True
        service.confirm("好呀")
        check("反面对照：点了 [好呀] 才进帮聊窗口", key in service._helping,
              repr(service._helping))
        service._running = False
        service._helping.pop(key, None)

        # ---------- ④ 黏连态出口 ----------

        section("④ 黏连态出口：建卡那一问的 [不用了] 清掉当前这一格")

        service._helping.clear()
        service._last_asked_by_window.clear()
        service._mark_helping()
        service._last_asked_by_window[key] = 1234.0     # 造一个冷却戳，验它不被碰
        check("前提：这一场进过帮聊窗口", service._is_helping() is True, "")
        check("前提：这一场也留着冷却戳",
              service._last_asked_by_window.get(key) == 1234.0,
              repr(service._last_asked_by_window))

        ok = service.confirm_card("不用了")
        check("★ [不用了] 回 False", ok is False, f"{ok}")
        check("★ 这一场从帮聊窗口里退出来了（§179-己.3 点明的那一行）",
              service._is_helping() is False, repr(service._helping))
        check("★ 冷却那一格一个字没碰（冒话冷却照旧按 30 分钟）",
              service._last_asked_by_window.get(key) == 1234.0,
              repr(service._last_asked_by_window))

        service._mark_helping()
        check("反面对照：[好呀] 不动这一格（建卡照建）",
              service.confirm_card("好呀") is True and service._is_helping() is True, "")
        service._helping.pop(key, None)
        service._last_asked_by_window.clear()

        # ---------- ⑤ 红线 ----------

        section("⑤ 红线：两库零写入、排队位置、冷却语义未动")

        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        offer_body = src[src.index("def _ask_offer"):src.index("def note_page_kind")]
        gate = "if _looks_like_loading_material(self._chat_text):"
        check("★ 加载那道闸排在落戳 `self._last_asked = self._clock()` **之前**",
              offer_body.index(gate)
              < offer_body.index("self._last_asked = self._clock()"), "")
        check("★ 也排在危机闸**之后**（危机红线不跳步、不被这道顶掉）",
              offer_body.index("crisis.is_crisis(ASK_LINE)")
              < offer_body.index(gate), "")

        list_body = src[src.index("def looks_like_list_page"):src.index("def looks_like_list_boxes")]
        check("★ 加载那条腿挂在 `looks_like_list_page` 里（判成非单聊、走 16-β 的空 read 路）",
              "looks_like_loading_page" in list_body, "")

        card_body = src[src.index("def confirm_card"):src.index("def last_request")]
        check("★ `confirm_card` 的 [不用了] 支路清了 `_helping`",
              "_forget_helping()" in card_body, "")
        check("★ 且**没碰** `_last_asked_by_window`（冷却语义按拍板保留）",
              "_last_asked_by_window." not in card_body, "")

        check("★ 这一格还是只在内存里（就是个字典）",
              isinstance(service._helping, dict), type(service._helping).__name__)
        check("不模拟按键 / 不读剪贴板（这一条改动一个都没碰）",
              not any(w in src for w in ("SendInput", "keybd_event", "uiautomation",
                                         "pyautogui", "pynput", "win32clipboard")), "")
        check("★ 危机两道闸一个字没动",
              src.count("crisis.is_crisis") >= 3, str(src.count("crisis.is_crisis")))

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交列表页误判（v2-social-list-leak-fix）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
