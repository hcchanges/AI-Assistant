# -*- coding: utf-8 -*-
"""社交起草·「阶段 → 话题方向」路线图（v2-social-topic-roadmap）的自动测试。

**病根**（只读回填前几批的结论）：到上一批 `v2-social-emotion-state` 为止，起草提示词已经
会读情绪、会认意图、会按关系阶段分档升温、会看这一轮的进退分寸。但**"聊什么"这件事一直
没规则**——那几段管的全是"推不推、推多少"，没有一段管"往哪个方向聊"。于是同一个"可攻"的
人，模型往哪带全凭随机：刚认识也被问"什么时候见面"，暧昧了还停在天气。

这一批补的是一张**纯文本的路线图**：把攻略卡那格写的**关系阶段**（`{peer_profile}` 里
「眼下关系」那几个字）映射成**话题方向**——

- **刚认识 / 破冰** → 安全区（他「情况是」里提到的兴趣、眼前这件小事、日常寒暄），
  **不往**感情 / 家里 / 从前 / 将来 / 见面这些私密方向带；
- **聊得来** → 顺着他提过的兴趣往深里聊、找共同点，可以试探着往"一起做点什么"引；
- **暧昧 / 处着** → 才可以带一点私人话题、情绪价值、暗示；
- 他「情况是」「最近记下的」里露出来的**烦心事是雷区**，一律不当话题方向。

**跟 `v2-social-emotion-state` 怎么协同**（这一段自己声明，也是这一批的关键取舍）：**方向
排在进退分寸之后**——那格判着「该退」，这一轮一个方向都不挑、先安抚收尾；判着「需守」，
就留在他这一句的话题上、别自己换方向；判着「可攻」，才按阶段挑方向。**"往哪儿聊"永远让
位于"这一轮该不该进"**，不然一张漂亮的路线图会把上一批刚学会的"该退就退"重新顶掉。

**这一批一个字都不动代码**：只往 `SOCIAL_DRAFT_PROMPT` 里加一段**静态**文本——没有新
占位符、没有新开关、没有新常量、没有新网络请求。动态那两格（口吻摘要 `{peer_tone_hint}`、
对象档案 `{peer_profile}`）一个字没动，那是 `v2-social-tone-adaptive` / `v2-social-card-inject`
的地盘。

红线（本模块逐条钉住）：
- 不新增 `{}` 占位符（还是那五格）；不新增任何开关（`config.yaml` 零改动）；
- 零新依赖；不新增网络请求（还是起草那一趟）；两库零写入；
- 这段话是**静态**的——`card_memory=off` / `tone_align=off` 时它照旧在（它不挂在任何
  动态那一格上），但**动态那两格该空的还得空**（上一批那两条红线不许被这一段撑开）。

**口径如实记（够不着的地方不装作验过）**：模型被桩掉了，本模块钉的是"该教的路线图画了
没有、跟既有那几段有没有打架"，不是"模型真会照着阶段换方向"——那只能真机看。
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

# 桩模型的分流字面（`tests/harness.py::StubLLM.route_json`）：新段一个都不许带，
# 带了就把起草这一趟劫去别的线上（那是要红的事，不是风格问题）
ROUTE_MARKERS = (
    '"anxiety"', "time_confidence", '"rel_state"', '"key_points"', '"components"',
    '"profile"', '"skill_name"', '"matched"', '"confidence"', '"intent"', "memories",
)


def main() -> int:
    # ---------- ① 路线图真的在：四档方向各归各 ----------

    section("① 路线图进提示词：阶段 → 话题方向 四档")

    check("这一段在（按它自己的标题认）", "话题往哪个方向引" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 说明它管的是「往哪儿聊」（跟前面几段「推不推、推多少」分工）",
          "往哪儿聊" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 刚认识 / 破冰 → 安全区（兴趣 / 眼前这件小事 / 日常寒暄）",
          "刚认识 / 破冰" in SOCIAL_DRAFT_PROMPT
          and "安全区" in SOCIAL_DRAFT_PROMPT
          and "日常寒暄" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 刚认识 / 破冰 时**不往**私密方向带（感情 / 家里 / 从前 / 将来 / 见面）",
          "别往感情 / 家里 / 从前 / 将来 / 见面这些私密方向带" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 聊得来 → 顺着他提过的兴趣往深里聊、找共同点",
          "聊得来" in SOCIAL_DRAFT_PROMPT
          and "顺着他提过的兴趣往深里聊" in SOCIAL_DRAFT_PROMPT
          and "找共同点" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 暧昧 / 处着 → 才可以带私人话题 / 情绪价值 / 暗示",
          "暧昧 / 处着" in SOCIAL_DRAFT_PROMPT
          and "私人话题、情绪价值、暗示" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ② 画像那两个输入（阶段 / 烦心事）都被用上 ----------

    section("② 吃攻略卡画像：关系阶段定向、烦心事列雷区")

    check("★ 方向这一段的**分档依据还是那格的关系阶段**（跟上一段同一个源头）",
          "也照上面那格分档" in SOCIAL_DRAFT_PROMPT
          and "关系阶段" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 兴趣从「情况是」里取（那是装配层喂进来的那一段里的字）",
          "「情况是」里提到的兴趣" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 烦心事（从「情况是」「最近记下的」露出来的）一律不当话题方向",
          "「情况是」「最近记下的」里露出来的烦心事" in SOCIAL_DRAFT_PROMPT
          and "一律不当话题方向" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 雷区这条跟既有的「不许拿对方在意的事当杠杆」挂上（不是另起一条口径）",
          "那是雷区，绕开" in SOCIAL_DRAFT_PROMPT
          and "不许拿对方在意的事当杠杆" in SOCIAL_DRAFT_PROMPT, "")

    # ---------- ③ 与 emotion-state 协同：方向让位于进退分寸 ----------

    section("③ 协同：进退分寸优先于话题方向")

    check("★ 明写「方向排在进退分寸之后」",
          "方向排在进退分寸之后" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 该退 → 一个方向都不挑、先安抚收尾",
          "这一轮一个方向都不挑" in SOCIAL_DRAFT_PROMPT
          and "先安抚收尾" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 需守 → 留在他这一句的话题上、别自己换方向",
          "就留在他这一句的话题上、别自己换方向" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 可攻 → 才按这个阶段挑方向",
          "才按上面这个阶段挑方向" in SOCIAL_DRAFT_PROMPT, "")
    check("★ 路线图排在进退分寸那一段**之前**（读起来是「先方向、再由进退覆盖」）",
          "话题往哪个方向引" in SOCIAL_DRAFT_PROMPT
          and "先看对方这轮给不给进" in SOCIAL_DRAFT_PROMPT
          and SOCIAL_DRAFT_PROMPT.index("话题往哪个方向引")
          < SOCIAL_DRAFT_PROMPT.index("先看对方这轮给不给进"), "")

    # ---------- ④ 静态：这一段不挂任何动态格 ----------

    box, stub, app = make_env()
    try:
        service: SocialReplyService = app.social_reply
        service._examples_provider = lambda query: ""
        stub.candidates_result = {"candidates": [{"draft": "行啊，你来定", "why": "接住对方"}]}

        section("④ 渲染实测：这一段是静态的，卡片/口吻那两格该空的还得空")

        service.set_peer_profile_provider(
            lambda peer: "叫小美、做电商；眼下关系：暧昧；最近记下的：喜欢爬山")
        app.config.screen.card_memory = "auto"
        app.config.screen.tone_align = "off"     # 隔离口吻那条线
        service._session_id, service._peer_name = "S1", "小美"

        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "今天天气不错")
        on_prompt = stub.prompts[-1]
        check("★ 卡片那格照旧进了提示词（路线图没把上一批的接缝挤掉）",
              "我手上关于他的记录" in on_prompt and "眼下关系：暧昧" in on_prompt, "")
        check("★ 路线图段在渲染出来的提示词里",
              "话题往哪个方向引" in on_prompt, "")

        app.config.screen.card_memory = "off"
        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "今天天气不错")
        off_prompt = stub.prompts[-1]
        check("★ 关掉卡片那格 → 卡片那半没了", "我手上关于他的记录" not in off_prompt, "")
        check("★ 但路线图段照旧在（它不挂在任何动态格上，是静态的）",
              "话题往哪个方向引" in off_prompt, "")
        baseline = SOCIAL_DRAFT_PROMPT.format(
            chat_context="今天天气不错",
            text=AUTO_TRIGGER_TEXT,
            examples=NO_EXAMPLES,
            peer_tone_hint="",
            peer_profile="",
        )
        check("★ 卡片 / 口吻两格都空时，渲染结果 == 占位填空串（一字不差）",
              off_prompt == baseline, f"{len(off_prompt)} 字 vs {len(baseline)} 字")
        check("★ 上一批那条红线没被撑开：状态块在这一趟里不参与（tone_align=off）",
              "对方现在这轮的进退分寸" not in off_prompt, "")

        # ---------- ⑤ 红线与既有钉子 ----------

        section("⑤ 红线：五格没多 / 静态段不带桩分流字面 / 两库零写入 / 既有钉子照旧")

        fields = {f for _, f, _, _ in string.Formatter().parse(SOCIAL_DRAFT_PROMPT) if f}
        check("★ 占位符一个没多、一个没少（还是那五个）",
              fields == {"chat_context", "text", "examples", "peer_tone_hint", "peer_profile"},
              str(sorted(fields)))

        if ("话题往哪个方向引" in SOCIAL_DRAFT_PROMPT
                and "先看对方这轮给不给进" in SOCIAL_DRAFT_PROMPT):
            roadmap = SOCIAL_DRAFT_PROMPT[
                SOCIAL_DRAFT_PROMPT.index("话题往哪个方向引"):
                SOCIAL_DRAFT_PROMPT.index("先看对方这轮给不给进")
            ]
        else:
            roadmap = SOCIAL_DRAFT_PROMPT   # 段没了 → 整份都当"嫌疑段"，判据必红
        hit = [m for m in ROUTE_MARKERS if m in roadmap]
        check("★ 新段不带桩分流字面（不然起草这一趟会被劫去别的线）", not hit,
              "、".join(hit) or "（一处都没有）")

        # —— 既有那几段的字面一个字没动（它们是别的模块钉着的，这里复查一遍）——
        check("★ 第 1 条 / 第 2 条照旧",
              "不许自己把话题升级" in SOCIAL_DRAFT_PROMPT
              and "直接回应它" in SOCIAL_DRAFT_PROMPT
              and "就只回一句贴合他情绪的收尾" in SOCIAL_DRAFT_PROMPT
              and "不许反问" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 升温段照旧（v2-social-draft-escalate）",
              "怎么自然地把关系往前带" in SOCIAL_DRAFT_PROMPT
              and "抛共建性话题" in SOCIAL_DRAFT_PROMPT
              and "轻邀约暗示" in SOCIAL_DRAFT_PROMPT
              and "一档一档来" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 分档段 / 进退段照旧（前两批）",
              "分档看上面那格" in SOCIAL_DRAFT_PROMPT
              and "不是你想推进多少" in SOCIAL_DRAFT_PROMPT
              and "先看对方这轮给不给进" in SOCIAL_DRAFT_PROMPT
              and "不升温、不抛共建、不邀约" in SOCIAL_DRAFT_PROMPT, "")
        check("★ 接缝没被撑开（`{peer_profile}只给` 还在，中间没插字）",
              "{peer_profile}只给" in SOCIAL_DRAFT_PROMPT
              and "{peer_profile}" + SEP not in SOCIAL_DRAFT_PROMPT, "")
        check("★ 路线图整段排在「要求（按重要性）」之前（冲突时仍以下面为准）",
              "话题往哪个方向引" in SOCIAL_DRAFT_PROMPT
              and "要求（按重要性）" in SOCIAL_DRAFT_PROMPT
              and SOCIAL_DRAFT_PROMPT.index("话题往哪个方向引")
              < SOCIAL_DRAFT_PROMPT.index("要求（按重要性）"), "")

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
        service.set_peer_profile_provider(lambda peer: "眼下关系：暧昧")
        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "今天天气不错")
        service._write(AUTO_TRIGGER_TEXT, "嗯")
        drain_threads()
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草·「阶段 → 话题方向」路线图（v2-social-topic-roadmap）")


if __name__ == "__main__":
    raise SystemExit(main())
