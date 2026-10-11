"""社交材料边界（v2-social-ui-batch 批次8）的自动测试。

真机坐实（2026-10-07 19:04-19:05 日志，四次模型 POST 全是"起草"）：

    材料一：'，是在撩我么？'                     （对方 11 行 / 自己 6 行）
    材料二：'…撩你？我这是正常发挥…'              （对方 12 行 / 自己 7 行）
    材料三：'按住鼠标语音输入文字'                （10 字）
    材料四：'那我该不该接受呢 / 按住鼠标语音输入文字'（19 字）

材料**微变一次就触发一次起草**，于是那四条方案对象分别落在"上次末句 / 这次末句 /
我自己发的 / 一团杂音"上。两处根：

**A. 材料按的是"说话人"，不是"条数"。** 老口径从末尾往前捡，碰到自己发的才停——
真机那一屏我上一次开口之后对方连回了十一二行，于是**一条不落全当材料**递下去，
"该回哪一句"只能由模型自己猜。提示词那边本来就写着"对方最近发来的那条"
（`SOCIAL_DRAFT_PROMPT` 1a：一条消息可能占好几行），材料现在跟它对齐：
**视觉最底部那一条对方气泡，就一条。**

**B. 输入框空着时的占位灰字进了材料。** 微信输入框空着会打一句灰字
「按住鼠标语音输入文字」，它深字打在浅底上、色卡一律认成"对方说的"；本该由抓屏
那一刀（`input_cut_px`）切掉，可那一刀取的是 `max(...)`，输入框顶一旦离底边不够远、
`detect_input_top` 又落到它那几条降级返回上，这行字就漏进来——真机第三、四趟 POST
的材料就是它，模型据此回出「行，那你先按着语音输入，我等你」。修法：**认字面**
（跟时间戳 / 系统提示同一个态度），整行等于那几句写死的提示就挑掉，两条路
（按条那条 + 认不出说话人的降级那条）都挑。

**C. 材料为空的那一拍不再起草。** A/B 收窄之后，"表上看着变了、材料其实是空"的拍
（屏幕上只多了一行时间戳、或者只多了一行占位灰字）真的会落到空材料上——不挡的话
每一拍都照着"没认到对方说了什么"起草一次。这道闸只加在**自动**那条路上（手动那条
是用户明着要一段草稿，照旧）。

红线：零新依赖（新加的只有模块里既有的 `re` 与既有常量）；pet.db / mind.db 零写入；
不模拟按键、不碰剪贴板、不读窗口标题；`_classify_incoming` / `_is_duplicate_source` /
两道冷却 / §D 那条 break 一分没动（④ 逐条钉了 tripwire）。
"""

from __future__ import annotations

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
    AUTO_TRIGGER_TEXT,
    SocialReplyService,
    _is_chat_chrome,
    _tail_context,
    _tail_lines,
)
from life.screen.social_window import (  # noqa: E402
    SENDER_OTHER,
    SENDER_UNKNOWN,
    SENDER_USER,
    ChatMessage,
    ChatRead,
)

# 真机第三、四趟 POST 的那行字：微信输入框空着时的占位灰字。
PLACEHOLDER = "按住鼠标语音输入文字"

# 真机那一屏的形状（2026-10-07 19:05，茗风场）：时间戳 / 回过一遍的老话 / 我自己发的那句 /
# 对方连回两条 / 最底下压在输入框里的占位灰字。
HISTORY_OTHER = "先问一句：忙完了吗？"
MY_LAST = "刚忙完，在改东西"
PEER_1 = "那正好，周末一起爬山吧"
PEER_2 = "我票都看好了"


def _real_screen() -> ChatRead:
    """真机那一屏（含装潢 / 历史 / 我自己发的 / 占位灰字），从上到下。"""
    return ChatRead(lines=[
        (SENDER_OTHER, "昨天22:09"),      # 时间戳（装潢）
        (SENDER_OTHER, HISTORY_OTHER),    # 回过一遍的老话
        (SENDER_USER, MY_LAST),           # 我上一次开口
        (SENDER_OTHER, "14:22"),          # 时间戳（装潢）
        (SENDER_OTHER, PEER_1),           # 对方连回的第 1 条
        (SENDER_OTHER, PEER_2),           # 对方连回的第 2 条（末句）
        (SENDER_OTHER, PLACEHOLDER),      # 输入框空着的占位灰字（色卡认成"对方说的"）
    ])


