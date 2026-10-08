"""
吐嘈服务：把用户说的一句话，走完 解析 → 存记忆 → 建提醒 → 宠物回应 的闭环。

回话不在这里生成：解析只做结构化抽取，回话交给注入进来的 responder（宠物本体就是林医生）。
玄学页那条通道随玄学族合并下线了，如今只剩本体这一条、一副腔调。

对话状态机：任何"等待用户补充信息"的状态都必须有出口。
- IDLE：空闲，用户说的话走正常吐嘈解析。
- AWAITING_TIME：宠物已反问"大概几点"，等用户补答。
- AWAITING_CARD：宠物已提议建卡，等用户点头（"好"就建，"不用"就算了，说别的就当新话题）。

等待态（AWAITING_TIME）下，每轮先做意图分类，再按四个出口之一处理：
  成功 answer_time   → 建提醒，回到 IDLE
  取消 cancel        → 放弃提醒，回到 IDLE
  话题切换 new_topic  → 放弃等待，把这句话当作新的吐嘈处理，回到 IDLE
  重试超限 unclear×N → 超过 MAX_TIME_RETRY 次仍没说清，放弃，回到 IDLE
每个出口第一步都先复位到 IDLE（_reset_to_idle），保证不会卡死在等待态。

进不进这个等待态有讲究：解析出模糊时间词（明天 / 晚上 / 改天）本该反问"几点"，
但这句要是生活大类里的一件事（"明天见客户没衣服穿愁死"），就当普通吐嘈回话、
让主动服务在回话之后接手——时间词不该压过品类词，见 _is_service_matter。

优先级一句话：报了时间的句子，显式提醒词 > 生活品类词 > 时间词；没报时间的句子
照旧要"品类词 + 求助信号"两层都命中才动手（怕的是每句话都来问一句）；
工作那条线独立在最上面，判意图、建提醒都照旧，不参与这条比较。

线程安排：网络请求（解析、意图分类、记忆抽取）全在后台线程，界面线程只做入库与发信号。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from brain.chains import chatter_parse
from brain.chains.chatter_parse import (
    ChatterAnalysis,
    ChatterParseChain,
)
from brain.chains.intent import IntentChain
from brain.llm.base import LLMOfflineError
from core.config import Config
from core.events import EventBus
from core.worker import run_async
from data.dao import ReminderDAO, ServiceConsentDAO, UtteranceDAO
from life.dialog import chat_intent, invite, location, plan, router
from life.intent import LIFE, TRAVEL_THEME, WORK, Intent, IntentJudge
from life.psych import crisis
from scheduler.jobs import ReminderJobs

logger = logging.getLogger(__name__)

# 断网兜底那句（v2-offline-fallback）：解析/意图分类断网时，宠物改说本地陪伴寒暄，
# 不再报「没听懂」。与 life/mind/intimacy.py 的 OFFLINE_GREETING_DEFAULT 同一句。
OFFLINE_DEFAULT_REPLY = "网络断了，我先陪你坐着~ 联网再聊。"

# 回话器：responder(用户说的话, 功能性的尾巴, 回话说完之后的回调, **关键词参数)。
# 由宠物本体（林医生）担任。关键词参数一路传到底：emotion_score（安抚分寸）、
# route（意图/情绪路由）、conflict（地点前后不一致，v2-chat-core #6）。
Responder = Callable[..., None]

# 建卡确认：用户点头后由生活卡牌服务真正建卡，返回卡牌标题
CardCreator = Callable[[str], str]

# "现在是谁在说话"（角色卡 id）：观察记录要带上它，卡牌服务才知道谁有权提议建卡
PersonaSource = Callable[[], str]

# 话题内容最多截这么多字存进提醒
MAX_REMINDER_CONTENT = 60

# 等待补答时间时，允许用户"没听清"的次数，超过则放弃本次提醒
MAX_TIME_RETRY = 3

# 提议建卡后等多久算作废：只认短句的答复，所以过了这么久还回"好"多半是别的事
CARD_OFFER_TIMEOUT_SEC = 5 * 60

# 取消关键词：规则优先匹配，保证即使 LLM 不可用，用户也一定能退出等待态
CANCEL_KEYWORDS = (
    "算了", "不用了", "不用", "别提醒", "不提醒", "取消",
    "先不", "不需要", "不用管", "先不用", "不用提醒",
)

# 建卡确认：只说"好""行"这种短句才算答复，免得"好累啊"被当成同意
CARD_REPLY_MAX_LEN = 8
CARD_NO_KEYWORDS = ("不用", "不要", "算了", "先不", "别", "不了", "不需要", "不建", "没兴趣")
CARD_YES_EXACT = (
    "好", "好的", "好呀", "好啊", "好滴", "行", "可以", "要", "要的", "嗯", "嗯嗯",
    "成", "来吧", "建", "建吧", "建一张", "建一个", "做一张", "来一张",
)

VERDICT_YES = "yes"
VERDICT_NO = "no"
VERDICT_OTHER = "other"

# ---------- 主动服务（指令 26-B） ----------

# 工作那条线拒绝后的静默期：拒绝过 24 小时内不再问（开工作模块是件大事，窗口放长到 24 小时；
# 跟生活卡的 POP_DECLINE_SILENCE 一个意思，只是更长）。重启用库里的记录算，不靠内存。
SERVICE_WINDOW = timedelta(hours=24)
# 问出口之后等这么久没人应，就当没问过（跟建卡提议一个道理）
SERVICE_OFFER_TIMEOUT_SEC = 5 * 60
# 工作那条线记账用的主题名（点头/拒绝都按"工作"这一个主题算）
WORK_THEME = "工作"

# 生活卡与工作两条线共用"渐进授权"（指令 26-B 修订）：
# 同一个主题先问 POP_ASK_TIMES 次，每次都点头，之后就直接开/弹（不再问）；
# 拒绝一次就静默（生活卡 POP_DECLINE_SILENCE、工作 SERVICE_WINDOW），从头再问。
POP_ASK_TIMES = 3
POP_DECLINE_SILENCE = timedelta(hours=12)

# 服务提议的归类：(WORK, "") 或 (LIFE, 品类)——分流时就看头一个
ServiceOffer = tuple[str, str]

# 功能性话术（反问几点、提醒确认、建卡确认…）：状态机决定"说什么"，这里决定"怎么说"。
# 只剩宠物本体一副腔调：玄学页下线之后，"一屏一腔调"不再需要第二套话术。
PET_VOICE: dict[str, str] = {
    "ask_time_hint": "「{hint}」大概是几点呀？告诉我个时间，我到点喊你",
    "ask_time_plain": "那我什么时候提醒你比较好呀？说个时间给我",
    "reminder_set": "好，{moment} 我准时喊你～",
    "reminder_cancelled": "好，那我不提醒啦。",
    "retry_give_up": "这次先不提醒啦，你想提醒我的时候再跟我说。",
    "retry_again": "还是没听清几点，可以说 19:30 这样；不想提醒的话就说「算了」。",
    "card_offer": (
        "你最近老提「{theme}」这件事，要不要我给它做一张卡？"
        "点下面「好呀」就建，不想就说「不用了」。"
    ),
    "card_made": "好，卡建好了：「{title}」。",
    "card_failed": "这张卡我没做成，回头再说。",
    "card_declined": "好，那这张先不做成卡，想聊随时跟我说。",
    "work_offer": "这件事要不要我把工作模块打开，陪你过一遍？点下面「好呀」就开。",
    "work_opened": "好，工作模块我给你打开了，咱们一条条过。",
    "life_offer": "要不要我把「{title}」给你翻出来看看？点「好呀」我就翻，不想就说「不用了」。",
    "service_declined": "好，那先放着，需要的时候喊我。",
    "life_card": "别急，我把「{title}」给你翻出来了，先看这个。",
    "life_declined": "好，那先不看，等你想看再喊我。",
}

class ConversationState(Enum):
    """对话状态机。"""

    IDLE = "idle"                    # 空闲，无等待
    AWAITING_TIME = "awaiting_time"  # 已反问，等用户补答时间
    AWAITING_CARD = "awaiting_card"  # 已提议建卡，等用户点头
    AWAITING_SERVICE = "awaiting_service"  # 已问"要不要我打开工作模块"，等用户点头（指令 26-B）


class FollowupIntent(Enum):
    """等待态下用户这句话的意图（对应四个出口）。"""

    ANSWER_TIME = "answer_time"  # 成功：补答了可解析的时间
    CANCEL = "cancel"            # 取消：不想提醒了
    NEW_TOPIC = "new_topic"      # 话题切换：说起了别的事
    UNCLEAR = "unclear"          # 没听清：想说时间但说不清（重试）


_INTENT_MAP = {
    chatter_parse.INTENT_ANSWER_TIME: FollowupIntent.ANSWER_TIME,
    chatter_parse.INTENT_CANCEL: FollowupIntent.CANCEL,
    chatter_parse.INTENT_NEW_TOPIC: FollowupIntent.NEW_TOPIC,
    chatter_parse.INTENT_UNCLEAR: FollowupIntent.UNCLEAR,
}


@dataclass
class _PendingQuestion:
    """宠物已反问"大概几点"，正在等用户补答。"""

    utterance_id: int
    original: str
    scene: str
    time_hint: str


@dataclass
class ServiceHooks:
    """主动服务要用到的那几样外部能力（指令 26-B）。

    判意图的词表跟卡牌判定是同一份（CardClassifier），这里只放"要查卡、要问一句、要记账"
    这几个动作：装配层接好之后，会话层照着决策树调用，不自己去翻数据库。
    """

    intent: IntentJudge
    chain: IntentChain
    consents: ServiceConsentDAO
    find_card: Callable[[str], tuple[int, str] | None]   # 品类 →（卡 id, 卡名）；手上没有返回 None
    invite: Callable[[str], bool]                        # 品类 → 要不要按邀请制问一句


class ChatterService:
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        utterance_dao: UtteranceDAO,
        reminder_dao: ReminderDAO,
        parse_chain: ChatterParseChain,
        jobs: ReminderJobs,
        user_id: int,
        responder: Responder,
        card_creator: CardCreator,
        current_persona: PersonaSource,
        serve: ServiceHooks | None = None,
        on_emotion: Callable[[str], None] | None = None,
    ):
        self._config = config
        self._bus = bus
        self._utterance_dao = utterance_dao
        self._reminder_dao = reminder_dao
        self._parse_chain = parse_chain
        self._jobs = jobs
        self._user_id = user_id
        self._responder = responder
        self._card_creator = card_creator
        self._current_persona = current_persona
        # 说话往哪儿送：宠物气泡（玄学页那条通道已随玄学族合并下线）
        self._emit = bus.pet_speak.emit
        # 功能性话术的腔调：只剩宠物本体这一副
        self._voice = PET_VOICE
        # 断网陪伴寒暄（v2-offline-fallback）：签名 () → 一句话，按熟悉度挑。
        # 后接、可选，没接上就用 OFFLINE_DEFAULT_REPLY。
        self._offline_reply_provider: Callable[[], str] | None = None

        self._state = ConversationState.IDLE
        self._pending: _PendingQuestion | None = None
        self._retry_count = 0
        self._card_offer: tuple[str, str] | None = None   # 已提议的（主题、标题），等用户点头
        self._card_offered_at = 0.0                       # 提议的时刻，用来判超时

        # 主动服务（指令 26-B）
        self._serve = serve
        self._service_offer: ServiceOffer | None = None   # 已问出口的（哪条线, 品类），等用户答复
        self._service_text = ""         # 问的时候用户在说的那句话：开工作模块时拿它落部门
        self._service_asked_at = 0.0
        self._acted_key: tuple[str, str] | None = None    # 上一场动手时的（归类, 主题）

        # 会话层刚说这句话时露出的情绪（路由骨架）：交给情感记忆的主动关怀当由头（S4）
        self._on_emotion = on_emotion

        # 地点追踪（v2-chat-core #6）：记住用户上一次把自己放在哪座城，这次换了地方
        # 就把不一致摆出来。纯内存、不写库，跟"一场对话"同寿（见 life/dialog/location.py）
        self._locator = location.LocationTracker()
        # 安排槽位追踪（v2-contradict-detect）：记住上一句明确报出来的"谁做饭/在哪吃"，
        # 这一句报得不一样就报冲突。跟地点追踪并排、互不干扰（见 life/dialog/plan.py）。
        self._plan_tracker = plan.PlanTracker()

    # ---------- 对外入口 ----------

    @property
    def state(self) -> ConversationState:
        return self._state

    def handle(self, text: str) -> None:
        """用户在生活面板里说了一句话。先按状态分派，等待态下先做意图分类。"""
        text = text.strip()
        if not text:
            return
        if self._state == ConversationState.AWAITING_TIME:
            self._handle_followup(text)
        elif self._state == ConversationState.AWAITING_CARD:
            if self._card_offer_expired():
                # 超时出口：这么久没理，就不当它是答复了，别让用户过半天说句"好"被误伤
                logger.info("建卡提议超过 %d 分钟没人接，作废", CARD_OFFER_TIMEOUT_SEC // 60)
                self._reset_to_idle()
                self._handle_new_utterance(text)
            else:
                self._handle_card_reply(text)
        elif self._state == ConversationState.AWAITING_SERVICE:
            if self._service_expired():
                logger.info("主动服务的提议超过 %d 分钟没人应，作废", SERVICE_OFFER_TIMEOUT_SEC // 60)
                self._reset_to_idle()
                self._handle_new_utterance(text)
            else:
                self._handle_service_reply(text)
        else:
            self._handle_new_utterance(text)

    # ---------- 等待建卡确认 ----------

    def begin_card_offer(self, theme: str, title: str) -> None:
        """卡牌服务提议建卡：切到等待确认态，开口问一句。

        由 core/events.py 的 card_proposed 信号触发（接线在装配处）。
        玄学卡与生活卡的说法不一样：前者是"起一卦"，后者是"做一张卡"。
        """
        self._state = ConversationState.AWAITING_CARD
        self._card_offer = (theme, title)
        self._card_offered_at = time.monotonic()
        self._emit(self._voice["card_offer"].format(theme=theme, title=title))

    def _card_offer_expired(self) -> bool:
        """提议已经放太久了吗（放下手机回来那句"好"不该算数）。"""
        return time.monotonic() - self._card_offered_at >= CARD_OFFER_TIMEOUT_SEC

    def _handle_card_reply(self, text: str) -> None:
        """等待确认建卡时，看这句话算不算答复；三个出口都不会把用户的话吞掉。"""
        verdict = self._card_verdict(text)
        offer = self._card_offer
        self._reset_to_idle()
        if offer is None:
            return

        theme, _title = offer
        if verdict == VERDICT_YES:
            made = self._card_creator(theme)
            if made:
                self._emit(self._voice["card_made"].format(title=made))
            else:
                self._emit(self._voice["card_failed"])
        elif verdict == VERDICT_NO:
            # 记下这次拒绝：卡牌服务靠它做限频（拒绝过的品类一段时间内不再提）
            self._bus.card_offer_declined.emit(theme)
            self._emit(self._voice["card_declined"])
        else:
            # 说的不是这件事：放弃等待，把这句话当新的吐嘈处理，别把它吞掉
            self._handle_new_utterance(text)

    def _card_verdict(self, text: str) -> str:
        """这句话是同意建卡、拒绝建卡，还是在说别的事。

        只认短句：长句里的"好""要"多半是别的话头（"好累啊""要加班"），
        认错了比不认更糟，所以拿不准就当用户在说别的事。
        """
        normalized = text.strip()
        for mark in (" ", "，", "。", "！", "!", "、", "~"):
            normalized = normalized.replace(mark, "")
        if len(normalized) > CARD_REPLY_MAX_LEN:
            return VERDICT_OTHER
        if any(word in normalized for word in CARD_NO_KEYWORDS):   # 先看拒绝：'不要' 里也有 '要'
            return VERDICT_NO
        if normalized in CARD_YES_EXACT or "建" in normalized:
            return VERDICT_YES
        return VERDICT_OTHER

    # ---------- 主动服务（指令 26-B） ----------

    def _wants_reminder(self, text: str) -> bool:
        """这句报了时间，是不是**要我到点做点什么**（v2-chat-intent ①）。

        判据只认显式提醒请求词（`config.intent.remind_words`：提醒我 / 叫我 / 闹钟 /
        别忘…），**不认时间词本身**。"我今晚吃了红烧肉你呢"里有"今晚"但没有请求，
        所以不进等待态——这就是用户报的"闲聊被当成提醒意图"那一刀。

        词表跟 `IntentJudge` 用同一份，用户在设置里加词两边一起生效，不另开一张。
        """
        return chat_intent.wants_reminder(text, self._config.intent.remind_words)

    def _begin_focus(self, text: str) -> None:
        """用户明说要专心（v2-chat-intent ②）：开一轮番茄钟，这一句不再走别的线。

        **多意图只取一个动作**（指令 ④）：既然这一句要的是专注，就不再建提醒、不弹卡、
        不进"等用户补答时间"——所以入口处直接 return，连路由与解析链都不跑。
        跟危机短路同一个姿态：不是这句话不配被处理，是它已经**有**归属了。

        这里**只发信号**，不自己动番茄钟：聊天层不认识屏幕计时器（分层跟
        `screen_focus_timer` 那条线一致），由装配层接（`core/bootstrap.py`）。
        v2-focus-quiet 之后进专注不冒开场白（只亮倒计时浮窗），这儿更不能补一句——
        那会破坏"专注静默"。
        """
        self._reset_to_idle()
        self._bus.focus_requested.emit()

    def _is_service_matter(self, analysis: ChatterAnalysis, text: str) -> bool:
        """这句话是不是工作/生活大类里的一件事（该让主动服务接手，而不是反问"几点"）。

        带时间词的句子原先整句被提醒那条线接走：反问一句"明天大概几点"就把话头占了，
        状态机进了 AWAITING_TIME，等回话说完 _maybe_serve 再开口时，头一句就撞上
        "不是 IDLE 就别插话"那道守卫——工作/生活里的事一句都轮不上。
        "明天 / 月底"是这件事的时限，不是用户要提醒。

        工作与生活两条线都拦：本地判出 actionable（工作或生活）就不进等待态，
        让主动服务在回话之后接手——"月底要交方案"这种工作死线、又不是要提醒的句子，
        照旧走"先问再开工作模块"，别被一句"几点提醒你"顶掉（工作卡不弹的根因）。
        显式提醒词（"提醒我…"）在 IntentJudge 里就判成 NONE 了，所以这里也不会拦它。
        """
        if self._serve is None:
            return False
        return self._serve.intent.judge(text, timed=analysis.timed).actionable

    def _maybe_serve(
        self, analysis: ChatterAnalysis, text: str, force_travel: bool = False
    ) -> bool:
        """回话之后：这句话是不是在求助，该不该主动做点什么。返回这次到底动没动手。

        决策树（指令 26-B）：
        - 工作意图（有任务动作）→ 先问一句"要不要打开工作模块"，点头才开（大窗口，先问再开）；
        - 生活意图 + 特别紧急 → 不管静默、也不问一句，直接把卡翻到眼前；
        - 生活意图 + 手上有这张卡 → 还没攒够点头次数就先问一句"要不要翻出来看看"，
          点头次数攒够了才直接弹（渐进授权，见 _serve_life）；
        - 生活意图 + 手上没卡 → 按邀请制问一句（指令 25-B 那套）；
        - 纯情绪、闲聊 → 什么都不做，只按 26-A 安抚。

        限频：一场对话最多动手一次（"一场"看话题有没有变）；工作与生活卡都走渐进授权——
        拒绝后静默一阵（工作 24 小时、生活卡 12 小时），连着点头 3 次之后直接开/弹。
        返回值给调用方用：动过手了，卡牌服务这一场就别再出建卡邀请。

        ``force_travel``（v2-chat-core #6）：这一句把用户从 A 城挪到了 B 城。
        那种句子（"我到上海了"）在出行词表里一个词都没有，品类那条线永远判不出来，
        所以这里**跳过词表与渐进授权，直接把出行卡翻到眼前**，跟"特别紧急"一档。
        """
        if self._serve is None:
            return False
        if self._state != ConversationState.IDLE:
            # 正在等用户答别的事（补个时间 / 建卡确认）：这一轮别再插一句
            return False
        if force_travel:
            # 地点变了这件事比"这一场动过手没有"更硬：哪怕刚聊完别的事，也得把卡递出来
            return self._serve_life(TRAVEL_THEME, analysis, text, urgent=True)
        if self._acted_key is not None and self._acted_key == (analysis.module, analysis.topic):
            return False          # 这一场已经动过一次手了
        intent = self._serve.intent.judge(text, timed=analysis.timed)
        if intent.actionable:
            return self._serve_intent(intent, analysis, text)
        # 本地认不出来、但话里有求助信号又带着情绪：请模型看一眼（把握不够就什么都不做）
        if self._serve.intent.needs_fallback(text, analysis.emotion_score):
            run_async(
                self._serve.chain.classify,
                text,
                on_done=lambda result: self._serve_from_model(result, analysis, text),
                on_failed=lambda exc: logger.error("意图兜底判定失败: %s", exc),
            )
        return False

    def _serve_from_model(self, result, analysis: ChatterAnalysis, text: str) -> None:
        """模型兜底的结果回来了：够把握才动手。"""
        if self._serve is None:
            return
        kind, theme, confidence, urgent = result
        intent = self._serve.intent.from_model(kind, theme, confidence, urgent)
        if intent.actionable:
            self._serve_intent(intent, analysis, text)

    def _serve_intent(self, intent: Intent, analysis: ChatterAnalysis, text: str) -> bool:
        if intent.kind == WORK:
            return self._serve_work(analysis, text)
        return self._serve_life(intent.theme, analysis, text, urgent=intent.urgent)

    def _serve_work(self, analysis: ChatterAnalysis, text: str) -> bool:
        """工作那条线（渐进授权，跟生活卡同一套）：先问再开，点头三次后直接开。

        - 刚被拒绝过（24 小时内）→ 不再问，这一句就只是聊天；
        - 连着点头够 POP_ASK_TIMES 次 → 直接开，不再问（直到拒绝才归零）；
        - 还没攒够 → 先问一句"要不要打开工作模块"，点头才开。
        """
        accepts, declined = self._serve.consents.state(self._user_id, WORK_THEME)
        if declined is not None and datetime.now() - declined < SERVICE_WINDOW:
            logger.info("「工作」24 小时内刚被拒绝过，这次先不再问")
            return False
        if accepts >= POP_ASK_TIMES:
            self._mark_served(analysis)
            self._emit(self._voice["work_opened"])
            self._bus.service_open_work.emit(text)
            return True
        self._mark_served(analysis)
        self._ask((WORK, ""), text, self._voice["work_offer"])
        return True

    def _serve_life(
        self, theme: str, analysis: ChatterAnalysis, text: str, urgent: bool = False
    ) -> bool:
        """生活那条线（渐进授权，指令 26-B 修订）：

        - 手上没有这张卡 → 按邀请制问一句要不要备一张（指令 25-B 那套）；
        - 特别紧急（urgent）→ 不管有没有被拒过、也不问一句，直接把卡翻到眼前；
        - 刚被拒绝过（12 小时内）→ 不弹也不问，这一句就只是聊天；
        - 点头次数已经攒够 → 直接弹（这是"以后就直接弹卡"那一步）；
        - 还没攒够 → 先问一句"要不要翻出来看看"，点头才弹。
        """
        found = self._serve.find_card(theme)
        if found is None:
            if self._serve.invite(theme):
                self._mark_served(analysis)
                return True
            return False
        card_id, title = found
        if urgent:
            self._mark_served(analysis)
            self._pop_card(card_id, title, text)
            return True
        if self._life_silenced(theme):
            return False
        accepts, _declined = self._serve.consents.state(self._user_id, theme)
        if accepts >= POP_ASK_TIMES:
            self._mark_served(analysis)
            self._pop_card(card_id, title, text)
            return True
        self._mark_served(analysis)
        self._ask((LIFE, theme), text, self._voice["life_offer"].format(title=title))
        return True

    def _ask(self, offer: ServiceOffer, text: str, line: str) -> None:
        """把提议问出口：进等待态、说话、露轻按钮（[好呀] [不用了]）。"""
        self._service_offer = offer
        self._service_text = text
        self._service_asked_at = time.monotonic()
        self._state = ConversationState.AWAITING_SERVICE
        self._emit(line)
        self._bus.service_offer.emit(offer[0])

    def _pop_card(self, card_id: int, title: str, text: str) -> None:
        """真把这张卡弹出来：安抚一句 + 让界面把它翻到眼前（带着用户那句话，好给场景建议）。"""
        self._emit(self._voice["life_card"].format(title=title))
        self._bus.service_show_card.emit(card_id, text)

    def _life_silenced(self, theme: str) -> bool:
        """这个品类刚被拒绝过吗（拒绝后静默一阵：不弹也不问）。"""
        _accepts, declined = self._serve.consents.state(self._user_id, theme)
        return declined is not None and datetime.now() - declined < POP_DECLINE_SILENCE

    def _mark_served(self, analysis: ChatterAnalysis) -> None:
        """记住"这一场动手时在聊什么"——话题变了才算新的一场。"""
        self._acted_key = (analysis.module, analysis.topic)

    def _service_expired(self) -> bool:
        return time.monotonic() - self._service_asked_at >= SERVICE_OFFER_TIMEOUT_SEC

    def _handle_service_reply(self, text: str) -> None:
        """等待提议的答复：三个出口，怎么答都不会把这句话吞掉。

        点头：工作那条线开模块，生活那条线把卡弹出来（并记一笔"点头次数"）；
        拒绝：生活那条线静默一阵、点头次数归零，工作那条线只回一句。
        """
        verdict = self._card_verdict(text)
        offer, self._service_offer = self._service_offer, None
        original, self._service_text = self._service_text, ""
        if verdict == VERDICT_YES:
            self._reset_to_idle()
            if offer is None:
                return
            kind, theme = offer
            if kind == WORK:
                # 记一次点头（连着够 3 次之后下次直接开）；先把"打开了"说出来再开窗口，
                # 这一句同时给气泡收个尾（按钮按下后要清掉忙态）
                self._serve.consents.accept(self._user_id, WORK_THEME)
                self._emit(self._voice["work_opened"])
                self._bus.service_open_work.emit(original)
            else:
                self._pop_card_after_yes(theme, text, original)
            return
        if verdict == VERDICT_NO:
            self._reset_to_idle()
            if offer is not None and offer[0] == LIFE:
                # 拒绝：点头次数归零 + 静默一阵，之后聊到这件事从头再问
                self._serve.consents.decline(self._user_id, offer[1])
                self._emit(self._voice["life_declined"])
            elif offer is not None and offer[0] == WORK:
                # 工作那条线拒绝：点头次数归零 + 24 小时静默（跟生活卡同一套渐进授权）
                self._serve.consents.decline(self._user_id, WORK_THEME)
                self._emit(self._voice["service_declined"])
            else:
                self._emit(self._voice["service_declined"])
            return
        # 答的是别的事：当成新话题，别硬算答复
        self._reset_to_idle()
        self._handle_new_utterance(text)

    def _pop_card_after_yes(self, theme: str, text: str, original: str) -> None:
        """用户点头要这张卡：点头次数加一，然后把卡弹出来。"""
        self._serve.consents.accept(self._user_id, theme)
        found = self._serve.find_card(theme)
        if found is None:
            # 攒次数这一瞬间卡被收进卡册了：那就当没这张卡，不再往下弹
            logger.info("「%s」的卡这会儿不在轮播上了，这次就不弹了", theme)
            return
        card_id, title = found
        self._pop_card(card_id, title, original or text)

    # ---------- 主流程：新吐嘈 ----------

    def _handle_new_utterance(self, text: str) -> None:
        """IDLE 状态下的正常吐嘈。

        危机词最高优先短路（红线）：命中就走危机话术、不进路由也不跑解析链——话由
        respond 里的危机拦截出，这里只负责别让模型和路由碰它；危机文本也不落 pet.db。
        其余先按纯规则分 (意图, 情绪)（路由骨架），再照旧走 解析 → 存记忆 → 回话。

        进城之前先过一次**地点追踪**（v2-chat-core #6）：这一句要是把用户从 A 城挪到了
        B 城，`conflict` 就不是 None，回话那头会先把不一致摆出来问一句，回完话这头
        再把出行卡递到眼前。
        """
        if crisis.is_crisis(text):
            self._responder(text, "", None)
            return
        if chat_intent.explicit_focus(text):
            self._begin_focus(text)
            return
        route = router.classify(text)
        self._notify_emotion(route.emotion)
        # 地点追踪（#6）与安排追踪（v2-contradict-detect）在这儿写：它们跟路由一样是
        # "模型回来之前"就该定下来的即时值。结果跟着这一句走（塞进下面那个闭包），
        # 不存实例上——解析是异步的，存实例上的话下一句会把这一句的结论冲掉。
        location_conflict = self._locator.note(text)
        plan_conflict = self._plan_tracker.note(text)
        # 两个追踪器都得跑一遍（各自记状态），有一条报冲突就把那条交给回话那头。
        conflict = location_conflict or plan_conflict
        utterance = self._utterance_dao.add(self._user_id, text)
        run_async(
            self._analyze,
            text,
            on_done=lambda result: self._apply(
                utterance, text, result, route=route, conflict=conflict
            ),
            on_failed=self._on_failed,
        )

    def _analyze(self, text: str) -> ChatterAnalysis:
        return self._parse_chain.analyze(text)

    def _apply(
        self, utterance, text: str, analysis: ChatterAnalysis, route=None, conflict=None
    ) -> None:
        # A+++：模型这一次给的 intent / emotion 回过头校正规则路由。
        # 规则值在入口就已经报过一次情绪了（不能等模型），这里只有模型**改成别的**
        # 才补一次；越界/认不出时 refine 原样返回，等于什么都没发生。
        if route is not None:
            refined = router.refine(route, analysis.intent, analysis.emotion)
            if refined.emotion != route.emotion:
                self._notify_emotion(refined.emotion)
            route = refined

        self._utterance_dao.update_analysis(
            utterance.id,
            analysis.scene,
            analysis.time_hint,
            analysis.time_iso,
            analysis.emotion,
            analysis.module,
        )

        # 这里原先每轮都问模型抽一遍"值得长期记住的事"、写进 pet.db 的 memory 表。
        # 那条链路本轮掐掉了（情感记忆改走 life/mind/ 的加密库），顺带省下一次模型调用。
        # 需要提炼的那句话不会丢：本轮末尾会发 utterance_analyzed，情感记忆提炼器
        # 就在那条信号上接着（见 core/bootstrap.py 的接线）。

        # 卡牌服务要在这句话"回完"之后再决定要不要提议建卡，
        # 否则"要不要建张卡"会抢在回话前面冒出来，读起来像抢话
        observer = self._observer(
            text, analysis.module, analysis.topic, analysis.emotion_score
        )

        def after_reply() -> None:
            """回话说完之后：先看要不要主动帮一把，再让卡牌服务看一眼这条吐嘈。

            顺序是先服务后观察（指令 26-B）：同一场对话只动一次手——主动服务真动了的话，
            卡牌服务这次就只记观察、不出"要不要建张卡"，免得一句话里冒出两件要用户答的事。
            地点换了（conflict 非 None）时强制把出行卡递出来（v2-chat-core #6）：
            "我到上海了"这种句子在出行词表里一个词都没有，不强制就永远轮不到那张卡。
            """
            acted = self._maybe_serve(analysis, text, force_travel=conflict is not None)
            observer(not acted)

        if analysis.time_confidence == "explicit" and analysis.time_iso is not None:
            self._create_reminder(utterance.id, analysis.scene or text, analysis.time_iso)
            # 时刻是功能信息，必须报给用户，所以拼在回话尾巴上；回话本身仍由角色卡生成
            self._responder(
                text,
                f"（{analysis.time_iso.strftime('%H:%M')} 我喊你）",
                after_reply,
                emotion_score=analysis.emotion_score,
                route=route,
                conflict=conflict,
            )
        elif analysis.time_confidence == "fuzzy" and (
            self._is_service_matter(analysis, text)
            or invite.is_invitation(text)
            or not self._wants_reminder(text)
        ):
            # 这句是工作/生活大类里的一件事，**或者是一句邀约**，不是"要提醒"：
            # 别拿"明天大概几点"把它顶掉（v2-chat-core #7 补的就是后半条——
            # "过两天请你吃饭"里的"吃饭"不在任何品类词表里，原先拦不住，于是被反问几点）。
            # 照正常吐嘈回话，主动服务在回话之后（after_reply 里）照旧接手——
            # 一旦进了 AWAITING_TIME，它就再也接不上手了（守卫见 _maybe_serve 开头）。
            #
            # 第三个条件（v2-chat-intent ①）是"压根没让我做事"那一档：模型读出了时间
            # （"今晚""几点"）就报 fuzzy，可句子里没有一个提醒请求词——"我今晚吃了红烧肉
            # 你呢"正是这种。时间词在闲聊里只是**叙述的一部分**，不绑动作，最多在
            # 自然回应里被带一句（"哎呀红烧肉！那你今晚可美了"），不该反问"几点提醒你"。
            # 用户真要在闲聊里顺口让做事，会说"提醒我"——那就落回下面那支等待态。
            self._responder(
                text,
                "",
                after_reply,
                emotion_score=analysis.emotion_score,
                route=route,
                conflict=conflict,
            )
        elif analysis.time_confidence == "fuzzy":
            # 进入等待态：等用户补答时间。这句反问是功能性的，不经过模型，免得模型一出错就卡住状态机
            self._state = ConversationState.AWAITING_TIME
            self._retry_count = 0
            self._pending = _PendingQuestion(
                utterance_id=utterance.id,
                original=text,
                scene=analysis.scene,
                time_hint=analysis.time_hint,
            )
            self._emit(self._ask_time_text(analysis.time_hint))
            after_reply()      # 反问已经发出去了，观察可以直接跟上
        else:
            # 情绪分一并交给回话那一步：它拿这个挑安抚分寸（轻 / 中 / 重，指令 26-A）
            self._responder(
                text,
                "",
                after_reply,
                emotion_score=analysis.emotion_score,
                route=route,
                conflict=conflict,
            )

        # 用户在聊天里让它做事（看今日活动 / 隐身）：回完话再抛给界面层执行
        if analysis.command:
            self._bus.user_command.emit(analysis.command)

    # ---------- 等待态：意图分类 + 四个出口 ----------

    def _handle_followup(self, text: str) -> None:
        """AWAITING_TIME 状态下，先判断这句话是补答/取消/新话题/没听清。"""
        # 出口·取消：规则优先，保证 LLM 不可用时用户也一定能退出等待态
        if self._is_cancel(text):
            self._exit_awaiting(FollowupIntent.CANCEL)
            self._emit(self._voice["reminder_cancelled"])
            return

        # 其余交给 LLM 做意图分类（含取消的语义兜底）
        # 状态机保证 AWAITING_TIME 时 _pending 必有值（进入/退出都成对维护）
        pending = self._pending
        run_async(
            self._parse_chain.classify_followup,
            pending.time_hint,
            text,
            on_done=lambda result: self._apply_followup(text, result),
            on_failed=self._on_failed,
        )

    def _apply_followup(self, text: str, result: tuple[str, datetime | None]) -> None:
        intent_str, moment = result
        intent = _INTENT_MAP.get(intent_str, FollowupIntent.UNCLEAR)

        if intent == FollowupIntent.ANSWER_TIME and moment is not None:
            # 出口·成功：补答了有效时间，建提醒
            pending = self._pending
            self._exit_awaiting(FollowupIntent.ANSWER_TIME)
            self._create_reminder(
                pending.utterance_id, pending.scene or pending.original, moment
            )
            self._emit(self._voice["reminder_set"].format(moment=moment.strftime("%H:%M")))

        elif intent == FollowupIntent.CANCEL:
            # 出口·取消（LLM 语义兜底，规则没匹配到的取消表达）
            self._exit_awaiting(FollowupIntent.CANCEL)
            self._emit(self._voice["reminder_cancelled"])

        elif intent == FollowupIntent.NEW_TOPIC:
            # 出口·话题切换：放弃等待，把这句话当作新的吐嘈走主流程
            self._exit_awaiting(FollowupIntent.NEW_TOPIC)
            self._handle_new_utterance(text)

        else:
            # 没听清：重试，超过上限则走"重试超限"出口
            self._retry_count += 1
            if self._retry_count >= MAX_TIME_RETRY:
                self._exit_awaiting(FollowupIntent.UNCLEAR)
                self._emit(self._voice["retry_give_up"])
            else:
                self._emit(self._voice["retry_again"])

    # ---------- 状态管理 ----------

    def _exit_awaiting(self, intent: FollowupIntent) -> None:
        """等待态的唯一出口入口：记录从哪个出口离开，并复位到 IDLE。"""
        logger.info("退出等待态，出口=%s", intent.value)
        self._reset_to_idle()

    def _reset_to_idle(self) -> None:
        """复位到 IDLE：清空等待上下文与重试计数。"""
        self._state = ConversationState.IDLE
        self._pending = None
        self._retry_count = 0
        self._card_offer = None
        self._card_offered_at = 0.0
        self._service_offer = None
        self._service_text = ""
        self._service_asked_at = 0.0

    # ---------- 工具 ----------

    def _observer(
        self, text: str, module: str, topic: str, emotion_score: float
    ) -> Callable[[bool], None]:
        """回话说完之后再让别处（卡牌服务）看一眼这条吐嘈。

        "是谁说的"在这里就先取定，免得回话这几百毫秒里换了身份、把归属算错。
        带一个 may_invite：这一场主动服务已经动过手了的话，卡牌这次只记观察、不出邀请。
        """
        persona_id = self._current_persona()
        return lambda may_invite: self._bus.utterance_analyzed.emit(
            text, module, topic, emotion_score, persona_id, may_invite
        )

    def _is_cancel(self, text: str) -> bool:
        """规则匹配取消意图（去空格后包含关键词即可）。"""
        normalized = text.replace(" ", "").replace("，", "").replace("。", "")
        return any(keyword in normalized for keyword in CANCEL_KEYWORDS)

    def _ask_time_text(self, time_hint: str) -> str:
        if time_hint:
            return self._voice["ask_time_hint"].format(hint=time_hint)
        return self._voice["ask_time_plain"]

    def _create_reminder(self, utterance_id: int, content: str, trigger_at: datetime) -> None:
        reminder = self._reminder_dao.add(
            self._user_id, trigger_at, content[:MAX_REMINDER_CONTENT], utterance_id
        )
        self._jobs.schedule(reminder)
        self._bus.reminder_changed.emit()
        logger.info("已建提醒 #%d：%s @ %s", reminder.id, reminder.content, trigger_at)

    def _notify_emotion(self, emotion: str) -> None:
        """把这句话露出的情绪报给情感记忆的主动关怀（S4）。

        只做一次通知，不写库、不调模型；听的人拿它当关怀由头，接不接它自己定。
        """
        if self._on_emotion is None:
            return
        try:
            self._on_emotion(emotion)
        except Exception as exc:  # noqa: BLE001 —— 通知不该拖垮主流程
            logger.error("情绪通知失败（%s），不影响回话", exc)

    def set_offline_reply_provider(self, provider: Callable[[], str] | None) -> None:
        """接上"断网陪伴寒暄"的来源（v2-offline-fallback）。签名 () → 一句话。

        后接、可选，没接上就用 OFFLINE_DEFAULT_REPLY。解析/意图分类断网时这句替掉
        「宠物没听懂」——宠物还在、还在陪你，只是这条命没联网而已。
        """
        self._offline_reply_provider = provider

    def _offline_reply(self) -> str:
        """断网时说的那句话：provider（按熟悉度挑）有就用它的，没有用默认那句。"""
        if self._offline_reply_provider is None:
            return OFFLINE_DEFAULT_REPLY
        try:
            return self._offline_reply_provider() or OFFLINE_DEFAULT_REPLY
        except Exception as exc:  # noqa: BLE001 —— 兜底文案不该因为取等级失败而塌
            logger.error("断网寒暄取不到（%s），用默认那句", exc)
            return OFFLINE_DEFAULT_REPLY

    def _on_failed(self, exc: Exception) -> None:
        logger.error("吐嘈处理失败: %s", exc)
        if isinstance(exc, LLMOfflineError):
            # 断网（v2-offline-fallback）：说一句本地陪伴寒暄，别报「没听懂」。
            logger.warning("断网，主聊天改走本地陪伴寒暄")
            self._emit(self._offline_reply())
            return
        self._bus.toast.emit(f"宠物没听懂：{exc}")
