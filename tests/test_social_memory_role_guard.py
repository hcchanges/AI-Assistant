# -*- coding: utf-8 -*-
"""起草时的**记忆角色隔离 + 禁编造**（v2-social-memory-role-guard）。

**病根**（真机图 image_674426949.png）：13 日茗风说"我老家桂林的"、用户回"我是茂名的"；
14 日对方问"你还不知道我住哪"，桌宠替用户回——**"桂林啊…我住象山区"**。两处都是错的：

1. **记忆不分角色**：桂林是**茗风**说的（他的老家），却被搬进"用户回复"里当**用户**的自述；
2. **凭空编造**："象山区"在库里出现 **0 次**——用户从没说过自己住在哪一区，是模型自己补的。

①的根因在写那一侧（`peer` 那一格按 OCR 出来的会话名落库，名字认错就串场），
**本批不动写那条线**（见 ⑧ 的红线钉子）；本批只在**喂料 + 纪律**上把这两处堵住：

| 改哪 | 干什么 |
| --- | --- |
| `life/screen/social.py::_peer_profile_block` | 那一行**带角色标注**：端出来的每一条都明说"都是他说过的，没有一条是你说过的" |
| `SOCIAL_PEER_PROFILE_BLOCK` | 补一段**角色纪律**：那格里的只能用来了解他 / 呼应他（主语是他），**绝不许挪成"我……"** |
| `SOCIAL_DRAFT_PROMPT` | 补一段静态的**「你自己那一份也不许编」**：用户没提过的自身信息（住址 / 工作 / 状态）一个字都不许替他补，没底就岔开或问回去 |

顺带把上一批 `v2-social-context-recall`（跨会话接上文）的料也钉在这里——那批没单独提交，
内容是并进本批的：`_peer_profile_for` 多带一句**共同经历**（`ep.events`）+ 一句**卡上记的**
（走唯一读端 `guide_profile_lines`），提示词里补「跨会话也要接上文」那段。

**口径如实记**：模型被桩掉了。本模块钉的是**喂进去的那一格**（带不带角色标注、桂林落在哪一段）
和**提示词里那几段纪律**，**不是"模型真照着守了"**——真守没守只能真机看。
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

from brain.prompts.templates import SOCIAL_DRAFT_PROMPT, SOCIAL_PEER_PROFILE_BLOCK  # noqa: E402
from core.bootstrap import (  # noqa: E402
    PEER_PROFILE_CLIP,
    PEER_PROFILE_EVENTS,
    DesktopPetApp,
)
from life.cards.chat import guide_profile_lines  # noqa: E402
from life.mind import peer_distill  # noqa: E402
from life.screen.social import (  # noqa: E402
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
    SocialReplyService,
)

# 起草模板自己的换行符（模板文件是 CRLF）
SEP = "\r\n" if "\r\n" in SOCIAL_DRAFT_PROMPT else "\n"

# 桩模型的分流字面（`tests/harness.py::StubLLM.route_json`）：新段一个都不许带
ROUTE_MARKERS = (
    '"anxiety"', "time_confidence", '"rel_state"', '"key_points"', '"components"',
    '"profile"', '"skill_name"', '"matched"', '"confidence"', '"intent"', "memories",
)

# 装配层那一行的角色标注（`social.py::_peer_profile_block` 里的字面）
ROLE_LABEL = "我手上关于他的记录（**都是他说过的**，没有一条是你说过的）："

# 真机那两个梗（图里就是这两样：短剧 + 打赌较劲）
GAG_INTERESTS = "短剧"
GAG_LIKES = "被叫哥、打赌较劲"
GAG_STRATEGY = "顺着短剧和打赌的梗接话，别急着认哥"
# 攻略卡上那几格（真机 #3 卡的值照抄）
PEER_FIELDS = {
    "relation_stage": "破冰",
    "likes": GAG_LIKES,
    "interests": GAG_INTERESTS,
    "strategy": GAG_STRATEGY,
}
# 共同经历（`ep_summary.events`）——只有这里写过、此前从没有人读
EVENTS = ("说好赌一局，输的请奶茶", "昨天推荐了一部短剧", "他提过周末想去爬山", "加微信第三天")

# 真机那一场串场：对方老家 / 用户老家 / 编出来的区名
PEER = "茗风"
PEER_HOMETOWN = "桂林"      # **对方**说的（他的老家）
OWNER_HOMETOWN = "茂名"     # **用户**说的（自己的老家）
PEER_FABRICATED = "象山区"  # 库里 0 次的编造
PEER_ASK = "你还不知道我住哪"  # 对方那一句
OWNER_LINE = "我是茂名的"      # 用户那句自述（在对话里，不在记忆里）


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
    """从提示词里切出 [start, end) 那一段；找不到那一段就把整份当"嫌疑段"（判据必红）。"""
    if start in prompt and end in prompt and prompt.index(start) < prompt.index(end):
        return prompt[prompt.index(start):prompt.index(end)]
    return prompt


def main() -> int:
    # ---------- ① 提示词：跨会话接上文那一段（静态、位置对、不带桩分流字面） ----------

    section("① 「跨会话也要接上文」这一段（静态、位置、分流字面）")

    check("★ 这一段在（按它自己的标题认）", "跨会话也要接上文" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 教的是「别当成一段没有上文的新对话」", "别把它当成一段没有上文的新对话" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 只接底牌里**真有**的（没写的、没编的一律不许接）",
          "只接底牌里真有的" in SOCIAL_DRAFT_PROMPT
          and "底牌上没写的事一律不许编" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 底牌空着就按普通寒暄接、**不硬扯**（不硬凹一个「上次」）",
          "就按普通寒暄接" in SOCIAL_DRAFT_PROMPT
          and "别硬扯、别硬凹一个" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 挑最近**一两件**就够（不是把底牌复述成清单）",
          "挑最近那一两件就够" in SOCIAL_DRAFT_PROMPT
          and "别把底牌整段复述成清单" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 破冰期联系上文用**轻松口吻**、点到为止（跟阶段调速协同）",
          "破冰期联系上文用轻松口吻" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 那段原文照旧不许出现在回话里（跟底牌那条同一条纪律）",
          "这些原文一个字都不许出现在回话里" in SOCIAL_DRAFT_PROMPT, "")

    recall_block = _slice(SOCIAL_DRAFT_PROMPT, "跨会话也要接上文", "你自己那一份也不许编")
    hit = [m for m in ROUTE_MARKERS if m in recall_block]
    check("★ 新段不带桩分流字面（不然起草这一趟会被劫去别的线）", not hit,
          "、".join(hit) or "（一处都没有）")
    check("★ 新段在模板里是**静态文本**（这一段里一个占位符都没有）",
          not any(f for _, f, _, _ in string.Formatter().parse(recall_block)), "")
    check("★ 位置：排在「分档」之后、「关系阶段调速」之前（先认识他，再决定能不能带）",
          SOCIAL_DRAFT_PROMPT.index("分档看的是那格里写的")
          < SOCIAL_DRAFT_PROMPT.index("跨会话也要接上文")
          < SOCIAL_DRAFT_PROMPT.index("关系阶段调速"), "")

    # ---------- ② 提示词：角色纪律（底牌那格）+ 自身信息禁编造 ----------

    section("② 「记忆分角色」两段纪律：那格讲的是他、你自己那一份也不许编")

    check("★ 底牌那格开头就写明**关于对方这个人**（角色归属的第一道标）",
          "关于**对方这个人**，我这边记着的是" in SOCIAL_PEER_PROFILE_BLOCK, "")
    check("★ 那格补了角色纪律：每一条**都是「他说过的」、没有一条是「你说过的」**",
          "都是「他说过的」，没有一条是「你说过的」" in SOCIAL_PEER_PROFILE_BLOCK, "")
    check("★ 那格补了用法：只用来**了解他、呼应他**，主语必须是他",
          "了解他、呼应他" in SOCIAL_PEER_PROFILE_BLOCK
          and "主语必须是他" in SOCIAL_PEER_PROFILE_BLOCK, "")
    check("★ 那格把最要命那种错说死了：**绝不许挪成「我……」当自述**",
          "绝不能搬进你自己那一封里当自述" in SOCIAL_PEER_PROFILE_BLOCK
          and "你的老家、住在哪、做什么工作" in SOCIAL_PEER_PROFILE_BLOCK, "")
    check("★ 分不清归属时的兜底口径：**就当是他的**（宁可不说）",
          "分不清是哪一头的，就当成他的" in SOCIAL_PEER_PROFILE_BLOCK, "")

    check("★ 起草模板里补了**「你自己那一份也不许编」**那一段",
          "你自己那一份也不许编" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 禁编造只认**对话里用户真说过的**",
          "用户没提过的事，一个字都不许替他补" in SOCIAL_DRAFT_PROMPT
          and "只有对话里用户真说过的才算数" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 禁的清单点到真机那几样（住址 / 老家 / 工作 / 状态）",
          "住址、老家、工作、年龄" in SOCIAL_DRAFT_PROMPT
          and "刚忙完" in SOCIAL_DRAFT_PROMPT and "在加班" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 没底时的口径：**宁可岔开、宁可问回去，也不许编**",
          "宁可岔开、宁可问回去，也不许编" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 也不许替用户许出去（没答应的约 / 没做过的承诺）",
          "也不许替用户编" in SOCIAL_DRAFT_PROMPT and "没做过的承诺" in SOCIAL_DRAFT_PROMPT, "")

    self_block = _slice(SOCIAL_DRAFT_PROMPT, "你自己那一份也不许编", "关系阶段调速")
    hit2 = [m for m in ROUTE_MARKERS if m in self_block]
    check("★ 禁编造那一段也不带桩分流字面", not hit2, "、".join(hit2) or "（一处都没有）")
    check("★ 禁编造那一段也是**静态文本**（一个占位符都没有）",
          not any(f for _, f, _, _ in string.Formatter().parse(self_block)), "")
    check("★ 位置：垫在「跨会话」之后、「关系阶段调速」之前（先有料，再说怎么用）",
          SOCIAL_DRAFT_PROMPT.index("跨会话也要接上文")
          < SOCIAL_DRAFT_PROMPT.index("你自己那一份也不许编")
          < SOCIAL_DRAFT_PROMPT.index("关系阶段调速"), "")

    check("★ 占位符一个没多、一个没少（还是那五个）",
          {f for _, f, _, _ in string.Formatter().parse(SOCIAL_DRAFT_PROMPT) if f}
          == {"chat_context", "text", "examples", "peer_tone_hint", "peer_profile"}, "")

    # 底牌那格是**插入**的文本（外面那层接缝不许动）：角色纪律必须垫在
    # "这些原文一个字都不许出现在回话里" 前面，结尾那两个换行还得在
    # ——`tests/test_social_card_inject.py` 钉的 `痕迹。\r\n\r\n只给 1 段` 就靠这个接缝。
    check("★ 接缝没动：角色纪律垫在「原文不许出现在回话里」**前面**",
          SOCIAL_PEER_PROFILE_BLOCK.index("都是「他说过的」")
          < SOCIAL_PEER_PROFILE_BLOCK.index("这些原文一个字都不许出现在回话里"), "")
    check("★ 接缝没动：那格结尾照旧自带两个换行（空串时渲染与加它之前一字不差）",
          SOCIAL_PEER_PROFILE_BLOCK.endswith(SEP * 2)
          and f"痕迹。{SEP}{SEP}" in SOCIAL_PEER_PROFILE_BLOCK, "")

    # ---------- ③ 装配层读得到「共同经历」（ep_summary.events 第一次有人读） ----------

    box, stub, app = make_env()
    try:
        section("③ 共同经历：`ep_summary.events` 写进去了，这次终于读得出来")

        store = app.mind_store
        store.save_ep(rel_state="刚加了微信", events=list(EVENTS), peer=PEER)

        assembled = app._peer_profile_for(PEER)
        check("★ 那一格里多了「共同经历：」这一项", "共同经历：" in assembled, assembled)
        check(f"★ 只带最近 {PEER_PROFILE_EVENTS} 条（不是把这一格全端出来）",
              EVENTS[-1] in assembled and EVENTS[-2] in assembled
              and EVENTS[0] not in assembled and EVENTS[1] not in assembled,
              assembled)
        check("★ 关系总述照旧在（这一批是**追加**，不是顶掉）", "眼下关系：" in assembled, "")

        long_event = "他" + "非" * (PEER_PROFILE_CLIP + 20)
        store.save_ep(rel_state="刚加了微信", events=[long_event], peer=PEER)
        clipped = app._peer_profile_for(PEER)
        check(f"★ 超长的一条共同经历截到 {PEER_PROFILE_CLIP} 字（背景料不该把提示词撑长）",
              long_event[:PEER_PROFILE_CLIP] in clipped and long_event not in clipped, "")

        # ---------- ④ 装配层读得到攻略卡那几格（起草这条路的新读端） ----------

        section("④ 卡上记的：`interests` / `likes` / `strategy` 只在卡上有，这次读得出来")

        before_card = app._peer_profile_for(PEER)
        check("★ 卡不存在 → 「卡上记的：」不出现（不摆空壳，也不替谁建一张）",
              "卡上记的：" not in before_card, before_card)

        blank = _make_guide_card(app)
        check("④ 前置：八格全空的攻略卡建出来了", blank is not None, repr(blank))
        check("★ 八格全空的攻略卡也不加这一段（不摆一个空壳表头）",
              "卡上记的：" not in app._peer_profile_for(PEER), "")

        app.card_service.set_profile(blank.id, **PEER_FIELDS)
        guide = app.card_service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE)

        after_card = app._peer_profile_for(PEER)
        check("★ 卡上补了那几格之后 → 「卡上记的：」出来了", "卡上记的：" in after_card, after_card)
        check("★ 真机那两个梗照原样端出来（短剧 / 打赌较劲）",
              f"兴趣：{GAG_INTERESTS}" in after_card and f"喜好：{GAG_LIKES}" in after_card,
              after_card)
        check("★ 关系阶段也在（阶段调速那条线要读的就是它）",
              "关系阶段：破冰" in after_card, after_card)
        check("★ 与卡窗 / 卡面**共用同一个读端**（这里端的字就是 `guide_profile_lines` 那一批）",
              all(line in after_card for line in guide_profile_lines(guide)), "")
        check("★ 空的那几格一个都没冒出来（没编、没补空行）",
              "禁忌：" not in after_card and "烦心事：" not in after_card, after_card)

        # ---------- ⑤ 端到端·串场那一场：桂林是**他的**，不许当用户自述 ----------

        section("⑤ 串场复现：卡 / 记忆里记着「对方老家=桂林」，他问「你住哪」")

        # 真机那一份料的落点：`intro` + `fragment`（`peer_distill._write` 走的就是这两手）
        store.save_profile(peer=PEER, intro=f"{PEER_HOMETOWN}人，90年生，三十出头")
        store.add_fragment(f"老家在{PEER_HOMETOWN}，可聊家乡话题", [], peer=PEER)

        app.config.screen.tone_align = "off"
        app.config.screen.card_memory = "auto"
        service = app.social_reply
        service.set_peer_profile_provider(app._peer_profile_for)
        service._session_id, service._peer_name = "S-ROLE", PEER

        # 对话窗里：用户自己说过"我是茂名的"，对方这一句问"你还不知道我住哪"
        chat_context = f"我：{OWNER_LINE}\n他：{PEER_ASK}"
        stub.prompts.clear()
        service._write("回一句", chat_context)
        prompt = stub.prompts[-1]

        check("★ 那一格进了提示词（不是算了没用）", ROLE_LABEL in prompt, "")
        check("★ 角色标注就在那一行上：**都是他说过的、没有一条是你说过的**",
              "都是他说过的" in prompt and "没有一条是你说过的" in prompt, "")
        check("★ 那一行原地明写了这是**他的记录**（第一人称的「我手上关于他」）",
              "我手上关于他的记录" in prompt, "")

        # 关键那一条：桂林**只能**出现在那行标注之后（= 被标成"他的"那一段里）
        check(f"★ {PEER_HOMETOWN} 端进来了（真有的料，接住他对）",
              PEER_HOMETOWN in prompt, "")
        check(f"★ {PEER_HOMETOWN} **只出现在角色标注之后**（在那之前=底牌外面，等于当自述喂）",
              prompt.index(ROLE_LABEL) < prompt.index(PEER_HOMETOWN)
              and PEER_HOMETOWN not in prompt[:prompt.index(ROLE_LABEL)], "")
        check("★ 标注之后就跟着那句纪律：主语是**他**、绝不许挪成「我……」",
              "主语必须是他" in prompt and "绝不能搬进你自己那一封里当自述" in prompt, "")
        check("★ 禁编造那段也在**同一趟**请求里（模型被明说过不许替用户补）",
              "你自己那一份也不许编" in prompt, "")
        # 「象山区」这几个字**会**出现在提示词里——但只准出现在禁编造那段，被当成反面例子点名
        # （"我住象山区"正是那段明写不许冒出来的一句）。要钉的是它**不是从料里喂进来的**：
        # 画像 / 碎片 / 卡 / 共同经历，哪一格都不该有它，模型没有"事实"可抄。
        check(f"★ 编造出来的「{PEER_FABRICATED}」不是从料里来的（画像/碎片/卡/共同经历一格都没有）",
              PEER_FABRICATED not in app._peer_profile_for(PEER), app._peer_profile_for(PEER))
        check(f"★ 提示词里「{PEER_FABRICATED}」只出现在禁编造那段之前的位置之后"
              "（= 被当成反面例子点名，不是当事实喂）",
              PEER_FABRICATED in prompt
              and PEER_FABRICATED not in prompt[:prompt.index("你自己那一份也不许编")], "")
        check("★ 用户自己在对话里说的老家照旧喂在对话那格里（自述走对话，不走底牌）",
              OWNER_HOMETOWN in prompt, "")
        check("★ 破冰禁撩那条硬底也在同一趟里（角色纪律没把它挤掉）",
              "关系阶段调速：破冰期一律不撩" in prompt, "")

        # 装配层源码那一行：角色标注是**写死在那行里**的，不是碰巧端进来的料
        block_src = inspect.getsource(SocialReplyService._peer_profile_block)
        check("★ 角色标注是装配层那行**写死**的（料里带不带都这么说）",
              "都是他说过的" in block_src and "没有一条是你说过的" in block_src, "")
        check("★ 装配层没多喂任何东西（还是同一个 profile 字符串，只是换了那句话引子）",
              "{profile}" in block_src, "")

        # ---------- ⑥ 端到端·跨天寒暄：昨天那两个梗端得进去 ----------

        section("⑥ 跨天新会话只发「晚上好」→ 昨天那两个梗端得进去")

        service._session_id = "S-NEW"
        stub.prompts.clear()
        service._write("回一句", "晚上好")
        prompt2 = stub.prompts[-1]
        check(f"★ 隔天只发一句寒暄，昨天那两个梗端得进去（{GAG_INTERESTS} / 打赌）",
              GAG_INTERESTS in prompt2 and "打赌" in prompt2, "")
        check("★ 共同经历也在同一趟里（两样合成一句）", "共同经历：" in prompt2, "")

        # ---------- ⑦ 反向对照：手上没有历史 → 一个字都不硬扯 ----------

        section("⑦ 反向对照：手上没有历史 → 没有角色标注、也没有梗")

        bare_box, bare_stub, bare = make_env()
        try:
            bare.config.screen.tone_align = "off"
            bare.config.screen.card_memory = "auto"
            bare_service = bare.social_reply
            bare_service.set_peer_profile_provider(bare._peer_profile_for)
            bare_service._session_id, bare_service._peer_name = "S-EMPTY", PEER
            bare_stub.prompts.clear()
            bare_service._write("回一句", "晚上好")
            bare_prompt = bare_stub.prompts[-1]
            check("★ 没这个人 / 没建过卡 → 那一格整格不填",
                  "我手上关于他的记录" not in bare_prompt, "")
            check("★ 没有共同经历、没有卡上那几格（不是摆了个空壳）",
                  "共同经历：" not in bare_prompt and "卡上记的：" not in bare_prompt, "")
            check("★ 不硬扯：提示词里没有凭空冒出来的梗（底牌那两样一个都没冒）",
                  "被叫哥" not in bare_prompt and "打赌较劲" not in bare_prompt, "")
            check("★ 那几段纪律照旧在（没料也照说「没有就别编」）",
                  "跨会话也要接上文" in bare_prompt
                  and "你自己那一份也不许编" in bare_prompt, "")
        finally:
            drain_threads()
            bare_box.cleanup()

        # ---------- ⑧ 红线：纯读 / 两库零写入 / 零新依赖 / 写线没动 ----------

        section("⑧ 红线：纯读（两库零写入）、零新依赖、写那条线一个字没动")

        pet_before = _utterance_rows(app.config.db_path)
        mind_before = app.mind_store.fragment_count()
        ep_before = dict(app.mind_store.ep(peer=PEER))
        profile_before = dict(app.mind_store.profile(peer=PEER))
        card_before = app.card_service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE)
        fields_before = {key: getattr(card_before, key, None) for key in PEER_FIELDS}

        for _ in range(3):
            app._peer_profile_for(PEER)
        drain_threads()

        check("★ 两库零写入：pet.db utterance 一行没多",
              _utterance_rows(app.config.db_path) == pet_before, "")
        check("★ 两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
        check("★ 读端不写记忆：共同经历那一格读前读后逐字相同",
              dict(app.mind_store.ep(peer=PEER)) == ep_before, "")
        check("★ 读端不写记忆：画像那一格读前读后逐字相同",
              dict(app.mind_store.profile(peer=PEER)) == profile_before, "")
        card_after = app.card_service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE)
        check("★ 读端不写卡：卡上那几格读前读后逐字相同",
              all(getattr(card_after, k, None) == v for k, v in fields_before.items()), "")

        check("★ 认不出的人仍回空串（没这个人就没有格，不拿别人那一档顶）",
              app._peer_profile_for("查无此人") == "", repr(app._peer_profile_for("查无此人")))
        check("★ 空名字直接回空串（没有名字就没有格，不拿主人那一档顶）",
              app._peer_profile_for("") == "" and app._peer_profile_for("   ") == "", "")
        check("★ 用户那一档（主人自己的记忆）**不许**从这儿进提示词"
              "——本批走的是「给对方的料贴角色标」，不是「把主人那档也端出来」",
              all(tok not in assembled for tok in ("桌宠的主人", "主人自己那一档的碎片")), "")

        src = inspect.getsource(DesktopPetApp._guide_card_bits)
        check("★ 卡那一份走的是**既有的唯一读端**（不另拼一套）",
              "guide_profile_lines" in src and "find_card" in src, "")
        check("★ 那只手是**纯读**（源码里没有 save / set_profile / add_fragment 这类写口）",
              not any(word in src for word in ("save_profile(", "save_ep(", "add_fragment(",
                                               ".set_profile(", "commit")), "")

        write_src = inspect.getsource(peer_distill.PeerDistiller._write)
        check("★ 写那条线一个字没动（抽取照旧写两处：三记忆库 + 卡片）",
              "save_ep(" in write_src and "add_fragment(" in write_src
              and "save_profile(" in write_src, "")
        check("★ 抽取字段表一个字没动（`peer_distill.CARD_FIELDS`）",
              peer_distill.CARD_FIELDS == (
                  "age", "emotion_state", "anxiety", "interests",
                  "likes", "dislikes", "strategy", "relation_stage",
              ), str(peer_distill.CARD_FIELDS))

        check("★ 破冰禁撩那条硬底照旧在（本批没把它冲掉）",
              "关系阶段调速：破冰期一律不撩" in SOCIAL_DRAFT_PROMPT, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草·记忆分角色 + 禁编造（v2-social-memory-role-guard）")


if __name__ == "__main__":
    raise SystemExit(main())
