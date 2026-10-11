"""进了帮聊窗口才持续自动出候选（v2-social-helping-window）的自动测试。

改之前：只要人在单聊里、对方回了新消息，宠物就自动拟一段候选——**它没问过用户
要不要这么干**。用户点过 [好呀] 的那一场（"这人我打算跟你一起回"）当然该接着帮；
可**从没点头过**、只是自己在跟人聊天的那一场（也许正跟对象拌嘴、正跟领导回话），
宠物在旁边一句一句地自动拟，既费模型又碍事。

这一节把"自动出候选"这条腿收进**帮聊窗口**：

- **进窗口**：用户点了一次 [好呀]（`confirm`），或自己开口喊"帮我回"且过了闸
  （`handle_request`）——那一场（`_cooldown_key()`，跟冒话冷却同一套按会话分格的键）
  从此算"进过"；
- **窗口内**：对方**每来一句**都自动出候选，不用每句再点一次确认；
- **窗口外**：一条候选都不自动出（要帮忙还是得点一下 / 喊一声）；
- **退出**：点了 [不用了] 就清掉这一格；
- **无 TTL、只按窗口**：切走再切回仍算"进过"（`dismiss_offer` 那条"离场"路不清它）。

外加**缺口3**（帮聊窗口里"末尾归属"那条兜底腿会咽消息）：用户在帮聊窗口里正打字
（最近几秒有键鼠动作），对方回的那条**色卡又认不出说话人**时，空闲时长那条兜底会
把它判成"自己发的"（`kind == "self"`），而尾巴一推进，下一拍就因为"画面没变"不再
认字了——对方那句就此咽掉。现在只在帮聊窗口里补一手：这种"自认为 self、但色卡
一个答案都没给"的一拍**不推进尾巴、把画面指纹作废**，下一拍重新认一遍。
**不碰 `_last_draft_source`**，所以同一份材料不会被反复拟（去重闸照旧兜住）。

守住的既有语义：
- **危机红线不动**：`is_request` 的危机闸照旧在落戳**之前**——危机那一句不算"进过
  窗口"，`_draft_now` 那两道闸也一个字没动；
- **`_ask_offer` 的其余闸门不动**：只是"这一场已经在帮聊窗口里了"多一道早退，且
  排在冷却日志**之前**（否则每次跃迁都刷一行"冷却挡下"）；
- **`_drain_pending` 的收口语义（e64700f）不动**：新闸排在它**之后**；
- **`_awaiting` / `_running` / 时钟口径 / 白名单 / 列表免 OCR / 冒话冷却分格不动**。

测试这一节推翻的是 `test_social_auto.py` 原来钉的那个口径（"对方回消息 → 直接拟
候选，不点确认"）——那一批已按 C-1 改成"进了帮聊窗口才自动出候选"。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；不模拟按键。
"""

from __future__ import annotations

