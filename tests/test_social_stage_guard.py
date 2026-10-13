# -*- coding: utf-8 -*-
"""社交起草·「关系阶段调速（破冰期禁撩）」（v2-social-stage-guard）的自动测试。

**病根**（只读回填前几批的结论）：到上一批 `v2-social-topic-roadmap` 为止，起草提示词已经
会读情绪、会认意图、会按关系阶段分档升温（`v2-social-draft-escalate`）、会看这一轮该不该进
（`v2-social-emotion-state`）、会挑话题方向（`v2-social-topic-roadmap`）。但这些全是**"温和
的档位"**——分档段说"刚认识 / 破冰就收一档"，方向段说"破冰往安全区引"。**没有一条硬约束
说"破冰期就是不许撩"**。于是真机上出现（用户点名的那张图）：刚互报完名字年龄老家，桌宠回
**"可爱得有点横。揍我可以，轻点"**——带**可得性 / 性暗示意味的玩笑**，显油腻、显急切。
分档和方向那两段在破冰期没压住它：它们说的是"往哪儿走"，没说的是"这一期压根不许带"。

这一批补的是**关系阶段调速的硬底**——**先从对话里推断阶段、不依赖卡上的记录**（卡那格可能
空着、也可能还没填准；`v2-social-card-autodistill` 抽出来写进 card 表那 8 列，UI 目前读不到，
见 `v2-social-card-uiread`）：

- **破冰期**（初识：刚互报名字、互相问基本信息、互不了解、对话还浅）→ **不升温、不撩、不
  推进关系**：不表达可得性、不油腻调侃、不许"揍我可以，轻点"这类带性暗示 / 可得性意味的
  玩笑；只**轻松同频接话、自然接梗、不越界**，不反问、不另开推进话题。
- **暧昧期**（已互有好感：有私下昵称、聊过情感话题、说话带明确亲近）→ **才允许适度升温、轻撩**。

**跟既有几条怎么协同**（这一段自己声明）：**不动**语气自适应那一格（`{peer_tone_hint}` 照旧
决定节奏与热络度）、**不动**解析那一格（策略解释照旧写 `why`）；**跟进退分寸协同**——破冰期
即便对方主动接梗、那格判成"可攻"，也**只轻松接住、不还撩**（"该不该带"要两关一起放行）。

**预留接口**：等对象画像那条线把关系阶段稳定填进攻略卡后，这一段**优先读卡上那一格**做更精
细的调速；在那之前只按对话信号判、**不依赖卡**（这是本批刻意不依赖卡空 bug 的地方）。

**这一批一个字都不动代码**：只往 `SOCIAL_DRAFT_PROMPT` 里加一段**静态**文本——没有新占位符、
没有新开关、没有新常量、没有新网络请求；动态那两格一个字没动。

红线（本模块逐条钉住）：
- 不新增 `{}` 占位符（还是那五格）；不新增任何开关（`config.yaml` 零改动）；
- 零新依赖；不新增网络请求（还是起草那一趟）；两库零写入；
- 这段话是**静态**的——`card_memory=off` / `tone_align=off` 时它照旧在（它不挂在任何动态
  那一格上），但**动态那两格该空的还得空**（前几批那几条红线不许被这一段撑开）。

**口径如实记（够不着的地方不装作验过）**：模型被桩掉了，本模块钉的是"该教的阶段硬底画了
没有、跟既有那几段有没有打架"，**不是**"模型真能在破冰期收住"——那只能真机看。候选那几个字
是桩给的；本模块另有一段**撩词判据自证**，用真机那句当样本证明"判据抓得住"，那验的是判据、
不是模型。
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

from brain.prompts.templates import SOCIAL_DRAFT_PROMPT  # noqa: E402
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    AUTO_TRIGGER_TEXT,
    NO_EXAMPLES,
    SocialReplyService,
)

# 起草模板自己的换行符（模板文件是 CRLF，渲染出来的提示词里就是 \r\n）
SEP = "\r\n" if "\r\n" in SOCIAL_DRAFT_PROMPT else "\n"

# 本批新加的那几处，逐条钉住（判据就是这些字面本身）
NEW_FACES = (
    "关系阶段调速",
    "破冰期",
    "暧昧期",
    "不表达可得性",
    "揍我可以，轻点",
    "只轻松接住、不还撩",
    "不依赖卡",
)

# 桩模型的分流字面（`tests/harness.py::StubLLM.route_json`）：新段一个都不许带，
# 带了就把起草这一趟劫去别的线上（那是要红的事，不是风格问题）
ROUTE_MARKERS = (
    '"anxiety"', "time_confidence", '"rel_state"', '"key_points"', '"components"',
    '"profile"', '"skill_name"', '"matched"', '"confidence"', '"intent"', "memories",
)

# 「可得性 / 撩 / 推进关系」的字面判据（本模块自带的小词表，用来给真机那句做**判据自证**，
# 以及给两个场景的桩候选扫一遍）。**不是**给模型打分——模型是桩。
TEASE_MARKERS = (
    "揍我", "抱抱", "亲", "撩", "要不要来", "来拿", "给你机会", "女朋友", "男朋友",
    "宝贝", "小姐姐", "喜欢你", "心动",
)

# 两个场景（用户点名的两条）：破冰（互报名字 / 刚认识）/ 暧昧（私下昵称 + 情感话题）
CHAT_ICE = "茗风：我叫茗风，90 年的，老家山东\n我：我叫阿哲，也山东的"
CHAT_FLIRT = "茗风：阿哲~ 今天又想我了没 😏\n我：想啊"


def _looks_like_tease(text: str) -> bool:
    """这段候选里有没有可得性 / 撩 / 昵称类的字面（粗判，只做判据自证用）。"""
    return any(m in (text or "") for m in TEASE_MARKERS)


def main() -> int:
    # ---------- ① 新段真的在：标题 + 两个阶段 + 禁项 ----------

    section("① 阶段硬底进提示词：破冰期禁撩 / 暧昧期才准升温")

    check("这一段在（按它自己的标题认）", "关系阶段调速" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 说明它管的是「先判是哪一期，再决定这一轮能不能带」",
          "先判是哪一期" in SOCIAL_DRAFT_PROMPT
          and "再决定这一轮能不能带" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰期的判据在（初识 / 刚互报名字 / 互相问基本情况 / 互不了解）",
          "破冰期" in SOCIAL_DRAFT_PROMPT
          and "刚互报名字" in SOCIAL_DRAFT_PROMPT
          and "互相问基本情况" in SOCIAL_DRAFT_PROMPT
          and "互不了解" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰期**整条禁项**都在（不升温 / 不撩 / 不推进关系）",
          "不升温、不撩、不推进关系" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 点名禁**可得性表达**（不留「你想要就来拿」那种暗示）",
          "不表达可得性" in SOCIAL_DRAFT_PROMPT
          and "你想要就来拿" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 点名禁**性暗示 / 可得性意味的玩笑**，用的就是真机那句当例子",
          "揍我可以，轻点" in SOCIAL_DRAFT_PROMPT
          and "带性暗示或可得性意味的玩笑" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰期只许做的那一件事写清了（轻松同频接话 / 自然接梗 / 不越界）",
          "轻松同频接他的话、自然接梗、不越界" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰期不反问、也不自己另开一个往前推进的话题",
          "不自己另开一个往前推进的话题" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 暧昧期才允许适度升温 / 轻撩（判据：私下昵称 / 情感话题 / 明确亲近）",
          "暧昧期" in SOCIAL_DRAFT_PROMPT
          and "已经互有好感" in SOCIAL_DRAFT_PROMPT
          and "才允许适度升温、轻撩" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ② 阶段怎么认 + 认不出按破冰 ----------

    section("② 阶段从对话信号推断（不依赖卡），认不出按破冰处理")

    check("★ 明写**先从对话里推断**、不依赖攻略卡上的记录",
          "先从对话里推断，不依赖攻略卡上的记录" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰信号四样：互称名字 / 刚认识 / 互相问基本信息 / 话还浅",
          "互称名字" in SOCIAL_DRAFT_PROMPT
          and "刚认识" in SOCIAL_DRAFT_PROMPT
          and "互相问基本信息" in SOCIAL_DRAFT_PROMPT
          and "话还浅" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 暧昧信号三样：私下昵称 / 情感话题 / 明确的亲近表达",
          "私下昵称" in SOCIAL_DRAFT_PROMPT
          and "情感话题" in SOCIAL_DRAFT_PROMPT
          and "明确的亲近表达" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 认不出是哪一期时**按破冰处理**（宁可收着）",
          "按破冰处理" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 预留接口在：卡修好后优先读卡上那一格做更精细的调速",
          "优先读卡上那一格" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 预留那段时点**明写「那之前不依赖卡」**（本批刻意不被卡空 bug 阻塞）",
          "不依赖卡" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ③ 协同：不动 why / 不动语气；进退分寸协同 ----------

    section("③ 协同：why 解耦、语气自适应、进退分寸两关一起放行")

    check("★ 明说不动语气自适应那一格（对方口吻照旧决定节奏与热络度）",
          "不动" in SOCIAL_DRAFT_PROMPT
          and "语气自适应那一格" in SOCIAL_DRAFT_PROMPT
          and "节奏与热络度" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 明说不动解析那一格（策略解释照旧写在 why 里）",
          "策略解释照旧写在 why 里" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 与进退分寸协同：破冰期即便那格判成「可攻」也只轻松接住、不还撩",
          "只轻松接住、不还撩" in SOCIAL_DRAFT_PROMPT
          and "即便对方主动接梗、抛出钩子" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 两关一起放行才带（不是替换既有那几条，是加一道硬底）",
          "两关一起放行才带" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 这一段也是**底牌**：这些字一个字都不许出现在回话里",
          "这些字一个字都不许出现在回话里" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ④ 顺序：排在分档段之后、话题方向段之前 ----------

    section("④ 位置：接在分档段之后、话题方向段之前")

    def _pos(needle: str) -> int:
        """模板里这一句在第几个字符；找不到回 -1（不用 `.index`：那会抛异常把整支打成
        一次 traceback，做灵敏度探针时看不清是哪一条判据哑了）。"""
        return SOCIAL_DRAFT_PROMPT.find(needle)

    check("★ 新段排在「分档看上面那格」那一段**之后**",
          -1 < _pos("分档看上面那格") < _pos("关系阶段调速"), "")
    check("★ 新段排在「话题往哪个方向引」那一段**之前**",
          -1 < _pos("关系阶段调速") < _pos("话题往哪个方向引"), "")

    # ---------- ⑤ 渲染实测：这一段是静态的，两格该空的还得空 ----------

    box, stub, app = make_env()
    try:
        service: SocialReplyService = app.social_reply
        service._examples_provider = lambda query: ""
        stub.candidates_result = {"candidates": [{"draft": "行啊，你来定", "why": "接住对方"}]}

        section("⑤ 渲染实测：静态段在 on/off 都在，两格该空的还得空")

        app.config.screen.card_memory = "off"
        app.config.screen.tone_align = "off"
        service._session_id, service._peer_name = "S9", "甲"
        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, CHAT_ICE)
        off_prompt = stub.prompts[-1]
        baseline = SOCIAL_DRAFT_PROMPT.format(
            chat_context=CHAT_ICE,
            text=AUTO_TRIGGER_TEXT,
            examples=NO_EXAMPLES,
            peer_tone_hint="",
            peer_profile="",
        )
        check("★ 两格都关时渲染结果 == 占位填空串（一字不差）", off_prompt == baseline,
              f"{len(off_prompt)} 字 vs {len(baseline)} 字")
        check("★ 但这一段照旧在（它不挂在任何动态格上，是静态的）",
              "关系阶段调速" in off_prompt and "揍我可以，轻点" in off_prompt, "")
        check("★ 上一批那条红线没被撑开：状态块在这一趟里不参与（tone_align=off）",
              "对方现在这轮的进退分寸" not in off_prompt, "")
        check("★ 档案那一格也没被撑开（card_memory=off）",
              "我手上关于他的记录" not in off_prompt, "")

        # ---------- ⑥ 两个场景：破冰 / 暧昧 喂进去的料不一样 ----------

        section("⑥ 两个场景：破冰对话（互报名字）/ 暧昧对话（私下昵称 + 情感话题）")

        app.config.screen.card_memory = "auto"
        app.config.screen.tone_align = "off"
        service.set_peer_profile_provider(
            lambda peer: "对象：茗风；关系阶段：破冰" if "冰" in peer else "对象：茗风；关系阶段：暧昧")

        stub.prompts.clear()
        service._session_id, service._peer_name = "S1", "冰"
        service._write(AUTO_TRIGGER_TEXT, CHAT_ICE)
        ice_prompt = stub.prompts[-1]
        check("★ 破冰那一趟：对方的原话真的进了材料那格", CHAT_ICE in ice_prompt, "")
        check("★ 破冰那一趟：整段硬底都在（禁撩 / 禁可得性 / 禁揍我可以那类玩笑）",
              all(face in ice_prompt for face in NEW_FACES), "")
        check("★ 破冰那一趟：既有的第 1 条 / 第 2 条照旧在（没被新段挤掉）",
              "不许自己把话题升级" in ice_prompt
              and "就只回一句贴合他情绪的收尾" in ice_prompt, "")

        stub.prompts.clear()
        service._session_id, service._peer_name = "S2", "昧"
        service._write(AUTO_TRIGGER_TEXT, CHAT_FLIRT)
        flirt_prompt = stub.prompts[-1]
        check("★ 暧昧那一趟：也喂了对方那一句进去", CHAT_FLIRT in flirt_prompt, "")
        check("★ 暧昧那一趟：这一段也在（它不是某些场景才出现的）",
              "才允许适度升温、轻撩" in flirt_prompt, "")
        check("两个场景喂进去的提示词不是同一份（材料真的不一样）",
              ice_prompt != flirt_prompt, "")

        service.set_peer_profile_provider(lambda peer: "")
        app.config.screen.card_memory = "off"

        # ---------- ⑦ 撩词判据自证（判的是判据，不是模型） ----------

        section("⑦ 撩词判据自证：真机那句抓得住，普通接话不误伤")

        check("★ 真机那句「可爱得有点横。揍我可以，轻点」判据抓得住",
              _looks_like_tease("可爱得有点横。揍我可以，轻点"), "")
        check("★ 普通同频接话不误伤",
              not _looks_like_tease("今天天气不错，你那边呢"), "")
        check("★ 破冰期该做的那种轻松接梗不误伤",
              not _looks_like_tease("哈哈那部短剧我也在追，一集才三分钟"), "")

        # ---------- ⑧ 红线与既有钉子 ----------

        section("⑧ 红线：五格没多 / 静态段不带桩分流字面 / 两库零写入 / 零新依赖")

        fields = {f for _, f, _, _ in string.Formatter().parse(SOCIAL_DRAFT_PROMPT) if f}
        check("★ 占位符一个没多、一个没少（还是那五个）",
              fields == {"chat_context", "text", "examples", "peer_tone_hint", "peer_profile"},
              str(sorted(fields)))

        if ("关系阶段调速" in SOCIAL_DRAFT_PROMPT and "话题往哪个方向引" in SOCIAL_DRAFT_PROMPT):
            guard = SOCIAL_DRAFT_PROMPT[
                SOCIAL_DRAFT_PROMPT.index("关系阶段调速"):
                SOCIAL_DRAFT_PROMPT.index("话题往哪个方向引")
            ]
        else:
            guard = SOCIAL_DRAFT_PROMPT   # 段没了 → 整份都当"嫌疑段"，判据必红
        hit = [m for m in ROUTE_MARKERS if m in guard]
        check("★ 新段不带桩分流字面（不然起草这一趟会被劫去别的线）", not hit,
              "、".join(hit) or "（一处都没有）")

        check("★ 新段在模板里是**静态文本**（这一段里一个占位符都没有）",
              not any(f for _, f, _, _ in string.Formatter().parse(guard)), "")

        # —— 既有那几段的字面一个没动（别的模块钉着的，这里复查一遍）——
        check("★ 第 1 条 / 第 2 条照旧",
              "不许自己把话题升级" in SOCIAL_DRAFT_PROMPT
              and "就只回一句贴合他情绪的收尾" in SOCIAL_DRAFT_PROMPT
              and "不许反问" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 升温段照旧（v2-social-draft-escalate）",
              "怎么自然地把关系往前带" in SOCIAL_DRAFT_PROMPT
              and "抛共建性话题" in SOCIAL_DRAFT_PROMPT
              and "轻邀约暗示" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 分档段 / 方向段 / 进退段照旧（前三批）",
              "分档看上面那格" in SOCIAL_DRAFT_PROMPT
              and "话题往哪个方向引" in SOCIAL_DRAFT_PROMPT
              and "先看对方这轮给不给进" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 接缝没被撑开（`{peer_profile}只给` 还在，中间没插字）",
              "{peer_profile}只给" in SOCIAL_DRAFT_PROMPT
              and "{peer_profile}" + SEP not in SOCIAL_DRAFT_PROMPT, "")
        check("★ 方向仍排在进退分寸之后（上一批那条口径没被这一段顶掉）",
              -1 < SOCIAL_DRAFT_PROMPT.find("话题往哪个方向引")
              < SOCIAL_DRAFT_PROMPT.find("先看对方这轮给不给进"), "")

        src = inspect.getsource(smod)
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

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        app.config.screen.card_memory = "auto"
        service.set_peer_profile_provider(lambda peer: "关系阶段：破冰")
        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, CHAT_ICE)
        service._write(AUTO_TRIGGER_TEXT, "嗯")
        drain_threads()
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草·「关系阶段调速（破冰期禁撩）」（v2-social-stage-guard）")


if __name__ == "__main__":
    raise SystemExit(main())
