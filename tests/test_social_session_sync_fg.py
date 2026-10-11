"""社交「换会话认人」那一拍补到抓屏那条腿上（v2-social-session-sync-on-foreground）。

v2-social-session-reset 把"这一场还是不是上一个人"这件事接进了服务：每拍拿装配层给的
会话标识比一次，换了人就清掉上一场那十格。可**刷新时机原来只有两处**：

- `_poll_listen`（1.5 秒一拍）——**只在自动监听开着时才跑**；
- `_snap_pending`（挂住那一问期间的按需抓拍）。

于是「社交软件一直挂在前台、人从甲的对话换到乙的对话」这条路上，会话标识还能靠监听那拍
刷新；可**用户切回来那一瞬**（`on_foreground` → `_read_social_screen`）不刷。这一节把
`_read_social_screen` 也接上：抓帧成功之后就地 `_sync_session()`，换人了就清上一场，
并把"这一屏已认过"那份签名作废（这一屏是新一场的底）。

**这一节为什么非有不可**：上一节（v2-social-cooldown-per-window）把冒话那道 30 分钟冷却
从"全场一格"改成"按这一场是谁分格"，键取的正是 `_session_id`。标识要是还端着上一个人，
切回乙那一场冒的话就**记在甲头上**——乙自己那一格仍旧"从没冒过"，分格这件事当场落空。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；**不额外读窗口标题**
（那只手还是原来那一次读取，见 `bootstrap._social_session_id`）；不动 `_awaiting` 状态机；
不动监听冷却 / 去重闸 / 气泡色。
"""

from __future__ import annotations

import inspect
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for  # noqa: E402