import inspect
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.screen import social as smod
from life.screen.social import (
    ASK_LINE,
    AUTO_TRIGGER_TEXT,
    SEND_IDLE_S,
    SocialReplyService,
)
from life.screen.social_window import (
    ChatRead,
    SENDER_OTHER,
    SENDER_UNKNOWN,
)


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
    offers: list[str] = []
    candidates: list[list[dict]] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)
    app.bus.social_offer.connect(offers.append)
    app.bus.social_candidates.connect(candidates.append)

    grab = {"frame": "F0"}
    rows = {"v": []}
    idle = {"sec": 10.0}
    clock = {"t": 1000.0}
    ident = {"s": "甲"}
    reads = {"n": 0}

    def _read(array) -> ChatRead:
        reads["n"] += 1
        return ChatRead(lines=list(rows["v"]))

    def _grab():
        return grab["frame"]

    service._grab_chat = _grab
    service._read_chat = _read
    service._idle_seconds = lambda: idle["sec"]
    service._clock = lambda: clock["t"]
    service._session_provider = lambda: ident["s"]
    service._examples_provider = lambda query: ""
    service._social_foreground = True
    service._last_proc = "Weixin.exe"
    # 先把「这一场是谁」认下来（不认的话会话标识还是空串，下面那笔冷却会记到空串
    # 那一格上——`_cooldown_key()` 读的却是"甲"那一格，等于没按住）。
    service._sync_session()
    # 冒话那道冷却按住（这一节验的是**候选腿**，不是 offer 腿）：不按住的话下面每次
    # "非单聊 → 单聊"的跃迁都会冒一句邀请，邀请一挂上 `_awaiting`，后面的几拍全被它挡回去。
    service._last_asked = clock["t"]
    service._last_fingerprint = None
    service._last_listen_tail = ""
    service._last_listen_kind = ""
    service._last_listen_at = 0.0
    service._running = False

    stub.candidates_result = {"candidates": [
        {"draft": "挺开心的，你呢？", "why": "寒暄回寒暄，不升级"},
    ]}

    def _reset_probe() -> None:
        spoken.clear()
        offers.clear()
        candidates.clear()
        reads["n"] = 0

    def _tick(frame_id: str, new_rows, *, idle_sec: float = 10.0) -> None:
        """驱动一拍监听，等这一拍落定（跟 `test_social_auto` 同一套手法）。"""
        wait_for(lambda: not service._listen_reading and not service._chat_reading)
        drain_threads()
        pump(0.05)
        service._running = False
        service._last_listen_at = 0.0
        grab["frame"] = frame_id
        rows["v"] = list(new_rows)
        idle["sec"] = idle_sec
        service._poll_listen()
        wait_for(lambda: not service._listen_reading)
        drain_threads()
        pump(0.1)

    def _enter_helping() -> None:
        """走真路进帮聊窗口：等于用户点了一次 [好呀]。

        抓屏那两只手**暂时摘掉**，只为让这一下不真去抓屏（没得抓，`_draft_now` 就只
        把忙态收掉）——这一节要的是**落戳那一下**，不是在数"点 [好呀] 当场拟出来的
        第一段"（那一段由 `test_social.py` 的 S5 盯着）。落戳本身走的是真路：
        `confirm("好呀")` 那个函数。
        """
        saved = (service._grab_chat, service._read_chat)
        service._grab_chat = service._read_chat = None
        service._awaiting = True
        service.confirm("好呀")
        service._grab_chat, service._read_chat = saved
        wait_for(lambda: not service.busy)
        drain_threads()
        pump(0.05)
        # 点 [好呀] 那一下可能顺手说了一句（"还没读到"），也可能真拟了一段：两样都收
        # 干净，下面的读数只算"之后对方每来一句"那几拍。
        service.reset_done()
        service._last_draft_source = ""
        service._pending_auto_source = ""
        service._last_listen_tail = ""
        service._last_listen_kind = ""
        service._last_listen_at = 0.0
        service._last_fingerprint = None
        service._awaiting = False
        _reset_probe()

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()
    src = inspect.getsource(smod)

    try:
        # ---------- ① 进窗口：两条真路都落戳，按会话分格 ----------

        section("① 进窗口：点 [好呀] / 喊「帮我回」都落戳，键是「这一场是谁」")

        check("前置：这一场还没进过帮聊窗口", service._is_helping() is False, "")
        check("前置：键就是冒话冷却那一格（同一个按会话分格的键）",
              service._cooldown_key() == "甲", repr(service._cooldown_key()))

        _enter_helping()
        check("★ 点了 [好呀] → 这一场进帮聊窗口了", service._is_helping() is True, "")
        check("★ 落戳落在**甲**那一格上", set(service._helping) == {"甲"},
              repr(sorted(service._helping)))

        service._awaiting = True
        service.confirm("不用了")
        service._awaiting = False
        check("★ 点了 [不用了] → 这一格清掉（这一场不再算「在帮聊」）",
              service._is_helping() is False, repr(sorted(service._helping)))

        # 另一条真路：用户自己开口喊「帮我回」（过了危机闸 / 词表闸之后）
        saved = (service._grab_chat, service._read_chat)
        service._grab_chat = service._read_chat = None
        shout = service.handle_request("帮我回一下，他刚问我周末去哪")
        wait_for(lambda: not service.busy)
        service._grab_chat, service._read_chat = saved
        drain_threads()
        pump(0.05)
        service.reset_done()
        service._last_draft_source = ""
        service._pending_auto_source = ""
        _reset_probe()
        check("★ 喊一声「帮我回」→ 也进帮聊窗口（这条线一直保留）",
              shout is True and service._is_helping() is True, f"接了 {shout}")

        # 按会话分格：换一场（换个人）→ 那一格是空的
        ident["s"] = "乙"
        service._sync_session()
        check("★ 换到乙那一场 → 不算「进过帮聊」（帮聊窗口按会话分格）",
              service._is_helping() is False, repr(sorted(service._helping)))
        ident["s"] = "甲"
        service._sync_session()
        check("★ 切回甲那一场 → 还算进过（无 TTL，只按窗口）",
              service._is_helping() is True, repr(sorted(service._helping)))

        # 危机那句不算「进过窗口」（红线：危机闸照旧在落戳之前）
        service._forget_helping()
        check("前置：清干净了", service._is_helping() is False, "")
        service._grab_chat = service._read_chat = None
        risky = service.handle_request("帮我回一下，可是我真的不想活了")
        service._grab_chat = _grab
        service._read_chat = _read
        check("★ 危机那一句照旧接不住（`is_request` 那道危机闸一个字没动）",
              risky is False, f"接了 {risky}")
        check("★ 而且它**不算**进过帮聊窗口（落戳排在危机闸之后）",
              service._is_helping() is False, repr(sorted(service._helping)))
        _reset_probe()

        # ---------- ② 没进窗口：对方回消息也不自动出候选（C-1 的收窄） ----------

        section("② 没进帮聊窗口：对方回消息 → 一条候选都不出")

        _tick("FA", [(SENDER_OTHER, "茗风：今天玩得开心吗")])
        check("前置：打底那一拍不出候选（第一次认到这一屏只当基线）",
              not candidates, f"{len(candidates)} 组")

        _tick("FB", [(SENDER_OTHER, "茗风：今天玩得开心吗"),
                     (SENDER_OTHER, "茗风：周末有空一起爬山吗")])
        check("★ 没进帮聊窗口 → 对方回了新的，一条候选都不出", not candidates,
              f"{len(candidates)} 组")
        check("★ 挡住它的是**新那道闸**、不是「本来就没内容」：这一拍真认了字",
              reads["n"] >= 1, f"认字 {reads['n']} 次")
        check("★ 归属那一步也真走到了「对方回了」这一档（不是被前面哪道闸拦在外面）",
              service._last_listen_kind == "incoming", repr(service._last_listen_kind))
        check("★ 也没冒「要帮忙吗」（要帮忙还是得点一下）",
              not offers and ASK_LINE not in spoken,
              f"offer {len(offers)} 次 / 说 {len(spoken)} 句")

        check("★ 源码层：新闸就是 `_auto_generate` 的**第一句**（材料 / 去重 / 冷却全在它后面）",
              bool(re.search(
                  r"def _auto_generate\(self, chat_text: str\) -> None:\s*\n"
                  r"\s*\"\"\".*?\"\"\"\s*\n"
                  r"(?:\s*#[^\n]*\n)*"
                  r"\s*if not self\._is_helping\(\):\s*\n\s*return\b",
                  src, re.S)), "")

        # ---------- ③ 进了窗口：对方每来一句都自动出候选，不用每句再点确认 ----------

        section("③ 进了帮聊窗口：对方每来一句都自动出候选（不用每句再点确认）")

        _enter_helping()
        check("前置：这一场进帮聊窗口了", service._is_helping() is True, "")

        _tick("FC", [(SENDER_OTHER, "茗风：周末有空一起爬山吗")])
        check("前置：打底那一拍不出候选（尾巴是用来比「变没变」的底）",
              not candidates, f"{len(candidates)} 组")

        # 对方接第二句：只多不少 → 完成态锁那条"有新内容就放行"照旧算数
        _tick("FD", [(SENDER_OTHER, "茗风：周末有空一起爬山吗"),
                     (SENDER_OTHER, "茗风：那就定周六上午吧")])
        check("★ 进窗口后对方来一句 → 候选直接出（不点确认）",
              wait_for(lambda: bool(candidates)), f"{len(candidates)} 组")
        first = candidates[-1][0]["draft"] if candidates else ""

        _tick("FE", [(SENDER_OTHER, "茗风：周末有空一起爬山吗"),
                     (SENDER_OTHER, "茗风：那就定周六上午吧"),
                     (SENDER_OTHER, "茗风：到时候我带茶")])
        check("★ 对方再来一句 → 还在自动出（持续跟聊这条腿是通的）",
              len(candidates) == 2, f"{len(candidates)} 组")
        check("出来的就是模型拟的那条", bool(first) and first == "挺开心的，你呢？", repr(first))
        check("喂模型的是固定那句「自动说明」+ 对方刚说的",
              any(AUTO_TRIGGER_TEXT in p for p in stub.prompts)
              and any("我带茶" in p for p in stub.prompts), "")
        check("★ 全程一句邀请都没冒（已经在帮聊窗口里了，不该再问「要不要帮你回」）",
              not offers and ASK_LINE not in spoken,
              f"offer {len(offers)} 次 / 说 {len(spoken)} 句")

        # ---------- ④ 窗口里 `_ask_offer` 早退，且排在冷却日志之前 ----------

        section("④ 窗口里不再冒那一句邀请，也不刷「冷却挡下」那行日志")

        _reset_probe()
        # 走真实的跃迁闸：先退出单聊那一档，再点进单聊
        service._awaiting = False
        service._running = False
        service._page_kind = ""
        service.note_page_kind(smod.PAGE_PERSONAL)
        check("★ 已经在帮聊窗口里 → 这一拍不冒邀请", not offers, f"{len(offers)} 次")

        body = src[src.index("def _ask_offer"):src.index("def note_page_kind")]
        check("★ 新闸排在 `_due()` / 冷却日志**之前**（帮聊里不刷「冷却挡下」）",
              body.index("if self._is_helping():") < body.index("if not self._due():"), "")
        check("★ 也排在 `_running / _awaiting` 那道闸之前（开局第一句）",
              body.index("if self._is_helping():")
              < body.index("if self._running or self._awaiting:"), "")
        check("★ 冷却日志那道仍按「这一场标识」只记长度（隐私口径没动）",
              "len(self._cooldown_key())" in body, "")

        # 反证：清掉这一格之后，同样的跃迁照旧冒一句邀请
        service._forget_helping()
        service._last_asked_by_window.clear()   # 冒话那道冷却也放开（这一节不验它）
        app.chat_bubble.hide_offer()
        _reset_probe()
        service._awaiting = False
        service._running = False
        service._page_kind = ""
        service.note_page_kind(smod.PAGE_PERSONAL)
        check("反证：清掉这一格之后，同样的跃迁照旧会冒一句邀请",
              len(offers) == 1, f"{len(offers)} 次")
        app.chat_bubble.hide_offer()
        service.dismiss_offer()
        # 这一节把冷却放开过（反证那一下），按住它再往下走：不按的话后面每一拍的
        # `note_page_kind` 跃迁都会真冒一句邀请，邀请一挂上 `_awaiting`，`_poll_listen`
        # 后面几拍就全被它挡回去了（挡轮询是既有的设计）。
        service._last_asked = clock["t"]
        service._awaiting = False
        _reset_probe()

        # ---------- ⑤ 缺口3：帮聊窗口里"打字时对方来句"不漏拍 ----------

        section("⑤ 缺口3：窗口里色卡认不出是谁、用户又在打字 → 不咽消息，下一拍再认")

        _enter_helping()
        check("前置：在帮聊窗口里", service._is_helping() is True, "")

        # 这一屏**一行都分不出说话人**（色卡整屏没给答案）：`tail_sender` 会跳过空串，
        # 只要还留着一行带得出颜色它就给得出答案——所以"认不出"这档得整屏都是 UNKNOWN。
        _tick("FF", [(SENDER_UNKNOWN, "茗风：今天玩得开心吗")])
        check("前置：打底那一拍（尾巴是用来比「变没变」的底）", not candidates,
              f"{len(candidates)} 组")

        # 摆一屏"对方刚来一条"，但色卡整屏认不出说话人，而用户正打字（空闲很短）
        # → 兜底那条腿判成 self：老口径到这儿就把尾巴推进去了，下一拍不再认字。
        _tick("FG", [(SENDER_UNKNOWN, "茗风：今天玩得开心吗"),
                     (SENDER_UNKNOWN, "茗风：那我先回去了")],
              idle_sec=SEND_IDLE_S / 5)
        check("这一拍按老口径判成「自己发的」（色卡没给答案、空闲又短）",
              service._last_listen_kind != "incoming", repr(service._last_listen_kind))
        check("★ 这一拍确实没出候选（不是我们要的结局，但它是这一拍的实情）",
              not candidates, f"{len(candidates)} 组")
        check("★ 关键是**尾巴没被推进**（没把这条消息记成「处理过了」）",
              service._last_listen_tail == "茗风：今天玩得开心吗",
              repr(service._last_listen_tail))
        check("★ 画面指纹也被作废了（下一拍哪怕画面没变，也会重新认一遍）",
              service._last_fingerprint is None, repr(service._last_fingerprint))

        # 下一拍：**同一帧**（画面一个字没变）。指纹被作废了，所以照样重新认字；
        # 这一回色卡认出来了（白气泡 = 对方）→ 该出的候选出得来。
        _tick("FG", [(SENDER_OTHER, "茗风：今天玩得开心吗"),
                     (SENDER_OTHER, "茗风：那我先回去了")],
              idle_sec=SEND_IDLE_S / 5)
        check("★ 下一拍（画面没变）照样重新认了字",
              reads["n"] >= 1, f"认字 {reads['n']} 次")
        check("★ 这一回认出来了 → 候选照出（那句没被咽掉）",
              wait_for(lambda: bool(candidates)), f"{len(candidates)} 组")

        # 反向对照：**不在**帮聊窗口里时，老口径一个字没动（尾巴照推进）
        service._forget_helping()
        service.reset_done()                    # 上一段刚交出去，完成态锁还立着
        _reset_probe()
        service._last_listen_tail = "茗风：今天玩得开心吗"
        service._last_fingerprint = None
        _tick("FH", [(SENDER_UNKNOWN, "茗风：今天玩得开心吗"),
                     (SENDER_UNKNOWN, "茗风：明天见吧")],
              idle_sec=SEND_IDLE_S / 5)
        check("★ 不在窗口里 → 老口径照旧：这一拍判 self，**尾巴照推进**去",
              service._last_listen_kind == "self"
              and service._last_listen_tail != "茗风：今天玩得开心吗",
              repr(service._last_listen_tail))

        # 也不许拿这条改动去"狂生成"：同一份材料不会被反复拟（去重闸原样兜住）
        _enter_helping()
        _tick("FI", [(SENDER_OTHER, "茗风：明天见吧")])
        _tick("FJ", [(SENDER_OTHER, "茗风：明天见吧"),
                     (SENDER_OTHER, "茗风：那就这样")])
        check("前置：这一份材料先拟一段", len(candidates) == 1, f"{len(candidates)} 组")
        _reset_probe()
        service._last_fingerprint = None
        # 画面变了（底下多一行宠物自己浮窗那串字），可**对方原话一个字没变**
        _tick("FK", [(SENDER_OTHER, "茗风：明天见吧"),
                     (SENDER_OTHER, "茗风：那就这样"),
                     (SENDER_UNKNOWN, "5134px")])
        check("★ 同一份对方原话（画面重认一遍、材料没变）→ 不重开一轮（去重闸没动）",
              not candidates, f"{len(candidates)} 组")
        check("记的还是那一份材料，没被这条改动改掉",
              service._last_draft_source == "茗风：那就这样",
              repr(service._last_draft_source))

        # ---------- ⑥ 切走再切回仍算「进过」（无 TTL） ----------

        section("⑥ 切走再切回仍算「进过」（无 TTL；离场那条路不清）")

        _enter_helping()
        app.chat_bubble.hide_offer()             # 让下面那声真走"按钮没了"那条清场支路
        service._awaiting = True
        service.dismiss_offer()                  # 按钮退休 / 走出聊天窗那一条"离场"路
        check("前置：`dismiss_offer` 真把它清了（这一声不是被「按钮还挂着」挡回来的）",
              service._awaiting is False, str(service._awaiting))
        check("★ `dismiss_offer`（离场 / 按钮退休）**不**清这一格",
              service._is_helping() is True, repr(sorted(service._helping)))

        service._social_foreground = True
        service.on_foreground("chrome.exe")      # 走出社交窗
        check("★ 走出社交窗 → 这一格还在", service._is_helping() is True, "")
        service._social_foreground = True
        service._sync_listen_timer()
        clock["t"] += 3600.0
        _reset_probe()
        _tick("FL", [(SENDER_OTHER, "茗风：在吗")])
        _tick("FM", [(SENDER_OTHER, "茗风：在吗"),
                     (SENDER_OTHER, "茗风：好久没聊了")])
        check("★ 一小时后切回来照样自动出候选（无 TTL，不是「过一阵就忘」）",
              wait_for(lambda: bool(candidates)), f"{len(candidates)} 组")

        # ---------- ⑦ 红线 ----------

        section("⑦ 红线：两库零写入、零新依赖、不模拟按键、不记名字")

        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
        check("这一格只在内存里（就是个字典，跟落盘那条线一点不沾）",
              isinstance(service._helping, dict), type(service._helping).__name__)
        check("★ 代码里没有把这一格的键写进任何日志（跟 `_session_id` 同一条隐私口径）",
              not any("_helping" in line for line in src.splitlines()
                      if "logger." in line), "")
        check("不模拟按键 / 不读剪贴板（这一条改动一个都没碰）",
              not any(w in src for w in ("SendInput", "keybd_event", "uiautomation",
                                         "pyautogui", "pynput", "win32clipboard")), "")
        check("★ 危机两道闸一个字没动（`is_request` / `_draft_now` 里那两处仍在）",
              src.count("crisis.is_crisis") >= 3, str(src.count("crisis.is_crisis")))
        check("★ 收口那条腿（`_drain_pending`）语义没动：新闸排在它之后调用的那一头",
              src.index("def _auto_generate") > src.index("def _drain_pending"), "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交帮聊窗口（v2-social-helping-window）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
