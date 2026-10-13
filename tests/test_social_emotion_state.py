# -*- coding: utf-8 -*-
"""社交起草·对方情绪状态机「进退分寸」（v2-social-emotion-state）的自动测试。

**病根**（只读回填前几批的结论）：桌宠已经能认出对方单句的意图 / 情绪（v2-social-card-base
的 `_peer_route`）、也能顺着料自然升温（v2-social-draft-escalate），但**只会进、不懂退**——
对方已经"累了想收"，它还在往更近一档贴。缺的是"看脸色"这一层：把对方**近几句**（不只是
当前这句）摆在一起看，先判"这一轮该进还是该退"，再决定往不往前带。

这一批补一个**纯规则状态机** `_emotion_state`（照字面判，不碰盘 / 库 / 模型，跟
`_tone_features` 同一个纪律），输出三态之一：

- **该退**：对方明说忙 / 累 / 想睡 / 改天、这一句的情绪被识别成负向（烦 / 怒 / 丧）、
  最近两句都是冷短回收尾（≤ 5 字、没反问没表情）、或上一句还有表情这一句一下没了；
- **可攻**：积极接梗 / 反问抛钩子 / 卖关子 / 撒娇轻盈 / 笑声表情 / 情绪轻松；
- **需守**：中性应答、单句没明确想进也没明确想走（默认态）。

**退信号优先于进信号**——宁可收一档，也别在对方已经想撤的时候还往前带。

状态接在 `SOCIAL_TONE_HINT_BLOCK` 后面、一起填进 `{peer_tone_hint}` 那一格（不新增占位符，
`tests/test_social_draft_escalate.py` 钉的五格一个没动）；`SOCIAL_DRAFT_PROMPT` 升温段旁再补
一段静态的反向分支：该退 → 安抚 / 收尾 / 留白、不升温；需守 → 同频不冒进；可攻 → 才升温。

红线（本模块逐条钉住）：
- **`tone_align=off` 时提示词与加它之前逐字相同**——状态跟口吻摘要走同一道闸（同一本账），
  关了就整段不参与、也不记（不抽、不注入、不留痕）；
- 不新增 `{}` 占位符（还是那五格）；`SOCIAL_TONE_HINT_BLOCK` 一个字没动；
- 纯规则、零新依赖、不新增网络请求（还是起草那一趟）；两库零写入；
- why 解耦那条没破：正文那一格只动"进退分寸"，"该说什么"仍归解析。

**口径如实记（够不着的地方不装作验过）**：模型被桩掉了，本模块钉的是**喂进去的状态**与
"该教的进退分寸有没有进提示词"，不是"模型真能收住"——那只能真机看。候选那几个字是桩给的。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    section,
    summary,
)

from brain.prompts.templates import (  # noqa: E402
    SOCIAL_DRAFT_PROMPT,
    SOCIAL_EMOTION_STATE_BLOCK,
)
from life.dialog import router as dialog_router  # noqa: E402
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    AUTO_TRIGGER_TEXT,
    NO_EXAMPLES,
    SocialReplyService,
    _SHORT_REPLY_MAX,
    _emotion_state,
)

# 起草模板自己的换行符（模板文件是 CRLF，渲染出来的提示词里就是 \r\n）
SEP = "\r\n" if "\r\n" in SOCIAL_DRAFT_PROMPT else "\n"


def main() -> int:
    # ---------- ① 纯函数：三态各归各 ----------

    section("① 纯函数 _emotion_state：可攻 / 需守 / 该退 各归各")

    check("★ 一句都没有、也没有情绪 → 空串（宁可不注入，也不瞎猜一个状态）",
          _emotion_state([]) == "", repr(_emotion_state([])))
    check("★ 全是空白行 → 也是空串", _emotion_state(["", "   "]) == "",
          repr(_emotion_state(["", "  "])))

    # —— 该退 ——
    check("★ 明说忙 → 该退", _emotion_state(["我今天好忙，先不聊了"]) == "该退", "")
    check("★ 累 / 改天 → 该退", _emotion_state(["最近好累，改天再说吧"]) == "该退", "")
    check("★ 想睡 / 晚安 → 该退", _emotion_state(["先睡了，晚安"]) == "该退", "")
    check("★ 连续冷短回收尾（嗯 / 哦 / 好）→ 该退",
          _emotion_state(["嗯", "哦", "好"]) == "该退", "")
    check("★ emoji 骤减（上一句还有表情、这一句没了）→ 该退",
          _emotion_state(["哈哈好开心😄", "嗯"]) == "该退", "")
    check("★ 情绪识别成负向（低落 / 焦虑 / 愤怒）→ 该退",
          _emotion_state(["随便吧"], dialog_router.EMOTION_LOW) == "该退", "")

    # —— 可攻 ——
    check("★ 反问抛钩子（试探话术 + 问号）→ 可攻",
          _emotion_state(["真的假的？你不是说下周才走吗"]) == "可攻", "")
    check("★ 卖关子 / 撒娇轻盈 / 带表情 → 可攻",
          _emotion_state(["你猜呀，不告诉你~ 😏"]) == "可攻", "")
    check("★ 撒娇轻盈（人家 / 想要 / 抱抱）→ 可攻",
          _emotion_state(["人家想要抱抱嘛"]) == "可攻", "")
    check("★ 情绪轻松（开心）→ 可攻",
          _emotion_state(["随便吧"], dialog_router.EMOTION_HAPPY) == "可攻", "")

    # —— 需守（默认态） ——
    check("★ 中性应答 → 需守", _emotion_state(["今天天气不错"]) == "需守", "")
    check("★ 单句冷短（只有一句，不构成「连续」）→ 需守",
          _emotion_state(["嗯"]) == "需守", "")

    # —— 退优先于进 ——
    check("★ 退信号优先于进信号：接梗 + 改天 同时出现 → 该退",
          _emotion_state(["真的假的？", "算了改天再说吧"]) == "该退", "")

    # —— 单字不误伤（只收多字 / 配字，单用会误伤"帮忙""积累""困难"）——
    check("★ 「帮忙」不误伤成忙", _emotion_state(["我最近在帮忙朋友搬家"]) != "该退",
          _emotion_state(["我最近在帮忙朋友搬家"]))
    check("★ 「积累」不误伤成累", _emotion_state(["这些都是平时积累的"]) != "该退",
          _emotion_state(["这些都是平时积累的"]))
    check("★ 「困难」不误伤成困", _emotion_state(["这个有点困难"]) != "该退",
          _emotion_state(["这个有点困难"]))

    # ---------- ② 方法按场分格：读同一本账 + 这一拍的情绪 ----------

    section("② _peer_emotion_state：按 sid|peer 分格、吃这一拍的情绪")

    box, stub, app = make_env()
    try:
        service: SocialReplyService = app.social_reply
        service._examples_provider = lambda query: ""
        stub.candidates_result = {"candidates": [{"draft": "行啊，你来定", "why": "接住对方"}]}

        service._session_id, service._peer_name = "S1", "甲"
        key_a = service._cooldown_key()
        check("前置：这一场的键就是 sid|peer", key_a == "S1|甲", key_a)

        service._remember_peer_lines(key_a, "嗯\n哦\n好")
        service._remember_peer_lines("S1|乙", "真的假的？你不是说下周才走吗")
        check("★ 两场各判各的：甲是冷短回 → 该退，乙是反问抛钩子 → 可攻（不串场）",
              service._peer_emotion_state(key_a) == "该退"
              and service._peer_emotion_state("S1|乙") == "可攻",
              f"{service._peer_emotion_state(key_a)} / "
              f"{service._peer_emotion_state('S1|乙')}")

        # 这一拍的情绪也进判定：账本是中性的，但这一句被判成负向 → 该退
        service._peer_name = "甲"
        service._peer_route = dialog_router.DialogRoute(
            dialog_router.INTENT_CHAT, dialog_router.EMOTION_LOW, matched=False, crisis=False)
        check("★ 情绪那一维也进判定（账本中性 + 这一句低落 → 该退）",
              service._peer_emotion_state(key_a) == "该退",
              service._peer_emotion_state(key_a))
        service._peer_route = None

        # ---------- ③ 注入进提示词：状态块 + 反向分支都在 ----------

        section("③ tone_align=auto：状态块进提示词，且随对方实时变")

        app.config.screen.tone_align = "auto"
        app.config.screen.card_memory = "off"      # 隔离口吻这条线，别让档案那格掺进来

        stub.prompts.clear()
        service._session_id, service._peer_name = "S1", "甲"
        service._peer_lines.clear()
        service._write(AUTO_TRIGGER_TEXT, "嗯\n哦\n好")           # 连续冷短回 → 该退
        retreat_prompt = stub.prompts[-1]
        check("★ 状态块真的进了提示词", "对方现在这轮的进退分寸" in retreat_prompt, "")
        check("★ 状态判的是「该退」", "进退分寸：该退" in retreat_prompt,
              retreat_prompt.split("对方现在这轮的进退分寸")[-1][:40].replace("\n", " / "))
        check("★ 反向分支那条静态指令也在（该退 → 不升温 / 收尾留白）",
              "先看对方这轮给不给进" in retreat_prompt
              and "不升温、不抛共建、不邀约" in retreat_prompt
              and "给一句收尾或留个口子" in retreat_prompt, "")

        stub.prompts.clear()
        service._peer_name = "乙"
        service._peer_lines.clear()
        service._write(AUTO_TRIGGER_TEXT, "真的假的？你不是说下周才走吗")   # 接梗 → 可攻
        attack_prompt = stub.prompts[-1]
        check("★ 换成接梗那一场 → 状态跟着变成「可攻」",
              "进退分寸：可攻" in attack_prompt, "")

        stub.prompts.clear()
        service._peer_name = "丙"
        service._peer_lines.clear()
        service._write(AUTO_TRIGGER_TEXT, "今天天气不错")           # 中性 → 需守
        hold_prompt = stub.prompts[-1]
        check("★ 中性那一场 → 状态是「需守」", "进退分寸：需守" in hold_prompt, "")

        # ---------- ④ off：不记、不抽、不注入（提示词逐字回到改动前） ----------

        section("④ tone_align=off：状态块整段不参与，提示词逐字相同")

        app.config.screen.tone_align = "off"
        app.config.screen.card_memory = "off"
        service._session_id, service._peer_name = "S9", "甲"

        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "嗯\n哦\n好")
        off_prompt = stub.prompts[-1]
        baseline = SOCIAL_DRAFT_PROMPT.format(
            chat_context="嗯\n哦\n好",
            text=AUTO_TRIGGER_TEXT,
            examples=NO_EXAMPLES,
            peer_tone_hint="",
            peer_profile="",
        )
        check("★ off 那趟的提示词 == 占位填空串渲染出来的那一份（一字不差）",
              off_prompt == baseline, f"{len(off_prompt)} 字 vs {len(baseline)} 字")
        check("★ off 那趟没有状态块那一段", "对方现在这轮的进退分寸" not in off_prompt, "")
        check("★ 但反向分支那条静态指令照旧在（它属于升温段，不属于这一格）",
              "先看对方这轮给不给进" in off_prompt, "")
        check("★ off 那趟一个字都没记（账本还是空的）",
              not service._peer_lines.get("S9|甲"), str(service._peer_lines.get("S9|甲")))

        # ---------- ⑤ 红线与接线 ----------

        section("⑤ 红线：占位符没多 / 零新依赖 / 两库零写入 / why 解耦")

        fields = {f for _, f, _, _ in string.Formatter().parse(SOCIAL_DRAFT_PROMPT) if f}
        check("★ 占位符一个没多、一个没少（还是那五个）",
              fields == {"chat_context", "text", "examples", "peer_tone_hint", "peer_profile"},
              str(sorted(fields)))
        check("★ `SOCIAL_EMOTION_STATE_BLOCK` 三态词表都在（该退 / 需守 / 可攻）",
              all(w in SOCIAL_EMOTION_STATE_BLOCK for w in ("该退", "需守", "可攻")), "")
        check("★ 那一格是底牌、原文不许出现在回话里（跟档案那格同一条纪律）",
              "这些原文一个字都不许出现在回话里" in SOCIAL_EMOTION_STATE_BLOCK, "")

        src = inspect.getsource(smod)
        check("★ 退信号排在进信号前面（源码里 `return \"该退\"` 在 `return \"可攻\"` 之前）",
              'return "该退"' in src and 'return "可攻"' in src
              and src.index('return "该退"') < src.index('return "可攻"'), "")
        write_src = inspect.getsource(SocialReplyService._write)
        check("★ _write 里真的取状态并接进 tone_hint（不是留了个没接的口子）",
              "state = self._peer_emotion_state(peer)" in write_src
              and "SOCIAL_EMOTION_STATE_BLOCK.format(emotion_state=state)" in write_src, "")
        check("★ 口吻摘要走的就是这一趟的 chat_text（不另开一路输入，既有口径没动）",
              "_remember_peer_lines(peer, chat_text)" in write_src, "")
        check("★ 状态块接在口吻摘要后面（先摘要、后状态）",
              "SOCIAL_TONE_HINT_BLOCK.format(peer_tone_hint=summary)" in write_src
              and "state = self._peer_emotion_state(peer)" in write_src
              and write_src.index("SOCIAL_TONE_HINT_BLOCK.format(peer_tone_hint=summary)")
              < write_src.index("state = self._peer_emotion_state(peer)"), "")

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "嗯\n哦\n好")
        service._write(AUTO_TRIGGER_TEXT, "真的假的？你不是说下周才走吗")
        drain_threads()
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        refs: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                refs.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(alias.name for alias in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api", "fuzzywuzzy", "regex"}
        check("★ 零新依赖：这一条线没多一个第三方 import", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        picked = SocialReplyService._normalize_candidates(
            {"candidates": [{"draft": "行啊，你来定", "why": "接住对方，不升级"}]})
        check("★ 候选仍是 draft + why 两个字段（正文那一格只动了进退分寸）",
              picked == [{"draft": "行啊，你来定", "why": "接住对方，不升级"}], str(picked))

        check("★ 冷短回那一档的阈值还是 5 字（判据被改宽 / 改窄，这里当场红）",
              _SHORT_REPLY_MAX == 5, repr(_SHORT_REPLY_MAX))
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草·对方情绪状态机「进退分寸」（v2-social-emotion-state）")


if __name__ == "__main__":
    raise SystemExit(main())
