"""「账号选择 / 进入微信」那一页不再被当成单聊（v2-social-account-pick-page-leak-fix）的自动测试。

**真机坐实**（2026-10-11 10:24-10:25，`data/logs/pet.log` + 取证帧）：登录后停在「账号选择 /
进入微信」那一页（头像 / 昵称 + 绿色「进入微信」按钮 + 底部「切换账号」/「仅传输文件」），
还没进聊天。这一页跟「还没登录」的二维码页是**两种页**——二维码页中间就一个「进入微信」大
按钮，`_LOGIN_SUBSTR_NAMES` 那条表头子串腿已经挡着；这一页上面多了头像 / 昵称那几行，把
「进入微信」顶出表头四行之外，而「切换账号」「仅传输文件」又长在整页**最底部**——那条腿只
扫表头，三句全够不着，于是 `classify_chat_page` 落到兜底 `return PAGE_PERSONAL`，被当成
"跟某人一对一"：冒 offer、读占位内容；用户点 [好呀] 之后同时污染 `_last_asked_by_window`
["微信"]（30min 冷却）与 `_helping["微信"]`（帮聊窗口黏连态）。

本测试钉四件事：

**① 判据**：整屏含「进入微信」+「切换账号」/「仅传输文件」、且没有任何聊天内容 → 判非单聊
（`classify_chat_page` 回 `PAGE_LOGIN`，兜底 `return PAGE_PERSONAL` 一个字没删）。

**② 不冒话、不落戳**：走真实回调 `_on_screen_read`，这一页不冒 offer，`_last_asked_by_window`
里也不留这一场的冷却戳。

**③ 从根切断**：这一页不冒 offer → 用户**没有可点的 [好呀]** → `_helping` 进不来；反面对照
真单聊照样冒、点 [好呀] 才进窗口。

**④ 回归保护**：真单聊页照旧回 `PAGE_PERSONAL`；带绿气泡 / 带时间行的"进微信"字样不误判。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；不模拟按键；两道危机闸 /
三白名单判定点 / 列表免 OCR / afebdcb 的 loading 腿 / 既有登录腿一分没动。
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
    PAGE_LOGIN,
    PAGE_PERSONAL,
    SocialReplyService,
    classify_chat_page,
    looks_like_account_pick_page,
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


def _account_pick_read() -> ChatRead:
    """账号选择 / 进入微信页：头像 / 昵称那几行把「进入微信」顶出表头四行，
    「切换账号」「仅传输文件」又长在整页最底部。"""
    return ChatRead(lines=[
        (SENDER_OTHER, "何澄"),
        (SENDER_OTHER, "昵称"),
        (SENDER_OTHER, "安全"),
        (SENDER_OTHER, "绑定"),
        (SENDER_OTHER, "进入微信"),
        (SENDER_OTHER, "切换账号"),
        (SENDER_OTHER, "仅传输文件"),
    ])


def _account_pick_with_self() -> ChatRead:
    """同上一页，但末尾多一行自己发过话（绿气泡）——那是聊天窗的硬证据。"""
    return ChatRead(lines=[
        (SENDER_OTHER, "何澄"),
        (SENDER_OTHER, "昵称"),
        (SENDER_OTHER, "安全"),
        (SENDER_OTHER, "绑定"),
        (SENDER_OTHER, "进入微信"),
        (SENDER_OTHER, "切换账号"),
        (SENDER_USER, "我在"),
    ])


def _account_pick_with_time() -> ChatRead:
    """同上一页，但末尾多一行时间行——聊天内容才有的装潢。"""
    return ChatRead(lines=[
        (SENDER_OTHER, "何澄"),
        (SENDER_OTHER, "昵称"),
        (SENDER_OTHER, "安全"),
        (SENDER_OTHER, "绑定"),
        (SENDER_OTHER, "进入微信"),
        (SENDER_OTHER, "切换账号"),
        (SENDER_OTHER, "14:22"),
    ])


def _enter_only_read() -> ChatRead:
    """只有「进入微信」大按钮、没有底栏——二维码登录页那种，由既有登录腿挡。"""
    return ChatRead(lines=[(SENDER_OTHER, "进入微信")])


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

        section("① 判据：账号选择 / 进入微信页 → 非单聊")

        ar = _account_pick_read()
        check("★ `looks_like_account_pick_page` 命中", looks_like_account_pick_page(ar) is True, "")
        check("★ `classify_chat_page` 不再回 `PAGE_PERSONAL`（这就是漏法的根）",
              classify_chat_page(ar) != PAGE_PERSONAL, classify_chat_page(ar))
        check("★ 落的是 `PAGE_LOGIN`（跟二维码登录同一档）",
              classify_chat_page(ar) == PAGE_LOGIN, classify_chat_page(ar))

        # 反面对照：不是"把单聊整条闷死"
        check("反面对照：一句真话 → `PAGE_PERSONAL`",
              classify_chat_page(_chat_read()) == PAGE_PERSONAL,
              classify_chat_page(_chat_read()))
        check("反面对照：自己发过话（绿气泡）的屏不判账号选择页",
              looks_like_account_pick_page(_account_pick_with_self()) is False, "")
        check("反面对照：带时间行的屏不判账号选择页（那是聊天内容的装潢）",
              looks_like_account_pick_page(_account_pick_with_time()) is False, "")
        check("反面对照：只有「进入微信」没底栏 → 本腿不命中",
              looks_like_account_pick_page(_enter_only_read()) is False, "")
        check("（那条老登录腿照旧挡得住它，落 `PAGE_LOGIN`）",
              classify_chat_page(_enter_only_read()) == PAGE_LOGIN,
              classify_chat_page(_enter_only_read()))

        # ---------- ② 不冒话、不落戳 ----------

        section("② 账号选择页不冒 offer、不落冷却戳")

        service._helping.pop(key, None)
        service._last_asked_by_window.pop(key, None)
        service._page_kind = ""
        service._awaiting = False
        service._running = False
        offers.clear()
        # 走真实回调：装配层认完字把这一屏交给服务（`_on_screen_read` 就是那一步）。
        service._on_screen_read(_account_pick_read())
        check("★ 这一页没冒 offer", offers == [], repr(offers))
        check("★ 也没落这一场的冷却戳（别把冷却按在没聊起来的页上）",
              key not in service._last_asked_by_window, repr(service._last_asked_by_window))
        check("★ 页型记成非单聊 `PAGE_LOGIN`（事实照记）",
              service._page_kind == PAGE_LOGIN, repr(service._page_kind))

        # ---------- ③ 从根切断 ----------

        section("③ 从根切断：这一页进不了帮聊窗口（误点 [好呀] 也白搭）")

        service._helping.pop(key, None)
        service._last_asked_by_window.pop(key, None)
        service._page_kind = ""
        service._awaiting = False
        service._running = False
        offers.clear()
        service._on_screen_read(_account_pick_read())
        check("★ 没冒 offer（这一页没有可点的 [好呀]）", offers == [], repr(offers))
        check("★ 这一场没被记进 `_helping`（污染从根上断了）",
              key not in service._helping, repr(service._helping))
        check("★ 页型照记 `PAGE_LOGIN`（事实照记）",
              service._page_kind == PAGE_LOGIN, repr(service._page_kind))

        # 反面对照：真单聊照样冒 offer；点了 [好呀] 才进窗口
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
        check("反面对照：点 [好呀] 才进帮聊窗口", key in service._helping,
              repr(service._helping))
        service._running = False
        service._helping.pop(key, None)

        # ---------- ④ 回归保护 ----------

        section("④ 回归保护：真单聊照旧回 personal")

        check("★ 真单聊 → `PAGE_PERSONAL`",
              classify_chat_page(_chat_read()) == PAGE_PERSONAL,
              classify_chat_page(_chat_read()))
        check("反面对照：带绿气泡的「进微信」屏 → 落回 `PAGE_PERSONAL`",
              classify_chat_page(_account_pick_with_self()) == PAGE_PERSONAL,
              classify_chat_page(_account_pick_with_self()))
        check("反面对照：带时间行的「进微信」屏 → 落回 `PAGE_PERSONAL`",
              classify_chat_page(_account_pick_with_time()) == PAGE_PERSONAL,
              classify_chat_page(_account_pick_with_time()))

        # ---------- ⑤ 红线 ----------

        section("⑤ 红线：两库零写入、排队位置、既有腿未动")

        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        cc_body = src[src.index("def classify_chat_page"):src.index("def is_personal_kind")]
        check("★ 账号选择腿挂在兜底 `return PAGE_PERSONAL` **之前**",
              cc_body.index("looks_like_account_pick_page(chat)")
              < cc_body.index("return PAGE_PERSONAL"), "")
        check("★ 兜底 `return PAGE_PERSONAL` 一个字符没删（还在原地）",
              "return PAGE_PERSONAL" in cc_body, "")
        check("★ 既有登录腿的多字名一个字没动（进入微信 / 切换账号 / 扫码登录 都在）",
              '"进入微信"' in src and '"切换账号"' in src and '"扫码登录"' in src, "")
        check("★ afebdcb 的 loading 腿没动（`looks_like_loading_page` 还在）",
              "def looks_like_loading_page(read" in src, "")
        check("★ 危机两道闸一个字没动",
              src.count("crisis.is_crisis") >= 3, str(src.count("crisis.is_crisis")))
        check("不模拟按键 / 不读剪贴板（这一条改动一个都没碰）",
              not any(w in src for w in ("SendInput", "keybd_event", "uiautomation",
                                         "pyautogui", "pynput", "win32clipboard")), "")
        check("这一格还是只在内存里（就是个字典）",
              isinstance(service._helping, dict), type(service._helping).__name__)

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交账号选择/进入微信页误判（v2-social-account-pick-page-leak-fix）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