def draft_bubble_text(app) -> str:
    """宠物那个非模态气泡里现在写着什么（候选 + 解析都在这儿）。"""
    history = app.chat_bubble._history
    return "\n".join(history.item(i).text() for i in range(history.count()))


def main() -> int:
    # ---------- ① 材料 = 视觉最底部那一条对方气泡（就一条） ----------

    section("① 材料 = 视觉最底部那一条对方气泡（历史 / 我方 / 上面的连回都不进）")

    read = _real_screen()
    tail = _tail_context(read)
    check("★ 材料就是最底部那一条对方气泡（真机那一屏要的就是这个）",
          tail == PEER_2, repr(tail))
    check("★ 输入框空着的占位灰字不进去（它压在更底下，被字面那条判据挑掉了）",
          PLACEHOLDER not in tail, repr(tail))
    check("★ 对方连回的**上一条**也不进去（收窄之后只留最底下那条）",
          PEER_1 not in tail and "爬山" not in tail, repr(tail))
    check("★ 回过一遍的老话不进去", HISTORY_OTHER not in tail and "忙完了吗" not in tail,
          repr(tail))
    check("★ 我自己发的那句不进去", MY_LAST not in tail and "改东西" not in tail, repr(tail))
    check("时间戳一条都不进去", "22:09" not in tail and "14:22" not in tail, repr(tail))
    # 反向对照：老口径（整屏对方的话）在这里必红——这一条钉住"收窄真发生了"
    check("反向对照：老口径那份（整屏对方的话）里这些字**都在**（所以上面几条不是假绿）",
          all(w in read.incoming for w in (HISTORY_OTHER, PEER_1, PLACEHOLDER)),
          repr(read.incoming))

    # 一条消息在屏幕上折成两行 → 那两行是**同一条**，都得留着（v2-social-multiline 那条口径没破）
    folded_a = "我想说的其实不是这个"
    folded_b = "我是说下个月那件事要不要提前定下来"
    folded = ChatRead(
        lines=[(SENDER_USER, "在的"), (SENDER_OTHER, folded_a),
               (SENDER_OTHER, folded_b), (SENDER_OTHER, PLACEHOLDER)],
        groups=[ChatMessage(sender=SENDER_USER, lines=["在的"]),
                ChatMessage(sender=SENDER_OTHER, lines=[folded_a, folded_b]),
                ChatMessage(sender=SENDER_OTHER, lines=[PLACEHOLDER])],
    )
    check("★ 折行的两行都留着（一条气泡占两行仍是同一条，「绝不切句子」没破）",
          _tail_context(folded) == f"{folded_a}\n{folded_b}", repr(_tail_context(folded)))

    # 最后开口的是我自己 → 退回"对方最近那一条"（老口径，一条不差）
    mine_last = ChatRead(lines=[(SENDER_OTHER, "在吗"), (SENDER_USER, "在的，刚到家")])
    check("最后一句是我自己发的 → 材料退回「对方最近那一条」（不清成空）",
          _tail_context(mine_last) == "在吗", repr(_tail_context(mine_last)))

    # 一屏全是装潢 / 一屏只有占位灰字 → 老实回空串（不是 None，也不编一句）
    check("一屏只有时间戳与撤回提示 → 老实回空串",
          _tail_context(ChatRead(lines=[(SENDER_OTHER, "12:53"),
                                       (SENDER_OTHER, "你撤回了一条消息")])) == "", "")
    check("★ 一屏只剩输入框那行占位灰字 → 也回空串（真机第三趟 POST 就是这一格）",
          _tail_context(ChatRead(lines=[(SENDER_OTHER, PLACEHOLDER)])) == "", "")

    # 降级那条路（认不出说话人）照旧按行取尾，但装潢 / 占位同样挑掉
    degraded = ChatRead(lines=[(SENDER_UNKNOWN,
                               f"在吗\n14:22\n{PLACEHOLDER}\n周末一起爬山吧")])
    check("认不出说话人 → 仍旧按行取尾（降级路那条口径在）",
          _tail_context(degraded) == _tail_lines(f"在吗\n14:22\n{PLACEHOLDER}\n周末一起爬山吧"),
          repr(_tail_context(degraded)))
    check("★ 降级路上那行占位灰字也挑掉了（两条路一个口径）",
          PLACEHOLDER not in _tail_context(degraded), repr(_tail_context(degraded)))
    check("降级路上时间戳也挑掉了", "14:22" not in _tail_context(degraded),
          repr(_tail_context(degraded)))
    check("降级路上人话一个字没动", "周末一起爬山吧" in _tail_context(degraded),
          repr(_tail_context(degraded)))

    # ---------- ② 占位灰字那条判据：只认字面，且只认整行 ----------

    section("② 输入框占位灰字：认字面（整行），不误伤夹着它的人话")

    for line in (PLACEHOLDER, "按住 鼠标语音输入文字", "按住说话", "  按住鼠标语音输入文字  "):
        check(f"★ 占位灰字「{line}」判得出来", _is_chat_chrome(line) is True, "")
    for line in ("你按住鼠标语音输入文字试试", "按住鼠标语音输入", "我刚按住说话键了",
                 PEER_2, "？"):
        check(f"人话「{line}」不算占位（只认整行，不做子串匹配）",
              _is_chat_chrome(line) is False, "")
    check("空行仍旧算装潢（那是「没东西」这一档，不是人话）", _is_chat_chrome("") is True, "")

    # ---------- ③ 端到端：对方连回多句 + 输入框空(占位) + 我方已回 → 起草仅 1 次 ----------

    section("③ 端到端：真机那一屏走完自动监听 → 只拟一次，材料就是末句")

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
    page = {"read": ChatRead(lines=[(SENDER_OTHER, "在吗")])}
    clock = {"t": 1000.0}
    service._grab_chat = lambda: grab["frame"]
    service._read_chat = lambda array: page["read"]
    service._idle_seconds = lambda: 10.0
    service._clock = lambda: clock["t"]
    # 冒话那道冷却不在这一批用例的射程里（这儿验的是**自动监听**那条线），先按住：
    # 不按住的话，第一次"非单聊 → 单聊"的跃迁会冒一句邀请，邀请一挂上 `_awaiting`，
    # `_poll_listen` 后面几拍就全被它挡回去（挡轮询是既有的设计，见它门口那行）。
    # v2-social-due-sentinel 之前这一格是**靠"开机 30 分钟内一律静默"那道 bug 蒙对的**
    # ——刻度 1000 秒 < 冷却 1800 秒，`_due()` 一律判"冷却没到"。哨兵一修，这层蒙的
    # 掩护就没了，得显式按住（跟 test_social_auto 第 473 行那条老规矩一个写法）。
    service._last_asked = clock["t"]
    service._examples_provider = lambda query: ""
    service._social_foreground = True
    service._last_fingerprint = None
    service._last_listen_tail = ""
    service._last_listen_kind = ""
    service._last_listen_at = 0.0
    service._running = False
    service._awaiting = False
    # v2-social-helping-window（C-1 收窄）：自动那条路只在**进了帮聊窗口之后**才出候选
    # （点过 [好呀] / 喊过"帮我回"）。本节验的是"进去之后材料收窄成末句"，所以先把这一格
    # 填上——真机落戳那一下（`confirm` / `handle_request`）在 `test_social_continuous.py` ①
    # 里验。本模块没接会话标识那只手，`_cooldown_key()` 一直是空串，填一次就一直开着，
    # 下面 ④ 的反向对照也跟着在窗口里跑。
    service._mark_helping()

    stub.candidates_result = {"candidates": [
        {"draft": "那我把票也订了，周六见", "why": "接住对方的行动，不升级"},
    ]}

    def _tick() -> None:
        service._poll_listen()
        wait_for(lambda: not service._listen_reading)
        drain_threads()
        pump(0.1)

    try:
        # 先打一屏底：第一次认到这一屏只当基线，不算"来了新消息"
        _tick()
        check("前置：第一拍只打底（还没来新消息就不拟）", not candidates,
              f"{len(candidates)} 组")

        # 对方连回两条、输入框空着、我自己上一句还挂在上面 → 这一屏就是真机那一屏
        candidates.clear()
        spoken.clear()
        stub.prompts.clear()
        clock["t"] += 5.0
        grab["frame"] = "FRAME_B"            # 画面变了 → 指纹翻盘
        page["read"] = _real_screen()
        _tick()

        check("★ 起草就一次（材料收窄之后不再一条消息撞出四条方案）",
              len(candidates) == 1, f"{len(candidates)} 组")
        check("★ 模型也只调了一次", len(stub.prompts) == 1, f"{len(stub.prompts)} 条")
        check("★ 候选是模型那条（不是兜底聊天凑出来的）",
              bool(candidates) and candidates[0][0]["draft"] == "那我把票也订了，周六见",
              str(candidates[0][:1] if candidates else "（空）"))
        check("没落到通用聊天兜底", handled == [], repr(handled))

        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 提示词里就是对方末句那一条（模型不用自己猜该回哪句）",
              PEER_2 in prompt, prompt[:120].replace("\n", " / "))
        check("★ 输入框占位灰字一个都没进提示词（真机那两趟 POST 的杂音在这儿被挡住）",
              PLACEHOLDER not in prompt, "")
        check("★ 对方连回的上一条不进提示词", "爬山" not in prompt, "")
        check("★ 回过一遍的老话不进提示词", "忙完了吗" not in prompt, "")
        check("★ 我自己发的那句不进提示词", "改东西" not in prompt, "")
        check("喂模型那一份也记对了（下一拍拿它比「变了没有」）",
              service._last_draft_source == PEER_2, repr(service._last_draft_source))
        check("自动起草的「用户话」那一格仍是固定那句自动说明",
              AUTO_TRIGGER_TEXT in prompt, "")

        # 同一屏再拍一次（画面没变）→ 一次 OCR、一次模型都不再耗
        stub.prompts.clear()
        candidates.clear()
        for _ in range(2):
            service._poll_listen()
        drain_threads()
        pump(0.05)
        check("同一屏再拍：画面没变 → 不再认字、不再拟（老口径那条没破）",
              stub.prompts == [] and candidates == [],
              f"{len(stub.prompts)} 条提示词 / {len(candidates)} 组")

        # ---------- ④ 材料为空的那一拍：自动路不拟，手动路照旧 ----------

        section("④ 材料为空 → 自动那条路一个字都不拟；手动那条路照旧（用户明着要）")

        service._running = False
        service._last_draft_source = ""
        service._last_listen_at = 0.0
        candidates.clear()
        spoken.clear()
        stub.prompts.clear()
        clock["t"] += 100.0
        service._auto_generate("")            # 真机第三趟 POST 的那种材料
        drain_threads()
        pump(0.1)
        check("★ 空材料 → 一条候选都不给、一次模型都不发",
              candidates == [] and stub.prompts == [],
              f"{len(candidates)} 组 / {len(stub.prompts)} 条")
        check("★ 空材料 → 连「我琢磨一下」都不应（用户什么都没看见）",
              "琢磨" not in " ".join(spoken), " | ".join(spoken) or "（安静）")
        check("★ 也没把自己标成忙态（下一拍照旧能接）",
              service.busy is False, str(service.busy))

        # 反向对照：同一套装置下，材料真有字就照拟（证明上面的"没拟"是那道闸拦的）
        service._last_listen_at = 0.0
        clock["t"] += 100.0
        service._auto_generate(PEER_2)
        check("反向对照：材料真有字（末句那一条）→ 照旧拟出来",
              wait_for(lambda: len(candidates) == 1), f"{len(candidates)} 组")
        drain_threads()
        check("反向对照：那一趟提示词里就是这句话", any(PEER_2 in p for p in stub.prompts),
              "")

        # ---------- ⑤ 红线 ----------

        section("⑤ 红线：零新依赖 / 两库零写入 / 不模拟按键 / 去重与冷却一分没动")

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        import ast
        import inspect

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
        check("也没碰剪贴板（那一条在装配层，服务自己一个字都没碰）",
              "clipboard" not in src.lower(), "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        # 红线：去重 / 冷却 / §D 那条 break 一个字都不许动。这几条是**读源码**钉的
        # tripwire——批次8 收窄的是"材料取哪一段"，不是"挡住重复的那道闸"。
        dedup_src = inspect.getsource(SocialReplyService._is_duplicate_source)
        check("★ 去重那道闸还是老口径（比的是材料相等，空材料仍不算重复）",
              "source == self._last_draft_source" in dedup_src
              and "if source and" in dedup_src, "")
        check("★ 两道冷却常量一分没动（8 秒 / 20 秒）",
              (smod.LISTEN_COOLDOWN_S, smod.SOCIAL_DONE_COOLDOWN_S) == (8.0, 20.0),
              f"{smod.LISTEN_COOLDOWN_S} / {smod.SOCIAL_DONE_COOLDOWN_S}")
        tail_src = inspect.getsource(_tail_context)
        check("★ §D 那条 break（碰到自己发的就停）还在",
              "if message.sender == SENDER_USER:" in tail_src
              and "break" in tail_src, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交材料边界（v2-social-ui-batch 批次8）")


if __name__ == "__main__":
    raise SystemExit(main())
