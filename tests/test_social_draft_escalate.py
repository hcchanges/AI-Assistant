"""
社交起草·「自然升温」策略段（v2-social-draft-escalate）的自动测试。

这一批治的是用户点名的一条真事：候选**总是同频接话就收工**——接得稳，但**关系不往前走**
（上一版的表述是"不升级、把话题轻轻合上"，落到真机上就是回复乏闷、关系停滞）。补的是
"对方给了料的时候，顺着眼前这个话题把关系往前带一档"：

- **适度调侃 / 反撩**：他逗一句，接住再回一句更俏的（接的是那句话的劲儿，不是岔开话题）；
- **抛共建性话题**：把眼前这件事说成"一起"的由头（他在刷短剧 → "那部我也在追，下次一起看？"）；
- **轻邀约暗示**：邀约不必正式、留个口子（"改天带你去那家店尝尝"），不是下通知；
- **一档一档来**：这一轮比上一轮近一点就够。

**三个必须一起绕开的坑**（这一批的取舍全在这儿）：

① **「不升级」≠「不升温」。** 第 1 条那句"不许自己把话题升级"防的是**跳话题**（从吃饭直接
   跳到表白、谈未来、质问），不是"别推进"。这一批在第 1 条后面补了一句把两件事分开
   （**原字样一个没删**，`tests/test_social_ocr_catch.py` 钉的那句照旧在），新段再从正面讲
   怎么升温。
② 新段跟第 2 条（对方只收情绪、没抛新话题时一句收尾、不许反问 / 质疑 / 另开话题）看着是
   打架的。所以新段写在「要求（按重要性）」**之前**，段里明说"冲突时以下面为准、那三样一个
   都不用"，并补一句"**收尾也可以留个口子**"（点第 2 条那则样板当例子——它本身就是"收了尾
   又留了下一个口"）。第 2 条是真机复盘修出来的硬规则（`v2-social-closing`），不许被稀释。
③ **升温要分档、要自然。** 分档看攻略卡那格（`{peer_profile}`）写的**关系阶段**和对方这一
   句的态度：可推进的阶段（暧昧 / 聊得来 / 处着）才带升温意图，刚认识 / 破冰 / 对方保守就
   **收一档**；节奏顺着他来，不硬撩、不突兀、不脱离语境、不油腻、不越界。

**不动的地方**（也是这一批的红线）：不新增 `{}` 占位符（既有测试拿
`.format(chat_context=, text=, examples=, peer_tone_hint=, peer_profile=)` 逐字比渲染结果，
多一格就是 KeyError）；`SOCIAL_DRAFT_SYSTEM` / 调用点 / 材料那几格 / `why` 解耦 / 语气自适应
一个字没动；`config.yaml` 零改动（这一批没有新开关）；**不新增任何网络请求**。

**模型是桩**：所以这里验的是"该教的策略有没有进提示词、有没有跟既有硬规则打架、两种场景
喂给模型的那份提示词对不对、接缝有没有被撑开"，**不是**"模型真能把关系往前带"。候选那几个字
是桩给的，真行为只能靠真机看——这一条如实记在这儿，别把这一批读成"升温已生效"。
"""

from __future__ import annotations

import ast
import inspect
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, section, summary  # noqa: E402

from brain.prompts import templates as tmod  # noqa: E402
from brain.prompts.templates import (  # noqa: E402
    SOCIAL_DRAFT_PROMPT,
    SOCIAL_DRAFT_SYSTEM,
)
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    NO_CHAT_CONTEXT,
    NO_EXAMPLES,
    SocialReplyService,
)

# 起草模板自己的换行符（模板文件是 CRLF，渲染出来的提示词里就是 \r\n）
SEP = "\r\n" if "\r\n" in SOCIAL_DRAFT_PROMPT else "\n"

# 这一批新加的那几处，逐条钉住（判据就是这些字面本身）
NEW_FACES = (
    "怎么自然地把关系往前带",
    "别把话题合上就停",
    "适度调侃 / 反撩",
    "抛共建性话题",
    "轻邀约暗示",
    "一档一档来",
    "收一档",
    "有来有回",
)

# 桩模型的分流字面（`tests/harness.py::StubLLM.route_json`）：新段一个都不许带，
# 带了就把起草这一趟劫去别的线上（那是要红的事，不是风格问题）
ROUTE_MARKERS = (
    '"anxiety"', "time_confidence", '"rel_state"', '"key_points"', '"components"',
    '"profile"', '"skill_name"', '"matched"', '"confidence"', '"intent"', "memories",
)