from life.screen.social import SocialReplyService  # noqa: E402
from life.screen.social_window import ChatRead, SENDER_OTHER  # noqa: E402


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.ocr_enabled = True
    # **自动监听故意关掉**：这一节验的就是"监听那条腿不在场时，换会话也认得出来"。
    # 开着的话监听每 1.5 秒刷一次标识，就把这一节要看的那一处永远遮住了。
    app.config.screen.social_auto_listen = False

    grab = {"frame": "F0"}
    lines = {"v": [(SENDER_OTHER, "在吗")]}
    ident = {"s": "甲"}
    clock = {"t": 1000.0}
    reads = {"n": 0}
    offers: list[str] = []
    app.bus.social_offer.connect(offers.append)

    def _read(array) -> ChatRead:
        reads["n"] += 1
        return ChatRead(lines=list(lines["v"]))

    service._grab_chat = lambda: grab["frame"]
    service._read_chat = _read
    service._idle_seconds = lambda: 10.0
    service._session_provider = lambda: ident["s"]
    service._examples_provider = lambda query: ""
    service._clock = lambda: clock["t"]

    # 冒话那道闸按住（这一节不验它，它另有 test_social_cooldown_per_window.py）；
    # 让它真冒的话，`_awaiting` 会挂起来，后面每一节的读屏就得先处理它，把要看的那一处
    # 搅进一堆无关状态里。⑤ 那一节再把它放回真的。
    orig_due = service._due
    service._due = lambda: False

    def _settle() -> None:
        """等这一拍抓帧 + 后台认字那一趟落定。"""
        wait_for(lambda: not service._chat_reading)
        drain_threads()
        pump(0.1)

    def _feed_memory() -> None:
        """把"上一场已经拟过什么"那十格喂成"确实有记忆"的样子。"""
        service._last_draft_source = "上一场那份原话"
        service._pending_auto_source = "上一场那一段在途"
        service._last_offered = 123.0
        service._last_listen_tail = "上一场的尾巴"
        service._last_listen_kind = "incoming"
        service._done = True
        service._done_lines = {"上一场这一屏"}

    def _memory_kept() -> bool:
        return (service._last_draft_source == "上一场那份原话"
                and service._pending_auto_source == "上一场那一段在途"
                and service._last_offered == 123.0
                and service._last_listen_tail == "上一场的尾巴"
                and service._last_listen_kind == "incoming"
                and service._done is True
                and service._done_lines == {"上一场这一屏"})

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 切回社交前台那一拍 = 换会话也认得出（不用等监听那拍） ----------

        section("① 切回社交软件那一拍（抓屏那条腿）就认得出换人，监听腿不在场也算数")

        check("前置：会话标识那只手接上了", service._session_provider is not None, "")
        check("前置：自动监听没开（监听那条腿不在场）",
              app.config.screen.social_auto_listen is False, "")

        service._social_foreground = True       # 人**一直在**社交软件里，没走开过
        service._session_id = "甲"
        _feed_memory()
        check("前置：上一场那十格确实有记忆", _memory_kept(), "")

        ident["s"] = "乙"                       # 微信里换了个人聊
        grab["frame"] = "F1"
        reads["n"] = 0
        service.on_foreground("Weixin.exe")     # 前台切回社交软件这一拍
        _settle()

        check("★ 切回来这一拍就认出来了：会话标识换成新那一场",
              service._session_id == "乙", repr(service._session_id))
        check("★ 上一场拟过的那份原话清掉了（就是它在挡新会话）",
              service._last_draft_source == "", repr(service._last_draft_source))
        check("★ 在途那一份也清掉了", service._pending_auto_source == "",
              repr(service._pending_auto_source))
        check("★ 建卡那一问的「这趟只问一次」跟着翻篇", service._last_offered == 0.0,
              str(service._last_offered))
        check("★ 完成态锁解开了（上一段的锁不该锁新会话）",
              service._done is False and service._done_lines == set()
              and service._locked_pending is False,
              f"done={service._done} / 剩 {len(service._done_lines)} 行")
        check("★ 末尾归属与尾巴都清了（新会话要从零起）",
              service._last_listen_kind == "" and service._last_listen_tail == "",
              f"{service._last_listen_kind!r} / {service._last_listen_tail!r}")
        check("这一拍真认了一遍字（不是空跑）", reads["n"] >= 1, f"{reads['n']} 次")

        # ---------- ② 同一场 / 认不出 → 一格都不动 ----------

        section("② 同一场 / 认不出这一场是谁 → 一格都不动（宁可漏清，不误清）")

        _feed_memory()
        grab["frame"] = "F2"
        service.on_foreground("Weixin.exe")     # 同一个会话，只是又切回来一次
        _settle()
        check("★ 同一场：十格一个都没动", _memory_kept(),
              repr(service._last_draft_source))
        check("同一场：会话标识照旧", service._session_id == "乙", repr(service._session_id))

        _feed_memory()
        ident["s"] = ""                         # 装配层这一拍读不出会话名
        grab["frame"] = "F3"
        service.on_foreground("Weixin.exe")
        _settle()
        check("★ 认不出（空标识）→ 当没换人：十格一个都没动", _memory_kept(),
              repr(service._last_draft_source))
        check("★ 认不出时也不把会话标识记成空（空串当「没这条线索」）",
              service._session_id == "乙", repr(service._session_id))
        ident["s"] = "乙"

        # ---------- ③ 换人那一拍：签名作废之后由新抓的这帧顶上 ----------

        section("③ 「这一屏已认过」那份签名：换人先作废，紧跟的新帧就是新一场的底")

        _feed_memory()
        ident["s"] = "丙"
        grab["frame"] = "F9"
        service.on_foreground("Weixin.exe")
        _settle()
        check("★ 换人认出来了（甲/乙 → 丙）", service._session_id == "丙",
              repr(service._session_id))
        check("★ 换人那一拍记下的就是**这一帧**的签名（新一场的底，不是上一场那份）",
              service._snap_seen_fp == SocialReplyService._fingerprint("F9"),
              repr(service._snap_seen_fp))

        # ---------- ④ 源码层：三处调用点同一套口径 ----------

        section("④ 源码层：抓帧成功之后就地同步，三处调用点口径一致")

        fg_src = inspect.getsource(SocialReplyService._read_social_screen)
        poll_src = inspect.getsource(SocialReplyService._poll_listen)
        snap_src = inspect.getsource(SocialReplyService._snap_pending)
        sync_src = inspect.getsource(SocialReplyService._sync_session)

        check("★ 抓屏那条腿里接了 `_sync_session()`", "_sync_session()" in fg_src, "")
        check("★ 接在**抓帧成功之后**（`array is None` 那道早退之后）",
              fg_src.index("if array is None") < fg_src.index("_sync_session()"), "")
        check("★ 换人了就把签名作废（`_sync_session():` 那一支里就是 `_snap_seen_fp = None`）",
              re.search(r"_sync_session\(\):\r?\n(?: *#.*\r?\n)* *self\._snap_seen_fp = None",
                        fg_src) is not None, "")
        check("★ 作废之后由这一帧顶上（顺序不能反）",
              fg_src.index("_snap_seen_fp = None") < fg_src.index("_snap_seen_fp = self._fingerprint(array)"), "")
        check("三处调用点都在：前台那条 + 监听那条 + 挂问那条",
              "_sync_session()" in fg_src and "_sync_session()" in poll_src
              and "_sync_session()" in snap_src, "")
        check("监听那条腿的签名作废没动", "_last_fingerprint = None" in poll_src, "")
        check("挂问那条腿的签名作废没动", "_snap_seen_fp = None" in snap_src, "")
        check("★ 会话标识不进日志，只记长度（跟原来那条口径一致）",
              "len(previous)" in sync_src and "len(ident)" in sync_src
              and "logger.debug(\n            \"社交窗换了会话（上一场标识 %d 字 → 这一场 %d 字）" in sync_src, "")
        check("★ `_sync_session` 一条新窗口读取都没加（用的还是装配层给的那只手）",
              "_session_provider" in sync_src and "GetWindowText" not in sync_src
              and "win32" not in sync_src and "import" not in sync_src, "")
        check("★ 不动 `_awaiting`：换会话不收那排按钮（那是界面的地盘）",
              "self._awaiting" not in sync_src and "self._awaiting" not in inspect.getsource(
                  SocialReplyService._forget_session_memory), "")

        # ---------- ⑤ 跟冒话冷却那条线接上：落戳记在**新那一场**上 ----------

        section("⑤ 接上一节（v2-social-cooldown-per-window）：切回来冒的那句记在新那一场")

        service._due = orig_due                 # 把冷却那道闸放回真的
        service._last_asked_by_window.clear()
        service._asked_key = ""
        service._awaiting = False
        service._running = False
        service._page_kind = ""                 # 退出来，「非单聊 → 单聊」那道跃迁才成立
        offers.clear()
        service._session_id = "甲"
        service._last_asked_by_window["甲"] = clock["t"]    # 甲刚冒过，还在冷却里
        check("前置：这会儿键还是甲（标识还没刷）",
              service._cooldown_key() == "甲", repr(service._cooldown_key()))

        _feed_memory()
        ident["s"] = "丁"                       # 微信里换到丁，切回社交软件
        grab["frame"] = "F10"
        service.on_foreground("Weixin.exe")
        _settle()
        check("★ 切回来标识就刷成丁了（这一拍是冒话之前最后一道刷新）",
              service._cooldown_key() == "丁", repr(service._cooldown_key()))
        check("★ 落戳记在**丁**那一格上（没刷标识的话会记到甲头上，丁那一格永远是空的）",
              service._last_asked_by_window.get("丁") == clock["t"],
              repr(service._last_asked_by_window))
        check("★ 这一句确实是那一拍冒出来的（`social_offer` 发出去了）",
              service._awaiting is True and offers == ["Weixin.exe"], f"{offers}")
        check("★ 记在哪一格的那一笔也跟着是丁",
              service._asked_key == "丁", repr(service._asked_key))

        # ---------- ⑥ 红线：两库零写入 ----------

        section("⑥ 红线：两库零写入")

        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
        check("`social.py` 顶层 import 没动（零新依赖）",
              "import time" not in fg_src and "import requests" not in fg_src
              and "import numpy" not in fg_src, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交换会话那一拍补到抓屏腿上（v2-social-session-sync-on-foreground）")


if __name__ == "__main__":
    raise SystemExit(main())
