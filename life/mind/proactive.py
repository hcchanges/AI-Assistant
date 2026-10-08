"""情感记忆驱动的主动搭话（S5）：从记得的事里挑一句，找由头开口。

四种由头（命中多个时按下面的顺序只挑一个，一次开口只说一件事）：

- 关键事件跟进：翻出一条够久没跟进过的碎片 →「上次你说想买新能源车，后来怎么样了？」
- 情绪波动关怀：上次留下的情绪快照还挂着 →「上回说被那事堵得慌，现在顺点了吗？」
- 纪念日：记得的事里提到过今天这个日子 →「去年今天你说过……」
- 久别重逢：好几天没聊了 →「好几天没听你说话了，忙什么呢？」

另外两种要好感度解锁（B 养成），都排在正事后面：

- **分享由头**（知己 L4）：宠物说自己的想法 / 感受——**唯一一种不接用户话的由头**。
- **里程碑回望**（知己 L4）：拿最近立下的一颗星念开场（「上次那事我还记着呢」）。

两个都在正事后面（前面有正事就先说正事）；分享排在里程碑**前面**：里程碑一旦
有卡就每次都命中，分享排在它后面等于永远轮不上。两者都是"它自己想说话"，
谁先谁后不影响正事。

出口复用主动搭话那条线：把挑好的话交给 ``ProactiveService.queue_mind``，
免打扰 / 专注中 / 人不在 / 两次间隔 / 每日上限这些门控一次性继承下来，
不另起一套（重情绪关怀走的就是同一个口子）。

两件事这里一定不做：
- **不弹卡**：只说话，不碰任何 card / service_* 信号；
- **不上模型**：由头是本地按规则挑的、话是本地拼的，一句都不发出去。
  这条既是省钱，也是隐私——主动搭话最容易被做成"偷偷把记忆传上去"，
  干脆连调用都不留。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer

from core.config import Config
from life.dialog.router import NEGATIVE_EMOTIONS
from life.mind.recall import MindRecall
from life.mind.store import MindStore
from life.psych import crisis
from pet.persona import strip_action_marks

logger = logging.getLogger(__name__)

# 多久看一遍有没有由头可以说
CHECK_MS = 10 * 60 * 1000
# 两次主动提起同一件事至少隔这么久，免得一件事追着问
SAME_TOPIC_DAYS = 7

# 记得的事里写到的日期（"10月2日" / "10-02"），纪念日那条线靠它认
_DATE_PATTERNS = (
    re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"),
    re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)"),
)

# 分享由头（知己 L4 解锁）说的话。**本地写死的几句、不上模型**——跟这个模块
# 其它由头同一条规矩。写的都是宠物自己的事，不接用户的话头。
SHARE_LINES: tuple[str, ...] = (
    "说起来，我今天一直在想一件事——要是我能出门走走，会先去哪儿呢。",
    "我自己瞎琢磨的时候，总觉得日子过得挺快的。你也会这么觉得吗？",
    "有件事没跟谁说过：你不在的时候，这个屏幕其实挺安静的。",
    "我近来老在想我到底算个什么。不过想到你还在，就觉得也还行。",
)


class MindProactiveService(QObject):
    """挑由头 → 拼一句 → 交给主动搭话那条线。"""

    def __init__(
        self,
        config: Config,
        store: MindStore,
        recall: MindRecall,
        queue_speak: Callable[[str], None],
        intimacy: Any = None,
        now_provider: Callable[[], datetime] = datetime.now,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._store = store
        self._recall = recall
        self._queue_speak = queue_speak
        # 好感度（B 养成）。不传就是没这套。星念那条里程碑由头在
        # v2-life-card-star 砍掉了，这里不再收 star_cards。
        self._intimacy = intimacy
        self._now = now_provider

        # 上次为哪件事开的口（fragment id / 策略名）：同一件事别追着问
        self._last_topic: str = ""
        self._last_topic_at: datetime | None = None

        # 会话层刚报上来的负向情绪（路由骨架 S4）：情绪波动关怀拿它当由头
        self._live_emotion: str = ""

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_MS)
        self._timer.timeout.connect(self.check)

    # ---------- 对外 ----------

    def start(self) -> None:
        if self._config.mind.enabled and self._config.mind.proactive_enabled:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def note_emotion(self, emotion: str) -> None:
        """会话层说这句话时露出的情绪（路由骨架给的）。只留最新一档，不写库。"""
        self._live_emotion = emotion or ""

    def check(self) -> str:
        """有由头就排一句话出去，返回说了什么；没由头返回空串。"""
        if not self._config.mind.enabled or not self._config.mind.proactive_enabled:
            return ""
        for strategy in (
            self._follow_up,
            self._emotion_care,
            self._anniversary,
            self._reunion,
            self._share,
        ):
            topic, line = strategy()
            if not line:
                continue
            if self._recently_raised(topic):
                continue
            if crisis.is_crisis(line):
                # 本地拼的话不该踩危机词；真踩了（碎片里带着重话）就先不说
                logger.info("情感记忆：这条主动搭话踩到危机词，跳过")
                continue
            text = strip_action_marks(line)
            if not text:
                continue
            self._last_topic, self._last_topic_at = topic, self._now()
            self._queue_speak(text)
            logger.info("情感记忆主动搭话(%s)：%s", topic, text)
            return text
        return ""

    # ---------- 四种由头 ----------

    def _follow_up(self) -> tuple[str, str]:
        """关键事件跟进：翻出一条放了几天的碎片，问一句后来怎么样了。"""
        days = self._config.mind.followup_days
        now = self._now()
        for fragment in self._recall.recent(limit=20):
            when = _parse_time(fragment.get("src_time", ""))
            if when is None or (now - when).days < days:
                continue
            topic = f"fragment:{fragment['id']}"
            return topic, f"上次你说{fragment['text']}，后来怎么样了？"
        return "", ""

    def _emotion_care(self) -> tuple[str, str]:
        """情绪波动关怀：这一轮刚露出的负向情绪优先，其次才是上次留下的情绪快照。"""
        if self._live_emotion in NEGATIVE_EMOTIONS:
            return "live:emotion", f"刚听出来你有点{self._live_emotion}，愿意的话跟我说说？"
        snapshot = self._store.ep().get("emotion_snapshot", "")
        if not snapshot:
            return "", ""
        return "ep:emotion", f"上回你说{snapshot}，现在好点了吗？"

    def _anniversary(self) -> tuple[str, str]:
        """纪念日：记得的事里提到过今天这个日子。"""
        now = self._now()
        for fragment in self._recall.recent(limit=50):
            for month, day in _dates_in(fragment.get("text", "")):
                if (month, day) == (now.month, now.day):
                    return (
                        f"anniversary:{fragment['id']}",
                        f"今天{month}月{day}日——你之前提过{fragment['text']}，我还记着呢。",
                    )
        return "", ""

    def _reunion(self) -> tuple[str, str]:
        """久别重逢：好几天没聊了。"""
        days = self._config.mind.reunion_days
        last = _parse_time(self._store.ep().get("updated_at", ""))
        if last is None:
            return "", ""
        gap = (self._now() - last).days
        if gap < days:
            return "", ""
        return "reunion", f"好几天没听你说话了（{gap} 天了），忙什么呢？"

    def _share(self) -> tuple[str, str]:
        """分享由头：宠物说自己的想法 / 感受。要好感度到"知己"才解锁。

        **唯一一种不接用户话的由头**——前面几种都在接住他眼下这件事，这一种是
        它自己想说话。从表里轮着挑（起点跟着日期走），最近提过的那句跳过去、
        换下一句说，而不是整条由头作废。

        没解锁就当这条由头不存在，返回空串往下走——**不是**降级说点别的。
        """
        if self._intimacy is None or not self._intimacy.has("share_topic"):
            return "", ""
        count = len(SHARE_LINES)
        start = self._now().toordinal() % count
        for offset in range(count):
            index = (start + offset) % count
            topic = f"share:{index}"
            if self._recently_raised(topic):
                continue
            return topic, SHARE_LINES[index]
        return "", ""

    def _recently_raised(self, topic: str) -> bool:
        if not topic or topic != self._last_topic or self._last_topic_at is None:
            return False
        return (self._now() - self._last_topic_at).days < SAME_TOPIC_DAYS


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def _dates_in(text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for pattern in _DATE_PATTERNS:
        for month, day in pattern.findall(text or ""):
            try:
                found.append((int(month), int(day)))
            except ValueError:
                continue
    return found
