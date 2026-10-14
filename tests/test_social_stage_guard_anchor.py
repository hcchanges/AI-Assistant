# -*- coding: utf-8 -*-
"""社交起草·「阶段边界锚点补强」（v2-social-stage-guard-anchor）的自动测试。

**病根**：`v2-social-stage-guard`（§199）已有"破冰禁撩 / 暧昧可升温"的静态规则，但缺**具体
表达锚点**——破冰期那条只举了"揍我可以，轻点"一个反面样例，真机那句里的另一个字眼
**"可爱得有点横"**没被点名；暧昧期那条只笼统说"才允许适度升温、轻撩"，**没明说"揍我可以
轻点 / 可爱得有点横 这些到了暧昧期才算解禁"**。于是模型在破冰期还是可能犯，在暧昧期又
不敢放开。

本批只**补 prompt 锚点**（不动逻辑）：破冰期反面样例补齐两样（"揍我可以，轻点""可爱得
有点横"），并明写**这两句在破冰期一律不许出现在回复里**；暧昧期明写**这两句到了这一期
才解禁、可作轻撩，但仍守机智不舔、不油腻**。阶段判定仍从对话信号。

**口径如实记**：模型被桩掉了，本模块钉的是**提示词里那两个锚点画没画、位置对不对**，
不是"模型真能在破冰期收住 / 在暧昧期放开"——那只能真机看。
"""

from __future__ import annotations

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
from life.screen.social import AUTO_TRIGGER_TEXT, NO_EXAMPLES, SocialReplyService  # noqa: E402

# 起草模板自己的换行符（模板文件是 CRLF）
SEP = "\r\n" if "\r\n" in SOCIAL_DRAFT_PROMPT else "\n"

# 两个反面样例（真机那句里的两个具体字眼）
NEG1 = "揍我可以，轻点"
NEG2 = "可爱得有点横"

# 桩模型的分流字面：新补的锚点一个都不许带
ROUTE_MARKERS = (
    '"anxiety"', "time_confidence", '"rel_state"', '"key_points"', '"components"',
    '"profile"', '"skill_name"', '"matched"', '"confidence"', '"intent"', "memories",
)