# 两个场景（用户点名的两条）：破冰闲聊（有料 → 该往前带）/ 对方保守（没料 → 该收一档）
CHAT_BREAKING = "茗风：最近在追一部短剧，一集才三分钟，看得停不下来"
CHAT_GUARDED = "茗风：嗯嗯，还好吧，最近都那样"


def main() -> int:
    box, stub, app = make_env()
    try:
        # ---------- ① 新段真的在提示词里 ----------

        section("① 新段进提示词：自然升温那几样")

        check("这一段在（按它自己的标题认）", "怎么自然地把关系往前带" in SOCIAL_DRAFT_PROMPT, "")
        check("总纲：接住是最低要求、**别把话题合上就停**（这正是上一版的病）",
              "接住" in SOCIAL_DRAFT_PROMPT and "别把话题合上就停" in SOCIAL_DRAFT_PROMPT, "")
        check("升温要有由头：顺着眼前这个话题、把关系往前带一档",
              "顺着眼前这个话题" in SOCIAL_DRAFT_PROMPT
              and "把关系往前带一档" in SOCIAL_DRAFT_PROMPT, "")
        check("递法一：适度调侃 / 反撩（接的是他那句话的劲儿，不是岔开话题）",
              "适度调侃 / 反撩" in SOCIAL_DRAFT_PROMPT
              and "接的是他那句话的劲儿" in SOCIAL_DRAFT_PROMPT
              and "不是把话题岔开" in SOCIAL_DRAFT_PROMPT, "")
        check("递法二：抛共建性话题（以短剧为例，把这件事说成「一起」的由头）",
              "抛共建性话题" in SOCIAL_DRAFT_PROMPT
              and "刷短剧" in SOCIAL_DRAFT_PROMPT
              and "下次一起" in SOCIAL_DRAFT_PROMPT, "")
        check("共建不是替他安排：说「咱们」，别把时间地点替他定死",
              "共建不是替他安排" in SOCIAL_DRAFT_PROMPT
              and "别把时间地点替他定死" in SOCIAL_DRAFT_PROMPT, "")
        check("递法三：轻邀约暗示，留口子而不是下通知（别给他必须答应的压力）",
              "轻邀约暗示" in SOCIAL_DRAFT_PROMPT
              and "留口子而不是下\n  通知" in SOCIAL_DRAFT_PROMPT
              and "必须答应" in SOCIAL_DRAFT_PROMPT, "")
        check("一档一档来：这一轮比上一轮近一点就够，不是一句话跨一大步",
              "一档一档来" in SOCIAL_DRAFT_PROMPT
              and "一句话就要跨一大步" in SOCIAL_DRAFT_PROMPT, "")

        # ---------- ② 分档：可推进才升温，保守就收一档 ----------

        section("② 分档：看攻略卡那格的关系阶段 + 他这一句的态度")

        check("明说要**分档看**攻略卡那格（「关于对方这个人，我这边记着的是」）",
              "分档看上面那格" in SOCIAL_DRAFT_PROMPT
              and "关于对方这个人" in SOCIAL_DRAFT_PROMPT, "")
        check("可推进的阶段（暧昧 / 聊得来 / 处着）→ 带一点升温的意思",
              "可\n推进的阶段" in SOCIAL_DRAFT_PROMPT
              and "暧昧" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 刚认识 / 破冰、或对方保守（只礼貌应答 / 只回短句 / 往回收）→ **收一档**",
              "刚认识 / 破冰" in SOCIAL_DRAFT_PROMPT
              and "他保守" in SOCIAL_DRAFT_PROMPT
              and "收一档" in SOCIAL_DRAFT_PROMPT
              and "别硬撩" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 分档的依据是「那格里写的和他这一句说的」，不是「你想推进多少」",
              "不是你想推进多少" in SOCIAL_DRAFT_PROMPT, "")
        check("那格仍是**底牌**：用来决定怎么接，原文一个字都不许出现在回话里",
              "那格是**底牌**" in SOCIAL_DRAFT_PROMPT
              and "原文一个字都不许出现在回话里" in SOCIAL_DRAFT_PROMPT, "")

        # ---------- ③ 自然不油腻 + 边界 ----------

        section("③ 升温要自然：不硬撩、不油腻、不越界")

        check("核心口径：升温得**顺着对方的节奏和话题长出来**",
              "顺着对方的节奏和话题长出来" in SOCIAL_DRAFT_PROMPT, "")
        check("他这句在聊工作就别突然撩；他这句在调侃才接得住调侃",
              "别突然\n撩" in SOCIAL_DRAFT_PROMPT and "接得住调侃" in SOCIAL_DRAFT_PROMPT, "")
        check("不硬撩 / 不突兀 / 不脱离语境 / 不油腻",
              "不硬撩、不突兀、不脱离语境、不油腻" in SOCIAL_DRAFT_PROMPT, "")
        check("油腻那几样点名（土味情话 / 宝贝 / 小姐姐）",
              "土味情话" in SOCIAL_DRAFT_PROMPT and "小姐姐" in SOCIAL_DRAFT_PROMPT, "")
        check("不越界：不许露骨色情 / 不许违法 / 不许严重骚扰",
              "不许露骨色情" in SOCIAL_DRAFT_PROMPT and "不许严重骚扰" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 不许拿对方在意的事当杠杆（焦虑 / 忌讳 / 家里的事都不许拿来升温）",
              "当杠杆" in SOCIAL_DRAFT_PROMPT and "不许拿来升温" in SOCIAL_DRAFT_PROMPT, "")
        check("机智但不舔：不讨好 / 不摆低姿态 / 不连珠炮追问（没回就不再递第二条）",
              "机智但\n不舔" in SOCIAL_DRAFT_PROMPT
              and "不讨好、不摆低姿态" in SOCIAL_DRAFT_PROMPT
              and "不再递第二条" in SOCIAL_DRAFT_PROMPT, "")
        check("要的是「有来有回」", "有来有回" in SOCIAL_DRAFT_PROMPT, "")

        # ---------- ④ 跟既有硬规则不打架 ----------

        section("④ 边界：跟第 1 条 / 第 2 条都不打架")

        check("★ 第 1 条补了「不升级 ≠ 不升温」（跳话题才不许，顺着话题往前带一档是要的）",
              "不升级" in SOCIAL_DRAFT_PROMPT and "不等于\"不升温\"" in SOCIAL_DRAFT_PROMPT
              and "不许的是**跳话题**" in SOCIAL_DRAFT_PROMPT, "")
        check("第 1 条原有的字面一个字没删（既有断言照旧）",
              "不许自己把话题升级" in SOCIAL_DRAFT_PROMPT
              and "直接回应它" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 别把「直接回应它」做成「原样同频就收工」——这一句正是这一批治的病",
              "原样同频就收工" in SOCIAL_DRAFT_PROMPT, "")

        check("第 2 条那条硬规则原样还在（收尾 + 禁反问 / 禁质疑 / 禁开话题）",
              "没抛新话题" in SOCIAL_DRAFT_PROMPT
              and "就只回一句贴合他情绪的收尾" in SOCIAL_DRAFT_PROMPT
              and "不许反问" in SOCIAL_DRAFT_PROMPT
              and "不许质疑" in SOCIAL_DRAFT_PROMPT
              and "不许另开一个话题" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 新段明说冲突时**以下面为准**",
              "冲突时以下面为准" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 新段点名了例外：对方只收情绪、没抛新话题时那几样一个都不用",
              "上面那几样（调侃 / 共建 / 轻邀约）一个都不用" in SOCIAL_DRAFT_PROMPT
              and "照下面第 2 条写" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 而且说明了理由：该收尾的时候硬推，比接不住还糟",
              "该收尾的时候硬推，比接不住还糟" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 但收尾**也可以留个口子**（合上话题不是目的、别把话堵死）",
              "收尾\n  也可以留个口子" in SOCIAL_DRAFT_PROMPT
              and "合上话题不是目的" in SOCIAL_DRAFT_PROMPT, "")
        check("点出了第 2 条那则样板当「收了尾又留口」的例子",
              "那下次还邀请你咯" in SOCIAL_DRAFT_PROMPT
              and "收了尾\n  又留了下一个口" in SOCIAL_DRAFT_PROMPT, "")

        check("★ 顺序：新段整段都排在第 2 条那条规则**前面**"
              "（真机上收尾是硬规则，得是最后压住的那一句）",
              _pos("怎么自然地把关系往前带") < _pos("上面那几样（调侃 / 共建 / 轻邀约）一个都不用")
              < _pos("就只回一句贴合他情绪的收尾"), "")
        check("★ 新段排在整个「要求（按重要性）」列表之前"
              "（把它当「怎么写得更好」，不当覆盖既有规则的指令）",
              _pos("怎么自然地把关系往前带") < _pos("要求（按重要性）"), "")
        check("「他真抛了新话题 → 正常接、别一律收尾」那一条也没动",
              "就正常接" in SOCIAL_DRAFT_PROMPT and "别一律收尾" in SOCIAL_DRAFT_PROMPT, "")

        # ---------- ⑤ 两个场景（用户点名的两条） ----------

        section("⑤ 两个场景：破冰闲聊该往前带 / 对方保守该收一档")

        service = app.social_reply
        service._examples_provider = lambda query: ""
        service._session_id, service._peer_name = "S1", "茗风"

        # 场景一：破冰闲聊（对方给了料——短剧），攻略卡那格写着「关系阶段：破冰」
        stub.candidates_result = {
            "candidates": [{"draft": "那部我也在追，下次一起看？", "why": "顺着他的料抛个共建的由头"}]
        }
        service.set_peer_profile_provider(
            lambda peer: "对方意图：分享\n关系阶段：破冰；爱好：短剧"
        )
        stub.prompts.clear()
        out1 = service._write("帮我回一下", CHAT_BREAKING)
        p1 = stub.prompts[-1] if stub.prompts else ""
        check("★ 场景一：这一趟只问了一次（升温段不新增请求）", len(stub.prompts) == 1,
              str(len(stub.prompts)))
        check("★ 场景一：对方的料真的进了材料那格（短剧这一句在提示词里）",
              "短剧" in p1, "")
        check("★ 场景一：攻略卡那格的关系阶段真喂进去了（分档才有依据）",
              "关系阶段：破冰" in p1, "")
        check("★ 场景一：升温段整段都在（不是某些场景才出现的）",
              all(face in p1 for face in NEW_FACES), "")
        check("★ 场景一：提示词明确要求「别把话题合上就停」+ 三种往前带的方式"
              "（这就是「而非纯合上」那一条的判据所在）",
              "别把话题合上就停" in p1 and "抛共建性话题" in p1 and "轻邀约暗示" in p1, "")
        check("场景一：既有硬规则照旧在那一份里（一次改动没把老的挤掉）",
              "不许反问" in p1 and "只答末行" in p1, "")
        check("场景一：候选照旧出得来（这一批没动解析那条链）",
              isinstance(out1, dict) and out1.get("candidates"), repr(out1))

        # 场景二：对方保守（只礼貌应答、没料），攻略卡那格写着「关系阶段：刚认识」
        service.set_peer_profile_provider(
            lambda peer: "对方意图：敷衍\n关系阶段：刚认识；说明：只在礼貌应答"
        )
        stub.prompts.clear()
        out2 = service._write("帮我回一下", CHAT_GUARDED)
        p2 = stub.prompts[-1] if stub.prompts else ""
        check("★ 场景二：这一趟也只问了一次", len(stub.prompts) == 1, str(len(stub.prompts)))
        check("★ 场景二：判「保守 / 刚认识」的两样判据都在提示词里"
              "（阶段写在卡那格 + 他这一句的原话）",
              "关系阶段：刚认识" in p2 and CHAT_GUARDED in p2, "")
        check("★ 场景二：收一档那一整条在（刚认识 / 破冰 / 保守 → 先接舒服、别硬撩）",
              "收一档" in p2 and "别硬撩" in p2 and "先把话接舒服" in p2, "")
        check("★ 场景二：不硬撩那几样也都在（不突兀 / 不油腻 / 不越界）",
              "不硬撩、不突兀、不脱离语境、不油腻" in p2
              and "不许露骨色情" in p2, "")
        check("★ 场景二：收尾那一条也在（没料的那一档照旧一句收尾、但可以留个口子）",
              "就只回一句贴合他情绪的收尾" in p2 and "收尾\n  也可以留个口子" in p2, "")
        check("两个场景喂进去的提示词不是同一份（分档的输入真的不一样）",
              p1 != p2 and "关系阶段：破冰" not in p2, "")

        service.set_peer_profile_provider(lambda peer: "")

        # ---------- ⑥ 不动的地方：占位符 / SYSTEM / 调用点 / why 与语气 ----------

        section("⑥ 不动的地方（多一格占位符 = 既有测试当场 KeyError）")

        fields = {f for _, f, _, _ in string.Formatter().parse(SOCIAL_DRAFT_PROMPT) if f}
        check("★ 占位符一个没多、一个没少（还是那五个）",
              fields == {"chat_context", "text", "examples", "peer_tone_hint", "peer_profile"},
              str(sorted(fields)))
        try:
            rendered = SOCIAL_DRAFT_PROMPT.format(
                chat_context=NO_CHAT_CONTEXT, text="（他的一句）", examples=NO_EXAMPLES,
                peer_tone_hint="", peer_profile="",
            )
        except Exception as exc:                      # noqa: BLE001
            rendered = ""
            check("★ 老调用点那种 `.format` 五个关键字照样渲染得出来", False, repr(exc))
        else:
            check("★ 老调用点那种 `.format` 五个关键字照样渲染得出来，而且新段都在里面",
                  all(face in rendered for face in NEW_FACES), "")

        check("SYSTEM 一个字没动（新段的字样一个都不在里面）",
              not any(face in SOCIAL_DRAFT_SYSTEM for face in
                      ("怎么自然地把关系往前带", "抛共建性话题", "轻邀约暗示", "收一档")), "")
        check("why 解耦那一条没动（解析写在这段的 why 字段里、不要混进正文）",
              "写在这段的 \"why\" 字段里，不要混进正文" in SOCIAL_DRAFT_PROMPT, "")
        check("语气自适应那一格（`{peer_tone_hint}`）的引用没动",
              "{peer_tone_hint}" in SOCIAL_DRAFT_PROMPT, "")
        check("`SOCIAL_TONE_HINT_BLOCK` 一个字没动（机智但不舔 + 两条硬约束都还在）",
              "机智但不舔" in tmod.SOCIAL_TONE_HINT_BLOCK
              and "字数量级必须贴近对方" in tmod.SOCIAL_TONE_HINT_BLOCK, "")

        src_write = inspect.getsource(SocialReplyService._write)
        call = None
        for node in ast.walk(ast.parse(_dedent(src_write))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "format"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "SOCIAL_DRAFT_PROMPT"):
                call = node
        check("★ 调用点还是拿那五个关键字在填（没多问一格、也没少填一格）",
              call is not None
              and [kw.arg for kw in call.keywords]
              == ["chat_context", "text", "examples", "peer_tone_hint", "peer_profile"],
              str([kw.arg for kw in call.keywords] if call else None))

        # ---------- ⑦ 接缝：新段没挤进既有那几格 ----------

        section("⑦ 接缝：新段落在正文里，没撑开既有那几格")

        check("★ `{examples}` 与「只给 1 段」之间仍是**一个空行**"
              "（新段没有插到那一格里去）",
              f'{NO_EXAMPLES}"""{SEP}{SEP}只给 1 段' in rendered, "")
        check("★ `{peer_profile}` 仍紧贴「只给」那一行行首"
              "（那一格的位置一动，`card_memory=off` 那趟就多出一个空行）",
              "{peer_profile}只给" in SOCIAL_DRAFT_PROMPT
              and "{peer_profile}" + SEP not in SOCIAL_DRAFT_PROMPT, "")
        check("新段整段都在 `{peer_profile}` 那一段**之后**（材料那几格一个字没动）",
              _pos("{peer_profile}") < _pos("怎么自然地把关系往前带"), "")
        check("★ 新段在模板里是**静态文本**（这一段里一个占位符都没有）",
              not any(f for _, f, _, _ in string.Formatter().parse(
                  SOCIAL_DRAFT_PROMPT.split("怎么自然地把关系往前带")[1]
                  .split("要求（按重要性）")[0])), "")

        # ---------- ⑧ 桩分流没被劫走 ----------

        section("⑧ 桩分流：起草这一趟还是回 candidates")

        hit = [m for m in ROUTE_MARKERS if m in p1]
        check("★ 新段没带任何一个别的线的分流字面（带了就把起草劫走了）",
              not hit, "、".join(hit))
        check("起草照旧按 `\"candidates\"` 那一条分流", '"candidates"' in p1, "")
        check("★ 拿渲染结果喂桩：回的就是候选那一份",
              stub.route_json(p1) == stub.candidates_result, "")
    finally:
        box.cleanup()

    return summary("社交起草「自然升温」策略段（v2-social-draft-escalate）")


def _pos(needle: str) -> int:
    """模板里这一句在第几个字符；找不到回 -1（**不用 `.index`**：那会抛异常把整支打成
    一次 traceback，做灵敏度探针时看不清是哪一条判据哑了）。"""
    return SOCIAL_DRAFT_PROMPT.find(needle)


def _dedent(src: str) -> str:
    """`inspect.getsource` 带缩进；`ast.parse` 前先按最小缩去掉。"""
    lines = [ln for ln in src.splitlines() if ln.strip()]
    if not lines:
        return src
    pad = min(len(ln) - len(ln.lstrip()) for ln in lines)
    return "\n".join(ln[pad:] if ln.strip() else "" for ln in src.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())
