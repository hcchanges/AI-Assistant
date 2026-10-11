"""意图 + 情绪路由（S1）：classify(text) -> DialogRoute(意图, 情绪)，纯规则关键词。

意图五类：倾诉 / 求助 / 闲聊 / 指令 / 确认。
优先级（先查到的赢）：指令 > 倾诉 > 求助 > 闲聊 > 确认；四张词表一个都没命中就兜底成「确认」。

情绪五类：低落 / 焦虑 / 愤怒 / 开心 / 平静（复用 B 养成 emotion_snapshot 那一档的
情绪维度，这里把它钉成五档死规则，不交给模型）。一个都没命中就兜底成「平静」。

为什么「焦虑」不进倾诉词表、却进情绪词表：倾诉是"说心里话、要人接着"，求助是"要办法"。
「怎么缓解焦虑」里的"焦虑"只是点题，整句在要办法，该判求助；
「好烦，怎么办」里的"好烦"才是倾诉，优先级压在"怎么办"上面。
所以倾诉词表只放"发泄式的情绪话"（好累 / 崩溃 / 好烦 / 难过…），情绪词归情绪词表，两套分开。

A+++ 之后这里仍然是**纯规则、主线程、零延迟**——它是"模型回来之前"的即时值，
所以一个字都不能让它等模型。模型那条线在解析链（brain/chains/chatter_parse.py）：
回来之后拿 ``refine()`` 校正这里给出的结果，越界/认不出就原样保留规则值。

``classify`` 开头还有一道 **危机词短路**（防御纵深）：真危机该由调用方在更前面拦掉
（chatter 拦过一道），这里是"万一谁忘了拦"的兜底——命中就直接返回、
既不判意图也不调模型。危机那条线的安全兜底永远在规则层，模型不参与。
"""

from __future__ import annotations

from dataclasses import dataclass

from brain.prompts.templates import SPEAKER_USER
from life.psych.crisis import is_crisis

# ---------- 意图 ----------

INTENT_COMMAND = "指令"
INTENT_VENT = "倾诉"
INTENT_HELP = "求助"
INTENT_CHAT = "闲聊"
INTENT_CONFIRM = "确认"

# 合法的意图取值（按优先级排）。模型给的值不在这张表里一律算越界 → 回落规则
VALID_INTENTS: tuple[str, ...] = (
    INTENT_COMMAND, INTENT_VENT, INTENT_HELP, INTENT_CHAT, INTENT_CONFIRM,
)

# 指令：让宠物做事（聊天是主入口）。只放足够明确的动作词，别把"打开门"也划进来
COMMAND_WORDS: tuple[str, ...] = (
    "关掉", "关闭", "退出",
    "躲起来", "隐身", "藏起来",
    "看看今天", "今日活动", "都在干嘛", "时间统计",
    "打开生活", "打开工作", "打开设置",
    "陪我玩", "陪玩", "不用陪", "别陪",
)

# 倾诉：说心里话、要人接着（发泄式的情绪话，不含"焦虑"这类点题词）
VENT_WORDS: tuple[str, ...] = (
    "好累", "累", "累死", "崩溃", "好烦", "烦", "烦死",
    "难过", "难受", "委屈", "想哭", "没劲", "孤独", "压抑",
    "没意思", "受不了", "撑不住", "心累", "好难", "好苦",
    "气死", "好气", "生气", "emo", "不开心", "低落", "绝望", "不想说话",
)

# 求助：要办法、要答案
HELP_WORDS: tuple[str, ...] = (
    "怎么办", "怎么", "如何", "帮我", "教我", "建议",
    "缓解", "解决", "有什么办法", "该怎么做", "怎么才能", "怎么样才能", "求助",
)

# 闲聊：轻松搭话
CHAT_WORDS: tuple[str, ...] = (
    "哈哈", "嘿嘿", "嘻嘻", "天气", "真好", "吃了吗", "好玩", "有意思",
    "好耶", "好呀", "不错", "挺好", "还行", "好看", "好听", "喜欢",
)

