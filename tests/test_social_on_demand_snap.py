"""点 [好呀] 那一问的"按需抓拍"（v2-social-on-demand-snap）的自动测试。

要治的断点（真机 2026-10-08 20:09:47–20:10:28，日志原文见收尾报告）：

    点开对话框 → 点帮聊 → 「我还没读到对方说了什么，稍等」→ 干等 5~10 秒 → 再点一次

三段取证：

1. **点 [好呀] 那一刻前台是宠物自己**（用户刚点完按钮，微信被挡着），`_grab_chat`
   按当下进程名一判，一个像素都不采——不是"缓存了旧帧"、也不是"发出去没等回来"，
   是这一拍**物理上抓不到**。所以那一刻只能如实说一句"还没读到"。
2. **「前台换人了」这条信号是每 5 秒采一次样才发的**（`monitor.foreground_changed`），
   真机 20:10:18.543 还在宠物、20:10:19.047 才认到 Weixin——切回来到被认到之间
   就是那段"干等"。
3. 每一趟 OCR 2.2~5.2 秒；切回来那一拍抓到的多半还是**会话总列表**（人还没点进那个
   对话），点 [好呀] 那一问于是当场作废（`_drain_pending` 里"没读到就如实说一句、
   忙态收掉"），用户得**再点一次**才有结果。

这一版给那条链加两件事：

- **挂住那一问不再当场作废**：这一屏没读到对方的话（多半是总列表 / 还没点进去），
  就把问题挂回去接着等（跟邀请那排按钮同一个寿命 `OFFER_HANG_S`），那一句
  「还没读到」**一问只说一次**；用户点进对话、那一屏认回来，自动接上起草——
  用户那一趟就够，不必点第二次。
- **挂住期间自己看前台**（`PENDING_SNAP_MS` = 500 毫秒一拍）：社交窗一回来立刻
  抓一屏，不再等前台采样那 5 秒那一拍。抓不成（前台还是宠物自己）当场 return None，
  几乎不耗；同一屏认过了就不再烧一趟 OCR（指纹闸）。**有挂问优先**（v2-social-orphan-fix
  B-2）：轮询计时器活着时这条腿也不让位——老口径让位后那一问整个让给监听腿，而监听
  腿认完字不回头收挂问（`_on_listen_read` 不 `_drain_pending`），那一问就孤儿化了。

红线：不动"仅单聊"那道触发闸（v2-social-trigger-window-gate）；不动 16-β；
不破坏首启 / 切窗那条后台抓拍路（批次17 甲的空载保护照旧：真抓不到才说"没读到"）；
零新依赖；pet.db / mind.db 零写入；不模拟按键、不碰剪贴板。
"""

from __future__ import annotations

import ast
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
    CHAT_TEXT_FRESH_S,
    LISTEN_POLL_MS,
    NO_CHAT_CONTEXT,
    NO_PEER_LINE_HINT,
    OFFER_HANG_S,
    PENDING_SNAP_MS,
    SocialReplyService,
)
from life.screen.social_window import SENDER_OTHER, SENDER_USER, ChatRead  # noqa: E402

