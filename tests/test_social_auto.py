"""
社交自动监听（v2-social-auto）的自动测试。

社交帮回从「手动点帮回」升级成「对方回消息刹那自动识别 → 生成话术 → 弹出候选」，
不再每次点「要帮忙吗」。三条对应用户任务书 S4：

- ① 自动监听：社交软件在前台、对方回了新消息，自动认出来、**直接拟候选**（不先冒
  「要帮忙吗」那扇邀请）；
- ② 平时不刷：画面没变就一次 OCR、一次模型都不耗（只比指纹，变了才认字）；
- ③ 认到不谎报：末尾归属——对方回的正常拟；用户自己发的，静默不插嘴（不拟候选、
  不弹任何提示）。

- ④ 拟完就收工：候选交到用户手上那一刻，服务把"这一屏"锁上（v2-social-copy-clean）——
  用户去粘贴时聊天窗难免重绘，不锁的话那一抖又被当成"来了新消息"，真机上就是"复制完
  卡死"。解锁要两个条件一起满足：过了短期冷却（20 秒）**并且**画面真换了内容。

红线：pet.db / mind.db 零写入；零新依赖（只用 PySide6 既有的 QTimer 与标准库 hashlib）；
不模拟按键 / 不做 UI 自动化。剪贴板那条（v2-social-copy-clean）在装配层，**只写一次、
从不读**，服务自己仍然一个字都没碰。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.screen import monitor as mon
from life.screen import social as smod
from life.screen.social import (
    ASK_LINE,
    LISTEN_COOLDOWN_S,
    LISTEN_POLL_MS,
    OFFER_HANG_S,
    SOCIAL_DONE_COOLDOWN_S,
    SocialReplyService,
)
from life.screen.social_window import ChatRead, SENDER_OTHER, SENDER_UNKNOWN


class Probe:
    """接住「宠物说了什么」「候选给了什么」。"""

    def __init__(self, app) -> None:
        self.spoken: list[str] = []
        self.offers: list[str] = []
        self.candidates: list[list[dict]] = []
        # 两条都要听：自动起草里的「我琢磨一下」是回用户的话（pet_speak），
        # 邀请那类主动线是 pet_speak_proactive——听漏一条断言就会假绿。
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.pet_speak_proactive.connect(self.spoken.append)
        # 「社交那一问冒没冒」现在只认这条（v2-social-chat-ui-fix2 提交D：服务不再
        # `pet_speak_proactive.emit(ASK_LINE)` 了）——下面那些"没冒邀请"的断言要是还盯着
        # `spoken`，会变成**永远为真**的空断言，所以这里补一条真有东西的信号。
        app.bus.social_offer.connect(self.offers.append)
        app.bus.social_candidates.connect(self.candidates.append)

    def reset(self) -> None:
        self.spoken.clear()
        self.offers.clear()
        self.candidates.clear()


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    # 这里原来要把模态候选框换成记录员（离屏没人关 `exec()`，必卡死）。候选框整个删掉了
    # （v2-social-copy-clean），候选走非模态气泡 + 静默复制，不再有任何会阻塞的东西，
    # 于是那段换装也不用要了。装配层那半（气泡里写了什么、剪贴板里是什么）在
    # `test_social.py` 的 S5 里验，这一节只管服务这条线的判定。
    service: SocialReplyService = app.social_reply
    probe = Probe(app)
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True

    # 可控的假帧 / 假字 / 假空闲（画面变了用 frame 换，字变了用 text 换）
    grab = {"frame": "FRAME_A"}
    ocr = {"text": ""}
    idle = {"sec": 10.0}
    reader_calls = {"n": 0}

    def _read(array) -> str:
        reader_calls["n"] += 1
        return ocr["text"]

    service._grab_chat = lambda: grab["frame"]
    service._read_chat = _read
    service._idle_seconds = lambda: idle["sec"]
    service._examples_provider = lambda query: ""
    # 可控时钟（v2-social-copy-clean）：完成态锁那段短期冷却按真时间走的话，这一节得
    # 干等 20 秒才验得到"解锁"。时间也钉住：往前推多少，就当过了多少秒。
    clock = {"t": 1000.0}
    service._clock = lambda: clock["t"]
    service._social_foreground = True
    service._last_fingerprint = None
    service._last_listen_tail = ""
    service._last_listen_kind = ""
    service._last_listen_at = 0.0
    service._running = False

    stub.candidates_result = {"candidates": [
        {"draft": "挺开心的，你呢？", "why": "寒暄回寒暄，不升级"},
    ]}

    # ---------- ① 自动监听：对方回消息 → 直接拟候选，不点「要帮忙吗」 ----------

    section("① 对方回消息 → 直接拟候选，不点「要帮忙吗」")

    check("自动监听开关默认开（config 里 social_auto_listen）",
          app.config.screen.social_auto_listen is True, "")
    check("轮询间隔钉在 1.5 秒（节奏由对方定，不抢不刷屏）",
          service._listen_timer.interval() == LISTEN_POLL_MS == 1500,
          f"{service._listen_timer.interval()} / {LISTEN_POLL_MS}")
    check("指纹签名稳定：同一帧同一签名、不同帧不同签名",
          service._fingerprint("FRAME_A") == service._fingerprint("FRAME_A")
          and service._fingerprint("FRAME_A") != service._fingerprint("FRAME_B"), "")

    # 起停那个计时器：不在社交前台 / 关掉开关都不该空转
    service._social_foreground = False
    service._sync_listen_timer()
    check("不在社交前台 → 计时器不跑", not service._listen_timer.isActive(), "")
    service._social_foreground = True
    service._sync_listen_timer()
    check("社交前台 + 自动监听开 → 计时器跑起来了", service._listen_timer.isActive(), "")
    app.config.screen.social_auto_listen = False
    service._sync_listen_timer()
    check("关掉自动监听 → 计时器停下", not service._listen_timer.isActive(), "")
    app.config.screen.social_auto_listen = True
    service._social_foreground = False
    service._sync_listen_timer()
    check("收尾：计时器回到不跑（后面手动驱动，别空转）",
          not service._listen_timer.isActive(), "")
    service._social_foreground = True

    # 先打一屏底：第一次认到这一屏只当基线，不算"来了新消息"
    ocr["text"] = "茗风：今天玩得开心吗"
    service._poll_listen()
    wait_for(lambda: not service._listen_reading)
    drain_threads()
    pump(0.05)
    check("第一拍只打底（还没来新消息就不拟）", not probe.candidates,
          f"{len(probe.candidates)} 组")
    check("打底拍认了一屏字（后台 OCR 真跑了一回）", reader_calls["n"] >= 1,
          f"{reader_calls['n']} 次")

    # 对方回新消息：画面变了 + 字变了 + 空闲（没在敲键盘）→ 直接拟
    probe.reset()
    reader_calls["n"] = 0
    stub.prompts.clear()
    idle["sec"] = 10.0
    grab["frame"] = "FRAME_B"           # 画面变了 → 指纹翻盘
    ocr["text"] = "茗风：今天玩得开心吗 茗风：周末有空一起爬山吗"
    service._poll_listen()
    wait_for(lambda: bool(probe.candidates))
    drain_threads()
    pump(0.1)
    check("对方回消息 → 候选直接出来了（不点确认）", len(probe.candidates) == 1,
          f"{len(probe.candidates)} 组")
    check("出来的就是模型拟的那条", bool(probe.candidates)
          and probe.candidates[0][0]["draft"] == "挺开心的，你呢？",
          str(probe.candidates[0][:1] if probe.candidates else "（空）"))
    check("没冒「要帮忙吗」那句邀请（跳确认）",
          not probe.offers and smod.ASK_LINE not in probe.spoken,
          f"offer {len(probe.offers)} 次 / 说 {len(probe.spoken)} 句")
    # 批次11：`GIVING_LINE`（"我琢磨一下怎么说合适…"）整条删了——这条路上宠物本来就
    # 是"看着屏幕自己决定的"，再补一句口头预报是多余的噪音。判据跟着翻过来。
    check("不再先应一句预报（批次11 删了「我琢磨一下」）",
          not any("琢磨" in s for s in probe.spoken), " | ".join(probe.spoken))
    check("提示词里带上了对方最新那句（不脑补）",
          any("周末有空一起爬山吗" in p for p in stub.prompts), "")
    check("自动起草的「用户话」那一格是固定的自动说明",
          any(smod.AUTO_TRIGGER_TEXT in p for p in stub.prompts), "")
    check("忙态清干净（下一段还能接）", service.busy is False, "")

    # ---------- ② 平时不刷：画面没变 → 不 OCR、不拟 ----------

    section("② 平时不刷：画面没变就一次 OCR 一次模型都不耗")

    service._running = False
    probe.reset()
    reader_calls["n"] = 0
    idle["sec"] = 10.0
    # 画面和上一拍一样（还是 FRAME_B）→ 指纹没翻，连 OCR 都不该发生
    for _ in range(3):
        service._poll_listen()
    drain_threads()
    pump(0.05)
    check("画面没变 → 一次 OCR 都不跑", reader_calls["n"] == 0,
          f"{reader_calls['n']} 次")
    check("画面没变 → 一条候选也不拟", not probe.candidates,
          f"{len(probe.candidates)} 组")
    check("画面没变 → 一句也不说", not probe.spoken, " | ".join(probe.spoken) or "（安静）")

    # ---------- ③ 末尾归属：用户自己发 → 不拟、静默不插嘴 ----------

    section("③ 末尾归属：用户自己刚发 → 不拟、一句都不说")

    # ① 那一拍给出候选时，服务把"这一段的完成态"锁上了（v2-social-copy-clean）。这一节
    # 验的是**另一次社交**，所以先把时钟推过那段短期冷却，再往下走。
    clock["t"] += SOCIAL_DONE_COOLDOWN_S + 1
    service._running = False
    probe.reset()
    reader_calls["n"] = 0
    idle["sec"] = 0.5            # 最近半秒有键盘动作 = 用户自己在发
    grab["frame"] = "FRAME_C"    # 画面变了
    ocr["text"] = "茗风：周末有空一起爬山吗 我：有空啊，就周末吧"
    service._poll_listen()
    wait_for(lambda: not service._listen_reading)
    drain_threads()
    pump(0.1)
    check("用户自己发 → 一条候选都不拟", not probe.candidates,
          f"{len(probe.candidates)} 组")
    check("自己发 → 一句都不说（静默不插嘴，不弹「我先不插嘴」那种提示）",
          not probe.spoken, " | ".join(probe.spoken))
    check("自己发时也没冒「要帮忙吗」",
          not probe.offers and smod.ASK_LINE not in probe.spoken,
          f"offer {len(probe.offers)} 次 / 说 {len(probe.spoken)} 句")

    # 反向对照：同样的尾巴再长一截、但空闲（= 对方回的）→ 该拟
    service._running = False
    service._last_listen_at = 0.0      # 冷却从零起算，别挡这一拍
    probe.reset()
    idle["sec"] = 10.0
    grab["frame"] = "FRAME_D"
    ocr["text"] = "茗风：周末有空一起爬山吗 我：有空啊，就周末吧 茗风：好的，那周六见"
    service._poll_listen()
    wait_for(lambda: bool(probe.candidates))
    drain_threads()
    pump(0.1)
    check("同样的新字、只是空闲了（=对方回的）→ 正常拟",
          len(probe.candidates) == 1, f"{len(probe.candidates)} 组")

    # 连续自己发两次，同样一句都不说（既不是"只提一次"，是一次都不提）
    # （上一拍又给过候选 → 完成态锁立着，同样得先当成"过了冷却的另一次社交"）
    clock["t"] += SOCIAL_DONE_COOLDOWN_S + 1
    service._running = False
    probe.reset()
    idle["sec"] = 0.5
    grab["frame"] = "FRAME_E"
    ocr["text"] = "我：有空啊，就周末吧 我：就定周六上午吧"
    service._poll_listen()
    wait_for(lambda: not service._listen_reading)
    drain_threads()
    pump(0.05)
    grab["frame"] = "FRAME_F"
    ocr["text"] = "我：就定周六上午吧 我：好，到时候见"
    service._poll_listen()
    wait_for(lambda: not service._listen_reading)
    drain_threads()
    pump(0.05)
    check("连续自己发，一句都不说（不刷屏、也不弹提示）",
          not probe.spoken, " | ".join(probe.spoken))

    # ---------- ④ 完成态锁：拟完就收工（v2-social-copy-clean） ----------

    section("④ 完成态锁：拟完就收工，粘贴时的重绘 / 切换窗口都不许再走一遍")

    check("冷却窗口钉在 20 秒（短期兜底，不是把这条线整个关掉）",
          SOCIAL_DONE_COOLDOWN_S == 20.0, str(SOCIAL_DONE_COOLDOWN_S))

    # 摆一个"刚把候选交出去"的现场：这一屏的字就是那一轮认到的
    def _poll(frame_id: str, text: str) -> None:
        """驱动一拍监听，等这一拍落定（跟上面几节同一套手法）。"""
        service._running = False
        service._last_listen_at = 0.0
        probe.reset()
        reader_calls["n"] = 0
        idle["sec"] = 10.0
        grab["frame"] = frame_id
        ocr["text"] = text
        service._poll_listen()
        wait_for(lambda: not service._listen_reading)
        drain_threads()
        pump(0.05)

    done_screen = "茗风：周末有空一起爬山吗"
    clock["t"] += 100.0
    # 摆"完成那一刻那一屏"：尾巴与**逐行的字**都要摆上（v2-social-grab-fix 起锁判的是行，
    # 不是一条扁平字符串——只摆尾巴的话，新旧两屏一比全是"没见过的行"，锁等于没立）。
    service._last_listen_tail = done_screen
    service._last_read_lines = {done_screen}
    service.mark_done()
    check("前置：这一段拟完了，完成态锁立着", service.done is True, str(service.done))

    # ① 冷却期内，聊天窗因为"用户去粘贴"重绘了一下（画面变了、字也认出来不一样）
    _poll("FRAME_H", done_screen + " 茗风：那周六见哈")
    check("★ 冷却期内画面抖了 → 一条候选都不重复拟（真机那个'复制完卡死'的回环）",
          not probe.candidates, f"{len(probe.candidates)} 组")
    check("★ 也不冒话（用户正要去粘贴，这会儿别插嘴）",
          not probe.spoken, " | ".join(probe.spoken) or "（安静）")
    check("★ 挡住它的是判定那一步、不是指纹那一步（该认的字照认，好知道画面变了）",
          reader_calls["n"] >= 1, f"认了 {reader_calls['n']} 次")
    check("冷却期内也**没有**解锁", service.done is True, str(service.done))

    # ② 冷却期过了，但对方没再说新的（还是完成那一刻那一屏）→ 照样不动
    clock["t"] += SOCIAL_DONE_COOLDOWN_S + 1
    _poll("FRAME_I", done_screen)
    check("★ 冷却过了 + 画面没换内容 → 还是不重开（同一屏不该被反复拟）",
          not probe.candidates and not probe.spoken,
          f"{len(probe.candidates)} 组 / {' | '.join(probe.spoken) or '（安静）'}")
    check("★ 这一档也没解锁（锁的判据是'画面真的换了'，不是'等够了'）",
          service.done is True, str(service.done))

    # ③ 冷却过了 + 对方真发了新的 → 解锁，正常拟
    _poll("FRAME_J", done_screen + " 茗风：那周六见哈")
    check("★ 冷却过了 + 画面真换了内容 → 解锁，照常拟候选",
          wait_for(lambda: bool(probe.candidates)), f"{len(probe.candidates)} 组")
    # 候选能出来，就说明判定那一步真走了 `reset_done` 那条支路（返回 "none" 的话
    # 一个候选都不会有）。拟完这一轮之后锁自然又立起来——但锁的是**新的一屏**。
    check("★ 解锁不是一次性哑火：新一轮拟完又锁上，锁的是新那一屏",
          service.done is True and service._done_tail != done_screen,
          f"done={service.done}，锁的是 {service._done_tail!r}")

    # ④ 走出聊天窗口 → 锁跟着解开：下一趟进来是全新的一轮
    service.mark_done()
    check("前置：又锁上了", service.done is True, "")
    service._social_foreground = True
    service.on_foreground("chrome.exe")
    check("★ 切出聊天窗口 → 完成态锁解开（下一趟进来是新一轮，不带着上一轮的锁）",
          service.done is False, str(service.done))
    service._social_foreground = True
    service._sync_listen_timer()

    # ---------- ④-2 新消息时效（v2-social-grab-fix） ----------
    #
    # 真机上"新消息到了等几分钟才反应"的根，除了上下文整屏没裁剪，还有一条：老口径的
    # 完成态锁拿**一整条扁平尾巴比相等**，冷却期内对方真发的新消息也被一道 20 秒冷却
    # 一起咽掉。这里验三件事：新消息**冷却期内**就该放行、上下文取的是**最新那条**、
    # 冷却内分不清的抖动过了冷却会被**主动重看**（不永久咽掉）。

    section("④-2 新消息时效：冷却只挡重开，不吞新消息；上下文取最新那条")

    # 这一节验的是"锁"与"取条"，读数换成能分出说话人的 ChatRead（色卡那半另测）
    rows_v = {"v": []}
    service._read_chat = lambda array: ChatRead(lines=list(rows_v["v"]))

    def _poll2(frame_id: str, rows) -> None:
        # 驱动这一拍之前先把上一节**还在飞**的那几拍等落定（进社交前台那一拍会顺手
        # "手动读"一次，`_chat_reading` 立着的时候 `_poll_listen` 头一句就把这一拍挡回去，
        # 看着像"这一拍什么都没发生"）。抓屏桩也**自己带上**：别的节可能把它换成了只会
        # 数次的探子（那个永远返回同一帧），指纹不翻 → 这一拍会在"画面没变"那一步早退。
        wait_for(lambda: not service._chat_reading and not service._listen_reading)
        drain_threads()
        pump(0.05)
        service._grab_chat = lambda: grab["frame"]
        service._running = False
        service._last_listen_at = 0.0
        probe.reset()
        reader_calls["n"] = 0
        idle["sec"] = 10.0
        grab["frame"] = frame_id
        rows_v["v"] = rows
        service._poll_listen()
        wait_for(lambda: not service._listen_reading)
        drain_threads()
        pump(0.05)

    # 摆一个"刚把候选交出去"的现场：完成那一刻就是这一屏
    clock["t"] += 100.0
    service._last_listen_tail = "茗风：周末有空一起爬山吗"
    service._last_read_lines = {"茗风：周末有空一起爬山吗"}
    service.mark_done()
    check("前置：完成态锁立着", service.done is True, str(service.done))

    # ① 冷却期内对方真发新消息（老行都在、底下多出没见过的）→ 锁直接放行，不吞
    probe.reset()
    stub.prompts.clear()
    _poll2("FRAME_N1", [
        (SENDER_OTHER, "茗风：周末有空一起爬山吗"),
        (SENDER_OTHER, "茗风：那周六见哈"),
    ])
    check("★ 冷却期内对方真发新消息（只多不少）→ 候选直接出，没被 20 秒冷却咽掉",
          wait_for(lambda: bool(probe.candidates)), f"{len(probe.candidates)} 组")
    check("★ 放行之后锁又立上，锁的是**新那一屏**（不是完成那一刻那一屏）",
          service.done is True and "那周六见哈" in (service._done_tail or ""),
          f"锁的是 {service._done_tail!r}")

    # ② 上下文取最新那条：聊天记录很长时，顶部旧话被按条留在外面（不跟着回来）
    #
    # 最新那条**换一句新的**（批次8-A 收窄之后非换不可）：材料现在只取视觉最底部那一条
    # 对方气泡，① 的材料与这一节要摆的那一屏会撞成同一份「茗风：那周六见哈」，而"同一份
    # 材料不重开"那道闸（红线：去重口径不动）会把这一拍整个压住——那验到的就不是"材料取
    # 哪一段"了。让这一拍真的是**新来的一条**，验的还是原来那件事。
    clock["t"] += SOCIAL_DONE_COOLDOWN_S + 1
    probe.reset()
    stub.prompts.clear()
    # 一长串旧话把 600 字上限占满，最新那条压在最下面——老口径整屏塞进去，模型又慢又回旧话
    _poll2("FRAME_N2", [(SENDER_OTHER, f"旧话第{i}行" + "字" * 40) for i in range(1, 40)]
           + [(SENDER_OTHER, "茗风：那周六见哈，我带茶")])
    wait_for(lambda: bool(probe.candidates))
    drain_threads()
    pump(0.1)
    prompt = next((p for p in stub.prompts if "他自己的口吻" in p), "")
    check("★ 上下文取的是**最新那条**（最顶部的旧话被按条留在外面）",
          "那周六见哈" in prompt and "旧话第1行" not in prompt,
          prompt[:120].replace("\n", " / "))

    # ③ 冷却期内画面滚了、新行认不出是谁 → 扣下不拟；冷却过了主动重看 → 拟出来
    clock["t"] += 100.0
    service._last_listen_tail = "茗风：那周六见哈"
    service._last_read_lines = {"茗风：那周六见哈"}
    service.mark_done()
    probe.reset()
    stub.prompts.clear()
    _poll2("FRAME_N3", [(SENDER_UNKNOWN, "茗风：好，周六上午十点")])
    check("冷却期内画面滚了、新行认不出是谁 → 扣下，不重复拟（也不谎报）",
          not probe.candidates, f"{len(probe.candidates)} 组")
    check("扣下时记下了'还有待办'（冷却一过要主动重看）",
          service._locked_pending is True, str(service._locked_pending))
    # 画面没再变，指纹那条路本来不会再看一眼——主动重看那一屏
    clock["t"] += SOCIAL_DONE_COOLDOWN_S + 1
    _poll2("FRAME_N3", [(SENDER_UNKNOWN, "茗风：好，周六上午十点")])
    check("★ 冷却一过主动重看那一屏 → 拟出来了（没被永久咽掉）",
          wait_for(lambda: bool(probe.candidates)), f"{len(probe.candidates)} 组")

    # ---------- ④-3 冷启动先抢前台：社交态由"现状"定，不由"换没换过人"定 ----------

    section("④-3 冷启动先抢前台（v2-social-foreground-fix）")

    monitor = app.screen_monitor
    probe.reset()
    # ④-2 把 `_read_chat` 换成了 ChatRead 桩（那个不计次数），这里改成数"抓了几次屏"：
    # 要验的是"进社交前台那一刻有没有去抓一屏"，不是"认了几次字"。
    grabbed = {"n": 0}

    def _grab_counted():
        grabbed["n"] += 1
        return "FRAME_A"

    service._grab_chat = _grab_counted
    # 老口径的起点：`foreground_changed` 只在**换人**时发，而冷启动那一刻前台正是
    # 宠物自己——那拍整个 return、一个信号都不发，`_social_foreground` 就停在初值上。
    service._social_foreground = False
    service._awaiting = False
    service._sync_listen_timer()
    check("前置：没人问过前台时，监听计时器是停的（老口径就卡在这儿）",
          service._listen_timer.isActive() is False, "")

    # **把"退路那一问"也钉死**（v2-social-await-prime-fix 改动二：焦点答不出社交软件时，
    # `_prime_social_foreground` 还会退一步去看"桌面上有没有**可见的**社交窗口"）。
    # 不钉的话这一问直接读**真桌面**——用户开着微信跑测试时，① 里那个空串会被桌面上的
    # 微信顶掉，社交态落成 True，下面"落成 False / 计时器停着"两条必红。这不是假设：
    # 2026-10-07 全量跑实测单独跑 85/85 绿、全量跑 83/85 红，根因正是这里漏钉一个 seam。
    # 退路本身由 `tests/test_social_await_prime.py` 那一节专门验（那边是存/还原着桩的），
    # 所以这里按"桌面上一个社交窗口都没看见"给，丢不掉任何覆盖。
    monitor.visible_procs = lambda: []

    # ① 冷启动时前台是宠物自己：探到空串 → 社交态落成 False —— 那是真值，不是"没问过"
    monitor.foreground_now = lambda: ""
    app._prime_social_foreground()
    check("★ 前台是宠物自己 → 探一次就把社交态落成 False（真值，不是「还没问过」）",
          service._social_foreground is False, str(service._social_foreground))
    check("这一档不冒「要帮你措辞吗」（自己在前台，没什么可看的）",
          not probe.offers and ASK_LINE not in probe.spoken,
          f"offer {len(probe.offers)} 次 / 说 {len(probe.spoken)} 句")
    check("监听计时器照旧停着（不该空转）",
          service._listen_timer.isActive() is False, "")

    # ② 冷启动时社交软件就在前台：探到它 → 当场抓屏 + 起监听（这一轮修的就是这一档）
    monitor.foreground_now = lambda: "wechat.exe"
    service._last_asked = clock["t"]        # 冒话那道冷却不在这一节的射程里，先按住
    app._prime_social_foreground()
    drain_threads()
    pump(0.05)
    check("★ 冷启动时微信就在前台 → 探一次就认（老口径要等用户先切一次窗）",
          service._social_foreground is True, str(service._social_foreground))
    check("★ 监听计时器当场起来（对方回消息的秒级触发有着落了）",
          service._listen_timer.isActive() is True, "")
    check("顺带把对方原话那一格也抓了（进社交前台本来就该抓一屏）",
          grabbed["n"] >= 1, f"抓屏 {grabbed['n']} 次")
    service._listen_timer.stop()
    service._awaiting = False
    service.reset_done()

    # ③ 再走一遍真路：前台**换人**那条信号（切到微信）把社交态翻 True
    monitor.foreground_now = lambda: ""
    service._social_foreground = False
    service._sync_listen_timer()
    check("前置：先当成「不是社交前台」",
          service._social_foreground is False and not service._listen_timer.isActive(), "")
    mon.foreground.foreground_pid = lambda: 4321
    monitor._pids.name_of = lambda pid: "wechat.exe"
    monitor._proc = ""                      # 让它觉得"换人了"
    monitor._poll_foreground()
    check("★ 切到微信（走真信号）→ 社交态翻 True、监听计时器跟着起",
          service._social_foreground is True and service._listen_timer.isActive() is True,
          f"{service._social_foreground} / {service._listen_timer.isActive()}")
    service._listen_timer.stop()
    service._awaiting = False
    service.dismiss_offer()
    service.reset_done()
    # 这一节的抓屏探子只用来数"进社交前台抓了几屏"，它每拍返回**同一帧**（指纹不翻）。
    # 出了这一节就得换回来——后面几节走的是"每拍换一张帧"那条真路。
    service._grab_chat = lambda: grab["frame"]

    # ---------- ④-4 同一份对方原话不重开一轮（v2-social-listen-dedup） ----------
    #
    # 真机那条：宠物**自己浮窗里的字**一变，画面指纹与 `visible` 的文本比相等就都翻了，
    # 可 `_tail_context` 取出来的**对方原话一个字没变**——于是过了 8 秒冷却就照着同一句
    # 话再拟一段（真机上看着就是"同一个方案隔一会儿蹦一段"）。这一节摆的正是这个现场：
    # 画面里**多一行宠物自己那串字**（5134px / 5133px），对方原话不动。

    section("④-4 同一份对方原话不重开一轮（v2-social-listen-dedup）")

    SOURCE = "茗风：周末有空一起爬山吗"
    _poll2("FRAME_D1", [(SENDER_OTHER, SOURCE)])
    check("前置：第一段照常拟出来", wait_for(lambda: bool(probe.candidates)),
          f"{len(probe.candidates)} 组")
    check("前置：记下了这一屏**喂给模型的那份**对方原话",
          service._last_draft_source == SOURCE, repr(service._last_draft_source))
    check("记的是喂模型那一份（对方原话），不是整屏 visible（宠物自己那行不在里面）",
          "5134px" not in service._last_draft_source, repr(service._last_draft_source))

    # 反证：把这道闸拆掉（= 修复前的样子）→ 同一份对方原话会被**再拟一遍**
    service._last_draft_source = ""
    probe.reset()
    _poll2("FRAME_D2", [(SENDER_OTHER, SOURCE), (SENDER_UNKNOWN, "5134px")])
    check("★ 反证：闸拆掉后，画面只多了一行（宠物自己浮窗那行）就把同一句话又拟了一遍",
          bool(probe.candidates), f"{len(probe.candidates)} 组")

    # 真口径：闸立着（上一拍刚把 SOURCE 拟过）→ 画面再被顶翻也不重开
    probe.reset()
    _poll2("FRAME_D3", [(SENDER_OTHER, SOURCE), (SENDER_UNKNOWN, "5134px"),
                        (SENDER_UNKNOWN, "5133px")])
    check("★ 同一份对方原话 → 一条候选也不重复拟", not probe.candidates,
          f"{len(probe.candidates)} 组")
    check("★ 也不吭声（不冒「我琢磨一下」，不刷屏）", not probe.spoken,
          " | ".join(probe.spoken) or "（安静）")
    check("★ 判定那一步真走到了「对方回了」这一步（不是被别的闸挡在外面）",
          service._last_listen_kind == "incoming", repr(service._last_listen_kind))
    check("忙态没立起来（这一拍根本没开工）", service.busy is False, str(service.busy))
    check("被挡下的那一拍没动记忆（下一拍还拿同一份比）",
          service._last_draft_source == SOURCE, repr(service._last_draft_source))

    # 新内容照旧拟：这道闸只挡"同一份材料"，不挡真新消息
    probe.reset()
    stub.prompts.clear()
    _poll2("FRAME_D4", [(SENDER_OTHER, SOURCE), (SENDER_UNKNOWN, "5134px"),
                        (SENDER_UNKNOWN, "5133px"), (SENDER_OTHER, "茗风：那就周六见吧")])
    check("★ 真来了新的对方消息 → 照旧拟（这道闸只挡重复）",
          wait_for(lambda: bool(probe.candidates)), f"{len(probe.candidates)} 组")
    check("新那一段的提示词里带上了新的那句",
          any("那就周六见吧" in p for p in stub.prompts), "")

    # 走出聊天窗口 = 翻篇：记忆清掉，下一趟同一份字该重新看一遍
    service._social_foreground = True
    service.on_foreground("chrome.exe")
    check("切出聊天窗口 → 「这一屏已拟过」也清了（下一趟进来重新看）",
          service._last_draft_source == "", repr(service._last_draft_source))
    service._social_foreground = True
    service._sync_listen_timer()

    # ---------- ④-5 邀请挂太久：自己收掉、自动监听接着跑（v2-social-fg-residual） ----------
    #
    # 真机那条：邀请那排按钮冒出来，用户没点 [好呀] / [不用了]，也没关气泡（切去忙别的
    # 了）。`_awaiting` 就这么一直挂着，而 `_poll_listen` 头一句就拿它 early-return——
    # **自动监听从此一声不响**，人明明还在跟对方一来一回，宠物再也不接。这一节摆的正是
    # 这个现场：邀请挂着，同时聊天窗里真来了新的对方消息。

    section("④-5 邀请挂着超阈值 → 自己收掉、监听恢复（v2-social-fg-residual）")

    check("阈值常量在，且比两道冷却都长（不是随手取个小数字）",
          OFFER_HANG_S > SOCIAL_DONE_COOLDOWN_S > LISTEN_COOLDOWN_S,
          f"{OFFER_HANG_S} / {SOCIAL_DONE_COOLDOWN_S} / {LISTEN_COOLDOWN_S}")

    # 数"这一拍到底认了几次字"：光看"没有候选"分不出是"被邀请挡回去了"还是"本来就没内容"
    reads = {"n": 0}
    inner_read = service._read_chat

    def _counted_read(array):
        reads["n"] += 1
        return inner_read(array)

    service._read_chat = _counted_read

    def _hang_offer(seconds: float) -> None:
        """把"正等用户点 [好呀]"这一档挂起来，挂到距今 seconds 秒之前。"""
        service._awaiting = True
        service._last_asked = clock["t"] - seconds
        service._want_situation_until = 0.0

    # ① 挂着、还没到阈值：这一拍整个被挡回去（连一次认字都没做、尾巴也没记）
    _hang_offer(0.0)
    clock["t"] += OFFER_HANG_S / 2
    tail_before = service._last_listen_tail
    probe.reset()
    reads["n"] = 0
    _poll2("FRAME_H1", [(SENDER_OTHER, "茗风：那我先回去了")])
    check("前置：邀请还挂着（没到阈值，不该收）", service._awaiting is True, "")
    check("★ 挂着的时候这一拍真的一次字都没认（不是「本来就没新内容」）",
          reads["n"] == 0, f"认字 {reads['n']} 次")
    check("★ 这一拍连账都没记（尾巴还停在上一拍那条，这条消息正被咽着）",
          service._last_listen_tail == tail_before, repr(service._last_listen_tail))
    check("★ 于是这一屏新消息被咽着（老口径就一直这么咽下去）",
          not probe.candidates, f"{len(probe.candidates)} 组")

    # ①-b 收的时候**两档一起收**：`_awaiting` 与完成态锁（单敲一次，读数才拿得到那一瞬）
    clock["t"] += OFFER_HANG_S
    service._last_listen_tail = "茗风：明天见吧"
    service._last_read_lines = {"茗风：明天见吧"}
    service.mark_done()
    check("前置：完成态锁立着（上一段刚交出去那份现场）", service.done is True, str(service.done))
    dropped = service._drop_stale_offer()
    check("★ 挂过阈值 → 邀请自己收掉（不用等用户点）",
          dropped is True and service._awaiting is False, str(service._awaiting))
    check("★ 完成态锁也一起解开（不解的话新内容照样被它咽回去）",
          service.done is False, str(service.done))

    # ② 挂过阈值那**一拍**：同一拍就把咽着的那条办出来（不是"恢复了但什么都不做"）
    _hang_offer(OFFER_HANG_S + 5)
    service._last_listen_tail = "茗风：周末有空一起爬山吗 茗风：那就周六见吧"
    service._last_read_lines = {"茗风：那我先回去了"}
    service.mark_done()                     # 锁又立上：这一拍要**它自己**解开才算数
    probe.reset()
    reads["n"] = 0
    _poll2("FRAME_H2", [(SENDER_OTHER, "茗风：那我先回去了")])
    check("★ 同一拍里：邀请收掉（`_awaiting` 落 False）",
          service._awaiting is False, str(service._awaiting))
    check("★ 监听真的接着跑了：这一拍认了字",
          reads["n"] >= 1, f"认字 {reads['n']} 次")
    check("★ 而且把咽着的那条办出来了（锁没解开的话它一定被咽回去）",
          wait_for(lambda: bool(probe.candidates)), f"{len(probe.candidates)} 组")
    check("办完照旧锁上**新那一屏**（不是把锁整个废掉）",
          service.done is True, str(service.done))

    # ③ 收掉的是**状态**，不是把这条路关了：用户之后才点 [好呀]，照旧接得住
    _hang_offer(OFFER_HANG_S + 5)
    probe.reset()
    _poll2("FRAME_H3", [(SENDER_OTHER, "茗风：明天见吧")])
    check("前置：这一次也自己收掉了", service._awaiting is False, str(service._awaiting))
    # 批次11：点 [好呀] 之后这条线**当场就开始拟**，不再布"等处境"那个窗口
    # （`_want_situation_until`）。所以"接住了"的凭据从"窗口立起来了"换成"这一趟真开跑"。
    took_it = service.confirm("好呀")
    check("★ 收掉之后用户**才**点 [好呀]：照旧接得住（收的是状态，不是把这条路口封了）",
          took_it is True and service.busy is True,
          f"confirm 回 {took_it} / 忙态 {service.busy}")
    check("★ 而且不再布「等处境」那个窗口了（批次11 起没人再装它）",
          service._want_situation_until == 0.0, f"{service._want_situation_until}")
    wait_for(lambda: not service.busy)
    drain_threads()
    pump(0.1)
    service._want_situation_until = 0.0
    service._awaiting = False
    service.reset_done()
    service._last_asked = clock["t"]        # 冒话那道冷却按住，别影响 ⑤
    service._read_chat = inner_read         # 换回不数次的桩

    # ---------- ⑤ 红线：零写入、零新依赖、不模拟按键 ----------

    section("⑤ 红线：零写入、零新依赖、不模拟按键")

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()
    # 再走一遍自动拟候选的完整链路，确认两库一行没多
    service._running = False
    service._last_listen_at = 0.0
    idle["sec"] = 10.0
    grab["frame"] = "FRAME_G"
    ocr["text"] = "茗风：周六见哈"
    service._poll_listen()
    wait_for(lambda: not service.busy)
    drain_threads()
    pump(0.1)
    check("pet.db utterance 一行没多",
          _rows(app.config.db_path, "utterance") == pet_before,
          f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
    check("mind.db 碎片一条没多",
          app.mind_store.fragment_count() == mind_before, "")

    import inspect

    src = inspect.getsource(smod)
    check("指纹哈希用标准库 hashlib（零新依赖）", "hashlib" in src, "")
    check("计时器用 PySide6 既有的 QTimer（零新依赖）", "QTimer" in src, "")
    check("服务自己不 import 截屏 / OCR 库（那只手仍在装配层）",
          "read_screen_array" not in src and "grab_foreground_window" not in src, "")
    check("自动监听没有模拟按键 / 读剪贴板那几条腿",
          not any(w in src for w in ("SendInput", "keybd_event", "uiautomation",
                                     "pyautogui", "pynput", "win32clipboard")), "")
    check("候选照旧不自动发（跳确认 ≠ 自动发送）",
          "不自动发送" in src, "")

    drain_threads()
    box.cleanup()
    return summary("社交自动监听（v2-social-auto）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
