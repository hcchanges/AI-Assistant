"""
心理安抚服务（对应指令 11 + 指令 14 + 指令 24）：一个实例 = 一个钉死的人格。

一次回话分三道：
1. 本地危机词先拦——命中就直接给危机话术（含全国心理援助热线 12356），不经过模型；
   这个实例挂在哪个页面都一样拦：玄机先生也算计不到生命，红线不因为"换了个说法"而让路。
2. 其余交给本实例绑定的那张角色卡：拼 messages（专业框架 + 安全策略 + 性格腔调 + few-shot 示范
   + 你最近的记忆 + 这次说的话），用这张卡自己的 temperature / top_p 生成回话。
3. 万一第 2 步没跑成（断网 / 没填 Key / 接口报错），且这句话是软表达（"撑不下去"这类
   指向强但歧义大的），就补一句本地关怀，别让它像没听见（见 crisis.soft_care_reply）。

人格在构造时就定下来，运行期没有"换身份"这回事（"和谁聊"已删）。宠物本体 = 林医生，
回话接到宠物气泡。玄学页那条通道随玄学族合并下线了；玄机先生如今只当命理卡的陪执行角色，
卡内那套腔调由卡牌服务按 persona 走（见 CardService._owner_persona）。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime

from brain.llm.base import LLMOfflineError
from brain.llm.factory import LLMFactory
from core.config import Config
from core.events import EventBus
from typing import Any, Callable

from core.reltime import memory_text
from core.worker import run_async
from life.dialog import invite, mood, plan, router
from life.dialog.router import INTENT_CHAT, INTENT_COMMAND, INTENT_CONFIRM
from life.psych import casual, comfort, crisis, grounding
from life.psych.persona_loader import DEFAULT_CHAT_PERSONA, load_persona
from memory.store import MemoryStore
from pet.persona import persona_style_layer

logger = logging.getLogger(__name__)

# 注入多少条长期记忆：太多会稀释当下这句话，太少又显得不记得你
MEMORY_LIMIT = 5

# 没有记忆时的占位，免得 system 里留一块空白让模型乱猜
NO_MEMORY_TEXT = "（暂无）"

# 往上带多少轮**本场对话的原话**（v2-chat-core #6）。带的是消息条数，一问一答算两条。
# 8 = 最近四轮：够接住"你刚才说的那个"这类回指，又不至于把整场聊天塞进每一次请求。
# 长期记忆（上面那 5 条）是另一回事——那是跨会话提炼出来的，这里是本场原文。
HISTORY_TURNS = 8

# ---------- 闲聊那条路的分界（v2-speech-polish ③） ----------
#
# 走林医生的四步框架，还是走"像熟人搭话"那条轻路，判据是**这一句的意图**：
# 指令 / 闲聊 / 确认 = 用户没在求助 → 轻路；倾诉 / 求助 = 真有事 → 照旧专业框架。
# 再加一道情绪闸：意图判成闲聊、可情绪分很高（0.35 以上），说明这句其实挺沉的，
# 宁可回到专业框架，也不要用轻路把它接轻了。**越界一律退回专业框架**——
# 路由没接上、意图认不出、字段缺失，统统当"不是闲聊"，一个字都不改。
CASUAL_INTENTS: tuple[str, ...] = (INTENT_COMMAND, INTENT_CHAT, INTENT_CONFIRM)
CASUAL_EMOTION_MAX = 0.35

# 断网兜底那句（v2-offline-fallback）：没接上 intimacy 的按熟悉度寒暄时就用它。
# 与 life/mind/intimacy.py 的 OFFLINE_GREETING_DEFAULT 同一句，别改成两套说法。
OFFLINE_DEFAULT_REPLY = "网络断了，我先陪你坐着~ 联网再聊。"


class PsychService:
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        memory_store: MemoryStore,
        persona_id: str = DEFAULT_CHAT_PERSONA,
    ):
        self._config = config
        self._bus = bus
        self._memory_store = memory_store
        self._persona_id = persona_id
        # 回话往哪儿送：宠物气泡
        self._emit = bus.pet_speak.emit
        # 追加腔调（好感度够了"更亲一点"，见 life/mind/intimacy.py）。
        # 默认没有——不接就是这个宠物本来的说话方式，跟这功能上线前一样
        self._tone_provider: Callable[[], str] | None = None
        # "反问拿主意"那条（B+ 知己档，L4 的 ask_back）。要看得见这句话和路由，
        # 所以签名是 (route, text) 而不是空参——跟 tone_provider 分开两条，
        # 它是有条件的，不是每句都注入
        self._ask_back_provider: Callable[[Any, str], str] | None = None
        # "翻旧记录"那条（v2-rag）：用户在问自己记过的事时，把本机存的原文捞出来。
        # 签名 (text) → 一段上下文。**顺带判"这句要不要翻"**——provider 自己有词表
        # （见 life/mind/kb.py），不是这句就问，它才有资格说"要不要走检索"。
        # 判定放在 provider 里而不是这儿，是为了让"召回策略"整个待在一个文件里，
        # 这儿只管"有就拼、没有就算了"
        self._kb_provider: Callable[[str], str] | None = None
        # 断网陪伴寒暄（v2-offline-fallback）：签名 () → 一句话，按熟悉度挑。
        # 跟 tone_provider 一样后接、可选，没接上就用 OFFLINE_DEFAULT_REPLY。
        self._offline_reply_provider: Callable[[], str] | None = None
        # 本场对话的原话（v2-chat-core #6）：一问一答按顺序攒着，回话时带上去。
        # 只活在内存里、只在这个实例上，进程退出就没了——它不是记忆，是"前文"。
        self._history: deque[tuple[str, str]] = deque(maxlen=HISTORY_TURNS)

    def set_tone_provider(self, provider: Callable[[], str] | None) -> None:
        """接上"追加腔调"的来源（一段文本，空串等于不加）。

        做成后接而不是构造参数，是因为这个服务先于情感记忆建起来（见 bootstrap），
        而情感记忆可能压根没开——没接上就一个字都不加。
        """
        self._tone_provider = provider

    def set_ask_back_provider(self, provider: Callable[[Any, str], str] | None) -> None:
        """接上"反问拿主意"的来源（B+ 知己档）。签名 (route, text) → 提示词。

        跟 tone_provider 一样是后接的：没接上就一个字都不注入，回话跟以前一模一样。
        """
        self._ask_back_provider = provider

    def set_kb_provider(self, provider: Callable[[str], str] | None) -> None:
        """接上"翻旧记录"的来源（v2-rag）。签名 (text) → 上下文，空串等于不翻。

        同 tone_provider：后接、可选、没接上就一个字都不注入。provider 内部自己判
        "这句话要不要翻"——不归这句问的，回空串，回话跟没这功能时一模一样。
        """
        self._kb_provider = provider

    def set_offline_reply_provider(self, provider: Callable[[], str] | None) -> None:
        """接上"断网陪伴寒暄"的来源（v2-offline-fallback）。签名 () → 一句话。

        同 tone_provider：后接、可选、没接上就用 OFFLINE_DEFAULT_REPLY。断网时这句
        替掉「没听懂」——让宠物还在、还在陪你，只是这条命没联网而已。
        """
        self._offline_reply_provider = provider

    # ---------- 我是谁 ----------

    @property
    def persona_id(self) -> str:
        """说话的是哪张角色卡（构造时定死，运行期不变）。"""
        return self._persona_id

    @property
    def display_name(self) -> str:
        """界面上的说话人名字：本体用宠物名，别的角色卡用卡上的名字。"""
        if self._persona_id == DEFAULT_CHAT_PERSONA:
            return self._config.pet.name
        return load_persona(self._persona_id).name

    # ---------- 回话 ----------

    def respond(
        self,
        text: str,
        extra: str = "",
        on_spoken=None,
        emotion_score: float = 0.0,
        route=None,
        conflict=None,
    ) -> None:
        """用户说了一句话，用本实例的角色卡回话。

        extra 是功能性的尾巴（例如"（19:30 我喊你）"），由调用方给定、不走模型。
        on_spoken 是"它把这句话说完之后"的回调：调用方靠它保证后续动作排在回话后面
        （例如卡牌服务的观察要等回话说完，否则会抢在回话前面开口）。
        emotion_score 是这次这句话的负向情绪强度（解析链给的分）：拿它挑安抚分寸
        （轻 / 中 / 重）与参考话术（指令 26-A）。
        route 是会话层按纯规则分的 (意图, 情绪)（life/dialog/router.py 的路由骨架）：
        有就拼成提示词注入角色卡消息（情绪落在语气、意图落在回应分寸）；没有（危机短路
        或没接路由）就是空串，一字不改。
        conflict 是会话层发现的地点前后不一致（v2-chat-core #6，`life/dialog/location.py`）：
        有就让宠物先把矛盾摆出来问一句。没有就是 None，一字不改。
        危机那一支不回调：那种话不该进卡牌观察，更不该被攒成一张卡。
        """
        if crisis.is_crisis(text):
            logger.warning("检测到危机表达，改走危机话术（当前人格 %s）", self.display_name)
            self._emit(crisis.CRISIS_REPLY)
            return
        run_async(
            self._compose,
            text,
            emotion_score,
            route,
            conflict,
            on_done=lambda reply: self._speak(reply, extra, on_spoken),
            on_failed=lambda exc: self._failed(exc, text),
        )

    def _compose(
        self, text: str, emotion_score: float = 0.0, route=None, conflict=None
    ) -> str:
        """后台线程里做的事：取记忆、拼消息、调模型。

        两条路的分岔口就在这儿（v2-speech-polish ③）：这一句是**闲聊**就走 casual
        那条轻路（呆萌人设，不套四步咨询流程），是**求助/倾诉**就照旧走林医生那张卡。
        **除了 system 那一段和生成参数，两条路拼的东西完全一样**——记忆、语气分寸、
        翻出来的旧记录、追加腔调，一个都不少，所以"闲聊"不是"敷衍版"。

        两条路也共用同一份"别编造"约束与同一段**本场前文**（v2-chat-core #4 #5 #6）：
        见 `_system_hint` 与 `_with_history`。
        """
        pet = self._config.pet
        # 记忆正文里可能留着"明早"这类相对说法，按现在重新说一遍再交给模型
        now = datetime.now()
        memories = [
            memory_text(memory, now) for memory in self._memory_store.recall(text)
        ][:MEMORY_LIMIT]
        memory_lines = "\n".join(f"- {item}" for item in memories) if memories else NO_MEMORY_TEXT
        hint = self._system_hint(emotion_score, text, route, conflict)
        kb_text = self._kb(text)
        provider = LLMFactory.create(self._config)
        # 一句纯寒暄（"早上好"）：平实应一声就行，只有情绪不高的时候才算
        # （"晚上好，我今天快崩溃了"是来倾诉的，不能按招呼接掉）
        greeting = router.is_greeting(text) and _below(emotion_score, CASUAL_EMOTION_MAX)

        if self._is_casual(emotion_score, route):
            logger.debug("这一轮是日常闲聊，走轻路回话（不套专业框架）")
            messages = casual.messages(
                pet,
                pet.name,
                text,
                memory_lines,
                tone=self._tone(),
                comfort_hint=hint,
                kb_text=kb_text,
                greeting=greeting,
            )
            # 字数也跟着对方走（v2-chat-mirror）：他起劲就多给点额度，他只应一声就把
            # 额度压到一句话——比在提示词里干说"短一点"管用。
            # v2-chat-mirror 补：短句（≤8 字）不看能量、一律压到一句短话那档，
            # 免得"我做了红烧肉"这种随口一句也放开 300 token 抻成一大段。
            reply = provider.chat_messages(
                _with_history(messages, self._history),
                temperature=casual.CASUAL_TEMPERATURE,
                top_p=casual.CASUAL_TOP_P,
                max_tokens=mood.budget_for_text(text, route),
            )
        else:
            card = load_persona(self._persona_id)
            messages = card.messages(
                user_text=text,
                style_layer=persona_style_layer(pet, pet.name, extra=self._tone()),
                memory_text=memory_lines,
                comfort_hint=hint,
                kb_text=kb_text,
            )
            reply = provider.chat_messages(
                _with_history(messages, self._history),
                temperature=card.temperature,
                top_p=card.top_p,
                max_tokens=card.max_tokens,
            )
        self._remember(text, reply)
        return reply

    def _remember(self, text: str, reply: str) -> None:
        """把这一问一答记进本场前文（v2-chat-core #6）。

        接在**生成成功之后**，不是在收到用户那句话时：回话没跑成（断网 / 没填 Key）
        这一轮就不该留在前文里——否则下一轮模型会看见一个自己从没回过的问题。
        """
        self._history.append(("user", text))
        self._history.append(("assistant", reply))

    def _is_casual(self, emotion_score: float, route) -> bool:
        """这一句该不该走闲聊那条轻路（v2-speech-polish ③）。

        保守优先：**任何一个条件说不准，就返回 False（照旧走专业框架）**。
        路由没接上（route is None）、危机短路那一支、人格不是宠物本体（林医生以外的
        实例，例如命理那位）、意图是倾诉/求助、或者情绪分过了闸——统统不算闲聊。
        换句话说，新行为只在"明确没事"的时候生效。
        """
        if route is None or self._persona_id != DEFAULT_CHAT_PERSONA:
            return False
        if getattr(route, "crisis", False):
            return False
        if getattr(route, "intent", None) not in CASUAL_INTENTS:
            return False
        return _below(emotion_score, CASUAL_EMOTION_MAX)

    def _tone(self) -> str:
        """追加腔调。取不到就当作没有——回话不该因为这段附属文本整条塌掉。"""
        if self._tone_provider is None:
            return ""
        try:
            return self._tone_provider() or ""
        except Exception as exc:  # noqa: BLE001 —— 见上
            logger.error("追加腔调取不到（%s），这次按原来的人设回话", exc)
            return ""

    def _kb(self, text: str) -> str:
        """翻旧记录（v2-rag）。翻不到 / 没接 / 不是问句，一律空串——一字不注入。

        跟 _tone / _ask_back 一个待遇：这条腿塌了（chroma 没起、笔记目录没了）
        不该让整次回话跟着塌。检索跑在 _compose 这个后台线程里，本来就不占界面。
        """
        if self._kb_provider is None:
            return ""
        try:
            return self._kb_provider(text) or ""
        except Exception as exc:  # noqa: BLE001 —— 见上
            logger.error("翻旧记录失败（%s），这次按原来的人设回话", exc)
            return ""

    def _system_hint(self, emotion_score: float, text: str, route, conflict) -> str:
        """这一轮要注入的那几段系统提示，按"从硬到软"排。

        顺序是有讲究的：
        1. **别编造**（v2-chat-core #4 #5，`grounding.hint()`）——硬约束，排最前；
        2. 这一句是**邀约**就别当日程登记（#7）；
        3. 这一句跟**前面说过的地方 / 安排对不上**，先问清楚（#6 / v2-contradict-detect）；
        4. 路由提示词（情绪落在语气、意图落在回应分寸，纯规则拼的，不走模型）；
        5. 安抚分寸（指令 26-A）排最后：它是参考话术，最容易被模型当成模板照念。

        4、5 两条各自可能为空（route 为 None / 情绪分为 0），拼的时候跳过。
        整段注入两条回话路，一个字不差。
        """
        parts = [grounding.hint()]
        if invite.is_invitation(text):
            parts.append(invite.INVITE_RULE)
        if conflict is not None:
            parts.append(plan.conflict_hint(conflict))
        parts.append(mood.hint_for_route(route))
        # v2-chat-mirror：跟上对方的节奏（这一句该多热络、该说多长）。任何一轮都给，
        # 中性也给——它就是用来压掉"不管对方说什么都是同一套恒温软萌短句"的那股劲。
        parts.append(mood.mirror_hint(text, route))
        parts.append(self._ask_back(text, route))
        front = "\n".join(part for part in parts if part)
        hint = comfort.library().comfort_hint(emotion_score, text)
        return f"{front}\n{hint}" if hint else front

    def _ask_back(self, text: str, route) -> str:
        """取"反问拿主意"那段。取不到就当作没有——回话不该因为这段附属文本整条塌掉。"""
        if self._ask_back_provider is None:
            return ""
        try:
            return self._ask_back_provider(route, text) or ""
        except Exception as exc:  # noqa: BLE001 —— 见上
            logger.error("反问提示取不到（%s），这次按原来的人设回话", exc)
            return ""

    def _speak(self, reply: str, extra: str, on_spoken) -> None:
        self._emit(f"{reply}{extra}")
        if on_spoken is not None:
            on_spoken()

    def _failed(self, exc: Exception, text: str) -> None:
        """回话这一步挂了（断网、没填 Key、接口报错）。

        顺序有讲究：**软表达先走那套调过的本地关怀**（"撑不下去"这类话，断不断网都该
        被接住，那套话术比一句寒暄贴心），关怀接不住时再看是不是断网——是就给一句本地
        陪伴寒暄（v2-offline-fallback：别报「没听懂」），两样都不是才如实 toast。
        """
        logger.error("心理安抚回话失败: %s", exc)
        care = self._soft_care(text)
        if care:
            logger.warning("模型不可用，软表达改走本地关怀话术")
            self._emit(care)
            return
        if isinstance(exc, LLMOfflineError):
            logger.warning("断网，主聊天改走本地陪伴寒暄")
            self._emit(self._offline_reply())
            return
        self._bus.toast.emit(f"它没能把话说完：{exc}")

    def _offline_reply(self) -> str:
        """断网时说的那句话：provider（按熟悉度挑）有就用它的，没有用默认那句。"""
        if self._offline_reply_provider is None:
            return OFFLINE_DEFAULT_REPLY
        try:
            return self._offline_reply_provider() or OFFLINE_DEFAULT_REPLY
        except Exception as exc:  # noqa: BLE001 —— 兜底文案不该因为取等级失败而塌
            logger.error("断网寒暄取不到（%s），用默认那句", exc)
            return OFFLINE_DEFAULT_REPLY

    def _soft_care(self, text: str) -> str:
        """离线关怀话术（人格不变）：林医生有林医生那套，玄机先生有玄机先生那套。"""
        divination = self._persona_id != DEFAULT_CHAT_PERSONA
        return crisis.soft_care_reply(text, divination=divination)


def _with_history(
    messages: list[dict[str, str]], history
) -> list[dict[str, str]]:
    """把本场前文插进拼好的消息里（v2-chat-core #6）。

    插在**最后一条 user 之前**：两条路拼出来的消息末尾一定都是"用户这次说的话"
    （见 casual.messages 与 PersonaCard.messages），前文排在它前面才读得通顺——
    顺序是 …system（人设/分寸/记忆）→ 前几轮原话 → 这一句。

    `history` 为空（刚开聊、或上一轮没生成成功）就原样返回，一个字节都不动。
    """
    if not history:
        return messages
    turns = [
        {"role": "user" if who == "user" else "assistant", "content": what}
        for who, what in history
    ]
    return messages[:-1] + turns + messages[-1:]


def _below(score, limit: float) -> bool:
    """情绪分是不是低于这条线。分不是个数（None / 字符串）就当"说不准"，返回 False。

    "说不准就按更重的那一侧处理"是这一整套判断的保守方向：认不出情绪时宁可走
    专业框架、宁可不当成寒暄，也不要把它接轻了。
    """
    try:
        return float(score or 0.0) < limit
    except (TypeError, ValueError):
        return False
