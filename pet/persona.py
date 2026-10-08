"""
性格预设与说话风格（对应指令 3 性格系统、指令 4 对话表现层）。

一个性格 = 人设描述（拼进 system prompt）
          + 说话风格参数（自称、对用户称呼、是否用括号动作描写 / 颜文字）
          + 行为权重系数（给指令 2 的行为库用）

数据驱动：想加性格，往 PERSONA_PRESETS 里加一条即可。
所有内容都在本地拼装，不额外调用模型；切换性格是热更新，不用重启。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# 配置里选"自定义"时用这个名字，人设取 pet.persona_prompt 里用户自己写的文字
CUSTOM_KEY = "自定义"

# 名字对不上任何预设（例如手改过配置文件）时的兜底性格
DEFAULT_KEY = "软萌黏人"

# 设置页"对用户称呼"下拉里的常见选项（也允许自己填）
USER_CALL_CHOICES: tuple[str, ...] = ("主人", "搭子", "你")


@dataclass(frozen=True)
class PersonaPreset:
    key: str                      # 预设名，也是配置里存的值
    blurb: str                    # 一句话气质说明（设置页提示用）
    prompt: str                   # 人设描述，拼进 system prompt
    self_call: str                # 自称
    user_call: str                # 默认对用户的称呼
    actions: bool                 # 默认是否用括号动作描写
    kaomoji: bool                 # 默认是否用颜文字
    behavior: dict[str, float] = field(default_factory=dict)  # 行为权重系数


PERSONA_PRESETS: tuple[PersonaPreset, ...] = (
    PersonaPreset(
        key="软萌黏人",
        blurb="撒娇爱贴贴，总想凑近你。",
        prompt="撒娇爱贴贴，总想凑近主人；说话软软的，关心主人的吃喝与休息。",
        self_call="人家",
        user_call="主人",
        actions=True,
        kaomoji=True,
        behavior={"look": 1.6, "knead": 1.4, "hop": 1.2, "tail_swish": 1.2},
    ),
    PersonaPreset(
        key="傲娇",
        blurb="嘴硬心软，口是心非。",
        prompt="嘴硬心软、口是心非；嘴上说“才不是关心你呢”，实际默默把事做好。",
        self_call="我",
        user_call="你",
        actions=True,
        kaomoji=False,
        behavior={"sit": 1.3, "look": 1.3, "tail_swish": 1.4, "chase_tail": 0.6},
    ),
    PersonaPreset(
        key="高冷御姐",
        blurb="话少、清冷、句句到位。",
        prompt="话少、清冷、偶尔犀利，话不多但句句到位；关心方式是“不动声色的提醒”。",
        self_call="我",
        user_call="你",
        actions=False,
        kaomoji=False,
        behavior={"sit": 1.6, "lie": 1.4, "hop": 0.4, "chase_tail": 0.4, "pounce": 0.4},
    ),
    PersonaPreset(
        key="元气活泼",
        blurb="能量满满，气氛担当。",
        prompt="能量满满、爱用感叹号和俏皮话，随时给主人打气，是气氛担当。",
        self_call="我",
        user_call="你",
        actions=True,
        kaomoji=True,
        behavior={"hop": 1.9, "chase_tail": 1.8, "pounce": 1.5, "sleep": 0.6},
    ),
    PersonaPreset(
        key="毒舌损友",
        blurb="爱吐槽但靠谱的老友。",
        prompt="爱吐槽爱调侃，嘴损但不坑主人；吐槽完必补一句实在建议，像并肩多年的老友。",
        self_call="我",
        user_call="搭子",
        actions=True,
        kaomoji=False,
        behavior={"tail_swish": 1.5, "look": 1.3, "sit": 1.1},
    ),
)

_ACTION_MARK = re.compile(r"\*[^*]*\*")

# 句首的动作描写括号：模型约定「句首用括号写一个小动作」，如（揉揉眼睛）、(尾巴扫过手背)。
# 只剥句首的括号组（半角 / 全角都算）——模型约定句首那个就是小动作。
# 正文里夹着的括号另算：看形状决定剥不剥（见下面的 _looks_like_action）。
_LEADING_ACTION_PAREN = re.compile(r"^\s*(?:[（(][^（）()]*[）)]\s*)+")

# 正文里夹着的动作括号（不在句首的那些）。
# 光看括号剥不得——正文里的括号也常是内容（危机话术的「（以你当地为准）」、
# 工作建议里的「（研发这边，跨部门要盯紧接口人。）」），全剥就把正经话削掉了。
# 所以只剥「短、没有句读、且点到了身体部位或动作」的那种，也就是舞台提示的形状。
_ANY_PAREN = re.compile(r"[（(][^（）()]*[）)]")
_ACTION_PAREN_MAX_CHARS = 10
_SENTENCE_PUNCT = "，。！？；、…：,;:!?~～"
_ACTION_WORDS: tuple[str, ...] = (
    # 身体部位
    "尾", "耳", "爪", "胡须", "毛", "头", "眼", "脸", "鼻", "嘴", "舌",
    "手", "肩", "背", "肚", "脚", "腿", "身", "脖子",
    # 动作 / 神态
    "扫", "晃", "摇", "歪", "眨", "揉", "伸", "缩", "蹭", "舔", "扑", "拱",
    "蹲", "坐", "站", "转", "顿", "叹", "笑", "咳", "愣", "耸", "嗅", "眯",
    "睁", "拍", "戳", "碰", "跑", "跳", "仰", "摇头", "点头", "停",
)

# 反过来：带这些字的多半是一句正经话（「身体不舒服就先停」「以你当地为准」），
# 不是小动作。宁可漏剥一个动作（只是不好看），也不能把用户要的信息削掉。
_CLAUSE_MARKERS: tuple[str, ...] = (
    "就", "先", "别", "不", "没", "很", "太", "可", "能", "会", "要", "该",
    "如果", "记得", "以", "为准", "一定", "千万", "吗", "呢", "吧", "啊",
)


def _looks_like_action(inner: str) -> bool:
    """括号里这段是不是舞台提示（（尾巴扫过手背）），而不是正经内容（（以你当地为准））。"""
    if not inner or len(inner) > _ACTION_PAREN_MAX_CHARS:
        return False
    if any(ch in inner for ch in _SENTENCE_PUNCT):
        return False        # 带句读的是一句话，不是一个小动作
    if any(word in inner for word in _CLAUSE_MARKERS):
        return False        # 带这些虚词的是个条件/说明，不是动作
    return any(word in inner for word in _ACTION_WORDS)


def strip_action_marks(text: str) -> str:
    """渲染前兜底：剥掉动作描写，只留正常对话。

    模型的约定是「句首用括号写小动作」，偶尔又会无视约定改用 markdown 星号（*歪了歪头*）。
    这两种是舞台提示，剥掉。正文里夹的括号分两说：像（尾巴扫过手背）这种小动作也剥，
    像危机话术里「以你当地为准」那种说明性内容保留（判定见 _looks_like_action）。
    最后顺手收掉残留的多余空格。
    """
    original = str(text or "")
    cleaned = _ACTION_MARK.sub("", original)
    cleaned = _LEADING_ACTION_PAREN.sub("", cleaned)
    cleaned = _ANY_PAREN.sub(
        lambda m: "" if _looks_like_action(m.group()[1:-1]) else m.group(), cleaned
    )
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if cleaned:
        return cleaned
    # 剥空了：说明这整句本来就是一个括号（（以你当地为准）、（身体不舒服就先停））。
    # 那样的括号是内容不是小动作——把星号去掉后的原文还回去，别把一句话削没了。
    return re.sub(r"[ \t]{2,}", " ", _ACTION_MARK.sub("", original)).strip()


# ---------- 颜文字（v2-speech-polish） ----------
#
# 用户真机反馈："颜文字被读出"。`(=^･ω･^=)` 送到 SAPI5 会被逐字符念成
# "括号 等号 尖 点 欧米伽 点 尖 等号 括号"，一个字都听不出是只猫。
# UI 上留不留是另一回事（照旧的样式渲染，只是前面那句还读得出来），
# **送进语音合成的那一份必须把颜文字剥干净**——所以这条收口在 VoiceEngine.say，
# 而不是在每个说话点各写一遍。
#
# 怎么认出颜文字：夹在一对括号里、短、**没有汉字也没有数字**、且带表情符号字符。
# 三条缺一不可，是为了不误伤正经内容：
#   （以你当地为准）  有汉字   → 留
#   （19:30 我喊你）  有数字有汉字 → 留
#   (研发这边，跨部门要盯紧接口人。) 有汉字 → 留
#   (=^･ω･^=)        无汉字无数字、有 ^ 和 ω → 剥
#   (T_T)             无汉字无数字、有 _   → 剥
_KAOMOJI_MAX_CHARS = 16          # 颜文字都是短的；这么长的括号组基本是内容
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
# 颜文字里高频出现的那些"不像在写话"的字符：各种眼睛、嘴、手势、标点
_KAOMOJI_CHARS = frozenset("^_=><~*/\\|`'\"-.,;:!?ωΩ∀▽△￣｀´・･。゜ゝゞノヾミ彡ΣΦεзθﾉﾟ๑•̀ㅂوಡ್⌒︶︹╥╯╰ㅠㅜㅇටᵕᗜ⌐¬¯°˚")
_KAOMOJI_GROUP = re.compile(r"[（(]([^（）()]{1,%d})[）)]" % _KAOMOJI_MAX_CHARS)


def _looks_like_kaomoji(inner: str) -> bool:
    """括号里这段是不是颜文字（(=^･ω･^=)），而不是正经内容（（19:30 我喊你））。"""
    if not inner or len(inner) > _KAOMOJI_MAX_CHARS:
        return False
    if _CJK.search(inner):
        return False          # 有汉字：是内容，不是表情
    if any(ch.isdigit() for ch in inner):
        return False          # 有数字：多半是时间/数字，不是表情
    return any(ch in _KAOMOJI_CHARS for ch in inner)


def split_kaomoji(text: str) -> list[tuple[str, bool]]:
    """把一句话切成 [(片段, 是不是颜文字)]，给界面"按段淡一档"用。

    `strip_kaomoji` 是给语音那条线**剥掉**用的，这个是给界面**认出来单独上色**用的
    （用户原话："颜文字 UI 淡化显示"）。判据共用同一对 `_KAOMOJI_GROUP` 与
    `_looks_like_kaomoji`——两处不会各认各的，剥掉的和淡化的永远是同一批字符。

    没有任何颜文字时返回 `[(原文, False)]`，调用方照旧走原文那条路。
    """
    original = str(text or "")
    segments: list[tuple[str, bool]] = []
    cursor = 0
    for match in _KAOMOJI_GROUP.finditer(original):
        if not _looks_like_kaomoji(match.group(1)):
            continue
        if match.start() > cursor:
            segments.append((original[cursor : match.start()], False))
        segments.append((match.group(0), True))
        cursor = match.end()
    if cursor < len(original):
        segments.append((original[cursor:], False))
    return segments or [(original, False)]


def strip_kaomoji(text: str) -> str:
    """把颜文字整段去掉（连括号），只留能读出来的话。

    剥完可能剩下个空串——那种情况下调用方（语音那条线）自己判"没得念就不念"，
    这里不替它决定，原文照旧返回（只有颜文字的句子，UI 上还是要显示那只猫的脸）。
    """
    original = str(text or "")
    cleaned = _KAOMOJI_GROUP.sub(
        lambda m: "" if _looks_like_kaomoji(m.group(1)) else m.group(), original
    )
    # 颜文字多半是拿空格跟正文隔开的，剥掉后别留一截空尾巴
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


# 自定义性格的说话风格兜底：用户只写人设描述，其余沿用最中性的默认
CUSTOM_PRESET = PersonaPreset(
    key=CUSTOM_KEY,
    blurb="人设由你自己写。",
    prompt="",
    self_call="我",
    user_call="你",
    actions=True,
    kaomoji=True,
)

# 设置页下拉里能选的全部选项（5 个预设 + 自定义）
PERSONA_KEYS: tuple[str, ...] = tuple(p.key for p in PERSONA_PRESETS) + (CUSTOM_KEY,)

_PRESET_MAP = {p.key: p for p in PERSONA_PRESETS}


@dataclass(frozen=True)
class PersonaVoice:
    """宠物此刻的说话风格：性格预设 + 用户在设置里的覆盖，解析后的结果。"""

    preset: PersonaPreset
    prompt: str        # 人设描述（自定义时就是用户写的那段）
    self_call: str     # 自称
    user_call: str     # 对用户的称呼
    actions: bool      # 是否用括号动作描写
    kaomoji: bool      # 是否用颜文字

    def style_rules(self) -> list[str]:
        """说话风格的公共规则，宠物本体与角色卡叠加时共用。"""
        lines = [
            # v2-chat-mirror：这句原来写死成"每次只说 1~2 句"，于是不管对方是兴致勃勃
            # 说一大段还是只回一个"嗯"，回出来的都一样长——"恒定、无波动"就是从这儿来的。
            # 现在"短"是常态、不是铁律，这一轮到底多长由 life/dialog/mood.py 的节奏那条定。
            "- 平时只说 1~2 句，短句，口语化（对方说得起劲、想多聊时，可以跟着多说两句）",
            "- 不说教、不列条目、不用 markdown",
            f"- 自称“{self.self_call}”，称呼用户“{self.user_call}”",
        ]
        if self.actions:
            lines.append("- 偶尔在句首用括号写一个小动作，例如（揉揉眼睛）、（尾巴扫过手背）")
        else:
            lines.append("- 不要用括号描写动作")
        if self.kaomoji:
            lines.append("- 适当用颜文字，例如 (=^･ω･^=)、(>ω<)")
        else:
            lines.append("- 不要使用颜文字")
        return lines

    def system_prompt(self, pet_name: str) -> str:
        """拼出交给模型的系统提示：人设 + 说话规则（动作/颜文字按开关取舍）。"""
        lines = [
            f"你叫{pet_name}，是用户桌面上的 AI 助理。",
            f"人设：{self.prompt}",
            "说话规则：",
            "- 先接住对方的情绪，再给回应，绝对不评判、不指责",
            *self.style_rules(),
        ]
        return "\n".join(lines)


def resolve_voice(pet) -> PersonaVoice:
    """把 pet 配置解析成说话风格。

    pet 需要提供 persona / persona_prompt / user_call / style_actions / style_kaomoji。
    自定义人设写了就用它，没写则回落到默认性格；称呼留空就用性格自带的那套。
    """
    key = (getattr(pet, "persona", "") or "").strip()
    custom_text = (getattr(pet, "persona_prompt", "") or "").strip()
    if key == CUSTOM_KEY and custom_text:
        preset = replace(CUSTOM_PRESET, prompt=custom_text)
    else:
        preset = _PRESET_MAP.get(key, _PRESET_MAP[DEFAULT_KEY])
    return PersonaVoice(
        preset=preset,
        prompt=preset.prompt,
        self_call=preset.self_call,
        user_call=(getattr(pet, "user_call", "") or "").strip() or preset.user_call,
        actions=bool(getattr(pet, "style_actions", True)),
        kaomoji=bool(getattr(pet, "style_kaomoji", True)),
    )


# v2-chat-style：去 AI 味的语气规则，写进 persona_style_layer 让两条回话路共用。
# 只动"怎么说"，不改变专业框架的回应顺序与禁忌、不碰意图/情绪/记忆。
CHAT_STYLE_RULES: tuple[str, ...] = (
    "- 短句为主，多用口语和语气词（啊 / 嘛 / 呀 / 啦 / 哈），别用书面连接词，别像写作文；",
    "- 有来有回：会反问、接梗、顺着对方上一句往下说，别自己一段一段单方面陈述；",
    "- 偶尔自嘲、幽默一点，别端着；",
    "- 说具体的事，别用「好的 / 没问题 / 我理解」这种空话应付；",
    "- 直接说出你的情绪（开心 / 可惜 / 心疼），别藏着；",
    "- 偶尔留一手、卖个关子，别一次把话说满；",
    "- 允许口语化的小不完美（跑题、感叹、语气词）；",
    "- 别油腻、别用力过猛，分寸感比热闹要紧。",
    # v2-chat-stage：关系分寸——现在是「刚认识、慢慢熟悉」的前期，别急着升温。
    "- 关系分寸：你们现在还在「刚认识、慢慢熟悉」的前期，由浅到深地了解，别急着升温、不撩、不暧昧、不越界；",
)


def persona_style_layer(pet, pet_name: str, extra: str = "", framework: bool = True) -> str:
    """角色卡叠加用的「表层腔调」层：身份 + 脾气 + 说话规则。

    角色卡（life/psych）负责专业框架——怎么共情、什么禁忌；这一层负责"是谁在说"：
    同一张林医生卡，配"软萌黏人"就温柔黏人，配"高冷御姐"就清冷克制。
    脾气只动措辞，不动专业框架的顺序与禁忌，所以这里要把优先级写清楚，否则性格会把框架冲掉。

    ``framework=False`` 是给"这一轮下面**没有**专业框架"的调用方用的（v2-speech-polish ③
    的日常闲聊那条路）：那时候还写"不改变下面专业框架里的回应顺序"，等于凭空指一段
    不存在的规矩，模型会自己脑补一套回来——机械化就是这么长出来的。所以那半句要跟着
    实际情况走，不能写死。

    ``extra`` 是调用方追加的一段腔调（好感度够了之后"更亲一点"，见 life/mind/intimacy.py）。
    由调用方拼好了传进来，而不是这里去读库：`pet/` 这层不认识 mind 那套存储，
    方向反过来会把分层搞乱；而且不是每个调用方都开着情感记忆。
    """
    voice = resolve_voice(pet)
    temper = (
        "你的脾气（只决定你的用词和语气，不改变下面专业框架里的回应顺序与禁忌）"
        if framework
        else "你的脾气（只决定你的用词和语气）"
    )
    lines = [
        f"你叫{pet_name}，是用户桌面上的 AI 助理。",
        f"{temper}：{voice.prompt}",
        "说话方式：",
        *voice.style_rules(),
        "语气（去 AI 味，只改怎么说话、不改回应内容）：",
        *CHAT_STYLE_RULES,
    ]
    tail = (extra or "").strip()
    if tail:
        lines.append(tail)
    return "\n".join(lines)


def preset_style(preset: PersonaPreset) -> tuple[bool, bool]:
    """这个性格默认的说话风格：(用动作描写, 用颜文字)。"""
    return preset.actions, preset.kaomoji


def preset_of(key: str) -> PersonaPreset | None:
    """按名字取预设；自定义或对不上名字时返回 None。"""
    return _PRESET_MAP.get((key or "").strip())