# 破冰 / 暧昧两个场景（沿用 stage-guard 的口径）
CHAT_ICE = "茗风：我叫茗风，90 年的，老家山东\n我：我叫阿哲，也山东的"
CHAT_FLIRT = "茗风：阿哲~ 今天又想我了没 😏\n我：想啊"


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    # ---------- ① 破冰期锚点：两个反面样例都点名，且明写"一律不许" ----------

    section("① 破冰期锚点：两个反面样例都点名，且明写一律不许")

    check(f"★ 第一个反面样例「{NEG1}」照旧在（没被顶掉）", NEG1 in SOCIAL_DRAFT_PROMPT, "")
    check(f"★ 第二个反面样例「{NEG2}」补进来了（真机那句里的另一个字眼）",
          NEG2 in SOCIAL_DRAFT_PROMPT, "")
    check("★ 这两样并成同一类：**带性暗示或可得性意味的玩笑**",
          "带性暗示或可得性意味的玩笑" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰期明写：这两句**一律不许出现在回复里**（不是「少说」，是「一个字都不行」）",
          "在破冰期一律不许出现在回复里" in SOCIAL_DRAFT_PROMPT
          and "一个字都不行" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰期照旧是「不升温、不撩、不推进关系」那一整条",
          "不升温、不撩、不推进关系" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ② 暧昧期锚点：这两句到了暧昧期才解禁、可作轻撩、仍守不舔 ----------

    section("② 暧昧期锚点：这两句解禁可轻撩，但仍守机智不舔 / 不油腻")

    check("★ 暧昧期明写：上面那两句**到了这一期才解禁**",
          "才解禁" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 暧昧期明写：这两句**可以作为轻撩用**",
          "可以作为轻撩用" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 但解禁不是放开：仍守「机智不舔」、不油腻、不连珠炮追问",
          "机智不舔" in SOCIAL_DRAFT_PROMPT
          and "不油腻" in SOCIAL_DRAFT_PROMPT
          and "不连珠炮追问" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 暧昧期照旧是「才允许适度升温、轻撩」那一句",
          "才允许适度升温、轻撩" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ③ 阶段判定照旧从对话信号 ----------

    section("③ 阶段判定照旧从对话信号（互称名字 = 破冰 / 私下昵称 = 暧昧）")

    check("★ 破冰信号在（互称名字 / 刚认识 / 互相问基本信息 / 话还浅）",
          "互称名字" in SOCIAL_DRAFT_PROMPT and "话还浅" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 暧昧信号在（私下昵称 / 情感话题 / 明确的亲近表达）",
          "私下昵称" in SOCIAL_DRAFT_PROMPT and "明确的亲近表达" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 认不出按破冰处理（宁可收着）", "按破冰处理" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ④ 位置 / 静态 / 分流字面 ----------

    section("④ 位置（在 stage-guard 段内）、静态、不带桩分流字面")

    def _pos(needle: str) -> int:
        return SOCIAL_DRAFT_PROMPT.find(needle)

    check("★ 新锚点在「关系阶段调速」段之内（破冰锚点 < 暧昧锚点 < 方向段）",
          -1 < _pos("关系阶段调速") < _pos(NEG2) < _pos("才解禁")
          < _pos("话题往哪个方向引"), "")
    check("★ 破冰锚点「可爱得有点横」排在破冰期那一条里（在「暧昧期」之前）",
          -1 < _pos("破冰期") < _pos(NEG2) < _pos("暧昧期"), "")
    check("★ 暧昧锚点「才解禁」排在暧昧期那一条里（在「暧昧期」之后）",
          -1 < _pos("暧昧期") < _pos("才解禁"), "")

    if "关系阶段调速" in SOCIAL_DRAFT_PROMPT and "话题往哪个方向引" in SOCIAL_DRAFT_PROMPT:
        guard = SOCIAL_DRAFT_PROMPT[
            SOCIAL_DRAFT_PROMPT.index("关系阶段调速"):
            SOCIAL_DRAFT_PROMPT.index("话题往哪个方向引")
        ]
    else:
        guard = SOCIAL_DRAFT_PROMPT
    hit = [m for m in ROUTE_MARKERS if m in guard]
    check("★ 补强的这一段不带桩分流字面（不然起草这一趟会被劫去别的线）", not hit,
          "、".join(hit) or "（一处都没有）")
    check("★ 补强的这一段是**静态文本**（一个占位符都没有）",
          not any(f for _, f, _, _ in string.Formatter().parse(guard)), "")
    check("★ 占位符一个没多、一个没少（还是那五个）",
          {f for _, f, _, _ in string.Formatter().parse(SOCIAL_DRAFT_PROMPT) if f}
          == {"chat_context", "text", "examples", "peer_tone_hint", "peer_profile"}, "")

    # ---------- ⑤ 渲染实测：破冰 / 暧昧两个场景，锚点都在，材料不一样 ----------

    box, stub, app = make_env()
    try:
        section("⑤ 渲染实测：破冰对话 / 暧昧对话，锚点都在、材料不一样")

        service: SocialReplyService = app.social_reply
        service._examples_provider = lambda query: ""
        app.config.screen.card_memory = "auto"
        app.config.screen.tone_align = "off"
        service.set_peer_profile_provider(
            lambda peer: "对象：茗风；关系阶段：破冰" if "冰" in peer else "对象：茗风；关系阶段：暧昧")

        stub.prompts.clear()
        service._session_id, service._peer_name = "S1", "冰"
        service._write(AUTO_TRIGGER_TEXT, CHAT_ICE)
        ice_prompt = stub.prompts[-1]
        check("★ 破冰那一趟：两个反面样例 + 一律不许，都在提示词里",
              NEG1 in ice_prompt and NEG2 in ice_prompt
              and "在破冰期一律不许出现在回复里" in ice_prompt, "")
        check("★ 破冰那一趟：也不缺「解禁」那句（阶段段是静态的，两期都写全）",
              "才解禁" in ice_prompt, "")

        stub.prompts.clear()
        service._session_id, service._peer_name = "S2", "昧"
        service._write(AUTO_TRIGGER_TEXT, CHAT_FLIRT)
        flirt_prompt = stub.prompts[-1]
        check("★ 暧昧那一趟：解禁 / 轻撩 / 机智不舔 都在",
              "才解禁" in flirt_prompt and "可以作为轻撩用" in flirt_prompt
              and "机智不舔" in flirt_prompt, "")
        check("★ 两个场景喂进去的材料不一样（阶段段照旧按对象名端不同的料）",
              ice_prompt != flirt_prompt, "")

        # ---------- ⑥ 红线：两库零写入 / 既有钉子没被顶掉 ----------

        section("⑥ 红线：两库零写入、既有那几段字面没被顶掉")

        check("★ 第 1 条 / 第 2 条照旧",
              "不许自己把话题升级" in SOCIAL_DRAFT_PROMPT
              and "就只回一句贴合他情绪的收尾" in SOCIAL_DRAFT_PROMPT
              and "不许反问" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 升温段照旧（v2-social-draft-escalate）",
              "怎么自然地把关系往前带" in SOCIAL_DRAFT_PROMPT
              and "抛共建性话题" in SOCIAL_DRAFT_PROMPT
              and "轻邀约暗示" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 上两批的字面照旧（跨会话 / 禁编造 / 别把话聊死）",
              "跨会话也要接上文" in SOCIAL_DRAFT_PROMPT
              and "你自己那一份也不许编" in SOCIAL_DRAFT_PROMPT
              and "别把话聊死" in SOCIAL_DRAFT_PROMPT, "")

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        service.set_peer_profile_provider(lambda peer: "关系阶段：破冰")
        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, CHAT_ICE)
        service._write(AUTO_TRIGGER_TEXT, "嗯")
        drain_threads()
        check("★ 两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("★ 两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草·「阶段边界锚点补强」（v2-social-stage-guard-anchor）")


if __name__ == "__main__":
    raise SystemExit(main())
