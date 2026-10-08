"""点 [好呀] 时的"当场强制即时抓拍"（批次17，裁定甲）的自动测试。

真机那条腿：用户点 [好呀] 要的是**能直接粘的一段**，而这一刻宠物手上那份"对方原话"
可能是空的——刚冷启动还没有过任何一屏、或者手上那屏字是几分钟前另一场戏里的。
老口径（批次11 起）点下去**当场就拟**：`_draft_now("")` 里那一格只在
"`_social_foreground` 为真（社交窗还在前台）"时才抓屏，人一旦点回宠物自己这扇窗
（点按钮本来就发生在这儿），采样信号已经是 False，于是**不抓、直接带着空材料去拟**，
提示词那格填的是 `NO_CHAT_CONTEXT`（"没认到对方说了什么"）——模型只能对着它瞎写一段。

裁定甲给的三个动作：

1. **先强制即时抓拍**：点 [好呀] 时手上没有能用的材料（一字没有 / 已过新鲜期），
   就不等"社交窗在前台"那个**采样信号**，当场抓一屏（`snap_now` 那个脾气：不排节流、
   当下就拍），等它认回来再起草（沿用既有的 `_pending_draft` / `_drain_pending` 那条路）；
2. **仍空才提示**：抓也抓不成、或者抓回来仍旧没有对方的话 → 如实说一句
   「我还没读到对方说了什么，稍等。」（`NO_PEER_LINE_HINT`），**不拟**，忙态收掉；
3. **不落"刚看到消息"类兜底占位**：那等于告诉用户"我看见了"，而这一拍压根没看见。

批次17 甲修正（同一份测试里钉着）：甲原来那一步是"点 [好呀] 就**当场硬抓**一屏"，
可点按钮这件事**就发生在宠物自己那扇窗上**——那一刻前台是宠物，微信被挡着，
`_grab_social_chat` 按当下进程名一判，必然一个像素都不采，抓回来必是空的。用户看到的
于是是"我还没读到对方说了什么，稍等"，而这一问**再也不会来**（忙态收了、挂起那格也丢了）。
甲修正把这一格换成：不再硬抓，把那一问**挂住**（`_pending_draft` + `_pending_draft_at`），
如实说一句就走；用户切回微信那一刻，既有的 `on_foreground` → `_read_social_screen` →
`_on_screen_read` → `_drain_pending` 这条链会把字认回来、自动接上起草（⑥ 钉的就是它）。
挂过 `OFFER_HANG_S`（跟邀请那排按钮同一个寿命）当作废、不再补（⑦ 钉）。

红线：零新依赖；pet.db / mind.db 零写入；不模拟按键、不碰剪贴板；喊"帮我回"
（`text` 非空）那条路**一个字没动**（批次8 那个"手动路材料空也照拟"的口径照旧，
④ 就是它的反向对照）；自动监听那条路（批次8 材料边界、`social.py` 那两道冷却 / 去重）
一分没动（⑥ 逐条钉）。
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
    CHAT_TEXT_FRESH_S,
    NO_CHAT_CONTEXT,
    NO_PEER_LINE_HINT,
    OFFER_HANG_S,
    SocialReplyService,
)
from life.screen.social_window import SENDER_OTHER, ChatRead  # noqa: E402

# 对方最新那条（真机那种一句白底的人话）
PEER_LINE = "那你周六到底来不来呀"
# 上一场戏里的旧话（过期那一档用；一个词都不该进提示词）
OLD_LINE = "几分钟前那一场的旧话"


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

    # 通用聊天那条兜底：**记一笔、不真跑**（真跑起来会去写 pet.db，红线）。
    handled: list[str] = []
    app.chatter_service.handle = handled.append

    grab = {"frame": "FRAME_A"}
    page = {"read": ChatRead(lines=[(SENDER_OTHER, PEER_LINE)])}
    clock = {"t": 1000.0}
    calls = {"n": 0}

    def _read(array):
        calls["n"] += 1
        return page["read"]

    service._grab_chat = lambda: grab["frame"]
    service._read_chat = _read
    service._clock = lambda: clock["t"]
    service._examples_provider = lambda query: ""

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
        service._chat_text = text
        service._chat_text_at = at
        service._last_draft_source = ""
        service._pending_auto_source = ""
        service._last_asked = 0.0
        service._last_listen_at = 0.0
        service._listen_timer.stop()
        stub.prompts.clear()
        spoken.clear()
        candidates.clear()
        handled.clear()
        calls["n"] = 0

    stub.candidates_result = {"candidates": [
        {"draft": "来，那周六见", "why": "接住对方的行动"},
    ]}

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 点了 [好呀] 却一个字都没读到：不拟，如实说一句 ----------

        section("① 点 [好呀] 而一个字都没读到 → 不拟，如实说一句（不落兜底占位）")

        _reset()
        grab["frame"] = None                  # 真抓不成（取不到帧）
        service._grab_chat = lambda: None
        took = service.confirm("好呀")
        drain_threads()
        pump(0.1)

        check("认下了这活儿（不是当没听见）", took is True, str(took))
        check("★ 一条候选都没给（不拿空材料去拟）",
              candidates == [], f"{len(candidates)} 组")
        check("★ 模型一次都没调（不白花一次 POST）",
              stub.prompts == [], f"{len(stub.prompts)} 条")
        check("★ 如实说一句「我还没读到对方说了什么，稍等」",
              NO_PEER_LINE_HINT in spoken, " | ".join(spoken) or "（安静）")
        check("★ 不落「刚看到消息」这类兜底占位（那等于说「我看见了」）",
              not any("刚看到" in s for s in spoken), " | ".join(spoken) or "（安静）")
        check("★ 忙态收掉了（不收掉这条线从此卡在「上一段我还在写」上）",
              service.busy is False, str(service.busy))
        check("★ 挂起那一格**留着**（甲修正：挂着等切回来，不是丢掉）",
              service._pending_draft == "" and service._pending_draft_at == clock["t"],
              f"{service._pending_draft!r} / {service._pending_draft_at}")
        check("★ 也没真抓那一屏字（被宠物自己的窗挡着，抓也白抓）",
              calls["n"] == 0, f"{calls['n']} 次")
        check("没落到通用聊天兜底", handled == [], repr(handled))

        # ---------- ② 手上没字、当场抓得成：不等前台信号，强制即时抓一屏 ----------

        section("② 手上没字：不等「社交窗在前台」那个采样信号，当场抓一屏再拟")

        _reset()
        grab["frame"] = "FRAME_B"
        page["read"] = ChatRead(lines=[(SENDER_OTHER, PEER_LINE)])
        service._grab_chat = lambda: grab["frame"]
        service._social_foreground = False     # ← 采样信号说"不在前台"，也要抓
        took = service.confirm("好呀")

        check("认下了这活儿", took is True, str(took))
        check("这一拍还没发提示词（在等认字）", stub.prompts == [], f"{len(stub.prompts)} 条")
        check("★ 也没先说那句「还没读到」（这一趟抓得成）",
              NO_PEER_LINE_HINT not in spoken, " | ".join(spoken) or "（安静）")

        check("★ 认回来带着真原话拟出来了",
              wait_for(lambda: len(candidates) == 1), f"{len(candidates)} 组")
        drain_threads()
        pump(0.1)
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 提示词里是对方的原话（不是「没认到对方说了什么」那格）",
              PEER_LINE in prompt and NO_CHAT_CONTEXT not in prompt,
              prompt[:80].replace("\n", " / "))
        check("★ 就抓了这一屏（不多认一遍字）", calls["n"] == 1, f"{calls['n']} 次")
        check("★ 强抓那一趟也过同一道去重闸（喂模型那一份记下来了）",
              service._last_draft_source == PEER_LINE, repr(service._last_draft_source))

        # ---------- ③ 手上那屏字过期了：照样当场重抓，不拿旧字充数 ----------

        section("③ 手上那屏字过了新鲜期：照样当场重抓一屏（不拿旧字充数）")

        _reset(text=OLD_LINE, at=clock["t"] - (CHAT_TEXT_FRESH_S + 30.0))
        grab["frame"] = "FRAME_C"
        page["read"] = ChatRead(lines=[(SENDER_OTHER, PEER_LINE)])
        service._grab_chat = lambda: grab["frame"]
        service.confirm("好呀")

        check("★ 当场重抓、带着这一屏的原话拟出来",
              wait_for(lambda: len(candidates) == 1), f"{len(candidates)} 组")
        drain_threads()
        pump(0.1)
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 过期那屏字一个字都没进提示词（不拿上一场的旧话当「刚发来的」）",
              OLD_LINE not in prompt and "旧话" not in prompt,
              prompt[:80].replace("\n", " / "))
        check("★ 提示词里是当场重抓那一屏的原话", PEER_LINE in prompt, "")
        check("★ 也没说那句「还没读到」（抓得成就不说）",
              NO_PEER_LINE_HINT not in spoken, " | ".join(spoken) or "（安静）")
        check("当场就抓这一次", calls["n"] == 1, f"{calls['n']} 次")

        # ---------- ④ 反向对照：喊「帮我回」那条路一个字没动 ----------

        section("④ 反向对照：喊「帮我回」（text 非空）而材料空 → 照旧当场拟（批次8 口径）")

        _reset()
        service._grab_chat = lambda: None
        took = service.handle_request("帮我回一下")
        check("认下了这活儿", took is True, str(took))
        check("★ 材料空也照拟（用户明着要的那条路没被批次17 碰到）",
              wait_for(lambda: len(candidates) == 1), f"{len(candidates)} 组")
        drain_threads()
        pump(0.1)
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 提示词那格如实写「没认到」（老口径不变）",
              NO_CHAT_CONTEXT in prompt, prompt[:80].replace("\n", " / "))
        check("★ 没说那句「还没读到」（那是点 [好呀] 才有的口径）",
              NO_PEER_LINE_HINT not in spoken, " | ".join(spoken) or "（安静）")

        # ---------- ⑤ 反向对照：手上有新鲜材料 → 一个字都不多说 ----------

        section("⑤ 反向对照：点 [好呀] 而手上就有新鲜的字 → 不另抓、照旧当场拟")

        _reset(text=PEER_LINE, at=clock["t"])
        service._grab_chat = lambda: None
        took = service.confirm("好呀")
        check("认下了这活儿", took is True, str(took))
        check("★ 手上那屏字还能用 → 不再另抓一屏（不白多一次 OCR）",
              calls["n"] == 0, f"{calls['n']} 次")
        check("★ 照旧当场拟出来（这条主路没被掐死）",
              wait_for(lambda: len(candidates) == 1), f"{len(candidates)} 组")
        drain_threads()
        pump(0.1)
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 提示词里就是手上那一份材料",
              PEER_LINE in prompt and NO_CHAT_CONTEXT not in prompt,
              prompt[:80].replace("\n", " / "))
        check("★ 也没说那句「还没读到」",
              NO_PEER_LINE_HINT not in spoken, " | ".join(spoken) or "（安静）")

        # ---------- ⑥ 甲修正：挂住的那一问，切回微信由既有抓拍路补上 ----------

        section("⑥ 点 [好呀] 抓不成 → 挂住；切回微信，既有抓拍路把这一问补上（甲修正）")

        _reset()
        grab["frame"] = "FRAME_D"
        page["read"] = ChatRead(lines=[(SENDER_OTHER, PEER_LINE)])
        service._grab_chat = lambda: None        # 点按钮那一刻：前台是宠物自己，抓不成
        took = service.confirm("好呀")
        drain_threads()
        pump(0.1)

        check("认下了这活儿", took is True, str(took))
        check("★ 结果还是空的，但这一问不丢：挂起那格留着、记着挂上的时刻",
              service._pending_draft == "" and service._pending_draft_at == clock["t"],
              f"{service._pending_draft!r} / {service._pending_draft_at}")
        check("★ 忙态收掉了（挂住不等于卡在「上一段我还在写」上）",
              service.busy is False, str(service.busy))
        check("★ 这一拍不拟（材料还是空的）",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")
        check("★ 如实说一句「我还没读到对方说了什么，稍等」",
              NO_PEER_LINE_HINT in spoken, " | ".join(spoken) or "（安静）")
        check("★ 没抓那一屏字（被宠物自己的窗挡着，硬抓只会抓回空的一屏）",
              calls["n"] == 0, f"{calls['n']} 次")

        # 用户切回微信：装配层那条前台信号进来（`on_foreground`），既有抓拍路认字 → 补上起草
        service._last_asked = clock["t"]         # 这一段只测"补那一问"，不测冒话
        service._grab_chat = lambda: grab["frame"]
        service.on_foreground("wechat.exe")

        check("★ 挂住的那一问补上了：带着对方的原话拟出来",
              wait_for(lambda: len(candidates) == 1), f"{len(candidates)} 组")
        drain_threads()
        pump(0.1)
        check("★ 切回微信就把这一屏认回来了（走的是既有那条抓拍路、只认这一屏）",
              calls["n"] == 1, f"{calls['n']} 次")
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 提示词里是对方的原话（不是「没认到对方说了什么」那格）",
              PEER_LINE in prompt and NO_CHAT_CONTEXT not in prompt,
              prompt[:80].replace("\n", " / "))
        check("★ 补完就把那一格清掉（判重：这一问只补一次）",
              service._pending_draft is None, repr(service._pending_draft))

        # 再认一屏也不该又补一遍
        service.on_foreground("wechat.exe")
        drain_threads()
        pump(0.1)
        check("★ 再认一屏也不会补第二遍（同一问只补一次）",
              len(candidates) == 1, f"{len(candidates)} 组")

        # ---------- ⑦ 挂过寿命的那一问：过期不补 ----------

        section("⑦ 挂过 OFFER_HANG_S 才等到字 → 当过期，不补那一问")

        _reset()
        service._pending_draft = ""
        service._pending_draft_at = clock["t"] - (OFFER_HANG_S + 5.0)
        service._chat_text = PEER_LINE
        service._chat_text_at = clock["t"]
        service._drain_pending()
        drain_threads()
        pump(0.1)

        check("★ 过期那一问不拟（人早聊到别处去了，突然冒一段更糟）",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")
        check("★ 也没又冒一句「还没读到」（那句点按钮的时候已经说过了）",
              NO_PEER_LINE_HINT not in spoken, " | ".join(spoken) or "（安静）")
        check("挂起那一格清掉了（不留着下次再补）",
              service._pending_draft is None and service._pending_draft_at == 0.0,
              f"{service._pending_draft!r} / {service._pending_draft_at}")

        # ---------- ⑧ 红线 ----------

        section("⑧ 红线：自动那条闸没动 / 零新依赖 / 两库零写入")

        _reset()
        service._auto_generate("")              # 批次8 那一档：空材料
        drain_threads()
        pump(0.1)
        check("★ 自动路空材料照旧一个字都不拟（批次8 那道闸没被推开）",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")
        check("★ 自动路也不说那句「还没读到」（那是点 [好呀] 的口径）",
              NO_PEER_LINE_HINT not in spoken, " | ".join(spoken) or "（安静）")

        auto_src = inspect.getsource(SocialReplyService._auto_generate)
        check("★ 自动那条闸的源码一个字没动（`if not source` + 批次8 那条注释都在）",
              "if not source:" in auto_src and "批次8" in auto_src, "")
        check("★ 两道冷却常量一分没动（8 秒 / 20 秒）",
              (smod.LISTEN_COOLDOWN_S, smod.SOCIAL_DONE_COOLDOWN_S) == (8.0, 20.0),
              f"{smod.LISTEN_COOLDOWN_S} / {smod.SOCIAL_DONE_COOLDOWN_S}")
        check("★ 新鲜期那道帘子也没动（120 秒）", CHAT_TEXT_FRESH_S == 120.0,
              str(CHAT_TEXT_FRESH_S))
        check("★ 新那条提示语登记进 __all__ 了（名字在里面，导得出去）",
              "NO_PEER_LINE_HINT" in smod.__all__
              and getattr(smod, "NO_PEER_LINE_HINT", None) == NO_PEER_LINE_HINT, "")

        import ast

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
              not any(word in src for word in ("keybd_event", "SendInput", "SetCursorPos")),
              "")
        check("也没碰剪贴板", "clipboard" not in src.lower(), "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交 [好呀] 抓不成挂住、切回来补上（批次17 / 甲修正）")


if __name__ == "__main__":
    raise SystemExit(main())
