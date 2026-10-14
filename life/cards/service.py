"""
生活卡牌服务（对应指令 12 的卡牌数据与能力 + 指令 13 的阈值、手动创建、分享
+ 指令 14 的角色与卡牌关系 + 指令 24 的卡间画像互联 + 指令 25 的卡牌改版）。

一条链路：
  日常对话 → 判品类 → 落一条观察记录 → 三门槛判定 → 限频放行 → 对话流里轻邀请
  → 用户点头、改好卡名 → 生成卡牌 → 进轮播

四块料分别在这里：
- 品类判定：life/cards/classify.py（本地词表 + 主诉求取一 + 模型兜底）
- 阈值模型：life/cards/threshold.py（7 天内同品类 ≥3 次负向，或单条强情绪）
- 可卡牌化过滤：life/cards/spawnable.py（心理类议题不成卡，危机表达也不成卡）
- 卡牌模板与能力：life/cards/templates.py
- 分享码：life/cards/share.py（纯本地，只带公开部分）

角色与卡牌（指令 14）：卡牌原先分两族，谁能提是分开的 —— 生活卡归宠物本体（它就是林医生），
  玄学卡只有具备产卡能力的角色（玄机先生）能产。
  **本轮把玄学族并进了生活卡**：玄学页整条通道下线，命理成了第 9 生活大类，
  由本体提卡、本体建卡（跟别的生活卡一样走邀请制），建出来绑玄机先生当陪执行角色——
  提卡权与执行角色就此解耦。观察记录不分角色，同一个品类的历史照旧跨角色累积。

卡牌的五种 AI 能力：引导 / 提醒 / 建议 / 打卡 / 对话。
提醒复用主动提醒调度（ReminderJobs），到点由关怀链开口；其余四种走 brain/chains/card_ai.py。

卡牌的三条生命周期规则（指令 25-B）：
- 限频硬规则：同一个品类 3 天内最多邀一次（只管自己这个品类，不牵连同别的品类）；
  同品类有自建卡就不再邀；被拒绝的品类 7 天内不再提；活跃卡满 4 张不邀；
- 自动归档：14 天无打卡/交互的个人卡收进卡册（数据保留，可随时拉回），宠物会提一句；
- 卡间画像互联（指令 24）：出行卡填了目的地+日期会联网规划并回写画像，减脂卡的饮食建议会参考它。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from brain.chains.card_ai import ACTION_CHECKIN, CardAIChain
from brain.prompts.templates import (
    CARD_ADVICE_NOTE_NONE,
    DIET_TRIP_NOTE,
    DIET_TRIP_NOTE_EMPTY,
    TRAVEL_PROFILE_EMPTY,
)
from core.config import Config
from core.events import EventBus
from core.reltime import memory_text
from core.worker import run_async
from data.dao import (
    CardCheckinDAO,
    CardDAO,
    CardInviteDAO,
    CardShareDAO,
    CardTopicEventDAO,
    ReminderDAO,
)
from data.models import Card
from life.cards import share as share_code
from life.cards import threshold
from life.cards.chat import ensure_card_kb_root
from life.cards.classify import CardClassifier
from life.cards.spawnable import is_safe_text
from life.cards.templates import (
    ALL_FEATURES,
    CARD_TITLE_MAX,
    EMOTION_CATEGORY,
    FAMILY_TITLES,
    LEGACY_DIVINATION_THEMES,
    LIFE_CATEGORIES,
    LIFE_TEMPLATES,
    METAPHYSICS_CATEGORY,
    METAPHYSICS_FEATURES,
    PERSONAL_ACTIVE_MAX,
    PRESET_THEMES,
    REMOVED_PRESETS,
    family_of,
)
from life.profile.advice import card_advice as rule_card_advice
from life.psych import crisis
from life.psych.persona_loader import (
    DEFAULT_CHAT_PERSONA,
    METAPHYSICS_PERSONA,
    NUTRITION_CHEF_PERSONA,
    TOUR_GUIDE_PERSONA,
    load_persona,
)
from memory.store import MemoryStore
from pet.persona import persona_style_layer, strip_action_marks
from scheduler.jobs import ReminderJobs

logger = logging.getLogger(__name__)

MEMORY_LIMIT = 5                 # 卡牌对话时注入几条记忆
CARD_MEMORY_KIND = "event"
CARD_MEMORY_IMPORTANCE = 0.7
UNCLASSIFIED = "未分类"           # 词表没命中的吐槽先记在这儿，攒够了才请模型判类

# ---------- 卡间画像互联（指令 24 第 4 条） ----------
TRIP_THEME = "出行"                        # 出行卡的主题（规划入口只在这张卡上出现）
DIET_THEME = "餐饮"                        # 饮食卡的主题（营养主厨角色只在这一类上）
DIET_KEYWORDS = ("饮食", "减脂", "热量", "减重")    # 认减脂卡的词：预置的饮食卡，或用户自建的减脂卡

# ---------- 主题 → 专业角色（指令 27） ----------
# 这两类卡的模型默认以专业角色开口：出行配导游、饮食配营养主厨。
# 卡上显式绑了别的角色（card.persona_id 有值）时以那个为准——用户选的压过默认。
# 只是"默认谁来说"，不写库：老库里这两张卡的 persona_id 还是空的，不用迁移也能生效。
THEME_PERSONAS: dict[str, str] = {
    TRIP_THEME: TOUR_GUIDE_PERSONA,
    DIET_THEME: NUTRITION_CHEF_PERSONA,
}
TRIP_MEMORY_KINDS = ("fact", "event")      # 链 a 回写的"计划出行"落在这两类里
TRIP_MEMORY_IMPORTANCE = 0.8
RECENT_TRIP_DAYS = 30                      # abs_time 落在未来这么多天内的，算"近期出行"
PROFILE_LIMIT = 5                          # 生成建议时只注入这么多条相关偏好，不喂全量记忆
MEMORY_SCAN_LIMIT = 80                     # 找"近期出行"时扫这么多条最近的记忆

# ---------- 轮播与卡册（指令 25-A 第 3、4 项） ----------


def is_preset_card(card: Card) -> bool:
    """是不是预置的那两张（饮食 / 出行）：它们常驻轮播，不占个人卡的名额。"""
    return card.theme in PRESET_THEMES


def is_personal_card(card: Card) -> bool:
    """个人卡 = 不是预设的那两张：只有这类才占 4 张的名额、才进得了卡册。

    玄学族并进命理之后，命理卡也是一张个人卡了（跟别的生活卡一视同仁：占名额、会归档）。
    """
    return not is_preset_card(card)


# 预设那两张的（大类，卡名）对。查重时**不算**"用户自己建过一张"（见 `find_card`）：
# 它们常驻轮播、不占个人卡名额，也不是用户挑的卡名——跟它们撞名不该把建卡挡回去。
PRESET_CARD_PAIRS: frozenset[tuple[str, str]] = frozenset(
    (template.theme, template.title) for template in LIFE_TEMPLATES
)


def is_diet_card(card: Card) -> bool:
    """这张卡是不是减脂卡：预置的饮食卡，或用户自己建的减脂卡（标题或目标里写明了才算）。

    打开这类卡会自动生成一段饮食建议（ui/panels/card_panel.py），
    跨卡画像里也会把它当成"正在减脂"的依据。
    """
    blob = f"{card.title}{card.goal}"
    return any(word in blob for word in DIET_KEYWORDS)


def is_trip_card(card: Card) -> bool:
    """这张卡是不是出行卡（只有它有目的地 + 日期那两个输入）。"""
    return card.theme == TRIP_THEME


# 联网正文里的来源角标（[1][6] 这种）：厂商接口强制加上去的，场景建议里要摘掉
_CITATION = re.compile(r"\[\d+\]")


def _strip_citations(text: str) -> str:
    """去掉联网正文里的来源角标（角标对应的来源列表本来就不摆上来，留着就是断头引用）。"""
    return _CITATION.sub("", text).strip()


class CardService:
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        card_dao: CardDAO,
        checkin_dao: CardCheckinDAO,
        event_dao: CardTopicEventDAO,
        invite_dao: CardInviteDAO,
        share_dao: CardShareDAO,
        reminder_dao: ReminderDAO,
        jobs: ReminderJobs,
        memory_store: MemoryStore,
        chain: CardAIChain,
        user_id: int,
    ):
        self._config = config
        self._bus = bus
        self._card_dao = card_dao
        self._checkin_dao = checkin_dao
        self._event_dao = event_dao
        self._invite_dao = invite_dao
        self._share_dao = share_dao
        self._reminder_dao = reminder_dao
        self._jobs = jobs
        self._memory_store = memory_store
        self._chain = chain
        self._user_id = user_id
        # 品类判定第一层就靠它：本地词表（指令 25-B 第 1 项）
        self._classifier = CardClassifier(config)
        # 命理那类的关键词：可卡牌化过滤对它放宽一档（见 spawnable.is_safe_text）
        self._metaphysics_words = tuple(
            config.card.categories.get(METAPHYSICS_CATEGORY, ())
        )

    # ---------- 观察与邀请（指令 13 第 2~5 节 + 指令 25-B） ----------

    def observe(
        self,
        text: str,
        module: str,
        topic: str,
        emotion_score: float,
        persona_id: str,
        may_invite: bool = True,
    ) -> None:
        """用户说了一句话：先判品类，再看这个品类该不该出邀请。

        persona_id 是"这句话是谁说的"：它只影响"谁有权开口"（指令 14），
        记录本身照记，这样同一个品类的历史能跨角色攒起来。

        may_invite=False 表示这一场主动服务已经动过手了（指令 26-B 的"同场只动一次"）：
        观察照记（品类的历史不能少一笔），只是这一次不另出建卡邀请。

        品类判定（指令 25-B 第 1 项）：
        ① **本地词表优先**：命中就归到那一类，一条消息只算一个主要类别；
        ② 词表没命中就先记成"未分类"，情绪很强或攒够 3 条时请模型判类，置信度不够就不出邀请。

        本地词表为什么必须排在解析层给的 module 前面：module 是一层模糊归类，"命理"这类
        词表命中却是确定的。早先的写法让 module 里那些玄学主题（姻缘/事业/财运/学业/运势）
        先走一条玄学支路，命理句于是被当成"该由玄机先生提的卡"整条挡下，一句也攒不成卡。
        玄学族并进「命理」之后那条支路已经删掉，命理句就落在第 9 大类上，由本体提卡。

        命理那一类（第 9 个生活大类）：命理句常和"诸事不顺"这类话一起来，心理词表照拦
        会把整类话题误伤成心理议题，所以对它放宽一档，只拦真危机词（见 spawnable.is_safe_text）。
        """
        # module 是解析层给的模糊归类：词表优先之后它不再参与路由（玄学支路已删）。
        # 签名里留着它，是因为它是 utterance_analyzed 信号契约的一部分（重情绪关怀那条线还在用）。
        if not is_safe_text((text,), self._metaphysics_words):
            # 心理类议题与危机表达一律不成卡，也先不记观察（指令 14 的红线）
            logger.info("这句话不适合做成卡牌（心理类或危机表达），交给林医生陪着聊")
            return

        match = self._classifier.local_match(text)
        if match is not None:
            if match.category == EMOTION_CATEGORY:
                # v2-emotion-log：情感/关系类的话题不再建「情感」卡，也不记观察——
                # 改由情感事件那条线记（归因 + 分类标签），见 life/mind/emotion_log.py。
                logger.info("「情感」类话题不建卡，改记情感事件")
                return
            # 词表里就是吐槽词，命中即算一次负向
            self._observe_category(
                match.category,
                text,
                topic,
                emotion_score,
                persona_id,
                negative=True,
                may_invite=may_invite,
            )
            return

        # 词表没命中：先落一条"未分类"，攒够次数或情绪很强时再请模型判类（异步，别卡界面）
        event = self._record_event(UNCLASSIFIED, topic, emotion_score, text, negative=False)
        unmatched = len(self._event_dao.since(UNCLASSIFIED, datetime.now() - threshold.WINDOW))
        if not self._classifier.should_ask_llm(emotion_score, unmatched):
            return
        run_async(
            self._chain.classify_category,
            text,
            on_done=lambda result: self._apply_remote_category(
                result, event.id, text, topic, emotion_score, persona_id, may_invite
            ),
            on_failed=lambda exc: logger.error("品类判定失败: %s", exc),
        )

    def _apply_remote_category(
        self, result: tuple[str, float], event_id: int, text: str, topic: str,
        emotion_score: float, persona_id: str, may_invite: bool = True,
    ) -> None:
        """模型判类回来了：置信度够才改归到那个品类，否则就留着"未分类"不出邀请。"""
        category, confidence = result
        if not category or confidence < self._classifier.min_confidence:
            logger.info(
                "词表外这句话模型也拿不准（%s / %.2f），只留着观察，不出邀请",
                category or "认不出",
                confidence,
            )
            return
        if category == EMOTION_CATEGORY:
            # v2-emotion-log：模型把这句话判成「情感」也一样——不建卡、不记观察，
            # 情感事件那条线已经按词表记过了。留在"未分类"里的那笔是词表没认出来的
            # 兜底记录，不 retag 成「情感」、也不出邀请，等于就当没这条品类。
            logger.info("模型判成「情感」，同样不建卡，改记情感事件")
            return
        # 把那条"未分类"改归到判出来的品类，不另记一条（免得同一句话被数两次）；
        # 模型判的这类，负向与否看情绪分
        self._event_dao.retag(event_id, category, negative=emotion_score >= threshold.NEGATIVE)
        if may_invite:
            self._invite_if_due(category, text, topic, emotion_score, persona_id)

    def _observe_category(
        self, theme: str, text: str, topic: str, emotion_score: float,
        persona_id: str, negative: bool, may_invite: bool = True,
    ) -> None:
        """归到某个品类了：落一条观察记录，再看要不要出邀请。"""
        self._record_event(theme, topic, emotion_score, text, negative=negative)
        if may_invite:
            self._invite_if_due(theme, text, topic, emotion_score, persona_id)

    def _record_event(
        self, theme: str, topic: str, emotion_score: float, text: str, negative: bool
    ):
        """落一条观察记录；顺手把出了 7 天窗口的老记录清掉。"""
        self._event_dao.prune(datetime.now() - threshold.WINDOW)
        return self._event_dao.add(
            self._user_id, theme, topic, emotion_score, text, negative=negative
        )

    def _invite_if_due(
        self, theme: str, text: str, topic: str, emotion_score: float, persona_id: str
    ) -> None:
        """三门槛都过了、限频也允许，才出邀请；否则只记观察（指令 25-B 第 2、3 项）。"""
        if not family_of(theme):
            return          # 不是能成卡的品类（工作、其他…）：观察照记，不出邀请
        if self.active_card_of_theme(theme) is not None:
            # 这个主题已经有卡摆在轮播上了（预设的饮食 / 出行，或用户自己建的）：
            # 手上有的东西不必再问"要不要建一张"，这一句就只是聊天。
            # 观察照记（上面的 _record_event 已经记过了），只是不弹不邀。
            logger.info("「%s」已经有卡在轮播上，不再邀建卡", theme)
            return
        if not self._may_propose(persona_id):
            logger.info(
                "「%s」属于%s，该由宠物本体来提；现在说话的是 %s，先攒着",
                theme,
                FAMILY_TITLES.get(family_of(theme), family_of(theme)),
                persona_id,
            )
            return

        now = datetime.now()
        events = self._event_dao.since(theme, now - threshold.WINDOW)
        stats = threshold.evaluate(events, now)
        logger.info(
            "品类「%s」（话题「%s」）：7 天内 %d 次（负向 %d）、最重情绪 %.2f → 触发=%s",
            theme,
            topic or "-",
            stats.total,
            stats.negatives,
            stats.emotion_max,
            stats.triggered,
        )
        if not stats.triggered:
            return
        blocker = self._invite_blocker(theme)
        if blocker:
            logger.info("「%s」这次先不邀请：%s", theme, blocker)
            return
        self._invite_dao.add(self._user_id, theme)
        # 邀请默认卡名就是品类名，用户可以在确认框里改（比如把"出行"改成"通勤"）
        self._bus.card_proposed.emit(theme, theme)

    def invite_now(self, theme: str) -> bool:
        """用户当场提起这件事、手上又没有这张卡：直接按邀请制问一句（指令 26-B ③）。

        和 observe 那条路的区别：那条靠"7 天内攒够次数"慢慢来，这条是用户当面开口，
        所以不看次数门槛，只看限频（位子满没满、这个品类 3 天内邀过没、这个品类拒绝过没）——
        当场的话该当场接住。
        """
        if not family_of(theme):
            return False
        blocker = self._invite_blocker(theme)
        if blocker:
            logger.info("「%s」这次先不邀请：%s", theme, blocker)
            return False
        self._invite_dao.add(self._user_id, theme)
        # 邀请默认卡名就是品类名，用户可以在确认框里改（比如把"出行"改成"通勤"）
        self._bus.card_proposed.emit(theme, theme)
        return True

    def _invite_blocker(self, theme: str) -> str:
        """现在能不能邀这个品类：能返回空串，不能就返回原因（只拿来写日志）。

        限频只落在自己这个品类上：邀过出行，挡的是出行，不牵连同命理/作息/消费。
        （原先那条"全局 3 天最多一次"是拿最近一次邀请、不分品类去比的，一条晾着没答的
        邀请就能让三天里什么卡都建不出来，一个品类不痛快连坐所有品类，不合理，已去掉。）
        """
        if self.personal_full():
            return f"活跃个人卡已满 {PERSONAL_ACTIVE_MAX} 张，先归档一张再建"
        now = datetime.now()
        latest = self._invite_dao.latest(self._user_id, theme)
        days = self._config.card.invite_global_days
        if latest is not None and now - latest.created_at < timedelta(days=days):
            return f"这个品类 {days} 天内已经邀请过"
        if self._has_personal_card(theme):
            return "这个品类已经有自建卡了"
        if (
            latest is not None
            and latest.accepted is False
            and now - latest.created_at < timedelta(days=self._config.card.decline_silence_days)
        ):
            return f"{self._config.card.decline_silence_days} 天内拒绝过这个品类"
        return ""

    def _has_personal_card(self, theme: str) -> bool:
        """这个品类下有没有用户自建的个人卡（预设那两张不算，否则出行/餐饮永远邀不了）。"""
        return any(
            card.theme == theme and is_personal_card(card) for card in self._card_dao.list_all()
        )

    def find_card(self, category: str, title: str) -> Card | None:
        """这个大类下**同名**的那张卡；没有就给 None（v2-social-card-dedup）。

        建卡查重的那只眼。判重比**两条都对上**：`theme`（大类）+ `title`（卡名）——
        光比卡名的话，用户在不同大类下都叫"打卡"那种会被误挡。

        三条口径（都写明白，别猜）：

        - **只看在轮播上的那一张**（`list_by_archived(False)`）。收进卡册的那张不算
          "已经有了"：归档本来就是把轮播上的位子腾出来，再建一张新的正好顶上；要接着
          用原来那张走 `restore_card`。反过来说，这一条也挡住"归档过的卡被当成在建的
          那张"这种更难查的错。
        - **预设那两张不算**（`PRESET_CARD_PAIRS`）：餐饮/饮食、出行/出行常驻轮播、
          不占个人卡名额，不是"用户自己建过一张"的意思。
        - 卡名先按 `CARD_TITLE_MAX` 截齐再比（跟 `create_manual_card` 落库时同一个口径），
          不然"12 个字的卡名"会跟自己截断后的那张对不上，查重等于没有。
        """
        theme = (category or "").strip()
        name = (title or "").strip()[:CARD_TITLE_MAX]
        if not theme or not name:
            return None
        for card in self._card_dao.list_by_archived(False):
            if card.theme != theme or card.title != name:
                continue
            if (card.theme, card.title) in PRESET_CARD_PAIRS:
                continue
            return card
        return None

    def find_card_by_peer(self, peer_name: str) -> Card | None:
        """按**对象名**找那张攻略卡（v2-social-card-identity）；没有就给 None。

        跟 `find_card` 一个口径：只在轮播上（`list_by_archived(False)`）找，收进卡册的那张
        不算"已经有了"；比对的是 `card.peer_name`（**查重归属字段**），不是可改的
        `card.name`——用户把显示名改了，下一次按同一个对象名来查照样命中，不会又建一张。

        对象名是隐私：这里只拿它当键比，不落盘、不进日志（调用方也只记 `len()`）。
        """
        peer = (peer_name or "").strip()
        if not peer:
            return None
        for card in self._card_dao.list_by_archived(False):
            if card.peer_name == peer:
                return card
        return None

    def resolve_invite(self, theme: str, accepted: bool) -> None:
        """用户答复了邀请：记下结果。拒绝的会静默一段时间，这是"限频"的一部分。"""
        self._invite_dao.resolve(self._user_id, theme, accepted)

    @staticmethod
    def _may_propose(persona_id: str) -> bool:
        """谁有权提卡（指令 14）。

        玄学族并进生活卡之后只剩一族，提卡权也就只剩一条：生活卡归宠物本体（它就是林医生）。
        玄机先生不再自己提卡——它是卡建好之后的陪执行角色（见 _owner_persona）。
        """
        return persona_id == DEFAULT_CHAT_PERSONA

    @staticmethod
    def _owner_persona(theme: str) -> str | None:
        """这张卡默认由谁陪执行：命理卡归玄机先生，其余生活卡归宠物本体。

        None 表示宠物本体。命理卡走的是生活卡那条路（本体提卡、本体建卡），
        但"命理"这档事该由玄机先生那套框架来说，所以建出来就绑它。
        """
        if theme == METAPHYSICS_CATEGORY:
            return METAPHYSICS_PERSONA
        return None

    # ---------- 建卡 ----------

    def ensure_preset_cards(self) -> int:
        """预置 2 张生活卡（指令 25-A 第 1 项）：饮食 / 出行。

        只补缺的：同一主题已经有卡（老库里的预设、或用户自己建的）就不新建，
        只把它的卡名按新模板归位（老库里那两张还叫"吃得舒服点/走得顺一点"）。
        点开这些卡走的是同一个卡详情页，不另做一套。返回这次新补了几张。
        """
        made = 0
        for template in LIFE_TEMPLATES:
            card = self._card_dao.by_theme(template.theme)
            if card is None:
                made_card = self._card_dao.add(
                    user_id=self._user_id,
                    title=template.title,
                    theme=template.theme,
                    summary="",
                    source_memory_ids=[],
                    features=list(template.features),
                    goal=template.blurb,
                    persona_id=self._owner_persona(template.theme),
                )
                self._attach_kb(made_card)
                made += 1
            elif card.title != template.title:
                # 只改卡名：打卡、背景、目标、出行信息都留着
                self._card_dao.set_title(card.id, template.title)
        if made:
            logger.info("预置了 %d 张生活卡", made)
            self._bus.card_changed.emit()
        return made

    def _attach_kb(self, card: Card) -> None:
        """这张卡刚建好：把它的专属知识库目录建出来，并把落点记回 `kb_path`（v2-card-kb）。

        落点是 `<全局知识库>/cards/<卡号>`；建不出目录只记日志（知识库那边会走全局兜底），
        但 `kb_path` 照记——用户以后把目录挂上，这张卡的卡级库就自动生效。
        """
        root = ensure_card_kb_root(self._config, card.id)
        self._card_dao.set_kb_path(card.id, str(root))

    # ---------- 数据迁移：老库搬进新结构（指令 25-A 第 4 项） ----------

    def migrate_cards(self) -> int:
        """把不该留在轮播上的卡收进卡册。每次启动都跑，幂等。

        两件事：
        1. 指令 25-A 删掉的那三张预设（穿搭 / 健身 / 情感陪护）不再出现在轮播上——
           按"主题 + 当初的标题"认出预设那张收进卡册，用户自己用同名大类建的卡不动。
           只收 archived 还是 NULL 的（从没迁移过的那张）：用户从卡册主动拉回来的会把它
           置成 False，那就随用户的意留在轮播上，别再收第二遍；
        2. 活跃个人卡超过 4 张的，按摆放顺序把多出来的收进卡册（历史数据可能早就超了）。
        返回这次一共收了几张。
        """
        moved = 0
        for theme, title in REMOVED_PRESETS:
            card = self._card_dao.by_theme(theme)
            if card is not None and card.title == title and card.archived is None:
                self._card_dao.set_archived(card.id, True)
                moved += 1

        actives = [card for card, _count in self.plaza() if is_personal_card(card)]
        for card in actives[PERSONAL_ACTIVE_MAX:]:
            self._card_dao.set_archived(card.id, True)
            moved += 1

        if moved:
            logger.info("迁移：%d 张卡收进了卡册", moved)
            self._bus.card_changed.emit()
        return moved

    def migrate_divination_themes(self) -> int:
        """把老库里散在玄学主题下的观察记录归并到「命理」（玄学族并进命理时跑，幂等）。

        玄学族没了之后，姻缘 / 事业 / 财运 / 学业 / 运势 不再是能成卡的品类，旧记录留着
        就是孤儿：既不会被人再看一眼，当时攒下的次数也白费。归并过来，同一个品类的历史
        （以及因此攒够的邀请资格）接着算——这正是合并想要的效果。
        返回这次改归了几条。
        """
        moved = 0
        for theme in LEGACY_DIVINATION_THEMES:
            for event in self._event_dao.since(theme, datetime.min):
                self._event_dao.retag(event.id, METAPHYSICS_CATEGORY, negative=event.negative)
                moved += 1
        if moved:
            logger.info(
                "迁移：%d 条玄学主题的观察归并到「%s」", moved, METAPHYSICS_CATEGORY
            )
        return moved

    def create_manual(
        self,
        category: str,
        title: str,
        icon: str,
        description: str,
        profile: dict[str, str] | None = None,
    ) -> str:
        """手动建一张卡（指令 13 第 6 节；指令 25-A 第 3 项改成三步）。

        三步：选大类（9 选 1）→ 卡名（默认大类名，用户可改，≤10 字）→ 图标 + 一句话描述。
        建好立即上轮播。活跃个人卡满 PERSONAL_ACTIVE_MAX 张就建不了（返回空串，
        调用方提示"先收一张再建"）；只有预设那 2 张不占这个名额。

        **同类同名已经有了一张就返回那一张的名字，不再重复建**（v2-social-card-dedup，
        判据见 `find_card`）——调用方据此也能认出"这不是新建，是本来就有"。
        """
        card = self.create_manual_card(category, title, icon, description, profile=profile)
        return card.title if card is not None else ""

    def create_manual_card(
        self,
        category: str,
        title: str,
        icon: str,
        description: str,
        profile: dict[str, str] | None = None,
        name: str | None = None,
        peer_name: str | None = None,
        tags: list[str] | None = None,
    ) -> Card | None:
        """同 `create_manual`，但返回**整张卡**（不是卡名）——要往卡专属知识库写东西
        的调用方（比如对象攻略卡抽完信息要落库）需要拿到 `card.id` / `card.kb_path`。
        建不成返回 None。

        **同类同名已经有一张时返回的是那一张**（v2-social-card-dedup）：不重复建、不再
        写一条卡牌记忆；调用方拿到的是既有卡的 `id` / `kb_path`，写知识库照旧落到它身上。

        ``profile``（v2-social-card-base）：建卡时一并写下的**对象档案**那四格
        （喜好 / 禁忌 / 攻略策略 / 关系阶段，见 `data/models.py::Card`）。不传就是 NULL，
        九类卡一视同仁。**同类同名撞上既有卡时也照写**——用户这次填的四格不能因为
        "卡本来就有"丢掉；不传 profile 的老调用点行为一字未变。

        ``name`` / ``peer_name`` / ``tags``（v2-social-card-identity）：对象攻略卡的
        **卡身份**三格。`name` 是显示名（`对象名·攻略卡`）、`peer_name` 是查重归属
        （原始对象名，改名不动它）、`tags` 是分类标签。只有对象攻略卡那条建卡路会传，
        其余卡不传就是 NULL / 空串，行为与加这三格之前一模一样。
        """
        category = (category or "").strip()
        if category not in LIFE_CATEGORIES:
            return None
        # 卡名先算出来（跟落库时同一个口径），查重与建卡用的都是它
        final_title = (title or category).strip()[:CARD_TITLE_MAX] or category
        peer = (peer_name or "").strip()
        if peer:
            # **按对象名查重**（v2-social-card-identity）：挂得出对象名，就**只**按对象名查。
            # 别再退回按标题查——标题「对象攻略卡」是所有攻略卡共用的，多对象一人一张、
            # 标题全一样，按标题查会把别人的卡当成"这张"。
            existing = self.find_card_by_peer(peer)
        else:
            # **同类同名不再重复建**（v2-social-card-dedup）：认不出对象名（没挂名）才退回
            # 老口径，按大类 + 卡名查。放在卡位那一关**前面**是故意的：卡位满了也该认得出现有
            # 那张（"建不了新的"与"你已经有这张了"是两件事），否则对象攻略卡那条路会在
            # 卡位满时谎报"卡位满了"。
            existing = self.find_card(category, final_title)
        if existing is not None:
            logger.info(
                "大类「%s」下已经有「%s」这张卡了（#%d），不再重复建",
                category, final_title, existing.id,
            )
            if profile:
                self.set_profile(existing.id, **profile)
            return existing
        if self.personal_full():
            logger.info("活跃个人卡已满 %d 张，先收一张再建", PERSONAL_ACTIVE_MAX)
            return None
        # 命理一事一卦，不开引导与打卡；其余 8 个大类开全套（见 templates 里两套能力表）
        features = (
            METAPHYSICS_FEATURES if category == METAPHYSICS_CATEGORY else ALL_FEATURES
        )
        card = self._card_dao.add(
            user_id=self._user_id,
            title=final_title,
            theme=category,
            summary="",
            source_memory_ids=[],
            features=list(features),
            goal=description.strip(),
            # 自建卡一般归宠物本体；命理那类归玄机先生（见 _owner_persona）
            persona_id=self._owner_persona(category),
            icon=icon,
            profile=profile,
            name=name,
            peer_name=peer_name,
            tags=tags,
        )
        self._attach_kb(card)
        self._remember_card(card, category)
        self._bus.card_changed.emit()
        logger.info("手动建卡 #%d：%s（大类 %s）", card.id, card.title, category)
        return card

    def set_profile(self, card_id: int, **fields: str) -> None:
        """后补这张卡的对象档案那四格（v2-social-card-base）。

        跟建卡时传 `profile=` 是一回事，区别只是卡已经在手上了（比如"先建卡、再抽信息"
        那种两步走的调用方）。只写这次给到的，其余留着；认不出的键、空值都跳过
        （口径见 `data/dao.py::CardDAO.set_profile`）。不碰专属知识库、不发事件——
        那四格是**给人看 / 给起草取用**的档案，不是卡面。
        """
        self._card_dao.set_profile(card_id, **fields)

    def accept_invite(self, category: str, title: str, icon: str) -> str:
        """邀请受理（指令 25-B 第 2 项）：按用户在确认框里改好的卡名建卡，并把邀请标为受理。

        建成了才记"受理"；没建成（比如卡位刚好满了）就不动邀请记录，下次还能再问。
        """
        made = self.create_manual(category, title, icon, "")
        if made:
            self._invite_dao.resolve(self._user_id, category, True)
            logger.info("邀请受理：%s（品类 %s）", made, category)
        return made

    def _remember_card(self, card: Card, theme: str) -> None:
        """卡牌也写回记忆（指令 12 第 5 节）：写记忆要建向量，放后台别卡界面。"""
        run_async(
            self._memory_store.remember,
            CARD_MEMORY_KIND,
            f"用户认领了一张「{theme}」卡：{card.title}",
            CARD_MEMORY_IMPORTANCE,
            on_failed=lambda exc: logger.error("卡牌记忆写入失败: %s", exc),
        )

    # ---------- 分享（指令 13 第 7 节） ----------

    def share(self, card_id: int) -> str:
        """生成分享码；公开快照同时留在本地，记录分享出去的是什么。"""
        card = self._card_dao.get(card_id)
        if card is None:
            return ""
        snapshot = share_code.public_snapshot(card)
        share_id = share_code.new_share_id()
        self._share_dao.add(card_id, share_id, share_code.snapshot_json(snapshot))
        logger.info("卡牌 #%d 生成了分享码 %s", card_id, share_id)
        return share_code.build_code(snapshot, share_id)

    def import_share(self, code_text: str) -> str:
        """导入别人分享的卡：只拿公开部分，背景留空，靠自己以后攒；看不懂返回空串。

        导入来的也是一张个人卡，同样受 4 张上限管：满了就导不进来（返回空串）。
        """
        payload = share_code.parse_code(code_text)
        if payload is None or self.personal_full():
            if payload is not None:
                logger.info("活跃个人卡已满 %d 张，导入的卡没进来", PERSONAL_ACTIVE_MAX)
            return ""
        data = payload.get("card") or {}
        features = [
            name for name in (data.get("features") or []) if name in ALL_FEATURES
        ] or list(ALL_FEATURES)
        card = self._card_dao.add(
            user_id=self._user_id,
            title=str(data.get("title") or "").strip() or "分享来的卡",
            theme=str(data.get("theme") or "").strip(),
            summary="",
            source_memory_ids=[],
            features=features,
            goal=str(data.get("goal") or "").strip(),
        )
        self._attach_kb(card)
        self._bus.card_changed.emit()
        logger.info("导入了一张分享卡 #%d：%s", card.id, card.title)
        return card.title

    # ---------- 生活广场与卡册（指令 25-A 第 4 项） ----------

    def plaza(self) -> list[tuple[Card, int]]:
        """轮播要展示的：还没收进卡册的卡 + 已打卡次数。"""
        counts = self._checkin_dao.counts_by_card()
        return [(card, counts.get(card.id, 0)) for card in self._card_dao.list_by_archived(False)]

    def card_book(self) -> list[tuple[Card, int]]:
        """卡册（归档区）：收起来不占轮播的卡。打卡次数照样算得出来，历史查得到。"""
        counts = self._checkin_dao.counts_by_card()
        return [(card, counts.get(card.id, 0)) for card in self._card_dao.list_by_archived(True)]

    def active_personal_count(self) -> int:
        """轮播上活跃的个人卡张数（预设与玄学卡不算）。"""
        return sum(1 for card, _count in self.plaza() if is_personal_card(card))

    def personal_full(self) -> bool:
        """个人卡位满了吗（满了就不能再建 / 再拉回）。"""
        return self.active_personal_count() >= PERSONAL_ACTIVE_MAX

    def archive_card(self, card_id: int) -> bool:
        """把一张个人卡收进卡册：腾出轮播上的位子。预设与玄学卡不收。"""
        card = self._card_dao.get(card_id)
        if card is None or card.archived or not is_personal_card(card):
            return False
        self._card_dao.set_archived(card_id, True)
        self._bus.card_changed.emit()
        logger.info("卡牌 #%d 收进了卡册", card_id)
        return True

    def delete_card(self, card_id: int) -> bool:
        """把一张生活卡**永久删掉**（轮播上的 X 删除键，v2-card-del）。

        跟 archive_card 的分工要说清楚：归档是"收进卡册、随时能拉回来"（卡册那条路照旧），
        这条是"这张不要了"——卡本身和它的打卡、分享记录一起删，删完发 card_changed 让轮播重排。
        两张预设卡也能删；但它们下次启动会被 ensure_preset_cards 按主题补回来
        （预设常驻是既有设计，不是这一轮的删除键失灵）。
        """
        card = self._card_dao.get(card_id)
        if card is None:
            return False
        if not self._card_dao.delete(card_id):
            return False
        self._bus.card_changed.emit()
        logger.info("卡牌 #%d「%s」被删掉了", card_id, card.title)
        return True

    def restore_card(self, card_id: int) -> bool:
        """把卡册里的卡拉回轮播：位子满了拉不回来（调用方先让用户挑一张收起来）。"""
        card = self._card_dao.get(card_id)
        if card is None or not card.archived or self.personal_full():
            return False
        self._card_dao.set_archived(card_id, False)
        self._bus.card_changed.emit()
        logger.info("卡牌 #%d 从卡册拉回了轮播", card_id)
        return True

    # ---------- 自动归档（指令 25-B 第 4 项） ----------

    def auto_archive_stale(self) -> list[str]:
        """太久没打卡/交互的个人卡自动收进卡册，返回收起来的卡名（宠物要提一句）。

        数据一条不删：打卡记录、背景、目标都留着，从卡册随时能拉回来。
        只动个人卡——预设那两张与玄学卡不参与自动归档。
        """
        now = datetime.now()
        idle = timedelta(days=self._config.card.archive_idle_days)
        moved = [
            card
            for card in self._card_dao.list_by_archived(False)
            if is_personal_card(card) and now - self._last_active_at(card) >= idle
        ]
        for card in moved:
            self._card_dao.set_archived(card.id, True)
        if moved:
            titles = [card.title for card in moved]
            logger.info("自动归档：%d 张卡太久没动，收进卡册", len(titles))
            self._bus.card_changed.emit()
            self._bus.pet_speak_proactive.emit(self._archive_line(titles))
        return [card.title for card in moved]

    def _last_active_at(self, card: Card) -> datetime:
        """这张卡最近一次"有动静"是什么时候：打卡、在卡上说话，都没有就按建卡时间算。"""
        last = card.last_active_at or card.created_at
        checkin = self.last_checkin_at(card.id)
        if checkin is not None and checkin > last:
            last = checkin
        return last

    @staticmethod
    def _archive_line(titles: list[str]) -> str:
        """收进卡册之后跟用户说一句（宠物本体的话）。"""
        if len(titles) == 1:
            return f"「{titles[0]}」这些天一直没动，我先收进卡册了，想用随时拉回来。"
        return f"「{titles[0]}」等 {len(titles)} 张卡好久没动了，我先收进卡册，想用随时拉回来。"

    def card(self, card_id: int) -> Card | None:
        return self._card_dao.get(card_id)

    def checkin_count(self, card_id: int) -> int:
        return self._checkin_dao.counts_by_card().get(card_id, 0)

    def last_checkin_at(self, card_id: int) -> datetime | None:
        rows = self._checkin_dao.recent(card_id, limit=1)
        return rows[0].created_at if rows else None

    def set_status(self, card_id: int, status: str) -> None:
        self._card_dao.set_status(card_id, status)
        self._bus.card_changed.emit()

    def set_goal(self, card_id: int, goal: str) -> None:
        self._card_dao.set_goal(card_id, goal)
        self._bus.card_changed.emit()

    def set_name(self, card_id: int, name: str) -> None:
        """改这张卡的**显示名**（v2-social-card-identity）。只动 `name`，不动查重归属。"""
        self._card_dao.set_name(card_id, name)
        self._bus.card_changed.emit()

    def set_tags(self, card_id: int, tags: list[str]) -> None:
        """改写这张卡的分类标签（v2-social-card-identity）。"""
        self._card_dao.set_tags(card_id, tags)
        self._bus.card_changed.emit()

    def set_persona(self, card_id: int, persona_id: str) -> None:
        """改这张卡的陪执行角色（指令 14；空串 = 宠物本体）。

        空串要原样存进去，不能转成 None：None 表示"这张卡还没被选过"，
        那时出行 / 饮食卡会落到主题默认的专业角色（见 persona_of）。要是把
        "选了宠物本体"也存成 None，用户在出行卡上选回本体后一刷新又被导游顶掉。
        """
        self._card_dao.set_persona(card_id, persona_id)
        self._bus.card_changed.emit()

    def speaker_name(self, card: Card | None) -> str:
        """这张卡上是谁在说话：有角色就用角色卡的名字，否则是宠物本体。

        跟 persona_of 同源，所以出行卡不填 persona_id 时也报「导游·老周」——
        报的必须是真的在开口的那位。
        """
        persona_id = self.persona_of(card) if card is not None else ""
        if persona_id:
            return load_persona(persona_id).name
        return self._config.pet.name

    # ---------- 打卡 ----------

    def checkin(self, card_id: int) -> None:
        """打一次卡：记一笔，再让它说句鼓励的话。"""
        if self._card_dao.get(card_id) is None:
            return
        self._checkin_dao.add(card_id)
        self._bus.card_changed.emit()
        self.ask(ACTION_CHECKIN, card_id)

    # ---------- 卡牌上的 AI（引导 / 建议 / 打卡鼓励 / 对话） ----------

    def ask(
        self,
        action: str,
        card_id: int,
        text: str = "",
        history: list[tuple[str, str]] | None = None,
    ) -> None:
        """在后台让模型就这张卡说一段；回话走 card_reply（详情页）与 pet_speak（宠物开口）。"""
        card = self._card_dao.get(card_id)
        if card is None:
            return
        # 在卡上说句话也算一次交互：自动归档（指令 25-B 第 4 项）按它算活跃
        self._card_dao.set_last_active(card_id)
        if text and crisis.is_crisis(text):
            # 卡牌里也不能绕红线：用户在卡上说自伤的话，一样给危机话术，不交给模型自由发挥
            logger.warning("卡牌 #%d 上出现危机表达，走危机话术", card_id)
            self._speak(card_id, crisis.CRISIS_REPLY)
            return
        run_async(
            self._compose,
            action,
            card,
            text,
            tuple(history or ()),
            on_done=lambda reply: self._speak(card_id, reply),
            on_failed=lambda exc: self._failed(exc, card_id, text),
        )

    def _compose(self, action: str, card: Card, text: str, history: tuple) -> str:
        # 记忆里可能留着"明早"这类相对说法，按现在重新说一遍再交给模型
        now = datetime.now()
        memories = [
            memory_text(memory, now)
            for memory in self._memory_store.recall(f"{card.title} {card.goal} {text}")
        ][:MEMORY_LIMIT]
        last = self.last_checkin_at(card.id)
        return self._chain.reply(
            action,
            card,
            memories,
            system=self._system_for(card),
            checkin_count=self.checkin_count(card.id),
            last_checkin=last.strftime("%m-%d %H:%M") if last else "",
            history=history,
            text=text,
        )

    def persona_of(self, card: Card) -> str:
        """这张卡上"是谁在说"的角色 id（空串 = 宠物本体）。

        两种来源，用户选的压过默认：
        1. 卡上显式绑过的（指令 14，用户在下拉里选过）——包括"选了宠物本体"存下的空串；
        2. 从没选过（NULL）时看主题：出行卡默认是导游、饮食卡默认是营养主厨（指令 27）。
        界面的下拉也用这个方法取，显示的才是真的在说话的那位。
        """
        if card.persona_id is not None:
            return card.persona_id
        return THEME_PERSONAS.get(card.theme, "")

    def _system_for(self, card: Card) -> str:
        """这张卡的模型该按哪套框架说话：角色卡的专业框架 + 宠物性格的表层腔调。

        没角色（宠物本体）时就只叠性格层，跟以前一样。
        """
        style = persona_style_layer(self._config.pet, self._config.pet.name)
        persona_id = self.persona_of(card)
        if not persona_id:
            return style
        return load_persona(persona_id).system_with_style(style)

    def _speak(self, card_id: int, reply: str) -> None:
        self._bus.card_reply.emit(card_id, reply)
        self._bus.pet_speak.emit(reply)

    def _failed(self, exc: Exception, card_id: int, text: str) -> None:
        """这张卡上的回话挂了（断网、没填 Key、接口报错）。

        软表达（"撑不下去"这类）在这里兜一句本地关怀，别让它像没听见；
        跟聊天里同一套话术，不另写一份。关怀的腔调跟着这张卡的陪执行角色走：
        玄学卡是玄机先生，就别在它这边冒出安抚腔。
        """
        logger.error("卡牌 AI 回话失败: %s", exc)
        card = self._card_dao.get(card_id)
        care = crisis.soft_care_reply(text, divination=bool(card and card.persona_id))
        if care:
            logger.warning("模型不可用，卡牌 #%d 上的软表达改走本地关怀话术", card_id)
            self._speak(card_id, care)
            return
        self._bus.toast.emit(f"这张卡上它没能说出话来：{exc}")

    # ---------- 卡间画像互联（指令 24 第 4 条） ----------

    def plan_trip(self, card_id: int, destination: str, trip_date: datetime) -> None:
        """链 a：出行卡填了目的地 + 日期，去联网规划一趟。

        三件事：目的地与日期记到卡上（重启还在）、把"计划出行"回写进共享画像
        （链 b 靠这条记忆判断近期出行）、联网检索景点/美食/穿搭/防晒。
        美食那一段会把画像里相关的偏好与减脂目标一起给模型（只给相关字段，不喂全量记忆）。
        """
        card = self._card_dao.get(card_id)
        if card is None:
            return
        self._card_dao.set_trip(card_id, destination, trip_date)
        self._bus.card_changed.emit()
        self._remember_trip(destination, trip_date)
        run_async(
            self._compose_trip,
            destination,
            trip_date,
            on_done=lambda text: self._speak(card_id, text),
            on_failed=lambda exc: self._failed(exc, card_id, ""),
        )

    def _compose_trip(self, destination: str, trip_date: datetime) -> str:
        text, sources = self._chain.plan_trip(
            destination, trip_date.strftime("%Y-%m-%d"), self._profile_text()
        )
        return text + self._sources_text(sources)

    def _remember_trip(self, destination: str, trip_date: datetime) -> None:
        """把出行计划写回共享画像：带 abs_time，重启也还在，别的卡才读得到。"""
        run_async(
            self._memory_store.remember,
            "fact",
            f"用户计划 {trip_date.strftime('%m-%d')} 去{destination}，正在准备这趟出行",
            TRIP_MEMORY_IMPORTANCE,
            trip_date,
            on_failed=lambda exc: logger.error("出行记忆写入失败: %s", exc),
        )

    # ---------- 卡面建议（指令 27）：打开卡时按专业角色先说一段 ----------

    @staticmethod
    def has_pro_advice(card: Card) -> bool:
        """这张卡的上半框该不该由专业角色来说（指令 27）。

        界面拿它决定"是请角色说、还是用规则版打底"，判定跟 advise_card 同一个来源——
        两边各写一套的话，界面会对着服务根本不接的卡一直显示"正在想…"。
        """
        return card.theme in THEME_PERSONAS

    def advise_card(self, card_id: int, profile: dict[str, str]) -> None:
        """出行 / 饮食卡打开时，让这张卡的专业角色先说一段（进上半框，不念出来）。

        出行卡是导游、饮食卡是营养主厨（见 THEME_PERSONAS）；别的生活卡没有专业角色，
        这里直接不动。profile 由调用方从会话画像里取好传进来——画像只在内存，
        服务层不碰它（跟 plan_trip 收 destination / trip_date 是同一个路子）。

        模型这条挂了就退回规则版那一段：建议框不能一直停在"正在想…"。
        """
        card = self._card_dao.get(card_id)
        if card is None or card.theme not in THEME_PERSONAS:
            return
        fallback = rule_card_advice(card.theme, profile)
        run_async(
            self._compose_advice,
            card,
            dict(profile),
            # 建议的渲染出口在这先剥一道动作描写（（尾巴扫过手背）之类），
            # 界面那边还会再剥一次，两头都挡住。
            on_done=lambda text: self._bus.card_advice.emit(
                card_id, strip_action_marks(text)
            ),
            on_failed=lambda exc: self._advice_failed(exc, card_id, fallback),
        )

    def _compose_advice(self, card: Card, profile: dict[str, str]) -> str:
        now = datetime.now()
        memories = [
            memory_text(memory, now)
            for memory in self._memory_store.recall(f"{card.title} {card.goal} {card.theme}")
        ][:MEMORY_LIMIT]
        # 危机拦截（红线）：喂给模型的就是画像 + 记忆这两处素材，动手前先过一遍。
        # 命中了就不调模型，直接给它危机话术——这条永远走本地、不依赖模型（见 life/psych/crisis.py）。
        if crisis.is_crisis(" ".join([*profile.values(), *memories])):
            return crisis.CRISIS_REPLY
        # 饮食卡的老规矩（链 b）：画像里有近期出行，就补一句外食怎么选
        note = CARD_ADVICE_NOTE_NONE
        if card.theme == DIET_THEME:
            trip = self._recent_trip()
            note = DIET_TRIP_NOTE.format(trip=trip) if trip else DIET_TRIP_NOTE_EMPTY
        return self._chain.card_advice(
            card, profile, memories, system=self._system_for(card), note=note
        )

    def _advice_failed(self, exc: Exception, card_id: int, fallback: str) -> None:
        """建议没生成出来：记一笔，把规则版那段顶上，别让框空着。"""
        logger.error("卡面建议生成失败，退回规则版: %s", exc)
        self._bus.card_advice.emit(card_id, strip_action_marks(fallback))

    def _profile_text(self) -> str:
        """生成建议时要注入的画像字段：忌口/饮食偏好 + 在减脂时的热量目标。

        只挑相关的几条，不把整本记忆倒给模型——喂全量等于什么都没喂。
        """
        lines: list[str] = []
        preferences = self._preference_lines()
        if preferences:
            lines.append("忌口 / 饮食偏好：" + "；".join(preferences))
        diet = self._diet_card()
        if diet is not None:
            lines.append(f"正在减脂（卡片「{diet.title}」），热量目标：{diet.goal or '未写明'}")
        return "\n".join(lines) if lines else TRAVEL_PROFILE_EMPTY

    def _preference_lines(self) -> list[str]:
        """画像里的忌口/饮食偏好（记忆里 kind=preference 的那些）。"""
        memories = self._memory_store.recall("忌口 过敏 不吃 饮食偏好")
        return [memory.content for memory in memories if memory.kind == "preference"][:PROFILE_LIMIT]

    def _diet_card(self) -> Card | None:
        """用户自己建的减脂卡（没有就返回 None，美食那一段照常推荐）。"""
        for card in self._card_dao.list_all():
            if is_diet_card(card):
                return card
        return None

    def _recent_trip(self) -> str:
        """画像里有没有近期出行（链 a 回写的那条带时间的记忆）。

        只看"未来 RECENT_TRIP_DAYS 天内"的，过期的出行不再影响饮食建议。
        """
        now = datetime.now()
        deadline = now + timedelta(days=RECENT_TRIP_DAYS)
        for memory in self._memory_store.recent(MEMORY_SCAN_LIMIT):
            if memory.kind not in TRIP_MEMORY_KINDS or memory.abs_time is None:
                continue
            if now <= memory.abs_time <= deadline:
                return memory.content
        return ""

    def active_card_of_theme(self, theme: str) -> Card | None:
        """这个品类在手边的卡（指令 26-B ②）：轮播上的才算，收进卡册的不算。

        用户自己认领的卡就摆在眼前，够得着才叫服务；收起来的那张得先去卡册拉回来。
        """
        for card, _count in self.plaza():
            if card.theme == theme:
                return card
        return None

    def scenario_advice(self, card_id: int, text: str) -> None:
        """情境建议（指令 26-B ②）：用户正卡在一件事上，就这张卡给一句马上能用的。

        只有出行卡有专属这条链（"赶时间该怎么走"，走联网）。别的卡打开时本来就把自己的内容
        带出来了（比如减脂卡的饮食建议），不在这里再补一段，免得同一件事说两遍。
        """
        card = self._card_dao.get(card_id)
        if card is None or card.theme != TRIP_THEME:
            return
        run_async(
            self._compose_scenario_trip,
            text,
            on_done=lambda body: self._speak(card_id, body),
            on_failed=lambda exc: self._failed(exc, card_id, ""),
        )

    def _compose_scenario_trip(self, text: str) -> str:
        """赶时间的走法：把用户刚说的这句连同相关记忆一起交给联网那条链。

        只留那两三句建议——角标与来源列表都不摆上来（指令 26-B 的"卡轻"）：
        人正赶着出门，要的是照着做的一句话，不是一份可核对的报告
        （要对照来源的完整规划，在出行卡详情页里，那条链照样带来源）。
        """
        now = datetime.now()
        memories = [
            memory_text(memory, now)
            for memory in self._memory_store.recall(f"{TRIP_THEME} {text}")
        ][:MEMORY_LIMIT]
        body, _sources = self._chain.quick_trip(text, memories)
        return _strip_citations(body)

    @staticmethod
    def _sources_text(sources) -> str:
        """把联网返回的真实来源附在建议后面（模型自己写的链接一律不摆上来）。"""
        if not sources:
            return ""
        lines = ["", "参考来源："]
        lines.extend(f"[{source.index}] {source.title} {source.url}" for source in sources)
        return "\n".join(lines)

    # ---------- 卡牌提醒（复用主动提醒调度） ----------

    def create_reminder(self, card_id: int, at: datetime) -> bool:
        """给这张卡设一条定时提醒；时刻已过就拒绝。"""
        card = self._card_dao.get(card_id)
        if card is None or at <= datetime.now():
            return False
        content = f"「{card.title}」{card.goal or card.theme}"
        reminder = self._reminder_dao.add(self._user_id, at, content[:60], None)
        self._jobs.schedule(reminder)
        self._bus.reminder_changed.emit()
        logger.info("卡牌 #%d 设了提醒 @ %s", card_id, at)
        return True
