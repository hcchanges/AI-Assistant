"""冷启动假 offer 与孤儿化修复（v2-social-orphan-fix）的自动测试。

两条真机毛病，一在"启动那一刻"，一在"点下去之后那一问"：

**B-1 · 冷启动假 offer。** 真机 22:35:51 冷启动那一拍：焦点是 explorer（桌面上），
桌面另有一扇可见的微信窗。老口径 `_prime_social_foreground` 把那个可见进程名直接冒充
前台喂给 `on_foreground`——它抓不到帧（可见 ≠ 前台），就撞上 `on_foreground` 里那道
"等不到认字就默认放行"的空 offer 闸，对着一个**根本不是前台**的微信冒一句空 offer，
还把冷却哨兵（`_last_asked`）给烧了。修法：可见窗口只把社交这条线置成"可抓屏态"
（`note_visible_social`：立 `_social_foreground`、醒监听），不抓屏、不冒 offer；
`on_foreground` 里那道默认放行闸再补一道"进程名在白名单里才放"（双保险）。

**B-2 · 孤儿化。** 点 [好呀] 那一刻前台是宠物自己，`_grab_chat` 抓不到微信那块像素，
这一问挂住等切回微信。老口径两条腿都漏了它：① `_poll_pending_snap` 一看自动轮询计时器
活着就 `return` 让位（`if self._listen_timer.isActive(): return`），挂问整个让给监听腿；
② `_on_listen_read` 认完字又不回头收挂问（不 `_drain_pending`）——于是那一问干挂到
`OFFER_HANG_S`（90 秒）过期，用户切回微信也等不来拟文。修法（双保险）：① 挂问优先，
`_poll_pending_snap` 不再让位；② `_on_listen_read` 拿到屏后也收口一次 `_drain_pending`，
续上挂住的那一问。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；不动白名单
（bec38e4/a6017d3/afa0ad8）那几处已修语义；不动 `_running` / `_awaiting` 状态机与时钟。
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

from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    NO_CHAT_CONTEXT,
    NO_PEER_LINE_HINT,
    OFFER_HANG_S,
    PENDING_SNAP_MS,
    SocialReplyService,
)
from life.screen.social_window import SENDER_OTHER, SENDER_USER, ChatRead  # noqa: E402

PEER_LINE = "你到了跟我说一声"
OLD_LINE = "上一场那句旧话"
DRAFT = "快到了，你在哪个门"
BECAUSE = "接住对方的行动"


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    app.config.screen.social_cooldown_min = 30

    spoken: list[str] = []
    candidates: list[list[dict]] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)
    app.bus.social_candidates.connect(candidates.append)

    handled: list[str] = []
    app.chatter_service.handle = handled.append

    # 单聊那一帧：对方末尾一句是 PEER_LINE
    peer_read = ChatRead(lines=[
        (SENDER_OTHER, OLD_LINE),
        (SENDER_USER, "好"),
        (SENDER_OTHER, PEER_LINE),
    ], raw=" ".join([OLD_LINE, "好", PEER_LINE]), page_kind="personal")

    grab = {"frame": None}
    clock = {"t": 1000.0}

    def _read(array):
        return peer_read if array == "FRAME_CHAT" else ChatRead(lines=[])

    service._read_chat = _read
    service._clock = lambda: clock["t"]
    service._examples_provider = lambda query: ""

    def _hints() -> int:
        return sum(1 for s in spoken if s == NO_PEER_LINE_HINT)

    def _reset(*, text: str = "", at: float = 0.0) -> None:
        """回到"手上就这些字、什么都没在跑"的那一刻。"""
        service._social_foreground = False
        service._running = False
        service._awaiting = False
        service._card_awaiting = False
        service._chat_reading = False
        service._listen_reading = False
        service._pending_draft = None
        service._pending_draft_at = 0.0
        service._pending_hinted = False
        service._snap_seen_fp = None
        service._chat_text = text
        service._chat_text_at = at
        service._last_draft_source = ""
        service._pending_auto_source = ""
        service._last_asked = None          # 出厂态：冷却哨兵还没被烧过
        service._last_proc = ""
        service._page_kind = ""
        service._last_listen_at = 0.0
        service._last_listen_tail = ""
        service._last_fingerprint = None
        service._snap_timer.stop()
        service._listen_timer.stop()
        service.reset_done()
        grab["frame"] = None
        stub.prompts.clear()
        spoken.clear()
        candidates.clear()
        handled.clear()

    stub.candidates_result = {"candidates": [{"draft": DRAFT, "why": BECAUSE}]}

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① B-1 冷启动假 offer ----------

        section("① B-1 冷启动：可见微信不冒充前台，不冒空 offer、不浪费冷却")

        real_now = app.screen_monitor.foreground_now
        real_visible = app.screen_monitor.visible_procs
        app.screen_monitor.foreground_now = lambda: "explorer.exe"
        app.screen_monitor.visible_procs = lambda: ["explorer.exe", "Weixin.exe"]

        _reset()
        grabs = {"n": 0}
        real_grab = service._grab_chat
        service._grab_chat = lambda: (grabs.__setitem__("n", grabs["n"] + 1), None)[1]

        app._prime_social_foreground()

        check("★ 桌面上有可见微信 → 社交这条线置成可抓屏态（`_social_foreground` 真）",
              service._social_foreground is True, str(service._social_foreground))
        check("★ 这条线知道是谁（`_last_proc` 记下 Weixin，等下一次换前台纠正）",
              service._last_proc.lower() == "weixin.exe", repr(service._last_proc))
        check("★ 没冒充前台：这一拍没抓屏（不像老口径先走 on_foreground 那一套）",
              grabs["n"] == 0, f"{grabs['n']} 次")
        check("★ 没冒空 offer（可见 ≠ 前台，不许对着它冒一句）",
              service.awaiting_confirm is False and app.chat_bubble.offer_theme == "",
              f"awaiting={service.awaiting_confirm} / theme={app.chat_bubble.offer_theme!r}")
        check("★ 冷却哨兵没被浪费（`_last_asked` 仍是出厂 None，不占下一次冷却）",
              service._last_asked is None, repr(service._last_asked))

        service._grab_chat = real_grab
        app.screen_monitor.foreground_now = real_now
        app.screen_monitor.visible_procs = real_visible

        # ---------- ② B-2 孤儿化 ----------

        section("② B-2 点 [好呀] 抓不到 → 挂起 → 切回微信 → 那一问被 `_drain_pending` 续上")

        # ②-a 监听腿收口：`_on_listen_read` 拿到屏后回头收挂住的那一问
        _reset()
        service._grab_chat = lambda: None        # 点 [好呀] 那一刻前台是宠物自己
        took = service.confirm("好呀")
        drain_threads()
        pump(0.1)

        check("认下了这活儿（不是当没听见）", took is True, str(took))
        check("★ 如实说一句「我还没读到对方说了什么，稍等」", _hints() == 1, " | ".join(spoken))
        check("★ 一条候选都没给（不拿空材料去拟）",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")
        check("★ 那一问挂住没丢（`_pending_draft` 留着）",
              service._pending_draft == "" and service._pending_draft_at == clock["t"],
              f"{service._pending_draft!r} / {service._pending_draft_at}")

        # 切回微信：监听腿认到单聊那一屏，回头把挂问收口
        service._social_foreground = True
        service._listen_reading = False
        service._on_listen_read(peer_read)

        check("★ 监听腿认完这一屏 → 那一问被 `_drain_pending` 续上（90s 内拟出来、不丢）",
              wait_for(lambda: len(candidates) == 1, timeout=8.0), f"{len(candidates)} 组")
        drain_threads()
        pump(0.1)

        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 拟的是切回来那一帧的对方原话（不是空材料那格）",
              PEER_LINE in prompt and NO_CHAT_CONTEXT not in prompt,
              prompt[:80].replace("\n", " / "))
        check("★ 那一问补完就清掉（这一问只补一次）",
              service._pending_draft is None and service._pending_draft_at == 0.0,
              f"{service._pending_draft!r} / {service._pending_draft_at}")
        check("★ 没再多冒一句「还没读到」（续上就不再说）", _hints() == 1,
              " | ".join(spoken))

        # ②-b 端到端：挂起 → 切回微信（自动轮询计时器活着）→ 90s 内不孤儿化
        _reset()
        service._grab_chat = lambda: None
        service.confirm("好呀")
        drain_threads()
        pump(0.1)
        check("前置：又挂住一问", service._pending_draft == "", repr(service._pending_draft))

        # 切回微信：画面回来了，而且自动轮询计时器醒着（正是老口径让位、孤儿化的那一种）
        service._social_foreground = True
        service._sync_listen_timer()
        check("前置：自动轮询计时器醒着（前台微信）",
              service._listen_timer.isActive(), str(service._listen_timer.isActive()))
        grab["frame"] = "FRAME_CHAT"
        service._grab_chat = lambda: grab["frame"]

        check("★ 轮询在跑也不让位：切回微信后那一问被续上（90s 内拟出来、不丢）",
              wait_for(lambda: len(candidates) == 1 and service._pending_draft is None,
                       timeout=8.0),
              f"{len(candidates)} 组 / {service._pending_draft!r}")
        drain_threads()
        pump(0.1)
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 拟的还是对方那一句原话", PEER_LINE in prompt,
              prompt[:80].replace("\n", " / "))
        check("★ 按需抓拍这条腿也收掉了（不留着空转）",
              service._snap_timer.isActive() is False, str(service._snap_timer.isActive()))

        # ---------- ③ 红线（源码层） ----------

        section("③ 红线：白名单 / 状态机 / 时钟 / 两库零写入")

        social_src = inspect.getsource(smod)
        prime_src = inspect.getsource(type(app)._prime_social_foreground)
        poll_src = inspect.getsource(smod.SocialReplyService._poll_pending_snap)
        listen_src = inspect.getsource(smod.SocialReplyService._on_listen_read)

        check("B-1：可见窗口只置可抓屏态，`_prime_social_foreground` 走 `note_visible_social`",
              "note_visible_social" in prime_src and "proc = visible" not in prime_src, "")
        check("B-1：默认放行闸补了白名单（`is_allowed_social_proc` 才放）",
              "is_allowed_social_proc(self._last_proc)" in social_src, "")
        check("B-2①：`_poll_pending_snap` 不再让位给监听腿（那行 return 没了）",
              "if self._chat_reading or self._listen_reading:\n            return\n"
              "        self._snap_pending()" in poll_src, "")
        check("B-2②：`_on_listen_read` 认完字也收口一次 `_drain_pending`",
              "_drain_pending()" in listen_src, "")

        for name, src in (("note_visible_social", inspect.getsource(smod.SocialReplyService.note_visible_social)),
                          ("_poll_pending_snap", poll_src),
                          ("_on_listen_read", listen_src)):
            for tok in ("_awaiting =", "monotonic", "time.", "SOCIAL_ALLOWED_PROCS ="):
                check(f"{name} 不碰 `{tok}`（状态机 / 时钟 / 白名单定义没动）",
                      tok not in src, "")

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

    return summary("冷启动假 offer 与孤儿化修复（v2-social-orphan-fix）")


if __name__ == "__main__":
    raise SystemExit(main())
