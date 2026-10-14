# -*- coding: utf-8 -*-
"""起草时**主动接话·不终结话题·留钩子**（v2-social-active-hook）。

**病根**（真机图 image_210905656.png + 用户原话"帮聊是帮用户去泡妞，调得主动一点"）：
对方说"刚洗完头发准备睡"，桌宠回"行，那你早点睡。头发不吹干小心头疼"——**话题终结者**。
根因是两处**过度收敛**叠加：`SOCIAL_EMOTION_STATE_BLOCK` 的「需守 → 维持同频」被模型读成
"收尾"，第 2 条"只回一句收尾、到此为止"又给了它干净收场的许可证。

本批把「需守」从"维持同频"改成**维持推进、留钩子**，并在正文补一段静态的
**「别把话聊死」**：不主动终结话题、对方释放结束信号也尊重但留尾巴、**主动接话 ≠ 撩**
（破冰仍禁撩，但不禁主动接话）。

**口径如实记**：模型被桩掉了。本模块钉的是**喂进去的那一段纪律**与**梗端得进去**
（材料在提示词里、纪律明写着"呼应底牌里真记着的梗"），**不是"模型真留了钩子"**——那只能真机看。
"""

from __future__ import annotations

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
from core.bootstrap import DesktopPetApp  # noqa: E402
from life.screen.social import (  # noqa: E402
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
)

# 起草模板自己的换行符（模板文件是 CRLF）
SEP = "\r\n" if "\r\n" in SOCIAL_DRAFT_PROMPT else "\n"

# 桩模型的分流字面（`tests/harness.py::StubLLM.route_json`）：新段一个都不许带
ROUTE_MARKERS = (
    '"anxiety"', "time_confidence", '"rel_state"', '"key_points"', '"components"',
    '"profile"', '"skill_name"', '"matched"', '"confidence"', '"intent"', "memories",
)

# 真机那一场：对方"准备睡"、卡上记着"打赌较劲"那个梗
PEER = "茗风"
GAG_LIKES = "被叫哥、打赌较劲"
GAG_INTERESTS = "短剧"
PEER_FIELDS = {
    "relation_stage": "破冰",
    "likes": GAG_LIKES,
    "interests": GAG_INTERESTS,
}
PEER_SLEEPY = "刚洗完头发准备睡"
PEER_ICE = "今天天气不错"