# 确认没有独立词表：它是兜底（上面四类都没沾上就是确认）。

# ---------- 犹豫（不是第六类意图） ----------
#
# "拿不定主意"跟"求助"不是一回事：求助是"给我办法"，犹豫是"两个选项我选不出"。
# 但**不为它单开一类意图**——那会动到上面五类的优先级，得把前面几轮的判例
# 重判一遍，代价大、收益小。这里只出一个判据，给"反问拿主意"那条线用
# （见 life/mind/intimacy.py 的 ask_back_hint）。
HESITATE_WORDS: tuple[str, ...] = (
    "纠结", "犹豫", "拿不定", "拿不准", "选不出", "选哪个", "不知道选",
    "不知道该怎么选", "该不该", "要不要", "两难", "要不要啊",
    "好不好啊", "哪个好", "值得吗", "值不值",
)


def is_hesitating(text: str) -> bool:
    """这句话是不是在拿不定主意。纯词表，一个模型都不调。"""
    return _has(text, HESITATE_WORDS)


# ---------- 寒暄（不是第六类意图） ----------
#
# 「早上好」原先四张词表都不命中，兜底成「确认 + 平静」——这个归类本身没错，问题是
# **没有任何东西告诉模型"这一句只是打个招呼"**，于是它按宠物人设自由发挥成关怀腔
# （用户真机反馈 #3）。这里出一个判据，给"这一轮平实回"那条用。
#
# 为什么不单开一类意图：那会动到上面五类的优先级，前几轮的判例得重判一遍，
# 代价大、收益小。判据只喂给语气那一层，跟 HESITATE_WORDS 一个待遇。
GREETING_WORDS: tuple[str, ...] = (
    "早上好", "早安", "上午好", "中午好", "下午好", "晚上好", "晚安",
    "你好", "您好", "哈喽", "在吗", "在么", "早",
)

# 超过这么长就不算"纯粹打招呼"了——长句里的"你好"多半是别的话头（"你好烦"）
GREETING_MAX_LEN = 12

# 寒暄里常见的语气词/标点：判"整句只剩一句招呼"时先抹掉
_GREETING_NOISE = "，。！？!?,、~～ 呀啊哦哈呢了啦吧哟"


