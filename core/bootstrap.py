"""
依赖装配：把配置、数据库、业务服务、界面拼成一个应用。

装配顺序（单向依赖）：config → logging → data → 智能层/记忆/调度 → 服务 → 界面 → 信号接线。
界面层只通过事件总线与服务层通信，服务层不引用任何界面类。
"""

from __future__ import annotations

import logging
import math

from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog

from brain.chains.activity import ActivityChain
from brain.chains.card_ai import CardAIChain
from brain.chains.care import CareChain
from brain.chains.intent import IntentChain
from brain.chains.chatter_parse import (
    COMMAND_ACTIVITY_REPORT,
    COMMAND_HIDE_PET,
    COMMAND_OPEN_LIFE,
    COMMAND_OPEN_SETTINGS,
    COMMAND_OPEN_WORK,
    COMMAND_QUIT,
    COMMAND_START_PLAYMATE,
    COMMAND_STOP_PLAYMATE,
    ChatterParseChain,
)
from brain.chains.playmate import PlaymateChain
from brain.chains.proactive import ProactiveChain
from core.config import Config
from core.events import EventBus
from core.logging import setup_logging
from core.reset import MemoryReset
from data.dao import (
    ActivitySessionDAO,
    AppCategoryDAO,
    CardCheckinDAO,
    CardDAO,
    CardInviteDAO,
    CardShareDAO,
    CardTopicEventDAO,
    CustomCardDAO,
    MemoryDAO,
    MoodEventDAO,
    PetProfileDAO,
    PomodoroSessionDAO,
    ReminderDAO,
    ServiceConsentDAO,
    UserDAO,
    UtteranceDAO,
)
from data.db import Database
from focus.pomodoro import Phase, PomodoroService
from life.cards.chat import LIFE_KIND, WORK_KIND, CardChatService, ensure_card_kb_root
from life.cards.layout import CardLayoutStore
from life.cards.reminders import extract_reminder_nodes
from life.cards.service import CardService
from life.care import CareService
from life.chatter import ChatterService, ServiceHooks
from life.dialog import router
from life.greeting import GreetingService
from life.intent import IntentJudge
from life.mind import emotion_log
from life.mind.distill import MindDistiller
from life.mind.index import MindVectorIndex
from life.mind.intimacy import EXP_CHAT, EXP_INTERACTION, EXP_STRONG_EVENT, MindIntimacy
from life.mind.kb import KnowledgeBase
from life.mind.memory_book import MemoryBook
from life.mind.proactive import MindProactiveService
from life.mind.recall import MindRecall
from life.mind.store import MindStore
from life.playmate import PlaymateService
from life.profile.extract import extract_diet_profile, extract_travel_profile, extract_work_profile
from life.profile.store import DIET, TRAVEL, WORK, ProfileStore
from life.proactive import MAX_PER_DAY, MIN_GAP_SEC, ProactiveService
from life.psych.heavy_care import HeavyCareService
from life.psych.service import PsychService
from life.screen.companion import ScreenCompanion
from life.screen.focus_timer import ScreenFocusTimer
from life.screen.game import (
    ASK_LINE,
    LOCAL_RULES_FILE,
    GameGuideService,
    LocalGameRules,
)
from life.screen.monitor import ScreenMonitor
from life.screen.scene_guard import (
    KIND_FOCUS,
    KIND_GAME,
    KIND_IDLE,
    KIND_SOCIAL,
    KIND_WATCH,
    KIND_WORK,
    SceneArbiter,
)
from life.screen.scene_state import ASK_NOTES, SceneStateService, is_work_page
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR
from life.screen.notes import ScreenNotesService
from life.screen.social import (
    ASK_CARD_LINE,
    ASK_LINE as SOCIAL_ASK_LINE,
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
    SocialReplyService,
    classify_chat_page,
    is_social_proc,
    looks_like_list_page,
)
from life.screen.social_examples import SocialExamples
from life.voice.listen import VoiceListener
from life.voice.tts import VoiceEngine
from memory.embedding import OnnxChineseEmbedding
from memory.store import MemoryStore
from memory.vector import MemoryVectorStore
from monitor.service import ActivityService
from pet.behavior import Behavior, BehaviorScheduler
from pet.persona import CUSTOM_KEY, PERSONA_KEYS, preset_of, preset_style, strip_action_marks
from pet.state import PetStateMachine
from scheduler.engine import SchedulerEngine
from scheduler.jobs import ReminderJobs
from ui.chat_bubble import ChatBubble
from ui.card_dialogs import CardOfferDialog
from ui.panels.card_panel import CardDetailPanel
from ui.panels.carousel import CardOverlay
from ui.panels.container import PanelWindow
from ui.panels.life_panel import LifePanel
from ui.panels.playmate_panel import PlaymatePanel
from ui.panels.pomodoro_panel import PomodoroPanel
from ui.panels.work_card_panel import WorkCardPanel, WorkDeptPanel
from ui.panels.work_panel import WorkFlowPanel, WorkPanel
from ui.pet_skin import POSE_IDLE, PetSkin, scene_skin
from ui.pet_window import PetWindow
from ui.pomodoro_bubble import PomodoroBubble
from ui.settings import SettingsDialog
from ui.social_draft import GuideCardDialog
from ui.tray import PetTray
from work.chain import WorkChain
from work.service import WorkService

logger = logging.getLogger(__name__)

FIRST_RUN_HINT_DELAY_MS = 1500
ACTIVITY_REPORT_SHOW_MS = 20000   # 今日活动报告要多留一会儿才看得完
SCREEN_CAPTION_SHOW_MS = 6000     # 看片开场字幕停 6 秒（v2-speech-polish ④，用户指定）
# 观影状态词弹出后**常驻**（v2-scene-state-fix ④）：用户要的是"情绪词
# 常驻头顶直到情绪变"，不再 60 秒自动淡出。这里用一个"常驻"哨兵值传给气泡（show_ms
# <= 0 = 不排自动淡出），情绪变（v2-mood-window 去抖后换词）时新词自己盖掉旧词，
# 散场（monitor.scene 回空串）时由 `_on_screen_scene_changed` 收掉。
SCENE_STATUS_PERSIST_MS = 0
# 观影状态词常驻的**两态**（v2-mood-fear-tune）：当前场景落在怒/笑里，状态词才常驻头顶。
# 恐惧从这三格里摘出去了——用户要的是「恐/紧张只当观影轻反应、不升格强情绪词」：恐惧
# 那句「我怕怕…」照说（companion 的一口轻反应），但不常驻、随默认时长淡出；真恐的表达
# 交给 `_apply_scene_skin` 切「举手害怕」国风图（皮肤那条腿照旧，不受这里影响）。
# v2-mood-tri：哭泣已整态删除，这一格跟着没了——它不可能再是 monitor.scene 的值
# （`_count_scene` 的兜底闸把不认识的态挡在投票窗前），留着就是挂起死配置。
_SCENE_STATUS = (SCENE_ANGRY, SCENE_FESTIVE)
QUIT_AFTER_REPLY_MS = 1200        # 聊天里说"退出"时，先把它的回话留在眼前一下再关

# ---- 社交候选怎么亮给用户（v2-social-copy-clean）----
# 老口径这两段（候选正文 + 解析）是摆在一个**模态弹窗**「这样回怎么样（回这条）」里的，
# 弹窗里还有"复制第 N 条"按钮。那一套整档删了：`exec()` 跑的是嵌套事件循环，1.5 秒
# 那个轮询照转，真机上表现成"复制完卡死 + 弹窗摞弹窗"。现在两件事各自归位——
# **候选走宠物自己的非模态气泡**，**复制由气泡上那个「复制回复」按钮触发**
# （v2-social-chat-ui-copy 批次11：那两句收尾交代整段去掉了，剪贴板也不在生成那一刻
# 偷偷写了——写剪贴板这件事只在用户真按下按钮时发生一次）。
SOCIAL_DRAFT_NO_WHY = "（这条没带解析）"

# ---- 社交"尾巴"诊断（v2-social-leak-mask）----
# 真机上"编造 / 答非所问"要能一条日志看出因果：采的时候前台是谁、涂掉了自己几块窗、
# 认完之后喂给模型的**末尾那一句**到底是什么。下面这两个只管日志，不参与任何判断。
SOCIAL_TAIL_TRACE_CLIP = 60        # 日志里只留尾巴末尾这么多字（脱敏，不是整屏聊天记录）
SOCIAL_TAIL_PROBE = "你脸皮也不薄"   # 真机上报"凭空冒出来"的那一句，日志里现问含不含它
SOCIAL_LEAK_DUMP_GAP_S = 60        # 认出会话列表草稿预览时落图复验的最密间隔（秒，15-Y）
# 上面那条是"认得出来才落"，这一条是它的**取证前置**（v2-social-leak-dump-debug）：
# 不等认出来，只要那一帧的原文里出现 `[草稿`，就把**裁剪前的整帧**落一张——漏源（左侧
# 会话列表有没有入镜）要拿真帧看，光有几个数判不了。
_LEAK_DUMP_MIN_GAP = 10            # 秒：漏是持续的，太密就一秒一张，撑爆临时目录
_leak_dump_ts = None               # 上一次落图的时刻（模块级，首次命中即写、只防刷屏）

# ---- 帮聊那一则的 3 分钟自动清除（v2-social-card-timeout）----
# 用户**离开对话窗口**（气泡失焦 / 被收起，或微信不在前台）那一刻起满 3 分钟、这中间
# 也没按「复制回复」，就把那一则清掉、回到初始聊天态（输入框还在、建议气泡收起）。复制
# 过的卡片不走这条：内容已经被取走了，留着无害。
# **计时只在"人不在"时跑**——用户一直坐在框跟前看，定时器自始至终不启动；这是这条线最
# 要紧的一处防错点（宁可多留，也不误清）。
CARD_CLEAR_S = 180.0


def social_draft_body(draft: str) -> str:
    """候选正文那一则（v2-social-chat-ui-copy 批次11：从 `social_draft_text` 里拆出来）。

    只含**要粘的那段**：顶格、带「」，一眼能看出哪句是要粘的。开头那句交代
    （老 `SOCIAL_DRAFT_HEAD`，"我按你俩的语境拟的，你看看要不要改"）与收尾那句
    （老 `SOCIAL_DRAFT_TAIL` / `..._FAIL`，"已经放进你剪贴板了…" / "没能放进剪贴板…"）
    **批次11 起整段删掉了**：
    - 开头那句：用户点 [好呀] 要的是能直接粘的那段，这句话只是横在中间；而且"按你俩的
      语境"在处境格留空时（点 [好呀] 那条路）本来就不成立。
    - 收尾那两句：它们替"剪贴板里到底有没有"打包票，而**剪贴板这件事已经不由这一刻决定**
      ——批次11 起不在生成那一刻偷偷写了，改由气泡上那个「复制回复」按钮触发（用户按下
      才写、写不写得成他自己看得见按钮的反应）。没有"这一刻写没写成"这个事实，那两句
      真话/假话的收尾也就没有立足的地方了。
    「」必须**跟着正文这一段走**：它单占一条气泡（正文进方框、解析另起一条居中小字），
    没有别的段落可以挂这个记号了。
    """
    body = (draft or "").strip() or "（这次没拟出来）"
    return f"「{body}」"


def social_draft_note(why: str) -> str:
    """解析那一则（批次11 起单独一条，走 `append_note` 的居中灰字）。"""
    note = (why or "").strip() or SOCIAL_DRAFT_NO_WHY
    return f"解析：{note}"


def social_draft_text(draft: str, why: str) -> str:
    """整则合成一段文本（正文 + 解析）：给"需要一口气看全"的地方与测试用。

    气泡里这两段现在是**两条**（正文进方框、解析居中小字，批次11），这个合成函数
    只保证还有一条把两段连起来读的口径在，正文/解析各自的措辞以那两个函数为准。
    """
    return f"{social_draft_body(draft)}\n\n{social_draft_note(why)}"


ARCHIVE_CHECK_MS = 6 * 60 * 60 * 1000   # 每 6 小时扫一遍"太久没动"的卡（指令 25-B 第 4 项）