def _utterance_rows(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM utterance").fetchone()[0])
    finally:
        conn.close()


def _make_guide_card(app, profile: dict | None = None):
    return app.card_service.create_manual_card(
        CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL,
        profile=dict(profile or {}),
    )


def _slice(prompt: str, start: str, end: str) -> str:
    """切出 [start, end) 那一段；找不到就把整份当"嫌疑段"（判据必红）。"""
    if start in prompt and end in prompt and prompt.index(start) < prompt.index(end):
        return prompt[prompt.index(start):prompt.index(end)]
    return prompt


def main() -> int:
    # ---------- ① 静态段「别把话聊死」：位置 / 静态 / 不带桩分流字面 ----------

    section("① 「别把话聊死」这一段（静态、位置、分流字面）")

    check("★ 这一段在（按它自己的标题认）", "别把话聊死" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 教的是**不主动终结话题**（对方还在话里就别回成干净收尾）",
          "不主动终结话题" in SOCIAL_DRAFT_PROMPT
          and "接住他这一句之后抛个轻钩子" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 对方真释放结束信号（准备睡 / 先忙）也**尊重但留尾巴**（别让对话死）",
          "别硬拖、别死缠" in SOCIAL_DRAFT_PROMPT
          and "留个尾巴" in SOCIAL_DRAFT_PROMPT
          and "别让对话死在这儿" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 主动接话 ≠ 撩：破冰仍禁撩，但不禁主动接话",
          "主动接话 ≠ 撩" in SOCIAL_DRAFT_PROMPT
          and "破冰期照旧禁撩，但不禁止主动接话" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 钩子可呼应上文梗、但不许编（跟角色隔离 / 禁编造同一条纪律）",
          "可以呼应上面那格底牌里真记着的梗" in SOCIAL_DRAFT_PROMPT
          and "不许编" in SOCIAL_DRAFT_PROMPT, "")

    hook_block = _slice(SOCIAL_DRAFT_PROMPT, "别把话聊死", "要求（按重要性）")
    hit = [m for m in ROUTE_MARKERS if m in hook_block]
    check("★ 新段不带桩分流字面（不然起草这一趟会被劫去别的线）", not hit,
          "、".join(hit) or "（一处都没有）")
    check("★ 新段是**静态文本**（这一段里一个占位符都没有）",
          not any(f for _, f, _, _ in string.Formatter().parse(hook_block)), "")
    check("★ 位置：垫在「收尾留口子」桥段之后、「要求（按重要性）」之前（压在第 2 条收尾之上）",
          SOCIAL_DRAFT_PROMPT.index("别把话堵死")
          < SOCIAL_DRAFT_PROMPT.index("别把话聊死")
          < SOCIAL_DRAFT_PROMPT.index("要求（按重要性）"), "")

    # ---------- ② 状态块「需守」从"维持同频"改成"维持推进、留钩子" ----------

    section("② 「需守」不再是收尾：`SOCIAL_EMOTION_STATE_BLOCK` 补了留钩子")

    check("★ 三态词表照旧都在（该退 / 需守 / 可攻）",
          all(w in SOCIAL_EMOTION_STATE_BLOCK for w in ("该退", "需守", "可攻")), "")
    check("★ 「需守」现在教的是**维持推进、留个能续上的钩子**",
          "需守" in SOCIAL_EMOTION_STATE_BLOCK
          and "维持推进、留个能续上的钩子" in SOCIAL_EMOTION_STATE_BLOCK, "")
    check("★ 「需守」明说**别把话收死**（「行 / 好的」干净收尾是最容易犯的错）",
          "别把话收死" in SOCIAL_EMOTION_STATE_BLOCK
          and "干净收尾是这一格最容易犯的错" in SOCIAL_EMOTION_STATE_BLOCK, "")
    check("★ 那一格照旧是底牌、原文不许出现在回话里（纪律没松）",
          "这些原文一个字都不许出现在回话里" in SOCIAL_EMOTION_STATE_BLOCK, "")
    check("★ 该退 / 可攻两档的语义没动（改的只是需守）",
          "该退" in SOCIAL_EMOTION_STATE_BLOCK and "可攻" in SOCIAL_EMOTION_STATE_BLOCK
          and "先安抚 / 收尾 / 留白、别硬推" in SOCIAL_EMOTION_STATE_BLOCK
          and "才顺着升温段走" in SOCIAL_EMOTION_STATE_BLOCK, "")

    # ---------- ③ 端到端：对方"准备睡" + 卡记"打赌较劲" → 梗 + 钩子纪律都在 ----------

    box, stub, app = make_env()
    try:
        section("③ 端到端：对方「准备睡」+ 卡记「打赌较劲」→ 梗端得进、钩子纪律在")

        card = _make_guide_card(app, PEER_FIELDS)
        check("③ 前置：卡上「打赌较劲」那个梗建出来了", card is not None, repr(card))

        # `_peer_profile_for` 只在**这个人本来就有记录**时才端卡上那份（认不出的人回空串）。
        # 所以先往 peer 那一格落一条记录，卡上那几格才会跟着端出来。
        store = app.mind_store
        store.save_ep(rel_state="破冰", events=["说好赌一局，输的请奶茶"], peer=PEER)

        app.config.screen.tone_align = "off"      # 隔离状态块，只看静态段这一条线
        app.config.screen.card_memory = "auto"
        service = app.social_reply
        service.set_peer_profile_provider(app._peer_profile_for)
        service._session_id, service._peer_name = "S-HOOK", PEER

        stub.prompts.clear()
        service._write("回一句", PEER_SLEEPY)
        prompt = stub.prompts[-1]

        check("★ 那段纪律真的进了提示词（不是算了没用）", "别把话聊死" in prompt, "")
        check("★ 留尾巴那条也在（尊重但别让对话死）", "留个尾巴" in prompt, "")
        check("★ 那个梗端得进去（模型手上有料可呼应，不是空口留钩）",
              GAG_LIKES in prompt, "")
        check("★ 破冰禁撩那条硬底也在同一趟（主动 ≠ 撩）",
              "不表达可得性" in prompt and "破冰期照旧禁撩，但不禁止主动接话" in prompt, "")

        # 反向：对方释放结束信号那一场，纪律里"早点睡"只是反面例子，不是收尾指令——
        # 钉"别把话聊死 / 留个尾巴"在前，而不是钉"早点睡"不在（它本就被当成反面点名的字）。
        check("★ 「早点睡」在提示词里只是被当反面点名的字（纪律不让它当收尾）",
              "早点睡" in prompt and prompt.index("早点睡") > prompt.index("别把话聊死"), "")

        # ---------- ④ 端到端：破冰闲聊 → 反问/延伸 + 禁撩（无可得性撩） ----------

        section("④ 端到端：破冰闲聊 → 反问 / 延伸照旧、无可得性撩")

        service._peer_name = "乙"
        stub.prompts.clear()
        service._write("回一句", PEER_ICE)
        ice_prompt = stub.prompts[-1]
        check("★ 破冰闲聊那趟也有「主动接话 ≠ 撩」这一条", "主动接话 ≠ 撩" in ice_prompt, "")
        check("★ 破冰禁撩的硬底照旧在（不表达可得性 / 不油腻调侃）",
              "不表达可得性" in ice_prompt and "不油腻调侃" in ice_prompt, "")
        check("★ 破冰期照旧禁撩、但不禁止主动接话（这条把「撩」和「接话」切开）",
              "破冰期照旧禁撩，但不禁止主动接话" in ice_prompt, "")

        # ---------- ⑤ 状态块端到端：需守那一场也带上了"维持推进" ----------

        section("⑤ 状态块端到端：需守那一场，提示词里带「维持推进、留钩子」")

        app.config.screen.tone_align = "auto"
        service._peer_name = "丙"
        stub.prompts.clear()
        service._write("回一句", PEER_ICE)
        hold_prompt = stub.prompts[-1]
        check("★ 中性那一场 → 状态是「需守」", "进退分寸：需守" in hold_prompt, "")
        check("★ 「需守」这一档的指令里带着**维持推进、留钩子**（不是「维持同频」那种收尾味）",
              "维持推进、留个能续上的钩子" in hold_prompt
              and "别把话收死" in hold_prompt, "")

        # ---------- ⑥ 红线：两库零写入 / 占位符没多 / 破冰禁撩没冲掉 ----------

        section("⑥ 红线：两库零写入、零新依赖、占位符没多、破冰禁撩没冲掉")

        pet_before = _utterance_rows(app.config.db_path)
        mind_before = app.mind_store.fragment_count()

        for _ in range(3):
            app._peer_profile_for(PEER)
        drain_threads()

        check("★ 两库零写入：pet.db utterance 一行没多",
              _utterance_rows(app.config.db_path) == pet_before, "")
        check("★ 两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
        check("★ 占位符一个没多、一个没少（还是那五个）",
              {f for _, f, _, _ in string.Formatter().parse(SOCIAL_DRAFT_PROMPT) if f}
              == {"chat_context", "text", "examples", "peer_tone_hint", "peer_profile"}, "")
        check("★ 破冰禁撩那条硬底照旧在（本批没把它冲掉）",
              "关系阶段调速：破冰期一律不撩" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 跨会话接上文 / 禁编造那两段也照旧在（本批是追加，不是顶掉）",
              "跨会话也要接上文" in SOCIAL_DRAFT_PROMPT
              and "你自己那一份也不许编" in SOCIAL_DRAFT_PROMPT, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草·主动接话不终结话题（v2-social-active-hook）")


if __name__ == "__main__":
    raise SystemExit(main())
