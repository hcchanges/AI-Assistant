"""情绪 → 语气修饰（S2）+ 意图 → 回应分寸（S3 的提示词）+ 回话紧跟对方（v2-chat-mirror）。

情绪落在语气上（低落 / 焦虑 / 愤怒要软、开心要欢快），意图落在回应分寸上
（倾诉先接情绪别急着给方案、求助才给建议、确认轻回、指令短回）。两者拼成一段提示词，
由 life/psych/service.py 在回话时注入角色卡的消息里——这里只产出文字，不改人设本身。

v2-chat-mirror 又加了一层**跟上对方的节奏**：上面那两样都只看"这一句是什么意图/情绪"，
不看"对方说了多少、劲头多大"，于是一句"嗯"和一段兴致勃勃的长篇，回出来的长度和调子
差不多——用户真机反馈的"情绪平、字数恒定"就是这么来的。`mirror_hint` 把对方的情绪能量
（积极 / 平淡 / 低落 / 愤怒 / 中性）认出来，写进提示词并给出字数预算。同样纯规则。

v2-chat-mirror 的字数跟随补强：光有"劲头 → 粗预算"还是不够——实机看下来短句照样被抻长。
这一补按对方这句的**字数**把"回多长"说成具体几个字（`reply_char_span` / `length_hint`，
≤8 字 ≈1~1.5 倍且硬顶 +8 字），`budget_for_text` 再给短句压一道 token 顶。
"""

from __future__ import annotations

from life.dialog import router
from life.dialog.router import (
    EMOTION_ANGRY,
    EMOTION_ANXIOUS,
    EMOTION_CALM,
    EMOTION_HAPPY,
    EMOTION_LOW,
    INTENT_CHAT,
    INTENT_COMMAND,
    INTENT_CONFIRM,
    INTENT_HELP,
    INTENT_VENT,
)

# 情绪 → 语气修饰（软化的 / 欢快的）；平静不加戏
TONE_FOR_EMOTION: dict[str, str] = {
    EMOTION_LOW: "语气放软、温和一点，别太轻快",
    EMOTION_ANXIOUS: "语气平稳、给人安心，别加重紧张",
    EMOTION_ANGRY: "先不争对错，语气克制、顺着情绪捋，别火上浇油",
    EMOTION_HAPPY: "跟着开心，轻快活泼一点",
    EMOTION_CALM: "",
}

# 意图 → 回应分寸
HINT_FOR_INTENT: dict[str, str] = {
    INTENT_VENT: "用户在倾诉、说心里话：先接住情绪、共情，别急着给方案或建议，除非对方主动问",
    INTENT_HELP: "用户在求助：先共情一句，再给具体、可行、不空泛的建议",
    INTENT_CHAT: "轻松闲聊，别端着",
    INTENT_COMMAND: "用户在下指令：一句简短回应即可，别长篇大论",
    INTENT_CONFIRM: "用户只是确认/应声：一句话轻轻回掉即可",
}


# ---------- v2-chat-mirror：回话的情绪与字数跟着对方走 ----------
#
# 用户真机反馈：回复"情绪平、字数恒定"——对方兴致勃勃说一大段，和只回一个"嗯"，
# 宠物都是同一套恒温的软萌短句。根子在两处：人设里写死"每次只说 1~2 句"（见
# pet/persona.py 的 style_rules），生成参数又固定 max_tokens=300（life/psych/casual.py），
# 于是"回多长、什么调子"跟对方那句话没有任何关系。
#
# 这里按**这一句的情绪能量**把对方的节奏认出来，写成一条提示词注入（两条回话路共用），
# 顺带给出字数预算（casual 那条路拿它当 max_tokens）。纯规则、不调模型；
# 认不出来就是"中性"，跟以前一个样。

ENERGY_BRIGHT = "积极"
ENERGY_FLAT = "平淡"
ENERGY_LOW = "低落"
ENERGY_ANGRY = "愤怒"
ENERGY_NEUTRAL = "中性"

ENERGY_ORDER: tuple[str, ...] = (
    ENERGY_BRIGHT, ENERGY_FLAT, ENERGY_LOW, ENERGY_ANGRY, ENERGY_NEUTRAL,
)

