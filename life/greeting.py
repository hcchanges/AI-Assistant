"""
场景化主动问候（对应指令 4 第 4 节）。

让宠物按时间与情境主动开口，而不是只在被叫的时候才说话：
- 开机问候（按当时的时段挑一句）；
- 早晨 / 午休 / 深夜 三个时段，各问候一次；
- 久未互动：超过 90 分钟没人理，轻轻念一句（最多每 3 小时一次）。

台词都是本地模板按性格填出来的（自称、对用户称呼、动作描写、颜文字），
不调用模型，所以没填 API Key 也能用；免打扰时段和番茄钟专注中一律不开口。

暂未接入的两个触发点：天气变化（需要外部天气源）、久坐加班（等指令 5/6 的前台监控）。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, time as dtime
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from core.config import Config, in_quiet_hours
from core.events import EventBus
from pet.persona import PersonaVoice, resolve_voice

logger = logging.getLogger(__name__)

CHECK_INTERVAL_MS = 60 * 1000        # 每分钟看一次，开销可以忽略
IDLE_NUDGE_SEC = 90 * 60             # 超过这么久没互动算"久未互动"
IDLE_NUDGE_COOLDOWN_SEC = 3 * 3600   # 同一条提醒至少隔这么久

# 时段：(名字, 起始, 结束, ((动作, 台词), ...))
SLOTS = (
    ("morning", dtime(5, 0), dtime(10, 30), (
        ("伸了个懒腰", "{call}早呀，今天也慢慢来就好"),
        ("揉揉眼睛", "天亮啦，{call}想做点什么呀"),
    )),
    ("noon", dtime(11, 30), dtime(13, 30), (
        ("舔舔爪子", "{call}，到饭点啦，别忘了吃饭"),
        ("尾巴扫过桌面", "中午了{call}，先歇一会儿再忙吧"),
    )),
    ("night", dtime(22, 0), dtime(5, 0), (
        ("打了个哈欠", "这么晚还没睡呀{call}，早点休息"),
        ("趴在你手边", "夜深啦{call}，事情明天再做也来得及"),
    )),
)

# 不在上面的时段里（开机问候用）
DAY_LINES = (
    ("歪头看着你", "{call}，我在这儿陪着你"),
    ("甩了甩尾巴", "我又来陪你啦，{call}"),
)

IDLE_LINES = (
    ("尾巴扫过手背", "{call}忙了很久啦，起来动一动吧"),
    ("凑近看了看你", "歇会儿吧{call}，天塌了也有我顶着"),
)

KAOMOJI = ("(=^･ω･^=)", "(>ω<)", "(=･ω･=)")


class GreetingService(QObject):
    """按时间与情境主动开口的服务。"""

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        is_focusing: Callable[[], bool],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._is_focusing = is_focusing

        self._greeted: tuple[str, object] | None = None   # (时段名, 日期)，保证一天只问一次
        self._last_interaction = time.monotonic()
        self._last_nudge = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self._check)

    # ---------- 对外 ----------

    def start(self) -> None:
        """开机问候 + 开始定时检查。"""
        self._timer.start()
        if self._is_focusing():
            return
        provider = self._config.llm.active_provider
        if not self._config.get_api_key(provider):
            # 还没填钥匙，这个时刻留给"怎么填钥匙"的提示，别抢话
            return
        slot = self._slot_of(datetime.now())
        self._greet(slot or "day", startup=True)

    def touch(self) -> None:
        """用户和宠物互动了，重置"久未互动"的计时。"""
        self._last_interaction = time.monotonic()

    # ---------- 内部 ----------

    def _check(self) -> None:
        now = datetime.now()
        if self._is_focusing() or self._is_quiet(now):
            return
        slot = self._slot_of(now)
        if slot is not None and self._greeted != (slot, now.date()):
            self._greet(slot)
            return
        self._nudge_if_idle()

    def _greet(self, slot_name: str, startup: bool = False) -> None:
        voice = resolve_voice(self._config.pet)
        lines = next((s[3] for s in SLOTS if s[0] == slot_name), DAY_LINES)
        action, body = random.choice(lines)
        self._speak(action, body, voice)
        self._greeted = (slot_name, datetime.now().date())
        if startup:
            logger.info("开机问候：%s", slot_name)

    def _nudge_if_idle(self) -> None:
        now = time.monotonic()
        if now - self._last_interaction < IDLE_NUDGE_SEC:
            return
        if now - self._last_nudge < IDLE_NUDGE_COOLDOWN_SEC:
            return
        self._last_nudge = now
        action, body = random.choice(IDLE_LINES)
        self._speak(action, body, resolve_voice(self._config.pet))

    def _speak(self, action: str, body: str, voice: PersonaVoice) -> None:
        text = body.format(call=voice.user_call)
        if voice.actions:
            text = f"（{action}）{text}"
        if voice.kaomoji:
            text = f"{text} {random.choice(KAOMOJI)}"
        # 走**主动**那条线（v2-speech-polish ①）：开机问候 / 久未互动都是它自己起的话头，
        # 用户此刻一个字都没说，所以出声前得先问一句"要我念出来吗"（走 pet_speak_proactive，
        # 由 Bootstrap._on_pet_speak_proactive 统一问）。原来发的是 pet_speak——那条线是
        # "回用户话"的口子，直接出声，于是每次打开宠物它都自己先念上了（用户真机反馈）。
        # 颜文字照旧拼进正文：UI 上要显示那只猫的脸，剥掉是语音合成那一头的事（见 tts.py）。
        self._bus.pet_speak_proactive.emit(text)

    @staticmethod
    def _slot_of(now: datetime) -> str | None:
        """当前时刻落在哪个问候时段（返回时段名）；不在任何时段里返回 None。"""
        current = now.time()
        for name, start, end, _ in SLOTS:
            if start <= end:
                if start <= current < end:
                    return name
            elif current >= start or current < end:
                return name          # 跨零点的深夜段
        return None

    def _is_quiet(self, now: datetime) -> bool:
        return in_quiet_hours(self._config.schedule.quiet_hours, now)
