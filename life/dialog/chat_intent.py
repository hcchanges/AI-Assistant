"""闲聊 vs 动作意图（v2-chat-intent）：这句话要不要真的去做点什么。

**先盘点现有的三套意图层**（它们各管一段，别混着看）：

| 层 | 在哪儿 | 判什么 | 谁在用 |
| --- | --- | --- | --- |
| 路由骨架 | `life/dialog/router.py` | 五类：指令 / 倾诉 / 求助 / 闲聊 / 确认（外加情绪五档） | 挑语气与回话框架 |
| 主动服务 | `life/intent.py` | 工作 / 生活 / 都不动（+生活品类、紧急） | 要不要弹卡、开工作模块 |
| 吐嘈解析 | `brain/chains/chatter_parse.py` | 情境 / 时间 / 情绪 / 主题 / 让宠物做的事（**模型给**） | 建提醒、挑卡 |

三套里**没有一套**回答这个问题："这句报了时间，用户是不是真要我到点做点什么？"
缺了这一环，模型只要在「我今晚吃了红烧肉你呢」里读出"今晚"（`time_confidence=fuzzy`），
状态机就进"等用户补答时间"、反问一句"「今晚」大概是几点呀"——用户真机上报的
"闲聊被当成提醒意图"就是这么来的。

这个模块补的就是那一环。三条纪律：

- **纯词表、主线程、零延迟、不调模型**。它是"模型回来之前"就该定下来的即时值，
  所以一个字都不能让它等模型（跟 `router.classify` 同一个姿态）。
- **口子开给"请求"，不开放给"提及"**。「提醒我八点吃药」是请求（要我做），
  「我今晚吃了红烧肉」是提及（只是在说给自己听）——**时间词本身不算请求**。
  提醒那条线的词表沿用 `config.intent.remind_words`（跟 `IntentJudge` 同一份，
  用户能在设置里加词），这里不另开一张表，免得两边漂移。
- **多意图只取一个**（指令 ④）：一句话里同时有专注和提醒的信号时，按
  **显式专注意图 > 提醒请求 > 闲聊** 取第一个，绝不并行输出两个互斥动作。

这一层**不改**任何模块自己的功能实现，只决定"这句话该交给谁"。
"""

from __future__ import annotations

# 这句话最终落在哪一档（同一时刻只会有一个）
ACTION_CHAT = "chat"        # 闲聊陪伴：只回话，不提醒、不计时、不开番茄钟
ACTION_REMIND = "remind"    # 到点提醒：可能反问"几点"、可能建提醒
ACTION_FOCUS = "focus"      # 显式专注意图：开一轮番茄钟

# 显式专注意图（v2-chat-intent ②）。**必须是明确说出口的**："我要专注""帮我计时"
# 这种；"我在写代码""有点忙"一律不算——那只是陈述状态，不是让我开计时器。
#
# 为什么卡这么死：番茄钟一开就是个会占着屏幕角落、还会静音宠物的东西（见
# `PetWindow._focus_silenced` 与 `_is_focusing` 那一串门控）。误开的代价远大于漏开——
# 漏开用户再说一句就是了，误开是"我没让它干这个，它自己干上了"。
explicit_focus_words: tuple[str, ...] = (
    "我要专注", "我要专心", "开始专注", "陪我专注", "进入专注",
    "专注模式", "番茄钟", "番茄钟开始",
    "开始计时", "帮我计时", "给我计时", "计个时",
    "陪我工作", "陪我学习", "陪我写", "陪我干活", "陪我赶",
    "我要学习", "我要写代码", "开始学习", "开始工作",
)

# 负面词：句子里带这些的，**不算**专注请求（"别陪我工作""不用专注了"）。
# 跟 router.py 的 `is_greeting` 一个道理——不挡的话"别开始计时"会把计时开起来，
# 那就成了字面反着做。
_focus_negations: tuple[str, ...] = (
    "别陪", "不用陪", "不要陪", "别开始", "不用开始", "不要开始",
    "结束专注", "停止专注", "退出专注", "不专注", "别专注", "别计时", "不用计时",
)


def _has(text: str, words: tuple[str, ...]) -> bool:
    """text 里有没有这些词里的任何一个。大小写不敏感（写代码 / 写 Python 都一样）。"""
    lowered = (text or "").lower()
    return any(word and word.lower() in lowered for word in words)


def explicit_focus(text: str) -> bool:
    """这句话是不是**明说**要我开一轮番茄钟。

    先看否定词：带"别陪/结束专注/不用计时"的整句直接否——不然"别开始计时了"
    会因为含"开始计时"这四个字把计时开起来。
    """
    if _has(text, _focus_negations):
        return False
    return _has(text, explicit_focus_words)


def wants_reminder(text: str, remind_words: tuple[str, ...] | list[str]) -> bool:
    """这句报了时间，是不是**要我到点做点什么**。

    `remind_words` 用 `config.intent.remind_words`（"提醒我 / 叫我 / 喊我 / 闹钟 /
    别忘 / 记着"…）。命中才是请求；不命中就是纯粹的提及或闲聊——哪怕句子里有
    "今晚""几点""什么时候"。

    这就是用户要的那条："今晚 / 几点 / 什么时候 在闲聊语境（无明确意图）下，
    不绑定计时、提醒动作，仅作自然回应"。
    """
    return _has(text, tuple(remind_words or ()))


def action_of(text: str, remind_words: tuple[str, ...] | list[str] = ()) -> str:
    """这一句取哪一档：专注 > 提醒 > 闲聊（**只取一个**）。

    优先级按"意图有多专属"排：显式说要专注是最具体的（它有专属词、有专属动作），
    提醒请求次之，其余一律按闲聊陪伴——不并行输出互斥动作（指令 ④）。
    """
    if explicit_focus(text):
        return ACTION_FOCUS
    if wants_reminder(text, remind_words):
        return ACTION_REMIND
    return ACTION_CHAT


__all__ = [
    "ACTION_CHAT", "ACTION_FOCUS", "ACTION_REMIND",
    "action_of", "explicit_focus", "explicit_focus_words", "wants_reminder",
]
