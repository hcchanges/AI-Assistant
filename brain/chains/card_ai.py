"""
卡牌的 AI 能力链（对应指令 12 第 4 节 + 指令 24 第 4 条 + 指令 25-B 第 1 项）。

五种能力里，引导 / 建议 / 打卡鼓励 / 对话说到底都是"带着这张卡的上下文让模型说一段"，
区别只在任务提示，所以合在一个链里、用 action 区分。
提醒不在这里：它复用主动提醒调度（ReminderJobs），到点由关怀链开口。
跨卡画像那两条链（出行规划 / 饮食建议）也在这里，因为它们同样是"就这张卡说一段"，
只是出行那条走联网检索、并把画像字段拼进提示词。
品类判定的"第二层"（词表没命中时请模型判类）也在这里，它同样是"就一句话问模型"。

**人设不在这里**：是谁在说（宠物本体的性格腔调，还是这张卡绑定的角色卡）由调用方
（life/cards/service.py）拼好 system 传进来 —— 角色卡属于 life 层，链不该反过来依赖它。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from brain.llm.factory import LLMFactory
from brain.llm.search import SearchSource, build_query
from brain.prompts.templates import (
    CARD_ADVICE_NOTE_NONE,
    CARD_ADVICE_PROFILE_EMPTY,
    CARD_ADVICE_PROMPT,
    CARD_CATEGORY_PROMPT,
    CARD_TALK_PROMPT,
    CARD_TASK_PROMPT,
    TRAVEL_PLAN_PROMPT,
    TRAVEL_PLAN_SYSTEM,
    TRIP_SCENARIO_PROMPT,
    TRIP_SCENARIO_SYSTEM,
)
from core.config import Config
from data.models import Card

ACTION_TALK = "talk"
ACTION_GUIDE = "guide"
ACTION_ADVISE = "advise"
ACTION_CHECKIN = "checkin"

# 判类只要结构化结果，不要宠物腔调：给一句中性的系统提示，也顺带避开注入
CATEGORY_SYSTEM = "你是文本分类助手，只按给定的候选类别归类，只输出 JSON。"

ACTION_HINTS: dict[str, str] = {
    ACTION_GUIDE: (
        "把这张卡变清晰：帮他定一个够得着的小目标，再拆成两三个今天就能做的小步骤。"
        "步骤用短句说，不要写序号、不要 markdown。"
    ),
    ACTION_ADVISE: "结合这张卡和你记得的事，给一两条具体、马上能做的建议。",
    ACTION_CHECKIN: "他刚打完卡，鼓励他一句，顺带肯定他坚持下来的这一点，别再提新要求。",
}

MEMORY_EMPTY = "（暂无）"
HISTORY_EMPTY = "（刚开始聊）"
LAST_CHECKIN_EMPTY = "（还没打过卡）"
GOAL_EMPTY = "（还没定）"

# 画像字段的中文名：提示词里给的是"- 目的地：成都"这种，比丢一串英文键名好读，
# 模型也更不容易把 destination 当成别的东西。认不出的键原样带上，不丢信息。
PROFILE_LABELS: dict[str, str] = {
    "destination": "目的地",
    "frequency": "出行频次",
    "preference": "出行偏好",
    "last_meal": "最近一顿",
    "goal": "饮食目标",
    "allergy": "忌口",
    "task": "在赶的事",
    "deadline": "截止",
    "dept": "部门",
}

# 卡面建议（指令 27）：同一段提示词，出行卡按导游说、饮食卡按主厨说。
# 是谁在说由 system 决定（服务层按主题挂了角色卡），这里只管"要它说什么"。
ADVICE_HINTS: dict[str, str] = {
    "出行": "按接下来这一趟的行程给两三条能直接照做的安排，说清先后与时机。",
    "餐饮": "给一段今天就照着吃的建议：这顿怎么搭、怎么做得好吃，三五句说清。",
}
ADVICE_HINT_DEFAULT = "给两三条具体、今天就能用上的建议。"


def _tidy(prompt: str) -> str:
    """把提示词里连着的空行收成一个，免得留一段空的引起模型乱猜。"""
    return re.sub(r"\n{3,}", "\n\n", prompt)


def _parse_confidence(value) -> float:
    """把模型给的判类置信度夹到 0~1；给的不是数字就按 0（等于不可信）算。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(score, 0.0), 1.0)