# 当前那个对话框里对方最后一句
PEER_LINE = "你到了跟我说一声"
# 上一场戏里的旧话
OLD_LINE = "上一场那句旧话"


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

    # 每一帧认出来的是什么（**按帧给**：这样才能钉住"读到的是当前那一框"）
    by_frame: dict[str, ChatRead] = {
        "FRAME_LIST": ChatRead(lines=[]),                                  # 会话总列表
        "FRAME_EMPTY": ChatRead(lines=[(SENDER_USER, "在吗")]),            # 真空对话
        "FRAME_CHAT": ChatRead(lines=[
            (SENDER_OTHER, OLD_LINE),
            (SENDER_USER, "好"),
            (SENDER_OTHER, PEER_LINE),
        ]),
    }
    grab = {"frame": None}
    clock = {"t": 1000.0}
    calls = {"n": 0}
    reads: list[object] = []

    def _read(array):
        calls["n"] += 1
        reads.append(array)
        return by_frame.get(array, ChatRead(lines=[]))

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
        service._last_asked = clock["t"]        # 这一段不测冒话，只测那一问
        service._page_kind = "personal"
        service._last_listen_at = 0.0
        service._snap_timer.stop()
        service._listen_timer.stop()
        grab["frame"] = None
        stub.prompts.clear()
        spoken.clear()
        candidates.clear()
        handled.clear()
        calls["n"] = 0
        reads.clear()

    stub.candidates_result = {"candidates": [
        {"draft": "快到了，你在哪个门", "why": "接住对方的行动"},
    ]}

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 点 [好呀] 那一刻抓不成：说一句、把那一问挂住 ----------

        section("① 点 [好呀] 那一刻前台是宠物自己（抓不成）→ 说一句，那一问挂住不丢")

        _reset()
        service._grab_chat = lambda: None          # 用户刚点完按钮：微信被挡着
        took = service.confirm("好呀")
        drain_threads()
        pump(0.1)

        check("认下了这活儿（不是当没听见）", took is True, str(took))
        check("★ 如实说一句「我还没读到对方说了什么，稍等」", _hints() == 1, " | ".join(spoken))
        check("★ 一条候选都没给（不拿空材料去拟）",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")
        check("★ 忙态收掉了（挂住不等于卡在「上一段我还在写」上）",
              service.busy is False, str(service.busy))
        check("★ 挂住的那一问留着（问题不丢，等切回来接着办）",
              service._pending_draft == "" and service._pending_draft_at == clock["t"],
              f"{service._pending_draft!r} / {service._pending_draft_at}")
        check("★ 按需抓拍这条腿起来了（半秒一拍，不用等前台那 5 秒一采样）",
              service._snap_timer.isActive() and service._snap_timer.interval() == PENDING_SNAP_MS,
              f"{service._snap_timer.isActive()} / {service._snap_timer.interval()}")
        check("★ 这一拍没白认字（抓不成就是抓不成，不硬抓）",
              calls["n"] == 0, f"{calls['n']} 次")

        # ---------- ② 用户切回那个对话框：不必再点第二次 ----------

        section("② 用户切回对话框（**不喊 on_foreground**）→ 半秒内自己抓到当前框末句，拟出来")

        # 前台采样那 5 秒还没到（`_social_foreground` 仍是 False），那一问还挂着——
        # 用户这时候点进了对话，画面就是这一帧。
        grab["frame"] = "FRAME_CHAT"
        service._grab_chat = lambda: grab["frame"]

        check("★ 不用再点第二次 [好呀]：这一问自己补上了",
              wait_for(lambda: len(candidates) == 1, timeout=8.0), f"{len(candidates)} 组")
        drain_threads()
        pump(0.1)

        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 认的就是**当前那一框**对方最后一句（不是空材料那格）",
              PEER_LINE in prompt and NO_CHAT_CONTEXT not in prompt,
              prompt[:80].replace("\n", " / "))
        check("★ 一次抓拍内出结果（就这一趟 OCR，不多认几遍）",
              calls["n"] == 1, f"{calls['n']} 次")
        check("★ 抓的是用户切回来那一帧（不是缓存里的旧帧）",
              reads == ["FRAME_CHAT"], repr(reads))
        check("★ 补完就把那一格清掉（这一问只补一次）",
              service._pending_draft is None and service._pending_draft_at == 0.0,
              f"{service._pending_draft!r} / {service._pending_draft_at}")
        check("★ 补上之后这条腿自己收掉了（不留着空转）",
              service._snap_timer.isActive() is False, str(service._snap_timer.isActive()))
        check("★ 全程没多说一句「还没读到」（补上了就不再说）", _hints() == 1,
              " | ".join(spoken))

        # ---------- ③ 同一屏不重复认；画面真变了才再认 ----------

        section("③ 挂住期间一拍一拍地看，但同一屏不重复烧 OCR")

        _reset()
        service._grab_chat = lambda: None
        service.confirm("好呀")
        grab["frame"] = "FRAME_LIST"                # 用户切回来了，但停在会话总列表上
        service._grab_chat = lambda: grab["frame"]

        check("★ 切回来这一屏是总列表：认了一趟，没读到对方的话",
              wait_for(lambda: calls["n"] == 1, timeout=8.0), f"{calls['n']} 次")
        drain_threads()
        pump(2.0)                                   # 好几个 500 毫秒的拍
        check("★ 同一屏（总列表）不再重复认（不白烧好几趟 OCR）",
              calls["n"] == 1, f"{calls['n']} 次")
        check("★ 那一问还挂着（总列表不算数，等他点进对话）",
              service._pending_draft == "", repr(service._pending_draft))
        check("★ 这一趟也没又说一句「还没读到」（一句只说一次）",
              _hints() <= 1, " | ".join(spoken))

        grab["frame"] = "FRAME_CHAT"                # 他点进了那个对话：画面真变了
        check("★ 画面真变了就再认：这一屏认回来就拟",
              wait_for(lambda: len(candidates) == 1, timeout=8.0), f"{len(candidates)} 组")
        drain_threads()
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 拟的是点进去那一屏的原话", PEER_LINE in prompt,
              prompt[:80].replace("\n", " / "))

        # ---------- ④ 真空对话：仍旧如实说一句，不编 ----------

        section("④ 真空对话（抓得成，这一屏就是没有对方的话）→ 说一句、挂住、到点自己收")

        _reset()
        service._grab_chat = lambda: "FRAME_EMPTY"
        service.confirm("好呀")
        check("★ 这趟抓得成，也认了字",
              wait_for(lambda: calls["n"] == 1, timeout=8.0), f"{calls['n']} 次")
        drain_threads()
        pump(0.2)

        check("★ 一个字都没读到就如实说一句（不落「刚看到消息」这类兜底占位）",
              _hints() == 1 and not any("刚看到" in s for s in spoken),
              " | ".join(spoken) or "（安静）")
        check("★ 一条候选都没给、模型一次都没调（绝不编一句）",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")
        check("★ 那一问仍旧挂着（真空对话不等于把问题丢了）",
              service._pending_draft == "", repr(service._pending_draft))

        pump(1.6)                                   # 好几个拍：别再重复刷那句
        check("★ 挂着的这几拍不再重复说同一句（同一句提示只说一次）",
              _hints() == 1, f"{_hints()} 次")
        check("★ 一直没读到就不拟（材料空到底）", candidates == [], f"{len(candidates)} 组")

        clock["t"] += OFFER_HANG_S + 5.0            # 挂过寿命
        pump(0.8)
        drain_threads()
        check("★ 到了跟邀请同一个寿命就自己收掉那一问（不留着下一趟再补）",
              service._pending_draft is None and service._pending_draft_at == 0.0,
              f"{service._pending_draft!r} / {service._pending_draft_at}")
        check("★ 到点收掉也不再多冒一句（那句点按钮的时候已经说过了）",
              _hints() == 1 and candidates == [] and stub.prompts == [],
              f"{_hints()} / {len(candidates)} 组 / {len(stub.prompts)} 条")

        # ---------- ⑤ 有挂问优先：轮询在跑时这条腿也不让位 ----------

        section("⑤ 挂问优先：轮询在跑时这条腿也不让位（v2-social-orphan-fix B-2）")

        _reset()
        service._pending_draft = ""
        service._pending_draft_at = clock["t"]
        service._snap_timer.start()
        service._listen_timer.start()               # 轮询在跑（社交窗在前台）
        seen = {"n": 0}
        real_snap = service._snap_pending
        service._snap_pending = lambda: seen.__setitem__("n", seen["n"] + 1) or True
        try:
            service._poll_pending_snap()
            check("★ 轮询在跑也不让位（挂问优先，这一拍照样抓）",
                  seen["n"] == 1, f"{seen['n']} 次")
            service._listen_timer.stop()
            service._poll_pending_snap()
            check("★ 轮询停了照样接着看",
                  seen["n"] == 2, f"{seen['n']} 次")
        finally:
            service._snap_pending = real_snap

        # ---------- ⑥ 红线 ----------

        section("⑥ 红线：单聊闸 / 冷却 / 新鲜期都没动，两库零写入")

        check("★ 这条腿的间隔登记进 __all__ 了",
              "PENDING_SNAP_MS" in smod.__all__ and PENDING_SNAP_MS == 500,
              str(PENDING_SNAP_MS))
        check("★ 跟那一问同寿的那条线没动（90 秒）", OFFER_HANG_S == 90.0, str(OFFER_HANG_S))
        check("★ 轻量轮询那一格（v2-social-offer-fast-latency 起 500）",
              LISTEN_POLL_MS == 500, str(LISTEN_POLL_MS))
        check("★ 新鲜期那道帘子没动（120 秒）", CHAT_TEXT_FRESH_S == 120.0,
              str(CHAT_TEXT_FRESH_S))
        check("★ 两道冷却常量没动（8 秒 / 20 秒）",
              (smod.LISTEN_COOLDOWN_S, smod.SOCIAL_DONE_COOLDOWN_S) == (8.0, 20.0),
              f"{smod.LISTEN_COOLDOWN_S} / {smod.SOCIAL_DONE_COOLDOWN_S}")

        gate_src = inspect.getsource(SocialReplyService.note_page_kind)
        check("★ 「仅单聊」那道闸一个字没动（还在 note_page_kind 里收口）",
              "is_personal_kind" in gate_src and "self._ask_offer(self._last_proc)" in gate_src,
              "")

        refs: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(smod))):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(a.name for a in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api", "fuzzywuzzy", "regex"}
        check("社交这一条线没多一个第三方 import", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        src = inspect.getsource(smod)
        check("没有模拟按键 / 不做 UI 自动化那几条腿",
              not any(w in src for w in ("keybd_event", "SendInput", "SetCursorPos")), "")
        check("也没碰剪贴板", "clipboard" not in src.lower(), "")

        _reset()
        service._auto_generate("")
        drain_threads()
        pump(0.1)
        check("★ 自动那条路的空材料闸没被推开（批次8 那道）",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")

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

    return summary("社交 点 [好呀] 那一问的按需抓拍（v2-social-on-demand-snap）")


if __name__ == "__main__":
    raise SystemExit(main())