# 短到只是随口应一声（"嗯""哦""好""知道了"）：没情绪、没内容。
FLAT_MAX_CHARS = 5
# 说得又多又带劲：对方起劲，宠物也起劲。
BRIGHT_MIN_CHARS = 14

# 每种能量对应的一条说话分寸。逐条都是冲着"去掉恒定无波动的萌系"写的：
# 该长的时候允许长，该短的时候不许硬凑情绪。
MIRROR_FOR_ENERGY: dict[str, str] = {
    ENERGY_BRIGHT: (
        "对方这一句说得起劲、有兴致：你跟着热络起来，可以多接两句、把自己的反应和想法"
        "接上去，有来有回地聊；别只丢一句冷冰冰的短话就完事。"
    ),
    ENERGY_FLAT: (
        "对方这一句只是随口应一声，没什么情绪也没什么内容：你就平实地短回一句，"
        "别硬凑情绪、别强行关心、别追问、别把话头往深里带。"
    ),
    ENERGY_LOW: (
        "对方这一句情绪是沉的：先把语气放软接住他，别轻快、别讲道理、别急着给建议。"
    ),
    ENERGY_ANGRY: (
        "对方在气头上：先顺着他的情绪、别争对错、别火上浇油。"
    ),
    ENERGY_NEUTRAL: (
        "对方这一句是平常说话：轻松接住就好，长度跟他差不多，别刻意加戏。"
    ),
}

# 每种能量允许的回话长度（casual 那条路的 max_tokens）。
# 中性就是原来那个数（casual.CASUAL_MAX_TOKENS）——认不出来的时候一个字都不改。
BUDGET_FOR_ENERGY: dict[str, int] = {
    ENERGY_BRIGHT: 450,
    ENERGY_NEUTRAL: 300,
    ENERGY_FLAT: 80,
    ENERGY_LOW: 260,
    ENERGY_ANGRY: 220,
}

# 压在每一条后面的总纲：人设那条"每次只说 1~2 句"从死规矩降成常态，
# 这一轮的劲头说了算。
MIRROR_TAIL = "（长度按对方这句的劲头来，不必死守「每次只说 1~2 句」；但也不用比对方更热。）"


# ---------- v2-chat-mirror 补：字数跟随的**数值**策略 ----------
#
# 上一版只把"这一句劲头多大"认出来、给一个粗预算（能量 → token），没把"回多长"
# 说成**具体几个字**——该短该长全让模型自己领会，实机看下来短句照样被抻成一大段。
# 这一补直接按对方这句的**字数**给出回话该落在的字数区间：
#
#   n ≤ 8（随口一句）：≈ 1~1.5 倍，且硬顶"不比他多 8 个字"；
#   n > 8（说得比较多）：同样 1~1.5 倍、硬顶 n+8——也就是"略长但克制"，不滚雪球。
#
# v2-pet-persona-rename：人格尾缀没了（人设不再带"喵"这种尾巴），区间就是正文本身，
# 不再需要"尾缀不算字数"那条豁免。
SHORT_MAX_CHARS = 8      # ≤ 这个字数算"随口一句"，走最紧的一档
REPLY_MAX_EXTRA = 8      # 回话最多比对方多这么多字（硬顶）
# 短句那档的 token 顶：够说一句带尾缀的完整话，又不给抻长的余地。
SHORT_MAX_TOKENS = 80