class CardAIChain:
    def __init__(self, config: Config):
        self._config = config

    # ---------- 品类判定第二层（指令 25-B 第 1 项 ②） ----------

    def classify_category(self, text: str) -> tuple[str, float]:
        """词表没命中时请模型判类，返回（品类, 置信度）。

        品类不在配置的大类里就返回空串——认不出来，交给调用方决定要不要出邀请。
        """
        provider = LLMFactory.create(self._config)
        categories = "、".join(self._config.card.categories)
        data = provider.chat_json(
            CARD_CATEGORY_PROMPT.format(categories=categories, text=text),
            system=CATEGORY_SYSTEM,
        )
        category = str(data.get("category") or "").strip()
        if category not in self._config.card.categories:
            return "", 0.0
        return category, _parse_confidence(data.get("confidence"))

    def reply(
        self,
        action: str,
        card: Card,
        memories: Sequence[str],
        system: str,
        checkin_count: int = 0,
        last_checkin: str = "",
        history: Sequence[tuple[str, str]] = (),
        text: str = "",
    ) -> str:
        provider = LLMFactory.create(self._config)
        common = {
            "title": card.title,
            "theme": card.theme,
            "goal": card.goal or GOAL_EMPTY,
            "summary": card.summary or "（暂无）",
            "status": card.status,
            "checkin_count": checkin_count,
            "last_checkin": last_checkin or LAST_CHECKIN_EMPTY,
            "memories": "\n".join(f"- {item}" for item in memories) if memories else MEMORY_EMPTY,
        }
        if action == ACTION_TALK:
            prompt = CARD_TALK_PROMPT.format(
                **common,
                history="\n".join(f"{who}：{what}" for who, what in history) or HISTORY_EMPTY,
                text=text,
            )
        else:
            prompt = CARD_TASK_PROMPT.format(**common, action_hint=ACTION_HINTS[action])
        return provider.chat(prompt, system=system)

    # ---------- 链 a：出行规划（联网检索 + 画像） ----------

    def plan_trip(
        self, destination: str, date_text: str, profile: str
    ) -> tuple[str, tuple[SearchSource, ...]]:
        """按目的地与日期联网检索，返回（建议正文，真实来源）。

        这里不叠角色卡的腔调层：这一段是要照做的清单，说清比说得好听重要；
        联网提示词自己带了"只用真实信息"的约束。
        """
        prompt = TRAVEL_PLAN_PROMPT.format(
            destination=destination, date=date_text, profile=profile
        )
        answer = LLMFactory.create_search(self._config).ask_multi(
            prompt,
            TRAVEL_PLAN_SYSTEM,
            queries=(
                build_query(destination, "旅游攻略"),
                build_query(destination, "美食", "穿搭", "天气", "防晒"),
            ),
        )
        return answer.text, answer.sources

    def quick_trip(
        self, text: str, memories: Sequence[str]
    ) -> tuple[str, tuple[SearchSource, ...]]:
        """链 c：赶时间时的走法（指令 26-B ②）。

        和 plan_trip 的区别：那条是"定了目的地与日期，规划一趟出行"，
        这条是"现在就已经卡住了"，要的是最快到 + 一句提醒，不列清单。
        """
        prompt = TRIP_SCENARIO_PROMPT.format(
            now=datetime.now().strftime("%Y-%m-%d %H:%M"),
            text=text,
            memories="\n".join(f"- {item}" for item in memories) if memories else MEMORY_EMPTY,
        )
        answer = LLMFactory.create_search(self._config).ask(
            prompt,
            TRIP_SCENARIO_SYSTEM,
            search_query=build_query(text, "怎么走"),
        )
        return answer.text, answer.sources

    # ---------- 卡面建议（指令 27）：出行 / 饮食卡打开时按专业角色说一段 ----------

    def card_advice(
        self,
        card: Card,
        profile: dict[str, str],
        memories: Sequence[str],
        system: str,
        note: str = CARD_ADVICE_NOTE_NONE,
    ) -> str:
        """就这张卡给一段专业建议，交给上半框。

        跟 reply() 那条的区别：这条不接用户的话，是"打开卡就自动说一段"，
        说的是画像里已经记下的那点事（目的地、目标、忌口…），没记的就不提。
        note 是这次额外要交代的一句（饮食卡靠它带上"有出行就补一句外食怎么选"）。
        """
        lines = "\n".join(
            f"- {PROFILE_LABELS.get(key, key)}：{value}"
            for key, value in profile.items()
            if value
        )
        prompt = CARD_ADVICE_PROMPT.format(
            title=card.title,
            theme=card.theme,
            goal=card.goal or GOAL_EMPTY,
            profile_lines=lines or CARD_ADVICE_PROFILE_EMPTY,
            memories="\n".join(f"- {item}" for item in memories) if memories else MEMORY_EMPTY,
            action_hint=ADVICE_HINTS.get(card.theme, ADVICE_HINT_DEFAULT),
            note=note,
        )
        # note 为空（出行卡）时那一行会留下连续空行，收一收，提示词干净些
        return LLMFactory.create(self._config).chat(_tidy(prompt), system=system)

    # 链 b（饮食建议）在指令 27 并进了 card_advice：那张卡现在由营养主厨角色说，
    # "近期有出行就补一句外食怎么选"改走 card_advice 的 note 参数，不再单开一条链。