class DesktopPetApp:
    def __init__(self) -> None:
        self.config = Config.load()
        # 数据根先定下来（E 盘写不进就退回项目内 data/），再做日志与迁移——
        # log_dir 也落在 data_dir 下，得连它一起退（v2-data-root）。
        fallback_root = self.config.prepare_data_root()
        setup_logging(self.config.app.log_level, self.config.log_dir)
        logger.info("=== %s 启动 ===", self.config.app.name)
        if fallback_root:
            logger.error("数据根 %s 不可写，运行期数据退回项目内 data/", fallback_root)
        self.config.migrate_runtime_data()

        self._init_storage()
        self._init_services()
        self._init_ui()
        self._wire()

    # ---------- 初始化 ----------

    def _init_storage(self) -> None:
        config = self.config
        self.db = Database(config.db_path, config.pet.name, config.pet.persona)
        self.db.init()
        session_factory = self.db.session_factory

        self.user = UserDAO(session_factory).local_user()
        self.pet_profile_dao = PetProfileDAO(session_factory)
        self.utterance_dao = UtteranceDAO(session_factory)
        self.memory_dao = MemoryDAO(session_factory)
        self.reminder_dao = ReminderDAO(session_factory)
        self.pomodoro_dao = PomodoroSessionDAO(session_factory)
        self.activity_dao = ActivitySessionDAO(session_factory)
        self.app_category_dao = AppCategoryDAO(session_factory)
        self.card_dao = CardDAO(session_factory)
        self.card_checkin_dao = CardCheckinDAO(session_factory)
        self.card_event_dao = CardTopicEventDAO(session_factory)
        self.card_invite_dao = CardInviteDAO(session_factory)
        self.mood_event_dao = MoodEventDAO(session_factory)
        self.service_consent_dao = ServiceConsentDAO(session_factory)
        self.card_share_dao = CardShareDAO(session_factory)
        self.custom_card_dao = CustomCardDAO(session_factory)

        self.bus = EventBus()
        # ---- 语音两条线：引擎留在手上，但这一轮一个入口都不接（v2-chat-core） ----
        #
        # 用户的真机反馈：聊天输入区只该有"文字框 + 发送"，说出去的话也不该被念回来。
        # 于是这一轮把"用户看得见的入口"全摘了——聊天气泡里那个「按住说话」按钮、
        # 桌宠与托盘菜单里的「语音输入」项、全局快捷键 Ctrl+Alt+Space 的注册与接线。
        #
        # **引擎本身没删**：`life/voice/` 四个模块、`config.yaml` 的 voice 段（ASR 那几项）、
        # 设置页的语音输入段，全都原样留着。下面这两个实例也照旧建出来，只是**没有任何
        # 地方会调它们**——留着是为了将来想开回来时只接一根线，不必重建。
        # 麦克风照旧只在 `begin()` 那一刻才开；没人 begin，就永远没有采集、没有音频落盘。
        self.voice = VoiceEngine(enabled=config.voice.enabled)
        self.voice_listener = VoiceListener(config)
        # 向量模型只加载一次，下面两条线共用（情感记忆的索引也用这个函数算向量）
        embedding = OnnxChineseEmbedding(config.model_dir)
        self.vector_store = MemoryVectorStore(config.vector_path, embedding)
        self.memory_store = MemoryStore(config, self.memory_dao, self.vector_store, self.user.id)

        # 情感记忆加密库（S1）：另起一个库、另起一把钥匙，跟上面这套完全没有交集。
        # 建不起来（比如凭据服务异常）不能连累桌宠启动——记忆是锦上添花，桌宠本体优先。
        self.mind_store: MindStore | None = None
        self.mind_index: MindVectorIndex | None = None
        if config.mind.enabled:
            try:
                self.mind_store = MindStore(config.mind_db_path)
            except Exception as exc:
                logger.error("情感记忆库打不开（%s），本轮不记情感记忆", exc)
            else:
                # 向量索引是第二条腿，跟加密库分开 try：它起不来只是检索退回字面
                # 匹配（见 life/mind/recall.py），不该把整条情感记忆线一起拖下水
                try:
                    self.mind_index = MindVectorIndex(config.vector_path, embedding)
                    self.mind_store.attach_index(self.mind_index)
                except Exception as exc:
                    logger.error("情感记忆向量索引没起来（%s），检索退回字面匹配", exc)

    def _init_services(self) -> None:
        config = self.config
        self.scheduler_engine = SchedulerEngine()
        self.jobs = ReminderJobs(config, self.reminder_dao, self.scheduler_engine, self.bus)

        # 宠物本体就是心理医生「林医生」：聊天里的回话都由它按角色卡生成（指令 11）
        self.psych_service = PsychService(
            config=config,
            bus=self.bus,
            memory_store=self.memory_store,
        )
        # 生活卡牌（指令 12 / 13）：对话里聊出来的主题攒到阈值就提议建卡
        # 会话级画像（指令 26-C C1）：工作 / 出行两块，只存内存不落库
        self.profile_store = ProfileStore()
        self.card_service = CardService(
            config=config,
            bus=self.bus,
            card_dao=self.card_dao,
            checkin_dao=self.card_checkin_dao,
            event_dao=self.card_event_dao,
            invite_dao=self.card_invite_dao,
            share_dao=self.card_share_dao,
            reminder_dao=self.reminder_dao,
            jobs=self.jobs,
            memory_store=self.memory_store,
            chain=CardAIChain(config),
            user_id=self.user.id,
        )
        # 预置 2 张生活卡（指令 25-A 第 1 项）：饮食 / 出行，只补缺的；
        # 紧接着把老库里不该留在轮播上的卡收进卡册（第 4 项：删掉的预设 + 超出 4 张的个人卡）
        self.card_service.ensure_preset_cards()
        self.card_service.migrate_cards()
        # 玄学族并进「命理」：老库里散在玄学主题下的观察记录归并过来（幂等）
        self.card_service.migrate_divination_themes()
        # 太久没打卡/交互的卡自动收进卡册（指令 25-B 第 4 项）；启动时先过一遍
        self.card_service.auto_archive_stale()
        self.chatter_service = ChatterService(
            config=config,
            bus=self.bus,
            utterance_dao=self.utterance_dao,
            reminder_dao=self.reminder_dao,
            parse_chain=ChatterParseChain(config),
            jobs=self.jobs,
            user_id=self.user.id,
            responder=self.psych_service.respond,
            # 生活卡：用户点头后先弹确认框（卡名可改），再建卡
            card_creator=self._create_card_from_offer,
            current_persona=lambda: self.psych_service.persona_id,
            # 主动服务（指令 26-B）：判意图、查卡、按邀请制问一句，都在会话层之外接好
            serve=ServiceHooks(
                intent=IntentJudge(config, self.card_service._classifier),
                chain=IntentChain(config),
                consents=self.service_consent_dao,
                find_card=self._find_card_of_theme,
                invite=self.card_service.invite_now,
            ),
            # 路由骨架 S4：会话层说这句话时露出的情绪，报给情感记忆的主动关怀当由头。
            # mind_proactive 在下方才建，这里用惰性 lambda（说话时才取值，那时一定建好了）
            on_emotion=lambda emotion: (
                self.mind_proactive.note_emotion(emotion) if self.mind_proactive else None
            ),
        )
        self.care_service = CareService(
            config=config,
            bus=self.bus,
            reminder_dao=self.reminder_dao,
            memory_store=self.memory_store,
            care_chain=CareChain(config),
        )
        self.pomodoro_service = PomodoroService(
            config=config,
            bus=self.bus,
            session_dao=self.pomodoro_dao,
            jobs=self.jobs,
            user_id=self.user.id,
        )
        self.greeting_service = GreetingService(
            config=config,
            bus=self.bus,
            is_focusing=self._is_focusing,
        )
        self.activity_service = ActivityService(
            config=config,
            bus=self.bus,
            session_dao=self.activity_dao,
            category_dao=self.app_category_dao,
            jobs=self.jobs,
            comment_chain=ActivityChain(config),
            user_id=self.user.id,
        )
        self.proactive_service = ProactiveService(
            config=config,
            bus=self.bus,
            memory_dao=self.memory_dao,
            chain=ProactiveChain(config),
            activity_summary=self.activity_service.today_summary_text,
            is_user_present=lambda: self.activity_service.user_present,
            is_focusing=self._is_focusing,
        )

        # 情感记忆三件套（S3/S4/S5）：攒对话 → 聊完提炼进加密库 → 有由头主动提一句。
        # 提炼要调模型（只发对话文字，绝不发屏幕内容）；检索与主动搭话全是本地的。
        # 主动搭话借 proactive_service 的出口排队，门控（免打扰/专注/人不在/间隔/上限）
        # 一次继承，不另起一套。库打不开时这三件一并省掉，不假装记得。
        self.mind_recall: MindRecall | None = None
        self.mind_distiller: MindDistiller | None = None
        self.mind_proactive: MindProactiveService | None = None
        # B 养成两件（好感度 / 记忆簿）：纯本地规则，不调模型、不联网。
        # 好感度决定语气多亲、多久主动找你一次；记忆簿挂在提炼回调上落库。
        # 星念（mind_star_cards）在 v2-life-card-star 砍掉了：设置页不再显示、
        # 数据层也不再立卡（见 _on_mind_distilled）。
        self.mind_intimacy: MindIntimacy | None = None
        self.mind_memory_book: MemoryBook | None = None
        if self.mind_store is not None:
            self.mind_recall = MindRecall(self.mind_store, index=self.mind_index)
            # 昵称是现取的：设置页改完立刻生效，不用重启
            self.mind_intimacy = MindIntimacy(
                self.mind_store,
                nickname_provider=lambda: config.mind.intimate_nickname,
            )
            # 记忆簿借一步好感度，只为在页头写一句"现在到哪一档了"
            self.mind_memory_book = MemoryBook(self.mind_store, intimacy=self.mind_intimacy)
            self.mind_distiller = MindDistiller(
                config=config,
                store=self.mind_store,
                on_distilled=self._on_mind_distilled,
            )
            self.mind_proactive = MindProactiveService(
                config=config,
                store=self.mind_store,
                recall=self.mind_recall,
                queue_speak=self.proactive_service.queue_mind,
                intimacy=self.mind_intimacy,
            )
            # 升到"熟络"以上，回话时多一段"更亲"的腔调；没到就是空串，一个字不加
            self.psych_service.set_tone_provider(self.mind_intimacy.tone_suffix)
            # 知己档（L4）：他拿不定主意来问时，反问一句要他的主意。
            # 等级、意图、犹豫词都由 intimacy 那边自己判，不命中就注入空串
            self.psych_service.set_ask_back_provider(self.mind_intimacy.ask_back_hint)
            # 断网兜底（v2-offline-fallback）：按熟悉度挑一句陪伴寒暄，替掉「没听懂」。
            # 会话（解析/意图分类）与心理（回话）两条线都接同一个来源。
            self.psych_service.set_offline_reply_provider(self.mind_intimacy.offline_greeting)
            self.chatter_service.set_offline_reply_provider(self.mind_intimacy.offline_greeting)
            # 它主动开的口被人接住了（十来分钟内又跟它说话 / 摸了摸它）→ 记一笔
            self.proactive_service.set_replied_handler(
                lambda: self._gain_intimacy(EXP_INTERACTION, "主动搭话被回应")
            )
            self._apply_intimacy_pace()

        # 清空记忆与好感度（v2-reset-state）：设置页那个开关的服务端。
        # **不塞进上面那个 if**：pet.db 那一半（对话记录 / 长期记忆）跟 mind.db 没关系，
        # 情感记忆库打不开时它照样该能清——没接上的那几件在清单里少几项而已。
        self.memory_reset = MemoryReset(
            utterance_dao=self.utterance_dao,
            memory_dao=self.memory_dao,
            memory_vectors=self.vector_store,
            mind_store=self.mind_store,
            mind_index=self.mind_index,
            distiller=self.mind_distiller,
            bus=self.bus,
        )

        # 知识库问答（v2-rag）：问"我之前记过的 X"，把本机存的原文翻出来当上下文。
        # 三处来源里碎片那一路要用 mind_recall，没有就退化成只翻笔记与旧记忆——
        # 所以**不塞进上面那个 if**：情感记忆没开，抓屏笔记照样翻得动。
        # 检索全在本机（chroma + bge + 读文件），出本机的只有最后拼好的那几行，
        # 走的是既有的生成链；这条线自己不认识任何模型，也就没有"检索走 API"这回事。
        # 知识库目录落定（v2-kb-init）：启动时确保它存在，用户往里丢 .md/.txt 就能被问到。
        # 建不出来（盘没挂 / 权限）不拦启动——问答退回其余三源，检索腿自己会判空。
        try:
            config.knowledge_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("知识库目录建不出来（%s），知识库问答退回其余三源", config.knowledge_dir)
        self.kb = KnowledgeBase(
            config=config,
            recall=self.mind_recall,
            memory_store=self.memory_store,
        )
        # 不判命中的句子这里回空串，回话链一个字都不变（判定在 provider 里，见 kb.py）
        self.psych_service.set_kb_provider(self.kb.context_if_question)

        # 屏幕感知三件套（S2/S3/S4）：看画面认场景 → 陪看陪聊 → 自动番茄钟。
        # 全是本地判断，一句话都不发给模型；识别结果与计时状态只在内存里，不落盘。
        #
        # 用户本地游戏名单（v2-game-activate）：**启动时读一次**，之后判定全是内存里
        # 的字典查（判定跑在每拍采样上，那里不能有文件 IO）。文件不在、读不动、写乱了
        # 都退回一份空名单，**不拦启动**——一份用户手写的名单不该把程序弄崩。
        # 路径走 config.data_path，本机正好是用户要的 `E:\电子宠物\game_procs.txt`。
        game_rules = LocalGameRules.load(config.data_path(LOCAL_RULES_FILE))
        self.screen_monitor = ScreenMonitor(config=config, game_rules=game_rules)
        self.screen_monitor.set_pet_rect_provider(
            lambda: self.pet_window.frameGeometry() if self.pet_window.isVisible() else None
        )
        # 场景态用到的三个标记（v2-scene-state）：**在仲裁点建起来之前**就得存在。
        # 场景状态机一接线就会让仲裁点先算一次（`attach` 末尾那一句），那一刻起
        # `_on_scene_state_changed` 就可能被叫到——它要读这三个。眼下第一次算出来
        # 一定是"空档"（还没人采样，什么都没成立），但把顺序押在这上面不划算。
        self._pet_user_hidden = False    # 用户自己把宠物藏起来了吗（场景态绝不抢这个）
        self._muted_kind = ""            # 场景态里点了「不用了」的那一场（换场次就解除）
        self._focus_quiet_on = False     # 刚进专注那一下清过残留没有（边沿触发）
        self._notes_offer_resumed = True  # 14-F：那一问（「要不要发」）已经出过面了吗，True = 没有待补的
        self._closing = False            # 正在退出：专注结束那条沿不许再补弹（见 shutdown）

        # 场景互斥守卫（v2-mood-detect）：专注 / 游戏 / 观影 / 工作 / 休闲 同一时刻只认一个。
        # 四条线各听各的信号，原先没有一个地方知道"现在到底算哪一场"——真机上那两件
        # 事（番茄钟跑着还冒"看电影啦"、全屏游戏被判成看电影）就是这么来的。这里把
        # "谁说了算"收成一个只读的仲裁点，下面陪看与游戏攻略都问它一句。
        #
        # 专注与工作两档给的是**回调**、不是布尔值（v2-scene-state）：信号是一条派发给
        # 所有线的，排在场景状态机前面的线（看片、番茄钟）会在派发半途问一次仲裁点，
        # 那时推过去的标记还是上一拍的——真机上就是"从工作页切走"那一下宠物先冒了句
        # 「我陪你一起工作」。现算就没有这个缝，详见 scene_guard 类开头那段。
        self.screen_scene_guard = SceneArbiter(
            is_focusing=self._is_focusing,
            is_working=lambda: is_work_page(self.screen_monitor),
            is_social=self._social_holds_floor,
        )
        # 专注那条线要**先**挂到采样器上（v2-scene-state）：前台是一条信号派发给所有线，
        # 看片/工作那几条在派发半途会回头问仲裁点"这会儿是哪一场"。专注那一档靠
        # `screen_focus_timer.running`，只有它自己的 handler 跑完才算数——排在它后面挂，
        # 前面的线问的时候它还是上一拍的"没在专注"，就会先算成"工作"、冒出
        # 「我陪你一起工作」，下一拍再被专注压回去（真机看到过）。专注优先级最高，
        # 得第一个认领前台这一拍。
        self.screen_focus_timer = ScreenFocusTimer(config=config, bus=self.bus)
        self.screen_focus_timer.attach(self.screen_monitor)
        self.screen_focus_timer.changed.connect(self._on_screen_focus_changed)
        self.screen_companion = ScreenCompanion(
            config=config, bus=self.bus, guard=self.screen_scene_guard
        )
        self.screen_companion.attach(self.screen_monitor)
        # 情绪词常驻之后，散场（monitor.scene 回空串）得有人收掉头顶那句（v2-scene-state-fix ④）。
        # 情绪**变**（horror→angry）不算散场：新词会自己盖掉旧词，这里只认空串那一拍。
        self.screen_monitor.scene_changed.connect(self._on_screen_scene_changed)
        # 聊天里明说要专心 → 开一轮（v2-chat-intent ②）。接的是**屏幕计时器**这条
        # 内存里的线，不是 `PomodoroTimer.start_focus`——后者会往 pet.db 落一行
        # 会话记录，那就破坏了"聊天不写库"这条红线（见 _on_focus_requested）。
        self.bus.focus_requested.connect(self._on_focus_requested)
        # 抓屏笔记（C 多模态 · 笔记先行版）：只在专注态抓前台窗口认字、提炼成笔记。
        # 默认关着——抓屏上模型是件要用户自己开的事（见 NotesSection）。
        #
        # 专注态用 `_is_focusing()`（手动番茄钟 **或** 自动那套），不是只看自动那套
        # running：用户手动点「开始专注」进番茄钟后，自动那套根本没被前台触发，
        # 原先只看 `screen_focus_timer.running` 就把这一拍当成"不在专注"跳过了，
        # 专注期间笔记一个字都不记（v2-notes-focus）。
        self.screen_notes = ScreenNotesService(
            config=config,
            bus=self.bus,
            is_focusing=self._is_focusing,
            # v2-scene-state：工作态里用户点过「好呀」→ 这一场随时整理（判据仍在
            # 场景状态机那边，只有前台真是工作/网课/创作页才算）。
            work_ok=lambda: bool(
                getattr(self, "scene_state", None) is not None
                and self.scene_state.notes_allowed()
            ),
            pet_rect=lambda: self.pet_window.frameGeometry() if self.pet_window.isVisible() else None,
        )
        # 换屏即拍（v2-notes-cadence）：前台换窗口/应用就立即抓一拍。复用 tick 的
        # 专注态判断——不在专注时 snap_now 内部自己就返回，不会误抓。
        self.screen_monitor.foreground_changed.connect(self._on_foreground_for_notes)
        # 游戏攻略（v2-game-guide）：前台是认得出的游戏就问一句"要不要攻略"。
        # 挂的是上面那个 screen_monitor——不新开采样循环；认游戏那五条线索（含要问
        # Windows 与要连拍的那几条）全由它代劳，见 ScreenMonitor.game_hit。
        # can_offer 那道闸是防"气泡上同时排两排按钮"：笔记正等答复时就不插嘴。
        # rules 跟采样器**共用同一份**（同一个 LocalGameRules 对象），两边口径一致。
        self.game_guide = GameGuideService(
            config=config,
            bus=self.bus,
            can_offer=lambda: not (
                self.screen_notes.awaiting_confirm or self._social_offer_pending()
            ),
            guard=self.screen_scene_guard,
            rules=game_rules,
            # "过一会儿再问"（v2-scene-state）：前台一切到游戏先冒开场白
            # （「我陪你一起玩游戏」），隔一会儿再问攻略，两句不挤在同一个气泡里。
            # 隔多久**不在这儿写死**，跟着 `screen.guide_ask_delay_sec` 走（默认 30 秒，
            # 服务每次要用时现读），同一个数字只有一个出处。
        )
        self.game_guide.attach(self.screen_monitor)

        # 场景状态机（v2-scene-state）：工作 / 电影 / 游戏三态互斥，宠物坐起来陪着。
        # 它自己**不新增采样**——工作页的判据喂给上面那个仲裁点，其余两档（电影 = 看片
        # 会话、游戏 = 前台认得出的游戏）本来就有人喂；这一层只把"这会儿是哪一场"
        # 翻译成宠物的样子（坐起 / 开场白 / 问一句），三态互斥由"只有一个仲裁点"保证。
        self.scene_state = SceneStateService(
            config=config,
            bus=self.bus,
            guard=self.screen_scene_guard,
            # 社交建卡询问是最高优先级（v2-social-reply-fix）：候选列完那一问别被工作态
            # 「整理笔记」顶掉。
            can_offer=lambda: not self._social_offer_pending(),
        )
        self.scene_state.attach(self.screen_monitor)
        # 接在**状态机自己**的 changed 上（而不是仲裁点那一条）：状态机先在同一拍里
        # 记好"这会儿是哪一档"，装配层拿到的就是已经更新的 `scene_state.state`。
        self.scene_state.changed.connect(self._on_scene_state_changed)
        self.scene_state.declined.connect(self._on_scene_declined)

        # 社交帮忙回复（v2-social-reply）：在社交软件里不知道怎么接话时，宠物冒一句
        # "要我帮你措辞吗"，用户把处境讲给它，它拟一段草稿。**草稿只落只读框给人复制，
        # 这条线上没有发送口子。**
        # 复用同一个 screen_monitor——不新开采样循环。
        #
        # v2-social-fewshot 给它接了两只手，**实现留在这儿、不搬进服务里**：
        #   grab_chat：社交窗在前台那一拍截一次屏（界面线程，只拷内存）；
        #   read_chat：把那帧丢后台 OCR 成字（借 monitor 的 RapidOCR，不另起引擎）；
        #   examples_provider：全局 + 对象攻略卡两路风格范例（只读盘）。
        # 认出来的字只活在服务的内存里，起草完即弃——不落盘、不进任何库。
        self.social_examples = SocialExamples(
            config, cards_provider=self._guide_cards
        )
        # 这一场社交窗对着的是谁（v2-social-session-reset）：由 `_grab_social_chat`
        # 抓屏那一拍顺手记下（就是那次"认前台是不是宠物自己"读到的标题），服务拿它比
        # "换没换会话"。**不额外读窗口标题**，所以 `foreground_title` 那只手的口径没动；
        # 这一格只活在内存里，不落盘、不进日志（见 `_social_session_id`）。
        self._social_session_hint = ""
        self.social_reply = SocialReplyService(
            config=config,
            bus=self.bus,
            # 气泡上只有一排按钮（v2-social-flow）：笔记正等答复、或游戏攻略正问着，
            # 社交这一句就先不冒，免得两条线互相顶掉对方的按钮。
            can_offer=lambda: not (
                self.screen_notes.awaiting_confirm or self.game_guide.awaiting_confirm
            ),
            grab_chat=self._grab_social_chat,
            read_chat=self._read_social_chat,
            # 「这一场是谁」（v2-social-session-reset）：微信里换会话时前台进程没变，
            # 服务那条"走出社交前台才清场"永远等不到——换成靠"会话标识变了"判。
            session_provider=self._social_session_id,
            examples_provider=self.social_examples.for_query,
            # 「那排 [好呀] [不用了] 还挂在气泡上吗」（v2-social-await-prime-fix 改动一）：
            # 服务要按界面那边的实况决定"这声作废该不该清旗"。只读界面那一格，不改它。
            # 这具 lambda 是**惰性**的（`chat_bubble` 在本行之后才建），调用只发生在
            # 前台切换 / 用户的点击上，那时早就建好了。
            offer_visible=lambda: self.chat_bubble.offer_theme == "social",
        )
        # 断网兜底那句按熟悉度挑（v2-offline-social-stage）：越熟说得越随便。
        # **接在这里而不是上面那个 if 块**——上面接 psych/chatter 时 social_reply 还没建。
        if self.mind_intimacy is not None:
            self.social_reply.set_offline_candidates_provider(
                self.mind_intimacy.offline_social_candidates
            )
        # 「对象攻略卡」建过没有（v2-social-card-dedup）：已经有这张卡了就不再问第二遍、
        # 也不再重复建。判据现问做卡那个模块（每次调用都查一遍库），社交这条线自己不存
        # 这份记忆——用户可能是在卡界面里自己建的那一张，存一份就会跟真值对不上。
        self.social_reply.set_card_exists_provider(self._guide_card_exists)
        self.social_reply.attach(self.screen_monitor)
        # 这里原来立着候选框的单例句柄（v2-social-ocr-isolate ④）。候选框整个删掉了
        # （v2-social-copy-clean），句柄跟着没有存在的理由：现在没有框可开、没有第二个
        # 可摞，候选走非模态气泡，天然是"换内容"而不是"再开一个"。

        # 重情绪关怀（指令 26-A）：连着两次重下来时，借主动搭话那条线问一句、提醒休息
        self.heavy_care_service = HeavyCareService(
            user_id=self.user.id,
            mood_dao=self.mood_event_dao,
            queue_care=self.proactive_service.queue_care,
        )
        self.playmate_service = PlaymateService(
            config=config,
            bus=self.bus,
            chain=PlaymateChain(config),
            app_name=lambda: self.activity_service.current_proc,
            # 宠物隐身时不涂白：隐藏后窗口几何还留着旧值，照着它涂会把屏幕上那块抹成白块
            pet_rect=lambda: self.pet_window.frameGeometry() if self.pet_window.isVisible() else None,
        )
        # 工作模块（指令 16）：流程数据 + 部门卡片 + 两张联网检索卡片 + 员工自建卡（指令 21）
        # 身份取配置里的「身份切换（调试用）」：真实账号体系还没做，管理员 / 员工先靠它切
        self.work_service = WorkService(
            config=config,
            bus=self.bus,
            chain=WorkChain(config),
            role=config.work.role,
            card_dao=self.custom_card_dao,
        )

    def _init_ui(self) -> None:
        config = self.config
        self.skin = PetSkin()
        self.state_machine = PetStateMachine()
        self.behavior_scheduler = BehaviorScheduler(config.pet.persona)
        self.pet_window = PetWindow(
            config,
            self.state_machine,
            self.skin,
            self.behavior_scheduler,
            # 点击要不要搭理（v2-focus-interact ①）：手动番茄钟在专注也算，
            # 不只看自动那套有没有进度。见 PetWindow._focus_silenced。
            focusing=self._is_focusing,
        )
        self.pomodoro_bubble = PomodoroBubble()
        self.chat_bubble = ChatBubble(config.pet.name)
        # 语音输入那几根线（talk_started / heard / hotkey / set_mic_enabled）本轮全摘了，
        # 见上面建引擎那段的长注释——引擎在手上，没接入口。
        self.tray = PetTray(
            self.skin,
            config.pet.name,
        )

        self.life_panel = LifePanel(
            config=config,
            bus=self.bus,
            card_service=self.card_service,
            on_card=self.open_card,
        )
        self.pomodoro_panel = PomodoroPanel(
            config=config,
            bus=self.bus,
            service=self.pomodoro_service,
            dao=self.pomodoro_dao,
        )
        self.playmate_panel = PlaymatePanel(
            config=config,
            bus=self.bus,
            service=self.playmate_service,
            on_start=self._start_playmate,
            on_stop=self._stop_playmate,
        )
        # 卡内小助手（v2-card-tools-A）：生活卡与工作卡底下那个小窗共用一个服务，
        # 会话与记录按**卡号**分开（`life:12` / `work:feature_a`），所以一个服务
        # 伺候两边也不会串。
        #
        # 把"现在这个身份"接进去（v2-card-tools-D）：卡上能长出什么部件要看身份
        # （工作卡上管理员不受限、员工只能摆记事的），而身份是 `WorkService.is_admin()`
        # ——**递的是这个函数本身，不是当时那个值**，所以设置页一切身份，下一句话就按
        # 新身份走（不用重启、也不用在这两处之间同步状态）。
        self.card_chat_service = CardChatService(
            config,
            self.bus,
            is_admin=self.work_service.is_admin,
            kb_provider=self.kb.card_context,
        )
        # 卡面摆成什么样（v2-card-tools-C）：两张卡的卡面共用同一个记录层，按卡号分开存
        self.card_layout_store = CardLayoutStore(config)
        self.card_panel = CardDetailPanel(
            config,
            self.bus,
            self.card_service,
            self.profile_store,
            self.card_chat_service,
            self.card_layout_store,
        )
        self.card_panel.card_chat_requested.connect(self._on_card_chat_send)
        # 工作模块（指令 18）：打开就是卡片轮播，流程图不再呈现
        self.work_panel = WorkPanel(
            service=self.work_service,
            bus=self.bus,
            on_dept=self.open_work_dept,
            on_flow=self.open_work_flow,
        )
        self.work_card_panel = WorkCardPanel(
            self.bus, self.work_service, self.card_chat_service, self.card_layout_store
        )
        self.work_card_panel.card_chat_requested.connect(self._on_card_chat_send)
        # 部门功能卡列表页：点部门卡卡身先进这里，再从列表进具体功能
        self.work_dept_panel = WorkDeptPanel(
            bus=self.bus,
            service=self.work_service,
            on_card=self._open_work_card_in_overlay,
        )
        # 底层连接页（指令 21，仅管理员）：入口是轮播页那个「编辑底层连接」按钮
        self.work_flow_panel = WorkFlowPanel(self.work_service, self.bus)
        self.panel = PanelWindow(
            config.pet.name,
            {
                "life": self.life_panel,
                "pomodoro": self.pomodoro_panel,
                "playmate": self.playmate_panel,
                "work": self.work_panel,
            },
        )
        # "放大定格"层（指令 17）：点卡片时把详情面板从卡片原位放大到铺满窗口，
        # 生活卡与工作卡共用这一层，只是装的详情页不同
        self.overlay = CardOverlay(self.panel)
        self.overlay.add_page("card", self.card_panel)
        self.overlay.add_page("work_card", self.work_card_panel)
        self.overlay.add_page("work_dept", self.work_dept_panel)
        self.overlay.add_page("work_flow", self.work_flow_panel)
        # 聊天框开着的时候浮动气泡一律闭嘴：它和聊天框是同一个位置的两种说法，
        # 两条窗各画一个尖角，并存就是用户说的"两个脚"。对话气泡优先级更高。
        self.pet_window.set_bubble_blocker(lambda: self.chat_bubble.isVisible())
        self.overlay.opened.connect(self._on_overlay_opened)
        self.overlay.closed.connect(self._on_overlay_closed)
        self.panel.set_overlay(self.overlay)
        self._pet_was_visible = True
        # 这一串定格层真的把宠物藏起来了吗（只有藏了，关上才该把它叫回来）
        self._pet_yielded = False
        # 场景态的三个标记在 `_init_services` 里就置好了（见那段注释）——那儿比这里早，
        # 因为场景状态机一接线仲裁点就可能问一次。
        # 定格层可以在开着的时候被重开（见 _on_overlay_opened），得知道这是不是同一个链上的
        self._overlay_was_open = False
        # 「建完卡自动翻开」这条不让位：置位期间 opened 不赶宠物走
        self._focus_keeps_pet = False
        # 帮聊那一则的 3 分钟清理线（v2-social-card-timeout）：卡片在不在眼前、内容有没
        # 有被复制走、用户这会儿算不算"离开了对话窗口"——三样都是**当下这一则**的状态，
        # 换一则（`_start_card_watch`）整套重置。定时器是单发的：只在"用户离开"那一拍起。
        self._card_open = False
        self._card_copied = False
        self._card_away = False
        # 不带 parent：`DesktopPetApp` 不是 QObject（跟 `_archive_timer` 同一写法），
        # 引用挂在本对象上就一直活着。
        self._card_clear_timer = QTimer()
        self._card_clear_timer.setSingleShot(True)
        self._card_clear_timer.timeout.connect(self._on_card_clear_due)

        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(QIcon(self.skin.pixmap(POSE_IDLE)))

    def _wire(self) -> None:
        self.bus.pet_speak.connect(self._on_pet_speak)
        # 宠物自己开口那条线（v2-proactive-voice）：文字照旧，出声先问一句
        self.bus.pet_speak_proactive.connect(self._on_pet_speak_proactive)
        self.bus.activity_report.connect(self._on_activity_report)
        self.bus.user_command.connect(self._on_user_command)
        self.bus.summon_pet.connect(self.pet_window.show_front)
        self.bus.reminder_due.connect(self.care_service.on_reminder_due)
        self.bus.toast.connect(self.panel.show_toast)
        self.bus.toast.connect(self._on_toast)

        # 生活卡牌：聊完一句 → 卡牌服务判品类、攒门槛 → 该邀了就在对话流里轻邀请
        self.bus.utterance_analyzed.connect(self.card_service.observe)
        # 同一条信号再接给重情绪关怀（指令 26-A）：够重的按主题记账，连着两次就排一句关怀
        self.bus.utterance_analyzed.connect(self.heavy_care_service.note)
        # 会话级画像（指令 26-C C1）：每句解析完顺手抽工作/出行画像，只进内存
        self.bus.utterance_analyzed.connect(self._on_utterance_for_profile)
        # 情感记忆（S3）：同一句话也交给提炼器攒着，聊完一轮再统一压进加密库。
        # 接在这条信号上而不是 chatter 里，是因为"用户说了什么"这件事本来就在这儿广播，
        # 提炼器没必要知道会话状态机长什么样。
        if self.mind_distiller is not None:
            self.bus.utterance_analyzed.connect(
                lambda text, *_rest: self.mind_distiller.note_turn(text)
            )
        # 好感度（B 养成）：用户每说一句就记一点经验。接在这儿而不是 chatter 里，
        # 跟提炼器一个道理——"用户说了句话"本来就在这条信号上广播。
        # 经验是纯本地的，不涉及联网，所以主线程直接写没问题
        if self.mind_intimacy is not None:
            self.bus.utterance_analyzed.connect(
                lambda *_args: self._gain_intimacy(EXP_CHAT, "对话")
            )
        # 情感事件（v2-emotion-log）：吐露情感/压力的话，记一条事件（归因 + 分类标签），
        # 落进加密库，不以卡牌出现。跟卡牌服务接在同一条信号上、各干各的。
        if self.mind_store is not None:
            self.bus.utterance_analyzed.connect(self._log_emotion_event)
        self.bus.card_proposed.connect(self._on_card_proposed)
        self.bus.card_offer_declined.connect(self._on_card_offer_declined)
        self.bus.card_changed.connect(self.life_panel.refresh_cards)
        # 主动服务（指令 26-B）：出了提议就露轻按钮、有卡就直接弹、点头后开工作模块
        self.bus.service_offer.connect(self._on_service_offer)
        self.bus.service_show_card.connect(self._on_service_show_card)
        self.bus.service_open_work.connect(self._on_service_open_work)

        self.pet_window.chat_requested.connect(self.open_chat)
        self.pet_window.action_requested.connect(self.handle_action)
        self.pet_window.persona_cycle_requested.connect(self._cycle_persona)
        self.pet_window.touched.connect(self.greeting_service.touch)
        self.pet_window.touched.connect(self.proactive_service.touch)
        self.tray.action_requested.connect(self.handle_action)

        self.pomodoro_service.timer.tick.connect(self._on_pomodoro_tick)
        self.bus.pomodoro_phase_changed.connect(self._on_pomodoro_phase)
        self.pet_window.moved.connect(self._on_pet_moved)

        self.chat_bubble.send_requested.connect(self._on_chat_send)
        # 邀请的轻按钮：点了就当成用户说了那句话，走同一条状态机。
        # 但抓屏笔记那条线也借这排按钮问"要不要发"——先给笔记那条线看一眼，不是它的才丢给聊天
        self.chat_bubble.offer_reply.connect(self._on_offer_reply)
        # 帮聊草稿下面那个「复制回复」按钮（v2-social-chat-ui-copy 批次11 需求6）：
        # **这是写剪贴板的唯一时机**（批次11 起不再在生成那一刻静默写一次），
        # 走的还是装配层那只**只写不读**的手。
        self.chat_bubble.copy_requested.connect(self._on_social_copy)
        # 帮聊那一则的 3 分钟清理线（v2-social-card-timeout）：气泡失焦 / 被收起 = 用户
        # 离开对话窗口，重新被激活 = 回来。两条都只转发事件，界面行为一个字没改。
        self.chat_bubble.user_left.connect(self._on_card_window_left)
        self.chat_bubble.user_back.connect(self._on_card_window_back)
        # 前台换人也是"离开 / 回来"的一条判据：微信还在前台、而且这一屏仍是原来那条
        # 单聊，才算回到对话窗口；换到别的程序 / 别的页都是离开。
        self.screen_monitor.foreground_changed.connect(self._on_foreground_for_card)
        self.bus.notes_offer.connect(self._on_notes_offer)
        self.bus.notes_delivered.connect(self._on_notes_delivered)
        # 游戏攻略也借这排按钮问"要不要攻略"（v2-game-guide），同款先问再给
        self.bus.game_offer.connect(self._on_game_offer)
        self.bus.game_guide.connect(self._on_game_guide)
        # 社交回复候选写好了：弹框列出来给人挑着复制（v2-social-reply-ext）。这条线**不发消息**。
        self.bus.social_candidates.connect(self._on_social_candidates)
        # 候选列完，问一句"要不要建对象攻略卡"，把那排轻按钮露出来（v2-social-reply-ext）
        self.bus.social_card_offer.connect(self._on_social_card_offer)
        # 社交冒话那排轻按钮（v2-social-flow）：光冒话不给按钮，用户没处点，功能是断的
        self.bus.social_offer.connect(self._on_social_offer)
        # 气泡上那排按钮退休了就回喊一声（v2-social-scene-boundary）：被顶掉 / 被收起 /
        # 点过之后都要，社交那条线据此把"等着点按钮"那一档作废，见 `_on_offer_hidden`。
        self.chat_bubble.offer_hidden.connect(self._on_offer_hidden)
        # 反方向那一句（v2-social-await-prime-fix 改动一）：邀请挂过期了，服务回过头来
        # 让界面把那排按钮收掉——**状态与按钮同寿**，不然用户点到的是一个已经不作数的
        # 邀请（点下去落到通用聊天兜底）。见 `SocialReplyService._drop_stale_offer`。
        self.bus.social_offer_expired.connect(self._on_social_offer_expired)
        # 场景态问的那一句（v2-scene-state）：工作态「要不要帮你整理笔记」也是同款轻按钮
        self.bus.scene_offer.connect(self._on_scene_offer)
        # 看片开场陪伴字幕（v2-speech-polish ④）：只显示、不出声。
        self.bus.screen_caption.connect(self._on_screen_caption)

        # 工作页的部门名与管理员标识写在窗口的灰色标题栏上（指令 20 第 4 项）
        self.work_panel.status_note.connect(
            lambda note: self.panel.set_window_note("work", note)
        )
        self.panel.set_window_note("work", self.work_panel.current_note())

    # ---------- 运行 ----------

    def start(self) -> None:
        self.pet_window.show()
        self.behavior_scheduler.start()   # 悠闲姿态调度：先安静一会儿，再自己找事做
        self.tray.show()
        self.scheduler_engine.start()
        self.jobs.sync_on_startup()
        self.pomodoro_service.sync_on_startup()
        self.greeting_service.start()
        self.activity_service.start()
        self.proactive_service.start()
        self.screen_monitor.start()
        # 冷启动把"社交软件在不在前台"先问一次（v2-social-foreground-fix）：那条信号
        # 只在前台**换人**时发，而启动这一刻前台多半就是宠物自己——不问这一句，社交
        # 那条线会一直停在"没问过"上，用户得先切一次窗才认。见 `_prime_social_foreground`。
        self._prime_social_foreground()
        if self.screen_notes is not None:
            self.screen_notes.start()
        if self.mind_distiller is not None:
            self.mind_distiller.start()
        if self.mind_proactive is not None:
            self.mind_proactive.start()
        QTimer.singleShot(FIRST_RUN_HINT_DELAY_MS, self._first_run_hint)
        # 自动归档（指令 25-B 第 4 项）：常驻期间也定时扫，不然跑很久都不会收
        self._archive_timer = QTimer()
        self._archive_timer.setInterval(ARCHIVE_CHECK_MS)
        self._archive_timer.timeout.connect(self.card_service.auto_archive_stale)
        self._archive_timer.start()
        logger.info("桌宠已就位")

    def shutdown(self) -> None:
        # 先立起"正在退出"这块牌子（v2-focus-notes-offer-resume 14-F）：下面
        # `screen_focus_timer.reset()` 会发出专注结束那条沿，而那条沿现在要去补"专注期
        # 压下的那一问"——程序都要关了，不该再冒一句「要不要发你」。牌子立在这儿，
        # 那条沿自然就不补了（`_resume_notes_offer` 第一道闸就是它）。
        self._closing = True
        # 本轮没有任何入口能 begin() 采集，这一行是"万一将来接回来别忘了收尾"的兜底：
        # 关程序时麦克风绝不能还开着，正在收的那一段直接丢掉
        self.voice_listener.cancel()
        self.playmate_service.stop()
        # 情感记忆：停掉定时器 → 抢提炼一次 → 再关库。
        # 顺序不能倒：flush 要往库里写，库得还开着；而关库之前若不抢这一次，
        # 用户"说完最后一句就关窗"的那些话（离空闲判定还差几分钟）就随进程没了。
        # flush 自己带超时（FLUSH_TIMEOUT_S）且吞异常，写不进去也只是少记几句。
        if self.mind_distiller is not None:
            self.mind_distiller.stop()
            self.mind_distiller.flush()
        if self.mind_proactive is not None:
            self.mind_proactive.stop()
        if self.mind_store is not None:
            self.mind_store.close()
        self.screen_notes.stop()
        self.screen_monitor.stop()
        self.screen_focus_timer.reset()
        self.proactive_service.stop()
        self.activity_service.shutdown()
        self.pomodoro_service.shutdown()
        self.scheduler_engine.shutdown()
        self.pet_window.hide_pet()
        self.pomodoro_bubble.hide_bubble()
        self.tray.hide()
        logger.info("=== %s 退出 ===", self.config.app.name)

    # ---------- 番茄钟 ----------

    def _on_pomodoro_tick(self, remaining: int, phase: Phase) -> None:
        self.pomodoro_bubble.apply_state(
            remaining, self.pomodoro_service.timer.planned, phase.value
        )

    def _on_pomodoro_phase(self, phase_key: str) -> None:
        # 专注中让猫咪安静陪着；气泡先跟随宠物定位，再按阶段决定显示或收起
        self.pet_window.set_quiet(phase_key == Phase.FOCUS.value)
        self.pomodoro_bubble.follow(self.pet_window.frameGeometry())
        timer = self.pomodoro_service.timer
        self.pomodoro_bubble.apply_state(timer.remaining, timer.planned, phase_key)
        # 进入专注的瞬间立即抓屏记笔记（v2-focus-notes）：不等 timer 的下一拍。
        # 排到本拍事件循环之后（v2-focus-mode-freeze-fix 14-B）：浮窗的 show() 就在
        # 上面几行，别让抓屏那点活跟"把浮窗画出来"挤在同一拍里。
        if phase_key == Phase.FOCUS.value:
            self._snap_notes_soon()
        # 手动番茄钟起来/结束也收一次看片会话（场景互斥，v2-mood-detect）：
        # `_on_screen_focus_changed` 那是自动那一套的钩子，手动这条走的是阶段信号。
        self._sync_scene_guard()

    def _on_pet_moved(self) -> None:
        pet_rect = self.pet_window.frameGeometry()
        self.pomodoro_bubble.follow(pet_rect)
        self.chat_bubble.follow(pet_rect)

    def _on_foreground_for_notes(self, proc: str) -> None:
        """前台换窗口/应用了：立即抓一拍（v2-notes-cadence 换屏即拍）。

        snap_now 复用 tick 的整套判断（专注态 / 上一次没提炼完），所以不在专注时
        这一下自然不抓，不会有额外开销。
        """
        if self.screen_notes is not None:
            self.screen_notes.snap_now()

    def _snap_notes_soon(self) -> None:
        """下一拍再抓屏记笔记（v2-focus-mode-freeze-fix 14-B）。

        进专注那一拍要干的活是排好的：改安静态 → 浮窗跟随 → `apply_state`（里面
        `show()/raise_()/update()`）→ 抓屏。抓屏本身（取帧 + aHash + 落临时图）虽然是
        毫秒级，但它和"把浮窗画出来"挤在同一个槽里，用户看到的是"框冒出来了，然后卡住"。
        排到事件循环的下一轮：先把浮窗画出来，再让抓屏这条线自己去跑（认字现在也在后台，
        见 notes.py 14-A）。换屏那条事件路（`_on_foreground_for_notes`）不排——那一下
        本来就是用户切窗口带出来的，没有同一拍要画的框。
        """
        notes = self.screen_notes
        if notes is None:
            return
        QTimer.singleShot(0, notes.snap_now)

    def _on_focus_requested(self) -> None:
        """用户在聊天里明说要专心 → 起一轮番茄钟（v2-chat-intent ②）。

        走 `start_manual()`（**只存内存**）：跟用户自己点「开始专注」是同一条路，
        只不过入口从按钮换成了聊天。**刻意不走 `PomodoroTimer.start_focus`**——
        那条会给 pet.db 落一行 `PomodoroSession`，而"聊天不写库"是红线；
        而且手动那套一开就占地儿、静音宠物，不该由一句闲聊误开。

        浮窗、"进入专注的瞬间抓屏记笔记"都由 `enter()` 里那句 `changed.emit()` 带着走
        （它接在 `_on_screen_focus_changed` 上），所以这儿 **不重复触发**——多调一次
        就是笔记抓两拍。已经在跑的那一轮由 `enter()` 自己挡掉（认 `_running`），
        这里不另判一遍。v2-focus-quiet 之后进专注不再冒开场白，只剩浮窗。
        """
        timer = getattr(self, "screen_focus_timer", None)
        if timer is None:
            return
        timer.start_manual()

    def _is_focusing(self) -> bool:
        """这会儿算不算"正在专注"：手动番茄钟在跑，或者自动那套正在陪做正事。

        两套都得算——不然用户在看网课时，主动搭话那条线还以为他闲着，会来插嘴。
        """
        if self.pomodoro_service.timer.is_focusing:
            return True
        timer = getattr(self, "screen_focus_timer", None)
        return bool(timer is not None and timer.running)

    # ---------- 屏幕感知（S2/S3/S4） ----------

    def _on_screen_focus_changed(self) -> None:
        """自动番茄钟的状态变了：把浮窗刷新成它这一轮的样子。

        手动那套番茄钟在跑的时候让位——同一时刻只显示一套（跟聊天框那条优先级一个道理）。
        """
        self._sync_scene_guard()
        timer = self.screen_focus_timer
        if self.pomodoro_service.timer.is_active:
            return
        if not timer.in_progress:
            # 没在跑、也没进度（还没开始 / 已经跑完）：收起浮窗，别显示空倒计时
            self.pomodoro_bubble.hide_bubble()
            return
        self.pomodoro_bubble.follow(self.pet_window.frameGeometry())
        self.pomodoro_bubble.apply_state(timer.remaining, timer.planned, Phase.FOCUS.value)
        # 进入/重回专注的瞬间立即抓屏记笔记（v2-focus-notes）：同样排到本拍之后
        if timer.running:
            self._snap_notes_soon()

    def _sync_scene_guard(self) -> None:
        """专注开合了：收掉看片会话 + 清掉头顶残留 + 喊仲裁点重算（场景互斥）。

        光在 `_begin_watch` 那道闸上加判断只挡得住"专注中**新开**一场"；真机上那个
        场景是**先在看片、随后番茄钟起来**——两件事撞在同一拍（前台既是"全屏"，又被
        判成在看片，同时又被认成专注类软件）。所以进专注这个动作本身也得回头收一次。

        为什么两头都做：只看这边，专注**期间**用户切进播放器仍会冒"看电影啦"；
        只看那边，已经开着的那一场不会自己收。两头各挡一半，合起来才是"同一时刻
        只有一场"。

        v2-scene-state 在这里补了两件事：

        1. **清残留**——刚进专注时把头顶那条状态词**立刻**收掉。原先只做到"不再冒
           新的"（`set_quiet` + 仲裁点那道闸），已经飘在屏幕上的那句要等它自己的计时器
           （状态词 60 秒）自然淡出，用户看到的就是"切回专注了，头上还挂着「我怕怕…」"。
           边沿触发（只在**刚**进专注那一下动手），不是每拍都收——后者会在专注期间把
           所有话都掐掉，那不是"清残留"，是"封口"。
        2. **喊仲裁点**——`is_focusing` 是回调，没有信号源，所以专注这一档只能在专注
           开合的两处显式通知一次（`notify_focus_changed`），让场景状态机跟着切换样子。
        3. **补那一问**（v2-focus-notes-offer-resume 14-F）——专注**结束**那一下，顺手把
           14-C 压下的笔记询问补出来（`_resume_notes_offer`）。手动那套走阶段信号、自动
           那套走 `changed`，两头都会到这儿，所以"由真转假"只有这一个收口。
        """
        focusing = self._is_focusing()
        if focusing and not self._focus_quiet_on:
            self._focus_quiet_on = True
            self.pet_window.hide_speech_bubble()
        elif not focusing:
            if self._focus_quiet_on:
                # 14-F：专注刚结束（这就是"由真转假"那条沿的唯一收口）——把被 14-C
                # 压下的那一问补出来，见 `_resume_notes_offer`。
                self._resume_notes_offer()
            self._focus_quiet_on = False
        if focusing and self.screen_companion is not None:
            self.screen_companion.stop_watch()
        if self.screen_scene_guard is not None:
            self.screen_scene_guard.notify_focus_changed()

    # ---------- 场景态（v2-scene-state） ----------

    def _on_screen_scene_changed(self, scene: str) -> None:
        """情绪场景散场了：把常驻头顶那句情绪词收掉（v2-scene-state-fix ④）。

        情绪词从"60 秒自动淡出"改成"常驻到情绪变/散场"。散场发空串，这时头顶那句不能
        再赖着——这里把它收掉。情绪**变**（horror→angry）不发空串，新词自己盖掉旧词，
        这里不动。别的线（提醒 / 关怀）的浮动气泡寿命照旧，跟这条只认"空串散场"的
        收口不冲突：散场那一刻头顶上该散的本来就是这句情绪词。

        情绪变也顺手重算一次整图换皮（v2-skin-guofeng）：电影态里怒 / 恐 / 笑切来切去，
        宠物那张图要**即时**跟着换。
        """
        if not scene:
            self.pet_window.hide_speech_bubble()
        self._apply_scene_skin()

    def _apply_scene_skin(self) -> None:
        """按当前场景态 + 情绪给宠物整图换皮（v2-skin-guofeng）。

        皮肤由"哪一场"和"什么情绪"**合起来**定，而这两样各走各的信号（场景态走
        `_on_scene_state_changed`、情绪走 `_on_screen_scene_changed`），所以两条路都
        收口到这一个函数再算一次。映射在 `ui/pet_skin.scene_skin`：专注→认真工作，
        电影里按怒/恐/笑换三张，工作/游戏/悠闲/睡觉没给图回 None 保持默认水墨立绘。
        """
        window = getattr(self, "pet_window", None)
        if window is None:
            return
        kind = getattr(getattr(self, "scene_state", None), "state", KIND_IDLE)
        emotion = getattr(self.screen_monitor, "scene", "") or ""
        window.set_scene_skin(scene_skin(kind, emotion))

    def _on_scene_state_changed(self, kind: str) -> None:
        """这会儿换成哪一场了：工作 / 电影 / 游戏 → 坐起来陪着；其余 → 回到悠闲。

        **强制坐起**走调度器的 `force()`（不是直接改立绘）：姿态是"推导"出来的
        （`PetSkin.pose_for`），唯一能把它钉住的入口就是让调度器一直演 `Behavior.SIT`
        ——它映射的正是"坐姿开心"那张图。解除由 `release()` 交回悠闲调度。

        **窗口显隐**：进场景态只保证"不把宠物藏起来"（用户自己隐身过 / 刚点了「不用了」
        / 全屏卡正让它让位，这三种一律不抢回来）；点「不用了」那一下由
        `_on_scene_declined` 收起来，切到别的场次再解除。

        **专注也锁坐姿（v2-pose-sync）。** 原先专注走的是 `release()`（"放回悠闲调度"），
        真机上就成了：番茄钟跑了五分钟，宠物还趴着睡、头上挂着 zzz。根因是 `set_quiet`
        只把可挑的池子缩小到"安静动作"，而**打盹本身就是安静动作**——它照样挑得到、也
        不会因为切进来而被停。现在四档（专注 / 工作 / 电影 / 游戏）一个待遇：先 `force()`
        钉住坐姿（它内部 `_finish_current()` 会停掉正在演的睡觉），再谈别的。
        专注**不抢窗口显隐**——它不额外让宠物露脸，保持原有"安静陪着"的分寸。
        """
        # 上一场问过的按钮**跟着场次作废**：那排按钮是按"这会儿这一场"问出来的，场次一换
        # 它就没了意义——留着的话用户点下去，`_on_offer_reply` 按**新**的等待闸派发，
        # 会落到别的服务头上（乃至落到闲聊）。只在摆着的确实是场景态那一排时才收，
        # 免得顺手把笔记 / 攻略正问着的按钮收掉。
        if getattr(self.chat_bubble, "offer_theme", "").startswith("scene_"):
            self.chat_bubble.hide_offer()
        self._apply_scene_skin()      # 场景态一变，整图皮肤跟着重算（v2-skin-guofeng）
        scheduler = getattr(self, "behavior_scheduler", None)
        if scheduler is None:
            return
        if kind == KIND_IDLE:
            scheduler.release()              # 回悠闲：这才放回自由调度
            return
        if self._muted_kind and kind != self._muted_kind:
            self._muted_kind = ""            # 换了场次：上一场那句「不用了」到此为止
        scheduler.force(Behavior.SIT)        # 强制坐起（坐姿开心）
        if kind in (KIND_FOCUS, KIND_SOCIAL):
            # 专注与社交都只锁坐姿，不抢窗口显隐：专注要的是安静（v2-focus-quiet）；
            # 社交这一档的活是"让位"（v2-social-scene-boundary），话由社交那条线自己
            # 冒——它冒话时本来就会把聊天框打开，这里再抢一次显隐是多余的，还会在
            # "用户自己隐身过 / 刚点了不用了"这些情况下把宠物硬拽出来。
            return
        if kind == self._muted_kind or self._pet_user_hidden or self._pet_yielded:
            return
        if not self.pet_window.isVisible():
            self.pet_window.show_pet()

    def _on_scene_declined(self, kind: str) -> None:
        """场景态里用户点了「不用了」：这一场把宠物收起来（切到别的场次再出来）。

        **不走 `_hide_pet()`**：那个记的是"用户自己把宠物藏了"（托盘点、聊天命令），
        一旦记上就再也不自动出来了；这里只是"这一场先别陪着"，换个场次照旧该出来。
        """
        self._muted_kind = kind
        self.pet_window.hide_pet()
        self.chat_bubble.close_bubble()
        self.tray.set_pet_visible(False)

    def _on_scene_offer(self, kind: str) -> None:
        """场景态问了用户一句：把 [好呀] [不用了] 露出来（台词由状态机自己发）。

        v2-scene-state-fix ③：这些邀请都在用户**没开聊天框**时触发，原来只 `show_offer`
        把按钮排在关着的框里，等于没排。这里打开聊天框，把问句和按钮一起放进去。
        """
        logger.debug("场景态冒话：打开聊天框，露出要/不要两个轻按钮（%s）", kind)
        self._pop_offer(f"scene_{kind}", ASK_NOTES)

    def _pop_offer(self, theme: str, text: str) -> None:
        """屏幕活动触发的邀请：打开聊天气泡，把问句和 [好呀][不用了] 一起摆出来。

        修的是"按钮藏在关着的聊天框里"（v2-scene-state-fix ③）：笔记 / 攻略 / 社交 /
        场景这四条线都在用户没开聊天框时触发，`show_offer` 只把按钮排进隐藏的框。这里
        先把框打开、把问句放进对话流，按钮跟着排出来。头顶那句问句收掉，免得和聊天框
        各带一个尖角（见 `open_chat` 的同款做法）。
        """
        pet_rect = self.pet_window.frameGeometry()
        self.pet_window.hide_speech_bubble()
        self.chat_bubble.follow(pet_rect)
        self.chat_bubble.append_pet(text)
        self.chat_bubble.show_offer(theme)
        self.chat_bubble.open_at(pet_rect)

    # ---------- 聊天（指令 7 修正一：聊天走宠物旁的气泡，不弹独立窗口） ----------

    def open_chat(self) -> None:
        self.greeting_service.touch()
        self.proactive_service.touch()
        pet_rect = self.pet_window.frameGeometry()
        # 开框先收掉头顶那条：不然它会和聊天框各带一个尖角，看着像两个脚
        self.pet_window.hide_speech_bubble()
        self.chat_bubble.follow(pet_rect)
        self.chat_bubble.open_at(pet_rect)

    def _on_chat_send(self, text: str) -> None:
        # v2-multimodal 期间在这里试过一道"同文短窗去重"，用来挡语音/打字两条腿重复
        # 投递。撤掉了：收口点上分不出"同一条输入被投递两遍"和"用户真的要再说一遍"，
        # 因为它俩本来就是同一个信号（详见 tests/test_multimodal.py 开头记的那笔）。
        self.greeting_service.touch()
        self.proactive_service.touch()
        # 用户一开口就把 zzz 叫醒（指令：任意发言立即切活跃态），别等回复才抬头
        self.pet_window.wake()
        # 用户自己打字了：挂着的邀请按钮就先收起来（状态机会把这句话该归档的归档）
        self.chat_bubble.hide_offer()
        # 社交那条线跟着作废"等着点按钮"这一档（v2-social-flow）：按钮都收了，
        # 服务那边还端着等答复的话，下一个社交软件切过来就永远冒不出话了。
        # **不动**"正等他说处境"那一档——他点了[好呀]之后紧接着打的就是处境。
        if self.social_reply is not None:
            self.social_reply.dismiss_offer()
        # 社交帮忙回复（v2-social-reply）：这句话是不是在跟我要一段回话的草稿。
        # 是就由它接走（草稿会落进只读框），不是才走普通回话——一个字都不变。
        # **危机词在 is_request 里就被挡回去了**，所以"帮我回…可我真不想活了"照样
        # 落进下面这条链，危机话术不会被起草稿的活儿盖掉（红线：危机词最高优先）。
        if self.social_reply is not None and self.social_reply.handle_request(text):
            return
        self.chatter_service.handle(text)

    # ---------- 卡内小助手（v2-card-tools-A） ----------

    def _on_card_chat_send(self, namespace: str, text: str) -> None:
        """某一卡的小窗里说了一句。**这条路跟上面那条全局聊天没有任何交叉。**

        全局那条会经过 `chatter_service`（写 pet.db、路由、解析、攒片段、喂 mind.db）；
        这条只把卡上现有的样子取好递进去，回话由卡窗服务自己走一条裸的模型通道。
        两边连信号都不是一对：这边回话走 `card_chat_reply`，只到得了卡号对得上的那个窗。
        """
        context = self._card_chat_context(namespace)
        if self.card_chat_service.ask(namespace, context, text):
            return
        # 没接住（空话 / 这张卡上一句还没回来）：把窗从"在想…"里放出来，
        # 否则输入框就永远锁着了（危机那条路已在服务里收下，不会走到这儿）。
        panel = self._card_chat_panel(namespace)
        if panel is not None:
            panel.set_busy(False)

    def _card_chat_context(self, namespace: str) -> dict:
        """这个卡号对应的是张什么卡、卡上现在写着什么。

        上下文一律从**服务层**取（不是从界面控件上读），卡窗提示词里那几行才算数。
        取不到（卡被删了、key 不认识）就给空 dict——服务那边会退成"这张卡还没有填下
        什么内容"，**不编**。
        """
        kind, _, ident = namespace.partition(":")
        if kind == LIFE_KIND:
            card = self.card_service.card(int(ident)) if ident.isdigit() else None
            if card is None:
                return {}
            count = self.card_service.checkin_count(card.id)
            last = self.card_service.last_checkin_at(card.id)
            rows = [
                f"目标：{card.goal}" if card.goal else "",
                f"背景：{card.summary}" if card.summary else "",
                f"状态：{card.status}（active 进行中 / done 已完成 / paused 暂停）",
                f"已打卡 {count} 次" + (f"，最近一次 {last.strftime('%m-%d %H:%M')}" if last else ""),
            ]
            if card.trip_destination or card.trip_date:
                when = card.trip_date.strftime("%Y-%m-%d") if card.trip_date else "还没定"
                rows.append(f"出行：{card.trip_destination or '还没定目的地'} · {when}")
            return {
                "type": f"{card.theme}卡",
                "title": card.title,
                "brief": "用户自己认领的一张生活卡",
                "data": "\n".join(r for r in rows if r),
                # 这张卡的专属知识库落点（v2-card-kb）：建卡时已填，老卡没有就现算并补建目录
                "kb": card.kb_path or str(ensure_card_kb_root(self.config, card.id)),
            }
        if kind == WORK_KIND:
            card = self.work_service.workflow.card(ident)
            if card is None:
                return {}
            rows = [
                f"在流程里的位置：{self.work_service.card_place(card)}",
                f"卡面短说明：{self.work_service.card_brief(ident)}",
            ]
            return {
                "type": "工作卡",
                "title": card.title,
                "brief": self.work_service.card_blurb(ident),
                "data": "\n".join(r for r in rows if r),
                # 工作卡在 JSON 里不在 DB，落点按 key 现算（v2-card-kb）
                "kb": str(ensure_card_kb_root(self.config, ident)),
            }
        return {}

    def _card_chat_panel(self, namespace: str):
        """卡号对得上的那个小窗（两个面板各一个，一次只有一个是这张卡）。"""
        for panel in (self.card_panel.card_chat, self.work_card_panel.card_chat):
            if panel.namespace == namespace:
                return panel
        return None

    def _on_utterance_for_profile(self, text, module, _topic, _emotion, _persona, _may_invite) -> None:
        """C1 S2：从解析完的话里抽工作 / 出行 / 饮食画像，存进会话内存（不落库）。

        只认「工作」「出行」「餐饮」三类；别的品类没有画像诉求，跳过。
        """
        if module == "工作":
            self.profile_store.update(WORK, extract_work_profile(text))
        elif module == "出行":
            self.profile_store.update(TRAVEL, extract_travel_profile(text))
        elif module == "餐饮":
            self.profile_store.update(DIET, extract_diet_profile(text))

    def _log_emotion_event(self, text, _module, _topic, _emotion, _persona, _may_invite) -> None:
        """v2-emotion-log：吐露情感/压力的话，记一条情感事件进加密库。

        归因与分类是纯本地词表（life/mind/emotion_log），情绪复用规则路由的即时值。
        整段不抛：这只是背景记录，出岔子顶多少记一条，不该拖垮回话或信号链。
        """
        hit = emotion_log.classify(text)
        if hit is None:
            return
        emotion = router.classify(text).emotion
        try:
            self.mind_store.add_emotion_event(text, emotion, hit.attribution, hit.category)
        except Exception as exc:  # noqa: BLE001
            logger.error("情感事件记录失败（%s）", exc)

    def _on_mind_distilled(self, result) -> None:
        """一轮对话提炼完（B 养成）：写今天的记忆簿、结一次强事件经验。

        两件事都接在这一个回调上，是因为它们的原料是同一份——`result` 里那批
        事件与碎片。分开各挂一次只会把同一批数据解两遍。

        星念这条线在 v2-life-card-star 砍掉了：这里不再 observe 立卡，
        只有"这轮有没有事件"这一档还照旧结经验。

        整段不抛：这是在提炼线程里跑的（退出抢提炼那条路也走它），
        出岔子顶多少记一页日记，不该把已经入库的记忆一起赔进去。
        """
        if self.mind_intimacy is None:
            return
        if self.mind_memory_book is not None:
            self.mind_memory_book.record()
        # 有事件 = 这轮聊到了件大事。普通寒暄不给这一档，不然"强事件"就跟聊天一样不值钱了
        if bool(result.get("events")):
            self._gain_intimacy(EXP_STRONG_EVENT, "提炼出值得记的事")

    def _gain_intimacy(self, exp: int, reason: str) -> dict:
        """加经验，升级就把新福利落下去（B 养成）。

        加经验的入口有三个（对话 / 被回应 / 强事件），都得走这儿——福利一共两处落地：
        语气是现取的（见 set_tone_provider），不用管；节流是常驻状态，必须显式同步。
        谁绕过这个函数直接调 add，就会出现"等级到了但频率没变"的半生效。
        """
        if self.mind_intimacy is None:
            return {}
        result = self.mind_intimacy.add(exp, reason)
        if result.get("level_up"):
            self._apply_intimacy_pace()
        return result

    def _apply_intimacy_pace(self) -> None:
        """把好感度给的福利落到主动搭话的节流上。没解开 frequent 就恢复默认值——
        这里必须是"设成什么"而不是"调快一点"，否则反复调用会越调越松。"""
        if self.mind_intimacy is None or self.proactive_service is None:
            return
        pace = self.mind_intimacy.pace()
        if pace is None:
            self.proactive_service.set_pace(MIN_GAP_SEC, MAX_PER_DAY)
        else:
            self.proactive_service.set_pace(*pace)
        # 语气那段不用推：psych 每次回话都现调 tone_suffix（见 set_tone_provider），
        # 升级之后下一句话就是新的。只有节流这一项是常驻状态，得在这儿同步。

    def _on_pet_speak(self, text: str) -> None:
        """回用户的话（v2-chat-core #2）：**只显示，一个字都不念**。

        原先这条线上无条件 `voice.say(...)`，只有危机定稿话术与看电影两道闸静音——
        用户真机反馈「回复老是被念出来」。这一轮把出声这件事整个摘了：没有自动念，
        也没有「要我念出来吗」的询问按钮，宠物说的话一律只出文字。

        `strip_action_marks` 照旧：动作描写是给表情看的，不该进正文。
        （v2-multimodal 期间在这里试过按文本去重，见 tests/test_multimodal.py 开头
        记的那笔：收口点上分不出"撞车"和"真的要再说一遍"，已经撤掉了。）
        """
        self._display_speech(strip_action_marks(text))

    def _display_speech(self, text: str, show_ms: int | None = None) -> None:
        """把一句话显出来（文字 + 表情），出声与否不归这儿管。

        聊天框开着就说进框里，不再另弹浮动气泡（免得同一句话看两遍）。
        `show_ms` 非空时给浮动气泡指定停留时长——观影愤怒场景那句要多停一会儿
        （见 `_on_pet_speak_proactive`）。
        """
        if self.chat_bubble.isVisible():
            self.chat_bubble.append_pet(text)
            self.pet_window.speak(text, bubble=False)
        elif show_ms is not None:
            self.pet_window.speak(text, show_ms=show_ms)
        else:
            self.pet_window.speak(text)

    def _on_pet_speak_proactive(self, text: str) -> None:
        """宠物自己开的口：**跟回话那条线一样，只显示不出声**（v2-chat-core #2）。

        这条线原先比回话那条还多一道手续——先冒话，再挂一排「念出来 / 不用」等用户点
        （v2-proactive-voice）。用户这一轮要的是「彻底不出声」，那排按钮跟着一起摘了，
        于是两条说话线在这一轮合并成同一件事：把话说出来，给人看。

        观影状态词（v2-scene-state-fix ④）：当前场景是三态里任一态时，这句**常驻头顶**
        不自动淡出——情绪变（v2-mood-window 去抖后换词）才换，散场才收。别的线
        （提醒 / 关怀 / 报告）照旧默认时长。
        """
        text = strip_action_marks(text)
        show_ms: int | None = None
        monitor = getattr(self, "screen_monitor", None)
        if monitor is not None and monitor.scene in _SCENE_STATUS:
            # 情绪词常驻（v2-scene-state-fix ④）：不秒退、不自动淡出，情绪变才换词。
            show_ms = SCENE_STATUS_PERSIST_MS
        self._display_speech(text, show_ms=show_ms)

    def _on_activity_report(self, text: str) -> None:
        """今日活动报告：内容多行，气泡要多停一会儿才看得完。"""
        text = strip_action_marks(text)
        if self.chat_bubble.isVisible():
            self.chat_bubble.append_pet(text)
            self.pet_window.speak(text, bubble=False)
        else:
            self.pet_window.speak(text, show_ms=ACTIVITY_REPORT_SHOW_MS)

    def _on_screen_caption(self, text: str) -> None:
        """看片开场陪伴字幕（v2-speech-polish ④）：**只显示、绝不出声**，停 6 秒。

        这条通道上**没有 voice.say**——不是"念的时候判了一下该不该念"，是结构上
        就没有念这一步。v2-chat-core 之后这条差别更不必说了：现在**任何**一条说话线
        都不出声，这条通道跟别的通道在"不出声"上已经一致，它只是还需要"停 6 秒"。
        """
        text = strip_action_marks(text)
        if not text:
            return
        if self.chat_bubble.isVisible():
            # 聊天框开着：字幕进对话流（还是不出声，append_note 那条路本来就不念）
            self.chat_bubble.append_note(text)
            return
        self.pet_window.speak(text, show_ms=SCREEN_CAPTION_SHOW_MS)

    def _on_toast(self, text: str) -> None:
        # 等回复的时候出错，就在聊天框里说清楚，别让人干等
        if self.chat_bubble.isVisible() and self.chat_bubble.busy:
            self.chat_bubble.append_note(text)

    # ---------- 交互处理 ----------

    def open_panel(self, page: str = "life") -> None:
        # 上一次放大定格的卡还开着就先收起来：不然新页面开在它后面，等于没开——
        # 在聊天里点了工作模块的 [好呀]，眼前却还停在刚才那张卡上，就是这么来的
        if self.overlay.is_open():
            self.overlay.close_overlay()
        if page == "work":
            # 身份切换（调试用）改过之后，重新打开工作模块才按新身份摆按钮与标题栏
            self.work_panel.apply_role()
        # 每次从菜单/托盘打开面板，都算"重新进来一次"：把"用户手动拖过尺寸"这件事清零，
        # 按目标页重新给高度。否则上一次停留里拖过/最大化过窗口之后，
        # 之后就再也不按页变高度了（打开工作页还是生活页那个高度）。
        self.panel.forget_manual_size()
        self.panel.show_page(page)
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def open_card(self, card_id: int, rect: QRect) -> None:
        """从生活广场点一张卡：从它原位放大定格到详情页（指令 17）。"""
        self.card_panel.show_card(card_id)
        self.overlay.show_page("card", rect)

    def open_work_card(self, card_key: str, rect: QRect) -> None:
        """点卡片墙上的卡片：从它原位放大定格到卡片详情（指令 17）。"""
        self.work_card_panel.show_card(card_key)
        self.overlay.show_page("work_card", rect)

    def open_work_dept(self, dept_id: str, rect: QRect) -> None:
        """点部门卡卡身：从它原位放大定格到这个部门的功能卡列表。"""
        self.work_dept_panel.show_dept(dept_id)
        self.overlay.show_page("work_dept", rect)

    def _open_work_card_in_overlay(self, card_key: str) -> None:
        """从部门功能卡列表点一张卡：定格层已经铺满，直接换页，不重播放大动画。"""
        self.work_card_panel.show_card(card_key)
        self.overlay.swap_page("work_card")

    def open_work_flow(self, rect: QRect) -> None:
        """管理员点「编辑底层连接」（指令 21）：从按钮原位放大到底层连接页。"""
        self.work_flow_panel.refresh()
        self.overlay.show_page("work_flow", rect)

    def _on_overlay_opened(self) -> None:
        """全屏卡期间宠物本体让位（指令 17）：记下它原来在不在，关闭后照原样恢复。

        只在「第一下」记：定格层开着的时候还会被重开（open_panel 先收起旧的、open_card
        再开新的；连着点两张卡也是），那几下宠物早让位了，再记一遍就把 True 盖成 False，
        关掉之后它再也回不来。
        """
        if self._overlay_was_open:
            return
        self._overlay_was_open = True
        if self._focus_keeps_pet:
            # 建完卡自动翻开这条不让位：卡是从聊天框里长出来的，宠物就在旁边看着，
            # 把它藏了用户先看见的是宠物"啪"地消失，比卡片本身还抢眼
            return
        self._pet_was_visible = self.pet_window.isVisible()
        self._pet_yielded = True
        self.pet_window.hide_pet()

    def _on_overlay_closed(self) -> None:
        self._overlay_was_open = False
        if self._pet_yielded:
            self._pet_yielded = False
            if self._pet_was_visible:
                self.pet_window.show_pet()

    def toggle_focus(self) -> None:
        """「开始专注 / 结束专注」的**程序化**入口（v2-focus-web ① ② 留下的）。

        v2-focus-notes2 ③ 把右键菜单里那两条摘掉之后，界面上已经没有按钮再发
        `action == "focus"` 了——用户手上的出口改在番茄钟面板（主按钮在跑的时候
        就是「结束专注」）。这里留着是给测试与将来的快捷键留一条路：行为没变。

        没在进行 → 手动起一轮（只存内存，不写 pet.db，跟前台判定脱钩）；
        在进行中 → 结束这一轮、回空闲、浮窗收掉。
        """
        timer = getattr(self, "screen_focus_timer", None)
        if timer is None:
            return
        if timer.in_progress:
            timer.stop()
        else:
            timer.start_manual()
        self._on_screen_focus_changed()

    def handle_action(self, action: str) -> None:
        if action in ("life", "work"):
            self.open_panel(action)
        elif action == "settings":
            self.open_settings()
        elif action == "focus":
            self.toggle_focus()
        elif action == "pomodoro":
            # 手动番茄钟面板从菜单点得进来（v2-focus-web ②）：专注 / 短休 / 长休
            # 三个阶段和「重置」（=退出专注）都在那一页上，原先入口卡被摘掉之后
            # 就没有任何用户可达的路了——"休息没法测"就是这么来的。
            self.open_panel("pomodoro")
        elif action == "toggle":
            self._toggle_pet_visible()
        elif action == "quit":
            self._quit_now()

    def _quit_now(self) -> None:
        self.shutdown()
        QApplication.quit()

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            config=self.config,
            bus=self.bus,
            pet_profile_dao=self.pet_profile_dao,
            activity_dao=self.activity_dao,
            category_dao=self.app_category_dao,
            memory_store=self.memory_store,
            reminder_dao=self.reminder_dao,
            jobs=self.jobs,
            user_id=self.user.id,
            intimacy=self.mind_intimacy,
            memory_book=self.mind_memory_book,
            reset=self.memory_reset,
            parent=self.panel,
        )
        dialog.saved.connect(self._on_settings_saved)
        dialog.exec()
        # 对话框以面板为父，父在了它就不会自动销毁；用完手动回收，免得每开一次设置留一个
        dialog.deleteLater()

    # ---------- 聊天里让它做的事（聊天是主入口） ----------

    def _on_user_command(self, command: str) -> None:
        """按用户明确说出的要求做事；只是聊到生活/工作的话题不在这里动界面。"""
        if command == COMMAND_ACTIVITY_REPORT:
            self.activity_service.announce_today()
        elif command == COMMAND_HIDE_PET:
            self._hide_pet()
        elif command == COMMAND_OPEN_LIFE:
            self.open_panel("life")
        elif command == COMMAND_OPEN_WORK:
            self.open_panel("work")
        elif command == COMMAND_OPEN_SETTINGS:
            self.open_settings()
        elif command == COMMAND_QUIT:
            # 先把它的回话留在眼前一下再退出，否则像是崩了
            QTimer.singleShot(QUIT_AFTER_REPLY_MS, self._quit_now)
        elif command == COMMAND_START_PLAYMATE:
            self._start_playmate()
        elif command == COMMAND_STOP_PLAYMATE:
            self._stop_playmate()

    # ---------- 抓屏笔记（C 多模态 · 笔记先行版） ----------

    def _social_offer_pending(self) -> bool:
        """社交那条线正端着气泡上一排按钮等人点吗（v2-social-reply-fix）。

        候选列完之后那一问「要不要建对象攻略卡」是**最高优先级**：它比工作 / 电影 /
        游戏态更靠前，别的线（游戏攻略 / 工作态整理笔记）问按钮前都得先过这一道，
        免得把「建卡询问」顶掉——真机就是 pythonw 被误判成游戏、游戏态抢了前台，
        把候选之后的建卡询问盖没了。
        """
        sr = getattr(self, "social_reply", None)
        if sr is None:
            return False
        return sr.awaiting_card or sr.awaiting_confirm

    def _on_offer_reply(self, answer: str) -> None:
        """轻按钮的答复：先给抓屏笔记那条线看一眼（它正等着就说给它），不是它的再走聊天。"""
        if self.screen_notes is not None and self.screen_notes.awaiting_confirm:
            if not self.screen_notes.confirm(answer):
                self.chat_bubble.append_pet("好，那先不发。")
            return
        # 游戏攻略（v2-game-guide）：也是借这排按钮问的，同样先问再给
        if self.game_guide is not None and self.game_guide.awaiting_confirm:
            if not self.game_guide.confirm(answer):
                self.chat_bubble.append_pet("好，那先不找了。")
                if self.scene_state is not None and self.scene_state.state == KIND_GAME:
                    # 游戏态里说了「不用了」→ 这一场把宠物收起来（v2-scene-state）
                    self._on_scene_declined(KIND_GAME)
            return
        # 场景态（v2-scene-state）：工作态那句「我可以帮你整理笔记，需要吗？」也是借
        # 这排按钮问的。点头 → 这一场随时整理（接着立刻记一拍）；摇头 → 收起来。
        if self.scene_state is not None and self.scene_state.awaiting:
            accepted = self.scene_state.answer(answer)
            self.chat_bubble.set_busy(False)
            if accepted and self.scene_state.accepted and self.screen_notes is not None:
                self.screen_notes.snap_now()      # 刚点头，当场记一拍
            return
        # 社交帮忙回复（v2-social-reply-ext）：候选列完之后的"要不要建对象攻略卡"。
        # [好呀] → 抽对方信息 → 弹可改框 → 建卡并写专属库；[不用了] → 只回候选，不多说。
        if self.social_reply is not None and self.social_reply.awaiting_card:
            if self.social_reply.confirm_card(answer):
                self._build_guide_card()
            else:
                self.chat_bubble.append_pet("好，那这几段你先挑着用。")
            self.chat_bubble.set_busy(False)
            return
        # 社交帮忙回复（v2-social-flow）：[好呀] 之后先问一句"是什么情况"，
        # [不用了] 就静默陪着——**一句都不说**（不劝、不追问、不换个建议）。
        if self.social_reply is not None and self.social_reply.awaiting_confirm:
            self.social_reply.confirm(answer)
            # 点按钮那一下气泡已经进了"在想…"：拒绝那条不出声，没别人替它收忙态；
            # 答应那条也正好——接下来就该他打字把处境讲出来了。
            self.chat_bubble.set_busy(False)
            return
        self.chatter_service.handle(answer)

    def _on_notes_offer(self, count: int) -> None:
        """抓屏笔记攒够了：气泡里问一句，露出 [好呀] [不用了]，等用户点头才发（不自动发）。

        专注期不弹（v2-focus-mode-freeze-fix 14-C）：笔记本来就是**专注里**攒出来的，
        这条询问也就必然在专注里到点。原来它不看专注态，框照开、按钮照排——用户正在
        看网课，眼前突然多一个要点的框，既打扰又容易点不着（v2-focus-quiet 那条纪律）。

        压下的那一问**不在专注里作废**（v2-focus-notes-offer-resume 14-F）：服务侧是在
        emit **之前**就落 `_awaiting = True` + `_remember_ask()` 的（`notes.py:447-449`，
        还带一个 7 天戳），所以被 14-C 挡下的这一问是**丢掉**、不是"留到以后"——笔记
        一直挂着待发，7 天内不会再问。这一档在这儿记一笔，等这一轮专注结束再补出来
        （见 `_resume_notes_offer`）。
        `_notes_offer_resumed` 就是这一笔，两头各置一次：专注里压下的那一支置 False
        （还欠着，等那条沿），非专注那一支置 True（这一问当场就出去了，不用补）。
        """
        if self._is_focusing():
            self._notes_offer_resumed = False
            return
        self._notes_offer_resumed = True        # 这一问当场就露面了，不用补
        self._show_notes_offer(count)

    def _show_notes_offer(self, count: int) -> None:
        """把"要不要发"这一问真问出去（专注结束补的那一次也走这儿，两处一个口径）。"""
        self.bus.pet_speak.emit(f"我攒了 {count} 条笔记，要不要发你？")
        self.chat_bubble.show_offer("notes")

    @property
    def _awaiting_offer_kind(self) -> str:
        """眼下正等着用户答复的那一问是谁（`""` = 没有）。

        14-F 补发那条沿看的就是它：笔记服务在 emit **之前**就把 `_awaiting` 落了
        （`life/screen/notes.py:442-449`），所以「服务侧还等着」就是「那一问还欠着」。
        看服务侧、不在装配层另记一本账——两边各记一份，迟早对不上。
        """
        notes = self.screen_notes
        if notes is not None and notes.awaiting_confirm:
            return "notes_offer"
        return ""

    def _resume_notes_offer(self) -> None:
        """专注刚结束（`_is_focusing` 由真转假的那条沿）：把压下的那一问补出来（14-F）。

        补之前三道闸：那一问到现在**还等着答复**（`_awaiting_offer_kind`——用户没从别处
        把它了了）、这一问在本次还没补过（`_notes_offer_resumed`）、程序没在退出
        （`_closing`）。第一道看的是**服务侧**的 `awaiting_confirm`。

        补完把 `_notes_offer_resumed` 置上：同一段专注里服务侧那道 `_awaiting` 闸本来
        也只让问一次（`notes.py:442`），置上是防 `_sync_scene_guard` 被反复喊（设置改动、
        暂停继续都会走到这儿）时一补再补；**条数现取** `pending`，专注里后来又攒下的也
        算进去。补的这一下走 `_show_notes_offer`（现成那条 show_offer 路径），**不再
        `bus.notes_offer.emit()`、也不再 `_remember_ask()`**：服务侧那个 7 天戳原样不动，
        补的确实是**同一问**。
        """
        if self._closing:
            return
        if self._awaiting_offer_kind != "notes_offer":
            return                  # 那一问已经不等着了（答过 / 撤了），不补
        if self._notes_offer_resumed:
            return                  # 已在本次补过，避免抖动重复补
        self._notes_offer_resumed = True
        notes = self.screen_notes
        logger.info("专注结束：把专注期压下的那一问补出来（攒着 %d 条）", notes.pending)
        self._show_notes_offer(notes.pending)

    def _on_notes_delivered(self, text: str) -> None:
        """用户点头了：把笔记正文念/显示在气泡里（文件里本来就有一份，这里只是"发你"）。"""
        self.chat_bubble.append_note(text)

    # ---------- 游戏攻略（v2-game-guide） ----------

    def _on_game_offer(self, game: str) -> None:
        """前台认出游戏了：气泡里问一句，露出 [好呀] [不用了]，点头才去找（不自动找）。"""
        text = ASK_LINE.format(game=game)
        self.bus.pet_speak.emit(text)      # 问句信号照发（别的线 / 测试听着）
        self._pop_offer("game", text)      # 打开聊天框，把问句 + 按钮摆出来（v2-scene-state-fix ③）

    def _on_game_guide(self, text: str) -> None:
        """攻略找好了：显示在气泡里（找不到时这里就是"没找着"的交代，不是空串）。"""
        self.chat_bubble.append_note(text)

    # ---------- 社交帮忙回复（v2-social-reply） ----------

    def _on_social_offer(self, proc: str) -> None:
        """社交软件在前台、宠物冒了那句邀请：把问句与 [好呀] [不用了] 一起摆出来。

        修前只 `show_offer`，按钮排在关着的框里等于没排（跟 v2-scene-state-fix ③ 同一个
        坑）。走 `_pop_offer`，跟笔记/攻略/场景四条线一个写法。

        **这里就是"冒话"这一拍的全部**（v2-social-chat-ui-fix2 提交D）：老口径服务那条线
        先 `pet_speak_proactive.emit(ASK_LINE)` 让头顶浮动气泡冒一遍，这里再走 `_pop_offer`
        把同一句摆进对话框——真机上同一问句冒两遍。现在服务只发 `social_offer` 这一条，
        问句、按钮、打开框全在这一处完成；`_pop_offer` 里那句 `hide_speech_bubble()`
        照旧先收头顶（防别的线留着）。
        """
        logger.debug("社交冒话：打开聊天框，露出要/不要两个轻按钮（%s）", proc)
        self._pop_offer("social", SOCIAL_ASK_LINE)

    def _on_social_candidates(self, candidates: list) -> None:
        """候选回复写好了：**用宠物自己的非模态气泡亮出来**。

        v2-social-copy-clean 把老口径整个换掉了。老口径是弹一个模态对话框
        「这样回怎么样（回这条）」，里面列正文与解析、给一个"复制第 N 条"按钮。它有两个
        毛病：**①它是模态的**——`exec()` 跑的是嵌套事件循环，1.5 秒那个轮询在里头照转，
        真机上就是"复制完卡死"，还摞过第二个框；**②那两件事本来都不需要模态**——把候选
        亮给人看、把候选放进剪贴板，用宠物自己的气泡加一个按钮就够了。

        现在这条路两步，没有一处会阻塞：

        1. **非模态气泡亮出「正文 + 解析」**：走 `_show_social_draft`，摆进宠物自己的
           聊天气泡里，不阻塞、不抢焦点；正文下面挂一行「复制回复」，要不要抄、什么时候
           抄由用户按下那一刻定。**记录区里只留最新一则**（需求5）——亮新草稿时先把上一
           轮那一则摘掉，普通的聊天记录一条不动。
        2. **把这一段的完成态锁上**（`mark_done`，服务那头在起草开工时就立好了），
           再把"要不要建对象攻略卡"那一问排出来——候选是候选，建卡是另一件事。

        **批次11 起这里不再自己写剪贴板了**（老口径是"生成那一刻静默抄一次"）：写剪贴板
        这件事改由用户按「复制回复」触发（`_on_social_copy` → `_copy_social_draft`）。
        碰剪贴板的手仍然只有一只，仍然只写不读、不监听、不按键、不发消息——只是**什么时候写**
        从"拟好的那一拍"挪到了"用户按下按钮的那一拍"。随之删掉的是气泡里那两句打包票的
        收尾（"已经放进你剪贴板了" / "没能放进剪贴板"）：它们替剪贴板背书，而现在这一拍
        压根没发生写这件事。
        """
        items = [c for c in (candidates or []) if str((c or {}).get("draft") or "").strip()]
        if not items:
            return
        first = items[0]
        draft = str(first.get("draft") or "").strip()
        why = str(first.get("why") or "").strip()

        self._show_social_draft(draft, why)

        if self.social_reply is not None:
            # 这一问到此为止（v2-social-scene-boundary）：不清 `_awaiting` 的话
            # `_poll_listen` 第一句就 early-return，自动监听会静默停摆。
            self.social_reply.dismiss_offer()
            self.social_reply.offer_card()

    def _copy_social_draft(self, draft: str) -> bool:
        """把候选正文放进剪贴板。**只写不读**，回 True = 写成功。

        走 Qt 自己那套剪贴板接口，不碰 Windows 原生剪贴板 API、不挂监听——
        那次"复制完卡死"里剪贴板那条腿本来就是无辜的（真凶是模态 `exec()` 的嵌套事件
        循环），所以这里也不替它加任何监听/防抖，写入就完事。

        **v2-social-chat-ui-copy 批次11 起唯一调用方是「复制回复」按钮**
        （`_on_social_copy`）：不再有"生成那一刻静默写一次"那条腿。返回值仍然保留——
        `setText` 不返回值、失败也不一定抛异常，所以"成功"的判据是"正文非空 + 没抛
        异常"；真抛了异常就记一条 error 兜底、回 False，**不静默吞掉**。粘不粘、粘到
        哪儿、发不发，仍旧全在用户手上：这只手放进的是剪贴板，不是聊天框。
        """
        if not draft:
            return False
        try:
            QApplication.clipboard().setText(draft)
        except Exception as exc:      # noqa: BLE001 —— 剪贴板被别的程序占着不该带崩这一拍
            logger.error("社交候选放进剪贴板失败（%s），本次不重试", exc)
            return False
        return True

    def _show_social_draft(self, draft: str, why: str) -> None:
        """把「正文 + 解析」摆进宠物自己的聊天气泡（非模态、不抢焦点、只留最新一则）。

        为什么用聊天气泡而不是头顶那条浮动气泡：这一步紧接着就要问"要不要建对象攻略卡"
        （`_pop_offer` → `chat_bubble.open_at`），而开聊天气泡时会**收掉头顶那条**
        （两条并存就是用户说的"两个尖角"）。摆进对话流里反而正合适。

        **这一则是套在一个框里的一整件**（v2-social-chat-ui-fix2 提交F）：外层粗边框
        （3px）把正文、解析、按钮圈在一起——这一整块就是"一则"；内层细边框（1px）只装
        **正文**，就是那句能直接粘走的；内层下方、外层之内是**解析**（灰字，写给用户看的
        门道）；外层右下角压着「复制回复」按钮：**写剪贴板这件事就等这一下**，用户不按就
        一个字都不写。三个部件一次 append（`append_social_card`），不再分两条——分两条
        时按钮是气泡底部的固定控件，摘掉那一则按钮还在，而且"哪句能粘"要靠眼力分辨。

        另外，开头先把**上一轮那一则**摘掉（`clear_social_draft`，需求5）：记录区里只留
        最新一则，不然聊上几轮以后满屏都是过期的草稿，最新那条反倒得往上翻。摘的只是
        打了 `ROLE_SOCIAL_DRAFT` 标记的那一条（正文 + 解析 + 按钮同属它），用户与宠物的
        普通聊天一条不动。
        """
        pet_rect = self.pet_window.frameGeometry()
        self.pet_window.hide_speech_bubble()
        self.chat_bubble.follow(pet_rect)
        # 先把上一轮帮聊那一则摘掉（批次11 需求5）：记录区里**只留最新一则**。摘的是
        # 打了 `ROLE_SOCIAL_DRAFT` 标记的那一条（提交F 起正文 + 解析 + 按钮同属一条），
        # 用户与宠物的普通聊天一条不动——不是整框 `clear()`。
        self.chat_bubble.clear_social_draft()
        self.chat_bubble.append_social_card(
            social_draft_body(draft), social_draft_note(why), draft
        )
        self.chat_bubble.open_at(pet_rect)
        # 新的一则上屏：那三样状态归零、表停着（用户就在框跟前，先不计）
        self._start_card_watch()

    def _on_social_copy(self, draft: str) -> None:
        """点了气泡上那个「复制回复」：把这一版正文抄进剪贴板（批次11 需求6）。

        **只调一次 `_copy_social_draft`**——全仓库碰剪贴板的手仍然只有那一只，走的是
        同一个**只写不读**的入口，没有第二条路径，也不读、不监听、不按键、不发消息。
        点几次抄几次，不弹提示、不加回声：抄没抄上那只手本来就会记一条 error，用户按下
        去那一下也看得见（想再要一遍就再点一下）。
        """
        self._copy_social_draft(draft)
        # 内容被取走了（v2-social-card-timeout）：这一则不再走 3 分钟清除那条路，离开
        # 窗口也不清——已经粘走的东西留着无害。表一并停掉。
        self._card_copied = True
        self._card_away = False
        self._card_clear_timer.stop()

    # ---------- 帮聊那一则：离开对话窗口满 3 分钟就清掉（v2-social-card-timeout） ----------

    def _start_card_watch(self) -> None:
        """新卡片上屏：三样状态归零、表停着——**用户就在框跟前，先不计**。

        口径里"卡片生成 → 起 3 分钟计时器"落在这儿是**不起表**：生成那一拍气泡刚
        `open_at`（被激活到前台），用户就在这一则跟前，而这条线的计时只在"人不在"时跑
        （`_note_card_left`）。换一则也走这儿：上一则的复制标记跟着作废——新的一则又是
        一段**没被取走**的正文。
        """
        self._card_open = True
        self._card_copied = False
        self._card_away = False
        self._card_clear_timer.stop()

    def _note_card_left(self, why: str) -> None:
        """用户离开对话窗口：**从这一刻起**计满 `CARD_CLEAR_S`，再离开就重新计。

        两道闸不放行就不计：手上没有卡片（`_card_open`）、内容已经被复制走
        （`_card_copied`——那是"已经取走了，留着无害"）。重新离开是 `start()` 重来一遍，
        所以"离开一分钟又回来、再离开"从第二次离开重新数。
        """
        if not self._card_open or self._card_copied:
            return
        self._card_away = True
        self._card_clear_timer.start(int(CARD_CLEAR_S * 1000))
        logger.debug("帮聊那一则没人看着了（%s）：开始计 %.0f 秒", why, CARD_CLEAR_S)

    def _note_card_back(self) -> None:
        """用户回到对话窗口：停表，卡片**原样留着**（不是清掉，也不是从头再计）。"""
        self._card_away = False
        self._card_clear_timer.stop()

    def _on_card_window_left(self) -> None:
        """气泡失焦 / 被收起：用户离开对话窗口（v2-social-card-timeout 判据一）。"""
        self._note_card_left("气泡失焦或被收起")

    def _on_card_window_back(self) -> None:
        """气泡重新被激活：用户回到对话窗口（判据一的反面）。"""
        if self._card_open:
            self._note_card_back()

    def _in_original_chat(self) -> bool:
        """微信这一屏还是不是**原来那条单聊**（服务那头记着的页型）。"""
        service = getattr(self, "social_reply", None)
        return bool(service is not None and service.in_personal_chat)

    def _on_foreground_for_card(self, proc: str) -> None:
        """前台换人了：算离开还是算回来，看换的是谁、这一屏还认不认得出是那条单聊。

        判据二（v2-social-card-timeout）：**微信对话窗口不在前台**也算离开；反过来说，
        用户切回微信、而且这一屏仍是原来那条单聊，才算"回到对话窗口"——切回微信却停在
        群聊 / 会话总列表上，不算回来（口径里"重新激活**且仍在原单聊**"那一句）。

        宠物自己那扇窗在最前时拿到的是空进程名（正常由 monitor 拦下，走不到这儿）：那
        等于人就在框跟前，按"回来"处理——**宁可多留一会儿，也不误清**。
        """
        if not self._card_open:
            return
        if not str(proc or "").strip():
            self._note_card_back()
            return
        if is_social_proc(proc):
            if self._in_original_chat():
                self._note_card_back()
            else:
                self._note_card_left("微信换到了别的页")
            return
        self._note_card_left("微信不在前台")

    def _on_card_clear_due(self) -> None:
        """满 3 分钟了：把那则清掉、回到初始聊天态（输入框还在、建议气泡收起）。

        三道闸在这儿**再判一遍**：表是单发的、离开 / 回来路上都 `stop()` 过，正常到不了
        这儿；但"用户回来又走开"这种抖动下多判一道不会错——多清一张卡片是用户看得见的
        损失，多等一会儿不是。
        """
        if not self._card_open or self._card_copied or not self._card_away:
            return
        self._card_open = False
        self._card_away = False
        self.chat_bubble.clear_social_draft()
        if getattr(self.chat_bubble, "offer_theme", "").startswith("social"):
            self.chat_bubble.hide_offer()
        logger.debug("帮聊那一则在没人看着的地方放了 %.0f 秒，清掉它、回到初始聊天态",
                     CARD_CLEAR_S)

    def _on_social_card_offer(self) -> None:
        """候选列完了：问一句"要不要建对象攻略卡"，把 [好呀] [不用了] 露出来。"""
        logger.debug("社交候选列完：打开聊天框，问要不要建对象攻略卡")
        self._pop_offer("social_card", ASK_CARD_LINE)

    def _prime_social_foreground(self) -> None:
        """冷启动把「社交软件在不在前台」**先问一次**（v2-social-foreground-fix）。

        `foreground_changed` 只在**换人**时发（见 `monitor._poll_foreground`），而
        启动那一刻宠物自己的窗口正抢着前台（`start()` 头一句 `pet_window.show()`）：
        500ms 那拍走 `_is_self_foreground()` 当场 return，**一个信号都不发**。社交
        那条线的 `_social_foreground` 于是停在 `__init__` 里那个 False 上——不抓屏、
        不自动监听，一直到用户真的切一次窗为止。真机上的表现就是"刚启动不认微信、
        再启动一次才认"：认的不是"现在谁在前台"，是"有没有人碰巧换过一次前台"。

        这里补的是**现状**那一问（`monitor.foreground_now()`，只看不发信号）：拿到
        什么就交给 `on_foreground` 走一遍。前台是宠物自己时拿到的是空串，落到
        `_social_foreground = False`——那是**真值**，不是"还没问过"。

        **焦点那一问答不出社交软件时，退一步枚举可见窗口**（v2-social-await-prime-fix
        改动二，真机 2026-10-07 17:57）。上面那一问只答得了"**焦点**在谁身上"，而真机
        那条路上焦点压根不在微信上：微信个人窗先在桌面上开着（不是焦点）→ 再开宠物 →
        启动这一刻探到的是 `explorer.exe`，社交那条线据此把 `_social_foreground` 定成
        False。用户不手动切一次窗，`foreground_changed` 就一直不发，那条线一直停摆——
        人明明就坐在微信跟前跟人聊着。所以焦点答不出社交软件时，再看一眼"桌面上有没有
        **可见的**社交窗口"（`monitor.visible_procs()`），有就把社交这条线置成可触发态。
        **那一趟只看进程号，窗口标题一个字都没读**（见 `foreground.visible_pids`）。

        只在启动时问这一次：之后前台每一变都由信号带过来，不必反复探。探不到
        （系统调用失败）就当没问过，照旧等下一次换前台——不该因为这一问把启动带崩。
        """
        service = getattr(self, "social_reply", None)
        monitor = getattr(self, "screen_monitor", None)
        if service is None or monitor is None:
            return
        try:
            proc = monitor.foreground_now()
        except Exception as exc:      # noqa: BLE001 —— 见上
            logger.debug("冷启动探前台失败（%s），社交那条线等下一次换前台", exc)
            return
        if not is_social_proc(proc):
            # 焦点不是社交软件：再看一眼桌面上有没有可见的社交窗口。
            visible = self._visible_social_proc(monitor)
            if visible:
                logger.debug(
                    "冷启动：焦点在 %r（不是社交软件），但桌面上有可见的社交窗口 %r，"
                    "社交那条线照它置成可触发态", proc, visible,
                )
                proc = visible
        logger.debug("冷启动探一次前台：%r（社交那条线据此定 _social_foreground）", proc)
        service.on_foreground(proc)

    @staticmethod
    def _visible_social_proc(monitor) -> str:
        """桌面上**可见、没最小化**的社交进程名；一个都没有返回空串。

        只问"有没有"，不挑"是哪一个"：用户同时开着微信和 QQ 时，随便哪一个都够把社交
        那条线置成可触发态——真正决定"要不要冒话"的是后面那一道抓屏 + 冷却，不是这里
        挑中的这个进程名（`on_foreground` 只拿它判"算不算社交软件"）。挑第一个就行。

        枚举失败 / 不是 Windows / 翻不出进程名，一律当"一个都没看见"，不影响启动。
        """
        try:
            procs = monitor.visible_procs()
        except Exception as exc:      # noqa: BLE001 —— 见上：这一问不该把启动带崩
            logger.debug("冷启动枚举可见窗口失败（%s），社交那条线等下一次换前台", exc)
            return ""
        for proc in procs:
            if is_social_proc(proc):
                return proc
        return ""


    def _social_holds_floor(self) -> bool:
        """这会儿是不是"聊天窗口说了算"（v2-social-scene-boundary 的硬规则）。

        用户拍板：**只要置顶的是聊天窗口，就必须优先处理社交回复**——无论旁边/后台
        在放着电影、开着游戏、还是工作页。仲裁点拿这一档压过游戏 / 观影 / 工作三档，
        于是那三句场景开场词在聊天窗口置顶时不飘，话头留给社交那条线的「要帮你措辞吗」。

        **判据分两半**：① 前台进程名**现算**（不读 `social_reply._social_foreground`
        ——那份是本服务自己的 handler 跑完才更新的，而前台是一条信号派发给所有线的，
        排在社交前面的线问的时候它还是上一拍）；② 社交那条线**正端着东西**（邀请 /
        候选人那一问 / 正在写的草稿）——这几种情况下就算用户切走了前台，这一档
        也该继续说了算：那一问还杵在那儿等人点呢。

        **完成态不再算"端着东西"（v2-social-copy-clean）**：候选已经亮出来，这一段就
        结束了（`mark_done`）。老口径拿"候选框还开着"当第二个判据，而
        现在没有框了——用户干完了自己的事，这一档就该让位，别一直压着场景那条线。

        排在**专注之后**是故意的：这条优先级管的是场景飘词，而社交帮回那条线压根不问
        仲裁点（置顶聊天窗口时它照常冒话、照常拟候选），专注在前面拦不到它；反过来把
        社交顶到最前，会掀掉「专注要安静」那条钉死的红线。
        """
        if is_social_proc(getattr(self.screen_monitor, "foreground_proc", "")):
            return True
        sr = getattr(self, "social_reply", None)
        return bool(sr is not None and sr.pending_offer)

    def _on_offer_hidden(self, theme: str) -> None:
        """气泡上那排轻按钮退休了：被顶掉 / 被收起 / 用户点过——都算这次邀请结束。

        只认社交那两条线（`social` / `social_card`）：**社交服务那头 `_awaiting` 一旦
        挂上，`_poll_listen` 第一句就是拿它 early-return**，没人来清的话自动监听就静默
        停摆了（真机"对方回了消息、几分钟不弹候选、无报错无日志"就是这个）。别的线的
        按钮退休跟它无关，不动。

        `dismiss_offer()` 只清"等着点按钮"这一档，不碰"正等他说处境"那一档——后者是
        用户点了 [好呀] 换来的，正是下一句的上下文。
        """
        if not theme.startswith("social"):
            return
        sr = getattr(self, "social_reply", None)
        if sr is not None:
            sr.dismiss_offer()

    def _on_social_offer_expired(self) -> None:
        """社交那句邀请过期了（v2-social-await-prime-fix 改动一）：把按钮一起收掉。

        `_on_offer_hidden` 是界面 → 服务（按钮退休了，把那档作废）；这一条是反方向
        服务 → 界面（那档过期了，把按钮收掉）。两条合起来才叫**同寿**：少一条，就有
        一段时间是"按钮还挂着、服务已经不认这个邀请"，用户点下去落到 `_on_offer_reply`
        的通用聊天兜底（真机回的那句"好的呀"）。

        走 `hide_offer()` 而不是 `close_bubble()`：只收那一排按钮，用户手上的对话**不动**。
        `hide_offer` 末尾会发 `offer_hidden`，回头又喊一声 `dismiss_offer()`——那时
        `_offer_theme` 已经摘空，服务那边判"按钮确实不显示了"放行，幂等，无害。
        """
        self.chat_bubble.hide_offer()

    # ---------- 社交：截屏认对方原话 + 两路范例（v2-social-fewshot） ----------

    def _guide_cards(self) -> list:
        """所有卡（轮播 + 卡册）。卡级范例拿它们去认"这一个是哪张攻略卡"。"""
        if self.card_service is None:
            return []
        cards = [card for card, _count in self.card_service.plaza()]
        cards += [card for card, _count in self.card_service.card_book()]
        return cards

    def _guide_card_exists(self) -> bool:
        """「对象攻略卡」是不是**已经在轮播上**了（v2-social-card-dedup）。

        社交那条线每次要问"要不要建对象攻略卡"之前现问一次。判据就是
        `CardService.find_card`——**一条不另写**：大类 + 卡名两条都对上、且**没归档**
        （收进卡册的那张不算，再建一张新的正好顶上轮播的位子，见 `find_card` 的说明）。
        """
        if self.card_service is None:
            return False
        return self.card_service.find_card(
            CARD_CATEGORY, GUIDE_CARD_TITLE
        ) is not None

    def _to_physical(self, rect):
        """Qt 的**逻辑像素**矩形 → 抓屏那张帧上的**物理像素**矩形。

        v2-social-mask-dpr：`grab_foreground_window` 里那张帧是物理像素（窗口矩形走
        DWM/`GetWindowRect`），而 `frameGeometry()` 是逻辑像素。**涂白那几块 mask 必须
        先换到帧的坐标系**，不换就是"拿逻辑尺子量物理图"——本机 DPR 1.5 时 mask 只有
        实际的 2/3、还钉在左上角，浮窗右下那大半（正是正文所在）一个像素都没涂掉，
        浮窗里的字被 OCR 读走、底色又落在"对方"那档判据里 → 变成"对方原话"。

        换算按**外扩取整**（左上 floor、右下 ceil）：宁可多涂半个像素，也不能因为
        舍入漏掉最外那一列/一行字。DPR=1 时 floor/ceil 恒等，行为与本条之前一模一样。
        """
        from PySide6.QtCore import QRect

        if rect is None or rect.isNull() or rect.isEmpty():
            return rect
        dpr = self._screen_dpr()
        if dpr == 1.0:
            return rect
        left = math.floor(rect.left() * dpr)
        top = math.floor(rect.top() * dpr)
        right = math.ceil((rect.left() + rect.width()) * dpr)
        bottom = math.ceil((rect.top() + rect.height()) * dpr)
        return QRect(left, top, max(0, right - left), max(0, bottom - top))

    def _social_pet_rect(self):
        """宠物本体那块矩形（`grab_foreground_window` 的老参数）。**物理像素**。"""
        window = getattr(self, "pet_window", None)
        if window is None or not window.isVisible():
            return None
        return self._to_physical(window.frameGeometry())

    @staticmethod
    def _screen_dpr() -> float:
        """屏幕的物理像素 / 逻辑像素倍数。

        抓下来的帧是**物理像素**，而 `social_window` 里那几个版面数（图标栏 60、会话
        列表 250、输入区 96）是**逻辑像素**——不乘这一步，在高分屏上会少切一截，
        会话列表和输入框就漏进 OCR 了。取不到就回 1.0（宁可少切，不按错的倍数切）。
        """
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return 1.0
        try:
            value = float(screen.devicePixelRatio())
        except (TypeError, ValueError):
            return 1.0
        return value if value > 0.1 else 1.0

    def _social_pet_masks(self) -> list:
        """宠物自己那几块盖在聊天窗上的矩形：本体 + 说话气泡 + 助手窗。

        v2-social-ocr-isolate ①：只涂宠物本体是不够的。说话气泡（`SpeechBubble`）、
        「AI助理」助手窗（`ChatBubble`）都是 `Qt.Tool | FramelessWindowHint` 的
        独立小窗，**没有标题、独立于本体**，就悬在聊天窗上面——不涂掉，它们盖住的那
        几行字会被 OCR 当成"对方说的话"读走，正是用户报的"宠物自己说的话混进对方原话"。

        现问、只算可见的：没显示出来的窗不该白占一块。

        **全部换到物理像素**（v2-social-mask-dpr）：这几个 `frameGeometry()` 都是逻辑
        像素，而抓下来的帧是物理像素——见 `_to_physical`。

        **上面那三块是"点名"列的，漏一个就漏一次**（v2-social-leak-mask）：建对象攻略卡
        那个对话框（`GuideCardDialog`）就是漏掉的那一个——它弹着的时候用户照样能点回
        微信，前台一变成微信，抓屏就放行，框里那几行字（"对象攻略卡"、抽出来的对方信息）
        浅底深字、判出来正好是"对方"，就这么进了"对方原话"。所以补一层**兜底名单**：
        本进程所有可见的顶层窗一律涂掉（`_own_window_masks`），不再靠一个个点名。
        """
        from PySide6.QtCore import QRect

        masks: list[QRect] = []
        pet_rect = self._social_pet_rect()
        if pet_rect is not None:
            masks.append(pet_rect)
        window = getattr(self, "pet_window", None)
        bubble = getattr(window, "_bubble", None) if window is not None else None
        chat = getattr(self, "chat_bubble", None)
        # 候选框那一路删了（v2-social-copy-clean）：候选现在摆进 `chat_bubble` 里，
        # 它的矩形上面已经涂过了，不用再多一块。
        for widget in (bubble, chat):
            if widget is None:
                continue
            try:
                if not widget.isVisible():
                    continue
                masks.append(self._to_physical(widget.frameGeometry()))
            except Exception:      # noqa: BLE001 —— 取不到就不涂这一块，别把抓屏带崩
                continue
        masks.extend(self._own_window_masks(already=masks))
        logger.debug(
            "社交涂白掩码：dpr=%s 共 %d 块 物理像素=%s",
            self._screen_dpr(), len(masks),
            [(r.left(), r.top(), r.width(), r.height()) for r in masks],
        )
        return masks

    def _own_window_masks(self, already=()) -> list:
        """本进程**所有可见顶层窗**的矩形（物理像素）——掩码的兜底名单。

        v2-social-leak-mask。上面那三块是**点名**列的（宠物本体 / 说话气泡 / 助手窗），
        名单外任何一个本进程的窗都漏：`GuideCardDialog`（对象攻略卡）、`CardFormDialog`、
        `CardBookDialog`、`SettingsDialog`、`PomodoroBubble`、`CardBoard`……它们全都是
        独立顶层窗，盖在聊天窗上时那块像素会被 OCR 当"对方原话"读走。

        只枚举本进程的顶层窗，所以**微信本身天然进不来**（它不是我们的 widget），
        不需要额外"别遮微信"的判断——这是白名单自己的边界。

        只算可见的（`isVisible`），`already` 里已经点过名的跳过，免得同一块涂两遍、
        日志里的块数也不好对。取不到矩形就不涂这一块——**绝不把抓屏带崩**。
        """
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QApplication

        masks: list[QRect] = []
        app = QApplication.instance()
        if app is None:
            return masks
        known = [r for r in (already or ()) if r is not None]
        for widget in app.topLevelWidgets():
            try:
                if not widget.isVisible():
                    continue
                rect = self._to_physical(widget.frameGeometry())
            except Exception:      # noqa: BLE001 —— 取不到就不涂这一块
                continue
            if rect is None or rect.isNull() or rect.isEmpty():
                continue
            if any(rect == r for r in known):
                continue
            known.append(rect)
            masks.append(rect)
        return masks

    def _social_session_id(self) -> str:
        """这一场社交窗对着的是谁（v2-social-session-reset）。**服务现问一次，回一个字符串。**

        值的来源是 `_grab_social_chat` 抓屏那一拍**已经读到**的那个标题（那次读是为了
        认"前台是不是宠物自己"）——这里**不额外读一次窗口标题**，`foreground_title`
        那只手的口径一分没松；只是把同一次读到的值留了一份给服务比"换没换人"。

        为什么要它：微信里换会话时**前台进程没变**（还是 wechat.exe 那一个），
        `foreground_changed` 不会发第二次，服务那条"走出社交前台就清场"一次都不触发
        ——上一场"拟过哪份对方原话、哪一屏已锁"的记忆原样带进下一场，把新会话的对方
        原话挡在门外（真机"换个对话、对方回话就不动了"）。

        **只回值、不做加工、不留副本**：服务那边存一份、换人即被覆盖，两边都不落盘、
        不写日志。取不到（还没抓过 / 前台是宠物自己 / 标题为空 / 抓屏没开）就回空串
        ——服务把空串当"这一场认不出是谁"，不动任何状态。
        """
        return getattr(self, "_social_session_hint", "")

    def _grab_social_chat(self):
        """社交软件在前台那一拍：截聊天窗那一块 + 转数组。**界面线程调**（只拷内存）。

        不挡事的前台检查在这儿：OCR 总闸关掉就一个像素都不采。取不到窗口（社交窗被
        挡住、宠物正好在前台）就回 None——这一场没有对方原话，起草照旧。

        **两道隔离（v2-social-ocr-isolate ①）：**

        1. **前台不是宠物自己才采**。按进程号认（`is_pet_window`）：宠物本体、说话气泡、
        「AI助理」助手窗全是**本进程**开的窗，标题各有各的（气泡干脆没标题），
        进程号是唯一认得住的那条线。前台是宠物自己 → 这拍不采。
        2. **只留聊天消息列表那一块**。整扇窗里有会话列表、工具栏、底下还有输入框，
        全是杂字；`chat_area_box` 量出中间那块消息区再交给 OCR（先按像素找聊天区的
        左边界，量不出来才退回 `chat_client_rect` 的按版面硬切，v2-social-ocr-catch；
        连硬切都切不动才走 `cut_edges_box` 把标题栏与会话列表切掉，v2-social-leak-mask）。
        **不扫全屏**这条老口径没动——抓的始终是前台窗口，现在是它中间那块。

        涂白那几块也**不再是点名制**（v2-social-leak-mask ②）：本进程所有可见顶层窗
        一律涂掉，见 `_own_window_masks`——建卡那个对话框就是这么堵上的。
        """
        if not self.config.screen.ocr_enabled:
            return None
        import os

        from life.screen import shot
        from life.screen.monitor import _to_numpy
        from life.screen.social_window import (
            chat_area_box,
            chat_client_rect,
            cut_edges_box,
            input_cut_px,
            is_pet_window,
        )
        from monitor import foreground

        title = ""
        try:
            title = foreground.foreground_title()
        except Exception:      # noqa: BLE001 —— 取不到标题不算错，进程号那条腿还在
            title = ""
        try:
            pid = foreground.foreground_pid()
        except Exception:      # noqa: BLE001
            pid = None
        if is_pet_window(
            title=title,
            pid=pid,
            pet_pid=os.getpid(),
            pet_name=self.config.pet.name,
        ):
            logger.debug("前台就是宠物自己（%s），这一拍不采社交画面", title or "无标题")
            return None
        # **前台不是社交软件就不采**（v2-social-input-draft-exclude）。上面那次进程号
        # 读取顺手判一下就够了，不额外读任何东西。
        #
        # 为什么非在这儿拦不可：服务里那个 `_social_foreground` 是**采样信号**设的，
        # 而采样隔一阵才问一次——前台切走之后、信号还没到之前的那一两拍，它仍旧是
        # 上一拍的 True，于是**谁在最前面就抓谁**。真机现场（2026-10-07 15:46:11）：
        # 前台已经是 explorer.exe，抓下来的是任务栏那一条（帧 2560x72），OCR 把任务栏
        # 的搜索框认成 `Q 搜索`，宠物照拟一段——日志里那次 `DRAFT source='Q 搜索'`
        # 就是它。这里读的是**当下**的前台，没有那一拍的滞后。
        #
        # 进程名取不到时不拦（空名认不出是谁）：宁可漏挡，也不把整条线误挡死。
        proc_name = self._proc_name_of(pid)
        if proc_name and not is_social_proc(proc_name):
            logger.debug("前台不是社交软件（%s），这一拍不采社交画面", proc_name)
            return None
        # 这一场对着的是谁（v2-social-session-reset）：上面那次标题读取顺手留一份给服务，
        # 换会话时前台进程不变、只有这一格会变——它就是"换人了"的唯一信号。
        # **留在这儿是有讲究的**：前台是宠物自己时上面已经 return 了，所以留下的这里
        # 一定是社交窗自己的标题（独立聊天窗上写的就是对方 / 群名；主窗写的是应用名，
        # 那种情况顶多"认不出换人"，不会认错人）。跟标题同一条隐私口径：用完即弃、
        # 不落盘、不进日志。
        self._social_session_hint = (title or "").strip()

        masks = self._social_pet_masks()
        image = shot.grab_foreground_window(
            self._social_pet_rect(), extra_masks=masks
        )
        if image is None:
            return None
        # 这一拍到底采的是谁、涂掉了自己几块窗（v2-social-leak-mask 的诊断）。
        # 真机上"对方那是谁"要能一眼看出来：前台进程名 + 帧尺寸 + 涂白块数。
        logger.debug(
            "社交抓屏：前台 pid=%s 进程名=%s（不是宠物自己，是社交软件）| 帧 %dx%d | 涂白 %d 块",
            pid, proc_name, image.width(), image.height(), len(masks),
        )
        # 裁剪前的那一帧留一份（v2-social-leak-dump-debug）：下面 `image` 会被切成聊天区，
        # 而"漏源"要判的恰恰是**被切掉的那一边**——左侧会话列表有没有入镜。这儿只存一个
        # **引用**（零拷贝、零转换，这一拍是界面线程，不做任何多余的事），真正落盘在后台
        # 线程认出 `[草稿` 之后（`_dump_social_leak_frame`）。`image.copy()` 出的是新对象，
        # 所以这个引用一直指着那张没裁过的。这一帧里有用户的对话内容，只在本机内存与
        # %TEMP% 里过，用完即弃。
        self._social_raw_image = image
        from PySide6.QtCore import QRect

        box = chat_area_box(image, self._screen_dpr())
        if box is not None:
            client = QRect(*box)
            if not client.isEmpty():
                image = image.copy(client)
        else:
            # 量不出聊天区时**不能把整扇窗丢给 OCR**（v2-social-input-guard）。整扇窗里
            # 有会话列表的人名和预览、有工具栏，**底下还有输入框里敲了一半的草稿**——
            # 那草稿是白底黑字，跟对方气泡一个色（真机实测都是 250,250,250），认回来
            # 分不出是谁说的，直接被当成"对方原话"。
            #
            # 三级兜底：先退回**按版面硬切**那条路（`chat_client_rect`，底部那一刀同样
            # 走 `bottom_cut_px`）；连它都切不动（窗太小，切完剩不下东西）时走
            # `cut_edges_box`——**至少把标题栏与会话列表切掉**（v2-social-leak-mask）。
            # 原来这一档只剩"切掉底部输入区"一刀，于是标题栏（独立聊天窗上写的就是
            # 联系人 / 群名）与会话列表（群名 + 预览）整块进了 OCR，浅底深字判出来正是
            # "对方"——群名就是这么变成"对方原话"的。
            # 真的一刀都切不动（`cut_edges_box` 给 None），才退回老路：
            # **至少把底部输入区那一带切掉**——那一条带子里只可能是用户自己的草稿，
            # 切它绝不会误伤对方的消息（对方消息永远在输入区之上）。
            dpr = self._screen_dpr()
            full = QRect(0, 0, image.width(), image.height())
            client = chat_client_rect(full, dpr)
            if client is not None and not client.isEmpty() and client != full:
                # 1a（v2-social-input-region-leak-fix）：底部那一刀也从固定的 `bottom_cut_px`
                # 换成当场量的 `input_cut_px`。`chat_client_rect` 内部那刀是按"输入区固定高"
                # 拍的，草稿把输入框顶高时会漏掉草稿；这里按像素腿把底边再往上收到
                # "当场量到的输入区上沿"，草稿就不会残留在裁剪框里。
                keep = image.height() - input_cut_px(image, dpr)
                if keep > client.top():
                    client = QRect(client.left(), client.top(),
                                   client.width(), keep - client.top())
                image = image.copy(client)
            else:
                box = cut_edges_box(image, dpr)
                if box is not None:
                    logger.debug(
                        "社交窗量不出聊天区（%dx%d），退回按版面切边：切掉标题栏与会话"
                        "列表 %s",
                        image.width(), image.height(), box,
                    )
                    image = image.copy(QRect(*box))
                else:
                    keep = image.height() - input_cut_px(image, dpr)
                    if keep > 0:
                        logger.debug(
                            "社交窗量不出聊天区（%dx%d），只切掉底部输入区那一带，认 %d 行",
                            image.width(), image.height(), keep,
                        )
                        image = image.copy(QRect(0, 0, image.width(), keep))
        # 这一拍落不落整帧（v2-social-leak-dump-widen，16-α 放宽）：**社交窗在前台就落**，
        # 不再要求"整屏看起来像会话总列表"——原来那条判据（`looks_like_chat_list`）留在
        # `social_window` 里没删，归 16-β 定闸时用，眼下这一格不调它。
        #
        # 摆在这儿是有讲究的：① **认字之前**——落的是这一拍真正抓下来的那扇窗；② 这一拍在
        # 界面线程，只读两个布尔量、**不碰磁盘**（落盘仍旧在后台线程，见 `_read_social_chat`），
        # 跟 `_social_raw_image` 那句"只存一个引用"同一个纪律。
        #
        # **两半各自的分量照实说**：`is_social_proc(proc_name)` 上面 :2175 已经拦过一道
        # （那儿的口径是"进程名取不到时不拦"）——写在这儿是把口径写全，顺带把"空进程名"
        # 那一种也挡住；`_social_foreground` 那一半是**采样信号**（`SocialReplyService`
        # 自己的 handler 跑完才更新，见 `social.py:746`），而抓屏这条路本来只在它为真时
        # 才走（`_poll_listen` 头一句、`handle_request` 里那句），所以真机上它基本恒为真。
        # 这一格真正放开的，是"必须像列表"那条。
        #
        # 它挂在 `social_reply` 上，**不是** app 自己的属性——写成 `self._social_foreground`
        # 会 AttributeError，掉进下面那个 `except` 变成"一张都不落"。
        try:
            service = getattr(self, "social_reply", None)
            self._social_dump_frame = bool(
                is_social_proc(proc_name)
                and getattr(service, "_social_foreground", False)
            )
        except Exception:      # noqa: BLE001 —— 这是取证，不该把抓屏那条线带崩
            self._social_dump_frame = False
        return _to_numpy(image)

    def _read_social_chat(self, array):
        """**后台线程调**：把那一帧认成字，**按气泡色分好谁说的**。

        借 monitor 已有的 RapidOCR（同一次识别，只是这回连字框一起要），不另起引擎。
        分出来的 `ChatRead` 里：绿气泡那半边归 user、浅色那半边归 other、认不出的记
        UNKNOWN（上层退回老的空闲时机那套）。**只读**；唯一的落盘是认出会话列表草稿预览
        时的取证图（`_dump_social_leak` / `_dump_social_leak_frame`），落的是本机
        `%TEMP%`，复验完自己删。
        """
        from life.screen.social_window import ChatRead, classify_lines

        monitor = getattr(self, "screen_monitor", None)
        if monitor is None:
            return ""
        read = classify_lines(array, monitor.read_screen_lines(array))
        if read.list_draft_rows:
            self._dump_social_leak(array)
            self._dump_social_leak_frame()
        elif getattr(self, "_social_dump_frame", False):
            # 社交窗在前台的那一拍（v2-social-leak-dump-widen）：判据在**认字之前**就定好了
            # （`_grab_social_chat` 里那一格），这儿只负责把**裁剪前的整帧**落盘。用 `elif`
            # 是免得同一帧落两张——两张图内容一横一模，看的是同一件事。
            self._dump_social_leak_frame("社交前台取证帧")
        # 这一拍前台是社交软件，可**这一屏不是一场单聊**（v2-social-listview-leak-fix
        # 16-β）：会话总列表 / 搜索页 / 设置页这类非 active 视图上，OCR 读出来的一行行是
        # "昵称:预览"与时间戳，色卡把每一行都认成对方白气泡（列表行就是白底），末尾那个
        # 纯时间戳再被 `_is_chat_chrome` 挑掉，材料于是落在**昵称**上——真机 2026-10-08
        # 12:32:59 那一拍就是拿「茗风」两个字当"对方原话"拟了一段。
        #
        # 判据在**认字之后**（`looks_like_list_page`，文本层）而不在认字之前：像素那条
        # （`social_window.looks_like_chat_list`）在这台机器的窄版单页上分不开——列表页
        # 右半全是时间戳列，"行尾留白"那条不成立，真帧上它老实回 False。这一刀**不省那次
        # OCR**（列表页第一次出现仍会认一次字），省掉的是"把列表当材料"这件事本身。
        #
        # 清成空 read 之后：`_classify_incoming` 走到 `not new` 直接回 "none"，
        # `_auto_generate` 压根不会被叫到，`_last_listen_tail` 也**不动**（`if new_tail:`
        # 为假）——不会把列表页的字记成"这条处理过了"。指纹在 `_poll_listen` 里已经推进
        # 过，所以这一页静止时不会反复认字；人切回聊天窗指纹一变，自然重新认一遍。
        #
        # **自动与手动两条路共用这一处**（16-β 裁定）：手动那条拿到的同样是这份空 read，
        # `_on_screen_read` 于是把"对方原话"记成空——照旧是"没认到"，不拿列表的字去拟。
        # 这一屏是**哪一种页**（v2-social-trigger-window-gate）：**在清成空 read 之前判**
        # ——上面那一刀把总列表清成空 read 之后，这份 read 在文本层就什么都看不出来了
        # （判出来只会是"单聊"）。判完把结果**挂在 read 上**，手动与自动那两条回调拿到的
        # 是同一份页型，"仅单聊"那道闸才有一处收口（见 `social.chat_page_kind`）。
        #
        # 判据全在**认出来的字**上（`social.classify_chat_page`）：群名栏的「群名 (N)」、
        # 整行 `群公告`、表头那一带的固定页名——不看颜色、不看像素，跟 16-β 同一个态度。
        # 这一刀**不省那次 OCR**（群聊 / 订阅号第一次出现仍旧认一次字），也不动 16-β
        # 那份空 read 的语义，只是多记一格。
        page_kind = classify_chat_page(read)
        if looks_like_list_page(read):
            logger.debug("这一屏看着像会话总列表（%d 行、没有一个自己发的），这一拍不拟、不进材料",
                         len(read.lines))
            read = ChatRead()
        read.page_kind = page_kind
        self._log_tail_trace(read)
        return read

    def _dump_social_leak_frame(self, reason: str = "") -> None:
        """把**裁剪前的整帧**落到 `%TEMP%/pet_social_leak_<ts>.png`（v2-social-leak-dump-debug）。

        **跟下面那一张的分工**：`_dump_social_leak` 落的是**喂给 OCR 的裁剪帧**——它答的是
        "认到了什么草稿"；这一张落的是**裁之前那一整扇窗**——它答的是另一个问题：
        **漏进来的那些字到底长在窗口的哪一块**。那一行草稿预览若真是从列表那一列漏进来的，
        就必须在整帧上看得见它长在列表中，而不是长在聊天区里；只看裁剪帧永远判不了这件事。

        **触发**（两条，各管一问）：

        1. *（默认，`reason=""`）* `classify_lines` 认出**会话列表草稿预览**（`[草稿` 行首，
           即 `ChatRead.list_draft_rows`）——v2-social-leak-dump-debug 那一档。别的列表残字
           （"广场店"这类群名碎片）**不触发落图**：那种字单看长得跟人话没有区别，拿它当
           触发条件就是误伤。触发与 15-Y 那半修共用**同一把尺**（`is_chat_list_draft`）。
        2. *（`reason="社交前台取证帧"`）* **社交窗在前台**的每一拍（v2-social-leak-dump-widen，
           把 16-α 那条"必须整屏像会话总列表"放宽掉了）：判据在**认字之前**由
           `_grab_social_chat` 定好、存在 `_social_dump_frame` 上——落的是**这一拍真正抓
           下来的那扇窗**，跟第 1 条（聊天里混进了列表的一行）是两件事，所以日志分得开。
           16-α 那条判据（`looks_like_chat_list`）留在 `social_window` 里没删，归 16-β 用。

        **间隔**：模块级 `_leak_dump_ts`，`_LEAK_DUMP_MIN_GAP` 秒内只落一张——漏是持续的，
        不设闸就是一秒一张。这一条挡住的是**同一档内部**的重复；两条触发之间也共用这一个
        闸。整段包在 `except` 里：诊断永远不许把认字那条线带崩。
        """
        global _leak_dump_ts
        import tempfile
        import time
        from datetime import datetime
        from pathlib import Path

        now = time.time()
        if _leak_dump_ts is not None and now - _leak_dump_ts <= _LEAK_DUMP_MIN_GAP:
            return                       # 刚落过：这一拍照旧认字，只是不再落第二张
        _leak_dump_ts = now
        try:
            image = getattr(self, "_social_raw_image", None)
            if image is None or image.isNull():
                logger.debug("社交漏源取证：这一拍手上没有裁剪前的整帧，跳过落图")
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = Path(tempfile.gettempdir()) / f"pet_social_leak_{stamp}.png"
            if image.save(str(path), "PNG"):
                if reason:
                    logger.debug(
                        "社交漏源取证：%s dump %s（%dx%d，裁剪前的整帧——这一拍抓下来的"
                        "那扇窗长什么样、列表有没有入镜，这张上一眼可见）",
                        reason, path, image.width(), image.height(),
                    )
                else:
                    logger.debug(
                        "社交漏源取证：dump %s（%dx%d，裁剪前的整帧——左侧会话列在不在"
                        "裁剪框里，这张上一眼可见）",
                        path, image.width(), image.height(),
                    )
        except Exception as exc:      # noqa: BLE001 —— 这是诊断，不该把认字那条线带崩
            logger.debug("社交漏源取证落盘失败：%s", exc)

    def _dump_social_leak(self, array) -> None:
        """这一屏里认出了**会话列表的草稿预览**（`[草稿] …`）→ 落一张临时图给复验（15-Y）。

        那一行已经在 `classify_lines` 里丢掉了（不再当"对方原话"），可它**本不该出现在
        裁剪框里**——它长在会话列表那一列。落一张图是为了拿真帧把几何那一半（聊天区左界
        该不该止于列表分隔线）判死，而不是靠日志里的几个数猜。

        **只在漏的那一拍落，而且同一分钟内只落一张**（漏是持续的，不然一秒一张）。
        落点是 `%TEMP%/pet_social_leak_<毫秒>.png`，路径打一条 debug；复验完自己删。
        这一张里有用户的对话内容，所以它只在**本机临时目录**、只在**真漏了**的时候出。
        """
        import time
        import tempfile
        from pathlib import Path

        now = time.time()
        if now - getattr(self, "_social_leak_dump_at", 0.0) < SOCIAL_LEAK_DUMP_GAP_S:
            return
        self._social_leak_dump_at = now
        try:
            from PySide6.QtGui import QImage

            height, width = int(array.shape[0]), int(array.shape[1])
            data = array.tobytes()             # QImage 不拷这块内存，`data` 得活到 save 之后
            image = QImage(data, width, height, width * 3, QImage.Format_RGB888)
            path = Path(tempfile.gettempdir()) / f"pet_social_leak_{int(now * 1000)}.png"
            if image.save(str(path), "PNG"):
                logger.debug("社交尾巴读出会话列表草稿预览，这一帧落盘复验：%s", path)
        except Exception as exc:      # noqa: BLE001 —— 这是诊断，不该把抓屏那条线带崩
            logger.debug("社交漏字那一帧落盘失败：%s", exc)

    def _proc_name_of(self, pid) -> str:
        """进程号 → 进程名。取不到回空串，绝不抛。

        两处在用：抓屏那行日志（"这一拍采的是谁"），以及 v2-social-input-draft-exclude
        那道"前台不是社交软件就不采"的闸——闸那边**空串当"认不出"**，放行不拦。
        """
        if not pid:
            return ""
        try:
            monitor = getattr(self, "screen_monitor", None)
            pids = getattr(monitor, "_pids", None)
            if pids is None:
                return ""
            return pids.name_of(pid) or ""
        except Exception:      # noqa: BLE001 —— 日志这一句不该把抓屏带崩
            return ""

    def _log_tail_trace(self, read) -> None:
        """这一趟认出来的"对方原话"到底是什么（v2-social-leak-mask 的诊断）。

        真机上要判的是两件事：**取到的末尾是不是真的最后一句**、以及那一句**是不是
        对方说的**。所以把喂给模型的那一份（`_tail_context` 的输出，就是 `_auto_generate`
        与 `_write` 拿到的同一份）打出来，并且现问一句："里面有没有那句不该出现的"。

        **脱敏**：只留**末尾 `SOCIAL_TAIL_TRACE_CLIP` 个字**（换行压成 ` / `）与总长，
        不落整屏聊天记录——够看出"取的是不是最后一句"，又不会把人家一场对话全记进日志。
        这一句只在 debug 档出，取不到就静静放过。
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            from life.screen.social import _tail_context

            tail = _tail_context(read)
        except Exception:      # noqa: BLE001 —— 这是诊断，不是功能
            return
        flat = " / ".join(tail.split("\n"))
        clip = flat[-SOCIAL_TAIL_TRACE_CLIP:] if flat else ""
        logger.debug(
            "社交尾巴（脱敏：末尾 %d 字 / 全长 %d）：%s%s | 含「%s」=%s",
            len(clip), len(tail), "…" if len(flat) > len(clip) else "", clip,
            SOCIAL_TAIL_PROBE, SOCIAL_TAIL_PROBE in tail,
        )

    def _build_guide_card(self) -> None:
        """用户对"要不要建对象攻略卡"点了 [好呀]：先抽对方信息，再让用户改，最后建卡。

        信息收集是 C 混合：从这次帮忙回复的对话里**自动抽**对方兴趣/爱好/约会地点，
        弹一个可改框，用户改完才写进卡专属库。
        """
        if self.social_reply is None or self.card_service is None:
            return
        self.social_reply.extract_profile(
            on_done=self._on_profile_extracted,
            on_failed=lambda exc: self.bus.pet_speak.emit("这段信息我一下没抽出来，先不建卡了。"),
        )

    def _on_profile_extracted(self, profile: str) -> None:
        """对方信息抽出来了：弹可改框，用户确认/改完再建卡、写专属库。"""
        dialog = GuideCardDialog(profile or "", self.pet_window)
        accepted = dialog.exec() == QDialog.Accepted   # 「保存并建卡」走 accept()
        edited = dialog.values()
        dialog.deleteLater()
        if not accepted or not edited:
            if not accepted:
                self.bus.pet_speak.emit("好，那先不建，这几段你挑着用。")
            return
        # 已经有这张卡了就别再说"建好了"（v2-social-card-dedup）：`create_manual_card`
        # 查到同类同名会把**既有那张**交回来，那段对方信息写进的是它的专属库——
        # 话得说成"写进那张里了"，不能谎报新建了一张。
        existing = self.card_service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE)
        card = (
            existing if existing is not None
            else self.card_service.create_manual_card(
                CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL
            )
        )
        if card is None:
            self.bus.pet_speak.emit("这会儿卡位满了，先收一张再建，要不你先挑段回话用着。")
            return
        self._write_guide_card_kb(card, edited)
        self._schedule_guide_reminders(card, edited)
        if existing is not None:
            self.bus.pet_speak.emit(
                f"「{GUIDE_CARD_TITLE}」你已经有一张了，这段对方信息我写进那张里了。"
            )
            return
        self.bus.pet_speak.emit(f"「{GUIDE_CARD_TITLE}」建好了，对方信息也存进它专属的知识库了。")

    def _write_guide_card_kb(self, card, text: str) -> None:
        """把（用户改过的）对方信息写进这张卡的专属知识库（v2-card-kb）。

        落点 `<知识库>/cards/<卡号>/对方信息.md`。写不进去只记日志——建卡已经成了，
        别因为落盘失败把整条链喊停。
        """
        from pathlib import Path

        root = card.kb_path or str(ensure_card_kb_root(self.config, card.id))
        path = Path(root) / "对方信息.md"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            logger.error("对象攻略卡 #%d 对方信息写不进去（%s）", card.id, exc)

    def _schedule_guide_reminders(self, card, text: str) -> int:
        """把对方信息里的时间点抽成提醒节点，走现有提醒线落库调度（v2-card-suite 增量②）。

        抽出来的每个节点 = 一条 Reminder（`utter_id` 为空——这不是从某句话里来的，是从
        对方信息里按日期抽的）。落库 / 调度 / 通知复用 ChatterService 同一条链
        （`ReminderDAO.add` → `ReminderJobs.schedule` → `reminder_changed`），
        不新建提醒机制。返回这次建了几条。
        """
        if self.reminder_dao is None or self.jobs is None:
            return 0
        nodes = extract_reminder_nodes(text)
        for node in nodes:
            reminder = self.reminder_dao.add(self.user.id, node.trigger_at, node.content, None)
            self.jobs.schedule(reminder)
            self.bus.reminder_changed.emit()
            logger.info(
                "对象攻略卡 #%d 建提醒：%s @ %s", card.id, node.content, node.trigger_at
            )
        return len(nodes)

    # ---------- 产卡：谁提的归谁说（指令 24） ----------

    def _on_card_proposed(self, theme: str, title: str) -> None:
        """卡牌服务提议建卡：由宠物本体开口（指令 24 的"一屏一腔调"）。

        玄学页整条通道下线之后只剩这一条：问法与确认出口都在本体的 ChatterService 里，
        再往聊天气泡上冒 [好呀] [不用了] 两个轻按钮（指令 25-B 第 2 项）。
        """
        self.chatter_service.begin_card_offer(theme, title)
        self.chat_bubble.show_offer(theme)

    def _on_card_offer_declined(self, theme: str) -> None:
        """用户点了「不用了」：记下这次拒绝，限频期间不再提这个品类。"""
        self.chat_bubble.hide_offer()
        self.card_service.resolve_invite(theme, False)

    def _create_card_from_offer(self, theme: str) -> str:
        """生活卡邀请受理（指令 25-B 第 2 项）：先弹确认框让用户改卡名，再建卡。

        用户在确认框里改主意（取消）算作拒绝，也记一笔；返回建成的卡名，空串表示没建成。
        建成了就当场把它翻到眼前——刚点头建的卡，不该还让人自己去生活广场翻。
        """
        self.chat_bubble.hide_offer()
        dialog = CardOfferDialog(theme, self.panel)
        accepted = dialog.exec() == QDialog.Accepted
        title, icon = dialog.values()
        dialog.deleteLater()
        if not accepted:
            self.card_service.resolve_invite(theme, False)
            return ""
        made = self.card_service.accept_invite(theme, title, icon)
        if made:
            # 复用主动服务那条聚焦逻辑（同一个信号、同一个槽），不另写一套：
            # 刚点头建的卡，就该当场翻到眼前，不用自己去生活广场找
            card = self.card_service.active_card_of_theme(theme)
            if card is not None:
                # 这一发不让宠物让位（跟 26-B 那条区分开）：翻开的卡是用户刚在聊天框里
                # 点头建的，让位会把宠物从眼前抽走，注意力反而从卡上跑掉了
                self._focus_keeps_pet = True
                try:
                    self.bus.service_show_card.emit(card.id, "")
                finally:
                    self._focus_keeps_pet = False
        return made

    # ---------- 主动服务（指令 26-B） ----------

    def _find_card_of_theme(self, theme: str) -> tuple[int, str] | None:
        """手边有没有这个品类的卡（生活意图那条线要用）：只认摆在大卡轮播上的。

        收进卡册的那张不算——摆在眼前、够得着才叫服务。
        """
        card = self.card_service.active_card_of_theme(theme)
        return (card.id, card.title) if card is not None else None

    def _on_service_offer(self, kind: str) -> None:
        """主动服务问出口了：对话框里露出 [好呀] [不用了]，不弹窗、不抢话。"""
        self.chat_bubble.show_offer(kind)

    def _on_service_show_card(self, card_id: int, text: str) -> None:
        """生活那条线：直接把这张卡弹出来，并让卡牌服务就着这句话给一句场景建议。

        卡是用户自己认领的，弹出来就是服务（指令 26-B 的"卡轻窗重"）；
        建议（出行：行程规划 + 交通方式 + 时间预估）是联网生成，晚一步自己再说一句。
        """
        self.open_panel("life")
        self.open_card(card_id, self._center_card_rect())
        self.card_service.scenario_advice(card_id, text)

    def _on_service_open_work(self, text: str) -> None:
        """用户点头了：开工作模块，并尽量落到他正说的那个部门那张卡上。"""
        self.open_panel("work")
        dept_id = self.work_service.guess_dept(text)
        if dept_id is not None:
            self.work_panel.focus_dept(dept_id)

    def _center_card_rect(self) -> QRect:
        """从聊天里弹卡的起手位置：没有"卡片原位"可依，就让详情页从面板中央长出来。"""
        box = self.panel.rect()
        width = int(box.width() * 0.62)
        height = int(width * 0.743)
        return QRect(
            box.center().x() - width // 2, box.center().y() - height // 2, width, height
        )

    # ---------- 游戏陪玩 ----------

    def _start_playmate(self) -> None:
        """陪玩模式：需要一个能看图的模型（设置页的「陪玩」里填 Key）。"""
        provider = self.config.llm.provider(self.config.vision.provider)
        if not self.config.get_api_key(provider):
            self.pet_window.speak(
                f"我还没拿到看图的钥匙，右键→设置→陪玩里填一下 {provider.name} 的 API Key，"
                "我就能看着画面陪你过关了。"
            )
            return
        self.playmate_service.start()
        self.panel.show_toast("陪玩模式已打开（说「不用陪玩了」就能关掉）")

    def _stop_playmate(self) -> None:
        self.playmate_service.stop()
        self.panel.show_toast("陪玩模式已关闭")

    def _hide_pet(self) -> None:
        """隐身：藏起桌宠（托盘图标还在，从托盘就能叫回来）。"""
        self.pet_window.hide_pet()
        self.chat_bubble.close_bubble()
        self.tray.set_pet_visible(False)
        # 记一笔"是用户自己要它藏的"（v2-scene-state）：场景态要摆样子时不抢这个。
        self._pet_user_hidden = True

    def _toggle_pet_visible(self) -> None:
        if self.pet_window.isVisible():
            self._hide_pet()
        else:
            self.pet_window.show_front()
            self.tray.set_pet_visible(True)
            self._pet_user_hidden = False

    # ---------- 性格（指令 3：切换后立刻见效，不用重启） ----------

    def _cycle_persona(self) -> None:
        """扯完脸蛋换下一个性格（按预设顺序轮换）。

        自定义人设没写过文字就不排进轮换——那样切过去等于没有性格。
        """
        keys = [key for key in PERSONA_KEYS if key != CUSTOM_KEY or self.config.pet.persona_prompt.strip()]
        current = self.config.pet.persona
        index = keys.index(current) if current in keys else -1
        self._switch_persona(keys[(index + 1) % len(keys)])

    def _switch_persona(self, key: str) -> None:
        if key not in PERSONA_KEYS:
            return
        self.config.pet.persona = key
        preset = preset_of(key)
        if preset is not None:
            # 说话风格跟着性格走（换完你还能在设置里自己微调）
            self.config.pet.style_actions, self.config.pet.style_kaomoji = preset_style(preset)
        try:
            self.config.save()
        except OSError as exc:
            logger.error("保存性格失败: %s", exc)
            self.panel.show_toast(f"性格没能保存：{exc}")
            return
        self.pet_profile_dao.update(self.config.pet.name, key)
        self.pet_window.apply_persona(key)
        # 换性格不弹任何文字提示：让它自己默默变成另一个脾气就好

    def _on_settings_saved(self) -> None:
        pet_name = self.config.pet.name
        # 身份切换（调试用）改了：工作模块的服务与界面立刻按新身份走（标题栏也跟着变）
        self.work_service.set_role(self.config.work.role)
        # 点评开关（指令 23 第 3 项，从工作页挪进设置页了）：立刻生效
        self.work_service.set_comment_enabled(self.config.work.pet_comment)
        self.work_panel.apply_role()
        self.panel.apply_pet_name(pet_name)
        self.life_panel.apply_pet_name(pet_name)
        self.pomodoro_panel.apply_pet_name(pet_name)
        self.playmate_panel.apply_pet_name(pet_name)
        self.pomodoro_panel.refresh_remind()
        self.pomodoro_service.apply_config()
        self.activity_service.apply_config()
        self.screen_monitor.apply_config()
        self.screen_focus_timer.apply_config()
        self.screen_notes.apply_config()
        if self.playmate_service.is_active:
            # 陪玩的截屏间隔之类的参数改了，重启一次让它按新参数跑
            self.playmate_service.stop()
            self.playmate_service.start()
        self.chat_bubble.apply_speaker(self.psych_service.display_name)
        self.pet_window.apply_persona(self.config.pet.persona)
        self.pet_window.setWindowTitle(pet_name)
        self.tray.setToolTip(f"{pet_name} 在陪着你")
        self.panel.show_toast("设置已保存，模型已热切换")

    def _first_run_hint(self) -> None:
        provider = self.config.llm.active_provider
        if self.config.get_api_key(provider):
            # 有钥匙就不用提示；开机问候由问候服务负责，别抢话
            return
        self.panel.show_toast("还没填 API Key：右键点猫咪 → 设置 → 模型 → 填好 API Key")
        self.pet_window.speak(
            f"我还没拿到钥匙，右键点我打开设置，把 {provider.name} 的 API Key 填上，我才能听懂你。"
        )
