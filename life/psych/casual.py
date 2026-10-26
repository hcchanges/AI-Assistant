"""日常闲聊那条回话路（v2-speech-polish ③）：呆萌人设照旧，但不套咨询流程。

**这条线是给"用户没在求助"的时候用的。** 宠物本体的回话一直走林医生那张角色卡
（`life/psych/personas/counselor_lin.json`），卡里的 system_prompt 明写着
`【回应准则（按顺序）】1 共情与反映 → 2 正常化 → 3 探索 → 4 赋能`，外加两段
long-form 咨询话术当 few-shot。那套框架在真要求助时是对的，可**闲聊也套它**，
说出来就永远是一个味道——接住情绪、说一句"这很正常"、反问你一句、再给个小建议。
用户真机反馈的"回复机械化"就是从这儿来的。

所以这里另起一条**轻**的路：system 里没有人设以外的流程，只有"像熟人搭话那样顺着说"，
外加一条不许丢掉的安全底线。判断哪一轮该走这条，不在这个文件里——见
`PsychService._is_casual`，它拿路由（意图 + 情绪）做判据，判不准就退回原来的
专业框架（保守那一侧是"照旧"，不是"改坏"）。

三条纪律：

- **不碰危机**：硬危机在 `PsychService.respond` 开头就被 `crisis.is_crisis` 拦掉了，
  根本到不了这儿；这里另留一段安全底线，是防"软表达"从这条路过。
- **不动角色卡**：只加一条路，`persona_loader` 与那四张 JSON 一个字没改，
  卡牌"陪执行角色"那条线照旧走 `PersonaCard.system_with_style`。
- **不自己拼人设**：腔调一律从 `pet/persona.py` 的 `persona_style_layer` 取，
  跟专业框架那条路**同一个来源**——用户换性格，两条路一起变。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from core.paths import bundle_dir
from pet.persona import persona_style_layer

logger = logging.getLogger(__name__)

# 生成参数：比林医生那张卡（0.8 / 0.9）更放开一点。闲聊要的是"像人"，不是"稳妥"。
CASUAL_TEMPERATURE = 0.9
CASUAL_TOP_P = 0.95
CASUAL_MAX_TOKENS = 300

# 这一轮的说话方式。**逐条都是冲着"去机械句式"写的**，不是泛泛的"自然一点"：
# 前两条点名拆掉四步流程，后面几条堵住模型最容易滑回去的那几个套路
# （每句反问收尾、复述对方感受、列条目）。
CASUAL_RULES: tuple[str, ...] = (
    "现在这一轮是**日常闲聊**：他不是来求助的，你也不用摆出开导的架势。",
    "- 直接接住他说的这件事本身，像熟人搭话那样顺着聊下去；",
    "- **不要**按「先共情 → 再正常化 → 再问一句 → 最后给建议」这套流程走，那是做咨询，不是聊天；",
    "- 不要每句都用反问收尾，也不用每次都复述一遍他的感受；",
    "- 不说教、不列条目、不用 markdown、不堆四字词；",
    "- 允许你有自己的反应和偏好（好奇、惊讶、吐槽、犯懒、馋），别只会顺着说好话。",
)

# 只在这一句是**纯粹寒暄**时才追加的一条（v2-chat-core #3）。见 router.is_greeting。
#
# 用户真机反馈「早上好」被回成一段嘘寒问暖：这句四张词表都不命中，兜底成"确认/平静"
# 走的就是这条轻路，可轻路原先只说"像熟人搭话那样顺着聊"，没说不许把招呼当倾诉接。
# 于是模型按软萌人设自由发挥。这条把口径钉死：招呼就回招呼。
GREETING_RULE = (
    "- 这一句只是**寒暄**（打个招呼、道声早晚）：平实地应一声就好——别升温、别抒情、"
    "别顺势关心他的近况、别追问、别给建议。他要真想聊，下一句会自己说。"
)

# 安全底线。硬危机轮不到它兜（前面已经拦了），这段是给"软表达"留的：
# 一句听着像闲聊、其实很重的话，不许被这条轻路当成玩笑接过去。
SAFETY = (
    "【安全底线】如果这句话里出现了很重的、指向伤害自己的意思，立刻停下闲聊的口吻，"
    "认真起来：表达关心、不评判、建议他找信任的人或专业帮助"
    "（全国心理援助热线 12356，以当地为准）。这一条优先于上面所有说话方式。"
)

# v2-chat-stage：聊天红线改为「分阶段」——恋爱/撩妹后期可解锁，前期由陌生到熟悉。
# v2-chat-style-fix：范本**不再写死在这个文件里**，改成启动时从下面这份文件读。
#
# 为什么要改成读文件：这个常量被换过两回（v2-chat-style 自拟 → v2-chat-style-fix 换原文
# → v2-chat-stage 又改回阶段样本），每次都是"谁改谁手抄"，抄着抄着原文就没了——现在
# 范本躺在 `pet/assets/范本/闲聊选段.md` 里，用户句是从 01/02 两份原始记录**逐字照抄**的，
# 原文也一并存在同目录，测试照着出处行回原文核对。改范本 = 改那个文件，不用动代码。
#
# 红线（两次变更合并后）：
#   - 前期（陌生 / 熟悉，当前启用）：不注入恋爱/撩妹话术、PUA、粗口、露骨；暧昧/调侃/反转可保留。
#   - 后期（好朋友 / 恋人，见 03_好朋友阶段.md、04_恋人阶段.md）：待解锁，解锁后再放开恋爱/撩妹；
#     PUA、粗口、露骨仍全程禁止。

# 当前启用的关系阶段（前期：陌生 → 熟悉）。03/04 待解锁，暂不加入。
CHAT_STAGES: tuple[str, ...] = ("陌生", "熟悉")

#: 范本文件的落点。跟着仓库走（`pet/assets/范本/`），**不挂用户目录**——它是产品的一部分，
#: 不是用户数据，换台机器跑同一份代码就该有同一套范本。
FEW_SHOT_DIR = bundle_dir() / "pet" / "assets" / "范本"
FEW_SHOT_FILE = FEW_SHOT_DIR / "闲聊选段.md"

_STAGE_RE = re.compile(r"^\[(?P<stage>[^\]]+)\]$")
_LINE_RE = re.compile(r"^(?P<who>用户|宠物)：(?P<text>.*)$")
_SOURCE_RE = re.compile(r"^出处：(?P<file>[^:]+):(?P<start>\d+)-(?P<end>\d+)$")


@dataclass(frozen=True)
class FewShot:
    """一段闲聊范本：用户句 + 宠物句，外加它俩的原文出处（给测试回原文核对用）。"""

    stage: str
    user: str
    pet: str
    source_file: str
    source_start: int
    source_end: int


def parse_few_shot(text: str) -> tuple[FewShot, ...]:
    """把范本文件的正文解析成一段一段。

    格式（见 `闲聊选段.md` 抬头）很简单：`[阶段]` 起一段，`用户：` / `宠物：` 各一行，
    `出处：文件名:起-止` 收尾。`#` 开头的注释与空行一律跳过。

    解析不动的内容**当场记一笔、跳过那一段**，不整份作废——一段写坏了不该把另外五段
    也连坐（跟抓屏笔记"一屏认不出就丢这一屏"一个态度）。
    """
    shots: list[FewShot] = []
    stage = user = pet = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if match := _STAGE_RE.match(line):
            stage, user, pet = match.group("stage"), None, None
            continue
        if match := _LINE_RE.match(line):
            if match.group("who") == "用户":
                user = match.group("text").strip()
            else:
                pet = match.group("text").strip()
            continue
        if match := _SOURCE_RE.match(line):
            if not (stage and user and pet):
                logger.error("语气范本：%r 那一段缺了阶段/用户句/宠物句，跳过", stage or line)
            else:
                shots.append(
                    FewShot(
                        stage=stage,
                        user=user,
                        pet=pet,
                        source_file=match.group("file").strip(),
                        source_start=int(match.group("start")),
                        source_end=int(match.group("end")),
                    )
                )
            stage = user = pet = None
            continue
        logger.error("语气范本：看不懂的一行，跳过：%s", line)
    return tuple(shots)


def load_few_shot(path: Path = FEW_SHOT_FILE) -> tuple[FewShot, ...]:
    """读范本文件。读不出来就空着，并在 ERROR 里把原因写清楚（不静默降级）。

    范本是随代码发的固定资产，读不到基本只有一种可能：打包时漏了这个文件。所以这里
    只记日志、让程序照常起来（没有范本仍能聊天，只是少了那点人味），但日志里必须留下
    话——"宠物突然变机械了"和"范本文件丢了"要能对上号。
    """
    try:
        return parse_few_shot(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error("语气范本读不出来（%s）：%s，这一版没有 few-shot", path, exc)
        return ()


#: 启动时读一次，之后不再变（改范本文件要重开程序，跟改 user 目录里的文档不一样）。
FEW_SHOTS: tuple[FewShot, ...] = load_few_shot()

#: 喂给模型的那份：两两成对的 (用户句, 宠物句)。顺序就是文件里的顺序。
CASUAL_FEW_SHOT: tuple[tuple[str, str], ...] = tuple((s.user, s.pet) for s in FEW_SHOTS)


def system_prompt(pet, pet_name: str, tone: str = "", greeting: bool = False) -> str:
    """这一轮交给模型的 system：人设（跟专业框架那条同源）+ 闲聊方式 + 安全底线。

    ``greeting`` 为真时多追一条 GREETING_RULE（v2-chat-core #3）：这一句只是打招呼，
    平实回一声，别把它当倾诉来接。
    """
    style = persona_style_layer(pet, pet_name, extra=tone, framework=False)
    rules = CASUAL_RULES + ((GREETING_RULE,) if greeting else ())
    return (
        f"【是谁在说】\n{style}\n\n"
        f"【这一轮怎么说话】\n" + "\n".join(rules) + "\n\n" + SAFETY
    )


def messages(
    pet,
    pet_name: str,
    user_text: str,
    memory_text: str,
    tone: str = "",
    comfort_hint: str = "",
    kb_text: str = "",
    greeting: bool = False,
) -> list[dict[str, str]]:
    """拼出这一轮要交给模型的消息。

    顺序**跟 `PersonaCard.messages` 对齐**（腔调 → 示范 → 语气分寸 → 旧记录 → 记忆 → 原话），
    这样两条路只有"框架那一段"不一样，别的地方不会悄悄分家：
    原来这里刻意不摆 few-shot（怕示范把"机械化"再引回来）；v2-chat-style 反其道而行，
    摆几段**日常陪伴**的示范，让模型照着一个"像熟人聊天"的样去学——范本见
    `CASUAL_FEW_SHOT`。comfort_hint 是安抚分寸（指令 26-A），语气轻的时候仍有意义，
    照旧带上。
    """
    out: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt(pet, pet_name, tone, greeting=greeting)}
    ]
    for shot_user, shot_assistant in CASUAL_FEW_SHOT:
        out.append({"role": "user", "content": shot_user})
        out.append({"role": "assistant", "content": shot_assistant})
    if comfort_hint:
        out.append({"role": "system", "content": comfort_hint})
    if kb_text:
        out.append({"role": "system", "content": kb_text})
    out.append(
        {"role": "system", "content": f"你记得关于用户的事（可能为空）：\n{memory_text}"}
    )
    out.append({"role": "user", "content": user_text})
    return out