def is_greeting(text: str) -> bool:
    """这句话是不是**一句纯粹的寒暄**。纯词表，不调模型。

    口径卡得紧：必须**短**，而且把语气词与寒暄词都摘掉之后要**一个字不剩**。
    「晚上好」算，「你好呀」算；「你好烦」「早上好，我今天好累」都不算——后面这两种
    要是也按"平实应一声"处理，用户真说的事就被寒暄那条规则压掉了，比不认更糟。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > GREETING_MAX_LEN:
        return False
    core = stripped
    # 先摘寒暄词，再抹语气词与标点：反过来的话「哈喽」会先被当语气词摘掉那个"哈"，
    # 剩下的"喽"就再也对不上词表了。
    matched = False
    for word in GREETING_WORDS:
        if word in core:
            core = core.replace(word, "")
            matched = True
    for mark in _GREETING_NOISE:
        core = core.replace(mark, "")
    return matched and not core


# ---------- 情绪 ----------

EMOTION_LOW = "低落"
EMOTION_ANXIOUS = "焦虑"
EMOTION_ANGRY = "愤怒"
EMOTION_HAPPY = "开心"
EMOTION_CALM = "平静"

HAPPY_WORDS: tuple[str, ...] = (
    "开心", "哈哈", "嘿嘿", "嘻嘻", "高兴", "真好", "太好了", "好幸福",
    "笑死", "好耶", "快乐", "棒", "爽", "喜欢", "开心死",
)

# 刻意不放单个"气"字——"天气"里也带"气"，会误判成愤怒
ANGRY_WORDS: tuple[str, ...] = (
    "愤怒", "生气", "气死", "好气", "火大", "恼火", "气人", "来气",
    "想骂人", "凭什么", "气炸", "火冒三丈",
)

ANXIOUS_WORDS: tuple[str, ...] = (
    "焦虑", "紧张", "担心", "害怕", "慌", "不安", "忐忑", "着急", "睡不着",
)

LOW_WORDS: tuple[str, ...] = (
    "累", "崩溃", "难过", "伤心", "委屈", "想哭", "没劲", "低落",
    "难受", "孤独", "压抑", "没意思", "绝望", "emo", "不开心", "烦", "心累",
)

# 负向三档：情绪触发 B 关怀（S4）只认这三档
NEGATIVE_EMOTIONS: tuple[str, ...] = (EMOTION_LOW, EMOTION_ANXIOUS, EMOTION_ANGRY)

# 合法的情绪取值（按判定顺序）。同上：不在表里就是越界 → 回落规则。
VALID_EMOTIONS: tuple[str, ...] = (
    EMOTION_HAPPY, EMOTION_ANGRY, EMOTION_ANXIOUS, EMOTION_LOW, EMOTION_CALM,
)

# 模型吐的是**自由词**（解析链的 emotion 字段是"疲惫/烦躁/压力大"这类，不是这五档），
# 这里把常见的收进五档里；收不进来的（生造词、外文词）就当越界，保留规则值。
# 这是"模型校正规则"的翻译层——不然后面谁都看不懂模型说了什么情绪。
MODEL_EMOTION_ALIASES: dict[str, str] = {
    # 开心
    "高兴": EMOTION_HAPPY, "快乐": EMOTION_HAPPY, "愉快": EMOTION_HAPPY,
    "兴奋": EMOTION_HAPPY, "轻松": EMOTION_HAPPY, "满足": EMOTION_HAPPY,
    "还行": EMOTION_HAPPY, "不错": EMOTION_HAPPY, "期待": EMOTION_HAPPY,
    # 愤怒
    "烦": EMOTION_ANGRY, "烦躁": EMOTION_ANGRY, "生气": EMOTION_ANGRY,
    "愤怒": EMOTION_ANGRY, "恼火": EMOTION_ANGRY, "不爽": EMOTION_ANGRY,
    "火大": EMOTION_ANGRY, "气": EMOTION_ANGRY, "抓狂": EMOTION_ANGRY,
    # 焦虑
    "紧张": EMOTION_ANXIOUS, "担心": EMOTION_ANXIOUS, "害怕": EMOTION_ANXIOUS,
    "不安": EMOTION_ANXIOUS, "着急": EMOTION_ANXIOUS, "压力": EMOTION_ANXIOUS,
    "压力大": EMOTION_ANXIOUS, "焦躁": EMOTION_ANXIOUS, "忐忑": EMOTION_ANXIOUS,
    # 低落
    "疲惫": EMOTION_LOW, "累": EMOTION_LOW, "疲劳": EMOTION_LOW,
    "难过": EMOTION_LOW, "伤心": EMOTION_LOW, "委屈": EMOTION_LOW,
    "失落": EMOTION_LOW, "沮丧": EMOTION_LOW, "孤独": EMOTION_LOW,
    "压抑": EMOTION_LOW, "无力": EMOTION_LOW, "emo": EMOTION_LOW,
    # 平静
    "中性": EMOTION_CALM, "平常": EMOTION_CALM, "没什么": EMOTION_CALM,
}


@dataclass(frozen=True)
class DialogRoute:
    """这一句的路由结果。matched=False 表示走了兜底（无命中 → 确认 + 平静）。

    crisis=True 是危机短路那一支（见 classify 开头）：这种结果不进任何按意图分支的
    下游动作，只为"万一有人拿它当真"留个可判的标记。
    """

    intent: str
    emotion: str
    matched: bool = True
    crisis: bool = False


def _has(text: str, words: tuple[str, ...]) -> bool:
    """text 里有没有这些词里的任何一个。大小写不敏感（emo / ok 这类英文词）。"""
    lowered = text.lower()
    return any(word and word.lower() in lowered for word in words)


def _emotion_of(text: str) -> str:
    """情绪五选一：开心 > 愤怒 > 焦虑 > 低落，都没有就是平静。"""
    if _has(text, HAPPY_WORDS):
        return EMOTION_HAPPY
    if _has(text, ANGRY_WORDS):
        return EMOTION_ANGRY
    if _has(text, ANXIOUS_WORDS):
        return EMOTION_ANXIOUS
    if _has(text, LOW_WORDS):
        return EMOTION_LOW
    return EMOTION_CALM


def classify(text: str, speaker: str = SPEAKER_USER) -> DialogRoute:
    """把一句话分成 (意图, 情绪)。纯规则，不调模型、不碰库、零延迟。

    优先级 指令 > 倾诉 > 求助 > 闲聊 > 确认（先查到的赢）；无命中兜底「确认」。
    情绪独立算，跟意图互不干扰。

    第一道是危机词（防御纵深，见模块开头）：命中就原样返回，不进五类分支、
    不调模型，情绪给"低落"——真出了事，往"更软"那边偏永远比往"更硬"那边偏安全。

    ``speaker``（v2-social-card-base）：这句话是谁说的。**规则一条都不按它分**——词表
    （指令 / 倾诉 / 求助 / 闲聊 / 确认）对谁说话都是同一套，这条路本来就是纯文本命中，
    没有"用户"这个前提可换。参数在这儿是为了跟意图底座那三层（`IntentJudge.judge` /
    `IntentChain.classify` / `ChatterParseChain.analyze`）**同一个口径**：调用方按说话者
    传下去，将来真有按说话者分表的词表时不用再改签名。默认 ``"user"``，行为零变化。
    """
    _ = speaker          # 规则与说话者无关，签名对齐用（见上面那段）
    t = text or ""
    if is_crisis(t):
        return DialogRoute(INTENT_CONFIRM, EMOTION_LOW, matched=False, crisis=True)
    if _has(t, COMMAND_WORDS):
        return DialogRoute(INTENT_COMMAND, _emotion_of(t))
    if _has(t, VENT_WORDS):
        return DialogRoute(INTENT_VENT, _emotion_of(t))
    if _has(t, HELP_WORDS):
        return DialogRoute(INTENT_HELP, _emotion_of(t))
    if _has(t, CHAT_WORDS):
        return DialogRoute(INTENT_CHAT, _emotion_of(t))
    return DialogRoute(INTENT_CONFIRM, _emotion_of(t), matched=False)


def _norm_intent(value: object) -> str:
    """模型给的意图归一；不在五类里（含空串、None、生造词）返回空串。"""
    return value.strip() if isinstance(value, str) and value.strip() in VALID_INTENTS else ""


def _norm_emotion(value: object) -> str:
    """模型给的情绪归一：先看是不是五档原词，再看别名表；都认不出返回空串。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if text in VALID_EMOTIONS:
        return text
    return MODEL_EMOTION_ALIASES.get(text.lower(), "")


def refine(route: DialogRoute, intent: object = "", emotion: object = "") -> DialogRoute:
    """拿模型这一次的判断校正规则结果。越界 / 认不出一律**保留规则值**。

    只换值、不换 `matched`：matched 说的是"规则层当时有没有命中"，那是事实，
    模型改不了。危机那一支原样返回——安全兜底不归模型管。
    """
    if route.crisis:
        return route
    new_intent = _norm_intent(intent) or route.intent
    new_emotion = _norm_emotion(emotion) or route.emotion
    if new_intent == route.intent and new_emotion == route.emotion:
        return route
    return DialogRoute(new_intent, new_emotion, route.matched)
