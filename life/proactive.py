"""
主动搭话（对应"聊天分主动/被动"：被动是双击找它聊，主动是它自己开口）。

每 15 分钟看一次值不值得开口；先把硬条件过一遍，再让模型判断要不要说、说什么。
硬条件（任一不满足就安静待着）：
- 配了 API Key（没钥匙就没法判断说什么）；
- 没落在免打扰时段、不在番茄钟专注中；
- "专注提醒"开关是开着的（关掉等于连主动开口一起关掉）；
- 用户确实在电脑前（前台监控里有人在用，人不在就别自言自语）；
- 距离上次主动开口够久、距离用户上次跟它说话也够久；
- 今天主动开口的次数还没到上限。

判断"说什么"用 brain/chains/proactive.py 的链，这里只管时机与门控。

说出去走的是 `bus.pet_speak_proactive`（跟回用户话的 `bus.pet_speak` 分开）：这条线上的话
是宠物**自己**想说的，出口处会先冒文字、要不要念出来再问用户一句（v2-proactive-voice）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from brain.chains.proactive import ProactiveChain, is_silent
from core.config import Config, in_quiet_hours
from core.events import EventBus
from core.reltime import memory_text
from core.worker import run_async
from data.dao import MemoryDAO

logger = logging.getLogger(__name__)

CHECK_INTERVAL_MS = 15 * 60 * 1000          # 每 15 分钟看一次
MIN_GAP_SEC = 45 * 60                       # 两次主动开口至少隔这么久
MIN_QUIET_AFTER_CHAT_SEC = 10 * 60          # 用户刚跟它说过话，先别插嘴
MAX_PER_DAY = 6                             # 一天最多主动开口几次
MEMORY_LIMIT = 8                            # 交给模型的最近记忆条数
REPLY_WINDOW_SEC = 10 * 60                  # 主动开口后多久内接的话算"被回应"（B 养成）


class ProactiveService(QObject):
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        memory_dao: MemoryDAO,
        chain: ProactiveChain,
        activity_summary: Callable[[], str],
        is_user_present: Callable[[], bool],
        is_focusing: Callable[[], bool],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._memory_dao = memory_dao
        self._chain = chain
        self._activity_summary = activity_summary
        self._is_user_present = is_user_present
        self._is_focusing = is_focusing
        # "主动开口被回应"的记账出口。后接的（见 set_replied_handler）：
        # 好感度那套比这个服务晚建起来，而且它可能压根没开
        self._on_replied: Callable[[], None] | None = None

        # 用 -inf 表示"从来没有过"：time.monotonic() 是开机到现在的秒数，
        # 若拿 0.0 当"很久以前"，在开机不满 MIN_GAP_SEC（45 分钟）的机器上会把主动搭话一直挡住
        self._last_spoken = float("-inf")
        self._last_interaction = float("-inf")
        self._count_date: object = None
        self._count_today = 0
        # 情绪那条线排进来的一句话（重情绪关怀，指令 26-A）：下个检查点优先说出去
        self._care_pending = ""
        # 情感记忆那条线排进来的一句话（S5）：同样在下个检查点说，但门控按普通主动开口算
        self._mind_pending = ""
        # 主动开了口、还等着人接话（B 养成"被回应"记账用）。放时间戳而不是布尔：
        # 隔了三小时才回来搭话，那不算"回应了你刚才那句"。
        self._awaiting_reply_at: float = float("-inf")
        # 常驻节流。默认就是上面那两个常量，升级后由 set_pace 调紧/放松
        self._min_gap_sec = MIN_GAP_SEC
        self._max_per_day = MAX_PER_DAY

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check)

    # ---------- 对外 ----------

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def touch(self) -> None:
        """用户跟它说话或者摸了摸它：先安静一会儿，别急着插嘴。

        顺带结一次"主动开口被回应"的账（B 养成）：上一次是它先开的口，
        这一下是人接的话/摸的头，那就算数。超过 REPLY_WINDOW_SEC 才来的不算——
        隔了三小时才想起它，跟"接住了刚才那句"是两回事。
        """
        now = time.monotonic()
        if now - self._awaiting_reply_at <= REPLY_WINDOW_SEC:
            self._awaiting_reply_at = float("-inf")
            self._notify_replied()
        self._last_interaction = now

    def set_replied_handler(self, handler: Callable[[], None] | None) -> None:
        """接上"主动开口被人接住了"的回调（B 养成记一笔好感）。不接就是纯不记。"""
        self._on_replied = handler

    def set_pace(self, min_gap_sec: int | None = None,
                 max_per_day: int | None = None) -> None:
        """改主动搭话的节流（好感度升级的福利，见 life/mind/intimacy.py）。

        传 None 就是那一项不动——调用方只想改一样的时候不用把另一样也报出来。
        节流调松了立即生效，不用重启。
        """
        if min_gap_sec is not None:
            self._min_gap_sec = max(int(min_gap_sec), 0)
        if max_per_day is not None:
            self._max_per_day = max(int(max_per_day), 0)

    def queue_care(self, text: str) -> None:
        """情绪关怀那条线排下来一句话（重情绪关怀，指令 26-A）：下个检查点说出去。

        和主动搭话共用同一套门控与通道：免打扰、专注中、人不在、开口太频繁，
        这些条件在这儿同样成立，所以不另起一套。
        """
        self._care_pending = text

    def queue_mind(self, text: str) -> None:
        """情感记忆那条线排下来一句话（S5）：下个检查点说出去。

        跟情绪关怀共用同一个出口和同一套门控，但**不享有关怀那条的豁免**——
        它属于锦上添花的搭话，得老实受"专注提醒"开关和 API Key 管。
        关怀那条能豁免，是因为"你连着崩了两回"不该被一个提醒开关一起关掉。
        """
        self._mind_pending = text

    # ---------- 内部 ----------

    def _check(self) -> None:
        now_dt = datetime.now()
        care = bool(self._care_pending)
        mind = bool(self._mind_pending) and not care      # 关怀优先，一次只说一件事
        if not self._can_speak(now_dt, care=care):
            return
        # 先把账记上再开口：一次检查只可能开口一次
        self._last_spoken = time.monotonic()
        self._count_today += 1
        if care:
            # 关怀话术是本地挑好的，不经模型，也不走"模型说不用开口"那条判断
            text, self._care_pending = self._care_pending, ""
            logger.info("重情绪关怀：%s", text)
            self._awaiting_reply_at = time.monotonic()
            self._bus.pet_speak_proactive.emit(text)
            return
        if mind:
            text, self._mind_pending = self._mind_pending, ""
            logger.info("情感记忆主动搭话：%s", text)
            self._awaiting_reply_at = time.monotonic()
            self._bus.pet_speak_proactive.emit(text)
            return
        run_async(
            self._compose,
            on_done=self._speak,
            on_failed=lambda exc: logger.error("主动搭话生成失败: %s", exc),
        )

    def _compose(self) -> str:
        # 记忆正文里可能留着"明早"这类相对说法，按现在重新说一遍再交给模型
        now = datetime.now()
        memories = [
            memory_text(memory, now)
            for memory in self._memory_dao.recent(limit=MEMORY_LIMIT)
        ]
        return self._chain.compose(self._activity_summary(), memories)

    def _speak(self, text: str) -> None:
        if is_silent(text):
            logger.info("主动搭话：模型判断现在不用开口")
            return
        logger.info("主动搭话：%s", text)
        self._awaiting_reply_at = time.monotonic()
        self._bus.pet_speak_proactive.emit(text)

    def _notify_replied(self) -> None:
        """"它开了口、人接了话"这件事说给外面听（B 养成记一笔好感）。

        不能让它抛：这条路上正走在 touch 里，用户刚点了一下宠物，
        记账出岔子不该把这次交互本身弄坏。
        """
        if self._on_replied is None:
            return
        try:
            self._on_replied()
        except Exception as exc:  # noqa: BLE001 —— 见上
            logger.error("好感度记账没成（%s），这次交互照常", exc)

    def _can_speak(self, now_dt: datetime, care: bool = False) -> bool:
        """现在能不能开口。

        care=True 是情绪关怀那条线（指令 26-A）：它不受"专注提醒"开关与 API Key 管——
        那个开关是活动提醒的总闸，不该把"你连着崩了两回"也一起关掉；关怀话术是本地挑的，
        没钥匙也说得出来。情感记忆那条线（S5）借同一个出口，但走 care=False 这套，
        豁免只给关怀。其余门控（免打扰、专注中、人不在、间隔、每日上限）三条线一样守。
        """
        if not care:
            if not self._config.activity.remind:
                return False
            if not self._config.get_api_key():
                return False
        if self._is_focusing():
            return False
        if in_quiet_hours(self._config.schedule.quiet_hours, now_dt):
            return False
        if not self._is_user_present():
            return False
        now = time.monotonic()
        if now - self._last_spoken < self._min_gap_sec:
            return False
        if now - self._last_interaction < MIN_QUIET_AFTER_CHAT_SEC:
            return False
        if now_dt.date() != self._count_date:   # 跨天：次数重新开始数
            self._count_date = now_dt.date()
            self._count_today = 0
        return self._count_today < self._max_per_day