def reply_char_span(text: str) -> tuple[int, int]:
    """对方这句 n 个字，回话该落在多少字（闭区间 [lo, hi]）。

    下限就是 n（不许比对方还短一截、显得敷衍）；上限取 ceil(1.5n) 与 n+8 的小者。
    纯整数算，不引 math：ceil(3n/2) 写成 (3n+1)//2。对方一个字没说就给 (0, 0)。
    """
    n = len((text or "").strip())
    if n <= 0:
        return (0, 0)
    hi = min((3 * n + 1) // 2, n + REPLY_MAX_EXTRA)
    return (n, max(n, hi))


def length_hint(text: str) -> str:
    """把"回多长"说成**具体字数**的那段提示词；对方一个字没说就不给。"""
    n = len((text or "").strip())
    lo, hi = reply_char_span(text)
    if n <= 0:
        return ""
    if n <= SHORT_MAX_CHARS:
        return (
            f"【字数跟着他走】他这一句 {n} 个字，你也回差不多长："
            f"大约 {lo}~{hi} 个字（约 1~1.5 倍，最多不比他多 8 个字），"
            "别把一句随口话抻成一大段。"
        )
    return (
        f"【字数跟着他走】他这一句 {n} 个字，你比他略长一点就行："
        f"大约 {lo}~{hi} 个字，克制些、别滚雪球。"
    )


def budget_for_text(text: str, route=None) -> int:
    """这一句该给多少 token：能量给一个数，短句再压一道顶。

    短句（n ≤ 8）不该按能量预算放开——他随口一句"我做了红烧肉"，回话是该同一量级的
    短句，不是 300 token 的篇幅。长句照旧听能量的，一个字不改。
    """
    budget = budget_for_energy(energy_of(text, route))
    n = len((text or "").strip())
    if 0 < n <= SHORT_MAX_CHARS:
        return min(budget, SHORT_MAX_TOKENS)
    return budget


def energy_of(text: str, route=None) -> str:
    """认出这一句的情绪能量（v2-chat-mirror）。纯规则，不调模型。

    判定顺序：愤怒 > 低落/焦虑 > 平淡 > 积极 > 中性——先看情绪，再看字数。
    短句优先于"开心"：一句光秃秃的"好呀"不该被当成起劲的积极。

    route 没给（或情绪字段空着）就现算一遍（`router.classify` 同样纯规则），
    这样调用方不接路由也能用。
    """
    body = (text or "").strip()
    if not body:
        return ENERGY_NEUTRAL
    if route is None or not getattr(route, "emotion", ""):
        route = router.classify(body)
    emotion = getattr(route, "emotion", "")
    if emotion == EMOTION_ANGRY:
        return ENERGY_ANGRY
    if emotion in (EMOTION_LOW, EMOTION_ANXIOUS):
        return ENERGY_LOW
    if len(body) <= FLAT_MAX_CHARS:
        return ENERGY_FLAT
    if emotion == EMOTION_HAPPY or len(body) >= BRIGHT_MIN_CHARS:
        return ENERGY_BRIGHT
    return ENERGY_NEUTRAL


def mirror_hint(text: str, route=None) -> str:
    """这一轮"怎么跟上对方"的那段提示词。任何能量都给，中性也给。

    两截拼起来：先按情绪能量说"该多热络"，再按**字数**说"该说几个字"
    （v2-chat-mirror 补的数值策略，见 `length_hint`）。
    """
    energy = energy_of(text, route)
    parts = [
        f"【跟上对方的节奏｜对方这一句是「{energy}」】"
        f"{MIRROR_FOR_ENERGY[energy]}{MIRROR_TAIL}",
        length_hint(text),
    ]
    return "".join(part for part in parts if part)


def budget_for_energy(energy: str) -> int:
    """这种能量下允许的回话长度。认不出来的能量回落到"中性"那个数。"""
    return BUDGET_FOR_ENERGY.get(energy, BUDGET_FOR_ENERGY[ENERGY_NEUTRAL])


def tone_for_emotion(emotion: str) -> str:
    """情绪 → 语气修饰（S2）。没认出来就空串，不加戏。"""
    return TONE_FOR_EMOTION.get(emotion, "")


def hint_for_route(route) -> str:
    """把 (意图, 情绪) 拼成要注入角色卡提示词的那一段。

    route 是 router.DialogRoute；传 None（危机短路、或没接路由）就空串。
    """
    if route is None:
        return ""
    tone = tone_for_emotion(route.emotion)
    intent_hint = HINT_FOR_INTENT.get(route.intent, "")
    parts = [part for part in (intent_hint, tone) if part]
    if not parts:
        return ""
    return f"【这一句怎么回：意图={route.intent}｜情绪={route.emotion}】" + "；".join(parts) + "。"
