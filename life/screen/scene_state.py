"""场景状态机（v2-scene-state）：工作 / 电影 / 游戏三态互斥，宠物坐起来陪着。

用户要的是"宠物看得出我这时候在干嘛，然后**换一副样子**陪着"：不是在干活时溜达、
趴着睡，而是坐起来（坐姿开心那张立绘）待在旁边，开场说一句它在陪你做什么。

这一层**不新增任何采样**：它只做两件事——

1. **给出"前台算不算工作页"这个判据**（模块级 `is_work_page`）：前台是网课 / 创作 /
   工作类软件，或者浏览器标题判成 focus（`browser.TITLE_FOCUS`，含用户在设置页填的
   「工作站点词」）。判据跟自动番茄钟 `focus_timer.is_focus_proc` **共用同一份**，
   不另立一套口径——两处不一致的话，会出现"番茄钟认得出、宠物认不出"这种自相矛盾
   的样子。递出去的是**回调**不是布尔值（`SceneArbiter(is_working=...)`）：前台是一条
   信号派发给所有线的，排在后面的线在派发半途被问到时会拿到上一拍的标记，真机上就是
   "从工作页切走那一拍先冒了句「我陪你一起工作」"（见 `scene_guard` 类开头那段）。
2. **听仲裁点说这会儿是哪一场**（`guard.changed`），据此说开场白 / 排问句；前台换页时
   喊一声 `guard.notify_work_changed()` 让它重算。

三态互斥**不是这一层自己实现的**，而是"只有一个仲裁点"这件事本身带来的：`active`
同一时刻只会返回一档，宠物的样子跟着它走，天然不会叠加。优先级沿用仲裁点那份
专注 > 游戏 > 观影 > 工作 > 休闲（见 `scene_guard.PRIORITY`）。

各档进入时的表现：

- **工作**：开场飘「我陪你一起工作」，过一会儿（`ASK_DELAY_MS`，等开场白那条气泡说完）
  再问一句「我可以帮你整理笔记，需要吗？」并摆出 [好呀] [不用了]。点头 → 这一场
  允许抓屏笔记随时整理（见 `notes.ScreenNotesService` 的 `work_ok`）；不点 → 装配层
  把宠物收起来。
- **电影**：开场飘「我陪你看电影」（v2-scene-state-fix：不再复用看片线那句「主人，看
  电影啦」——用户要的原话是「我陪你看电影」，看片线的开场陪伴字幕是另一条线的活）。
  后续随情绪的飘词照旧走 mood 那条线。
- **游戏**：开场飘「我陪你一起玩游戏啦」；"问攻略"那句不在这里——游戏攻略本来就会问
  （`game.GameGuideService`，自带 [好呀] [不用了]，v2-scene-state 只把"过一会"落成
  `screen.guide_ask_delay_sec`，默认 30 秒），这里再加一句就是同一件事问两遍。
- **专注**：什么都不冒（专注要的是安静，v2-focus-quiet），宠物连姿态都放掉（悠闲态）；
  进专注时装配层还会把头顶残留的情绪状态词**立刻**收掉（v2-scene-state 补的那道缺口）。
- **社交**（v2-social-scene-boundary）：什么都不冒。这一档的活是**让位**——置顶的是
  聊天窗口时，上面那三句开场词一律不许飘，话头留给社交那条线自己的「要帮你措辞吗」。
- **悠闲**：自由——侧身漫步 / 趴卧睡觉 / 跳跃兴奋 / 歪头疑惑都由 `BehaviorScheduler` 挑。

红线：只看前台进程名与浏览器标题判类（标题那条是既有行为，只做网课/创作/工作分类），
**不读屏、不读标题正文**；不落盘、不写 pet.db / mind.db；零新依赖。
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from core.config import Config
from core.events import EventBus
from life.psych import crisis
from life.screen.browser import TITLE_FOCUS
from life.screen.focus_timer import is_focus_proc
# 点头/摇头的口径跟抓屏笔记、游戏攻略、社交回复**共用同一张表**，不另立一份——
# 同一个按钮在几条线上该判出同一个结果。
from life.screen.notes import YES_WORDS
from life.screen.scene_guard import (
    KIND_FOCUS,
    KIND_GAME,
    KIND_IDLE,
    KIND_SOCIAL,
    KIND_WATCH,
    KIND_WORK,
)

logger = logging.getLogger(__name__)

# 开场那句（用户给的原文，别改）。走 `pet_speak_proactive`（只显示不出声，跟观影陪伴
# 那条线一个通道）。
OPENING_WORK = "我陪你一起工作"
OPENING_GAME = "我陪你一起玩游戏啦"
OPENING_WATCH = "我陪你看电影"

# 工作态那问句。**先问再给**：没点头之前不抓屏、不整理（跟抓屏笔记那条线同款纪律）。
ASK_NOTES = "我可以帮你整理笔记，需要吗？"

# 开场白说完再问（毫秒）。浮动气泡里一句话停 9 秒（`ui/bubble.SHOW_MS`），
# 同一块气泡上再挤一句就等于把开场白吞了——所以等它说完。
ASK_DELAY_MS = 10_000


def is_work_page(monitor) -> bool:
    """这会儿前台算不算工作/网课/创作页（判据与自动番茄钟 `is_focus_proc` 同一份）。

    写成**模块级函数**而不是服务的私有方法：仲裁点那一档要的是"问的时候现算"
    （见 `scene_guard.SceneArbiter` 类开头那段——推过去的布尔值会在信号派发半途陈旧），
    而装配层要在场景状态机之外把这个回调递给仲裁点。
    """
    if monitor is None:
        return False
    proc = getattr(monitor, "foreground_proc", "") or ""
    if is_focus_proc(proc):
        return True
    # 网页版网课 / 在线创作 / 办公系统只有标题分得出来（进程名都是浏览器那一个）。
    # 标题判成 watch（在看剧）的不算工作页——那是电影态。
    return (getattr(monitor, "title_kind", "") or "") == TITLE_FOCUS


class SceneStateService(QObject):
    """把"这会儿是哪一场"翻译成宠物该有的样子。"""

    changed = Signal(str)      # 场景态变了（KIND_*），装配层据此摆姿态 / 显隐窗口
    # "该露轻按钮了"**不在这里另开一条信号**：按钮是界面层的事，笔记 / 攻略 / 社交几条线
    # 都从总线同一条出口（`bus.scene_offer`）走，这里跟着走免得又多一个接口。
    declined = Signal(str)     # 用户点了「不用了」：这一场先别陪着（装配层收宠物）

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        guard,
        monitor=None,
        ask_delay_ms: int = ASK_DELAY_MS,
        can_offer: Callable[[], bool] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._guard = guard
        self._monitor = monitor
        self._ask_delay_ms = max(0, int(ask_delay_ms))
        # 气泡上只有一排按钮：别的线正等着用户点按钮（社交建卡询问 / 笔记 / 攻略）时，
        # 工作态这句「整理笔记」就不插嘴——不然会把别人正问着的按钮顶掉。
        self._can_offer = can_offer if can_offer is not None else (lambda: True)
        # 只存内存：这一场是哪一档、正等不等答复、用户点头了没有
        self._kind = KIND_IDLE
        self._awaiting = False
        self._accepted = False
        # 开场白说完再问那一手：进工作态时排上，切走了就作废（新一场重新排）。
        self._ask_timer = QTimer(self)
        self._ask_timer.setSingleShot(True)
        self._ask_timer.timeout.connect(self._ask_notes)

    # ---------- 接线 ----------

    def attach(self, monitor) -> None:
        """挂到采样器上：前台换人 / 浏览器换标签页都重判一次工作页。"""
        self._monitor = monitor
        monitor.foreground_changed.connect(self.on_foreground)
        monitor.title_changed.connect(self.on_title)
        if self._guard is not None:
            self._guard.changed.connect(self.on_kind)
            self._guard.notify_work_changed()      # 先让仲裁点算一次（初始那一页）

    def on_foreground(self, _proc: str = "") -> None:
        """前台换人了：重判工作页（换个标签页也算，浏览器标题那条线管它）。"""
        self.note_page()

    def on_title(self, _kind: str = "") -> None:
        self.note_page()

    def note_page(self) -> None:
        """前台换页了：让仲裁点重算一次（判据见模块级 `is_work_page`）。"""
        if self._guard is None:
            return
        self._guard.notify_work_changed()

    def _is_work_page(self) -> bool:
        """这会儿前台算不算工作/网课/创作页（判据与自动番茄钟同一份）。"""
        return is_work_page(self._monitor)

    # ---------- 状态 ----------

    @property
    def state(self) -> str:
        """这会儿是哪一场（`KIND_*`）。仲裁点说什么就是什么，这一层不另算一套。"""
        return self._kind

    @property
    def awaiting(self) -> bool:
        """正等着用户点那排轻按钮。"""
        return self._awaiting

    @property
    def accepted(self) -> bool:
        """这一场用户点过「好呀」。"""
        return self._accepted

    def notes_allowed(self) -> bool:
        """这一场允不允许抓屏笔记随时整理（工作页 + 用户点过「好呀」）。

        只做"这一场点过头了没有"这一件事，**判据仍是工作页**（`state == KIND_WORK`）：
        用户在刷剧 / 闲逛时点头也不会让它去抓——「只在网课/工作时抓」这条纪律不松。
        """
        return self._kind == KIND_WORK and self._accepted

    def on_kind(self, kind: str) -> None:
        """仲裁点说"这会儿换成这一档了"。切进新的一档就重新摆一遍样子。"""
        if kind == self._kind:
            return
        self._kind = kind
        self._reset_scene()
        if self._enabled:
            self._enter(kind)
        logger.info("场景态：%s", kind)
        self.changed.emit(kind)

    def _reset_scene(self) -> None:
        """换了一场：开场白、问句、点头这些**都是按场算的**，一律作废重来。"""
        self._ask_timer.stop()
        self._awaiting = False
        self._accepted = False

    @property
    def _enabled(self) -> bool:
        return bool(getattr(self._config.screen, "enabled", True))

    def _enter(self, kind: str) -> None:
        """进这一档要做的事（说话、排问句）。要摆的样子由装配层按 `changed` 做。"""
        if kind == KIND_WORK:
            self._speak(OPENING_WORK)
            if self._ask_delay_ms <= 0:
                self._ask_notes()
            else:
                self._ask_timer.start(self._ask_delay_ms)
        elif kind == KIND_GAME:
            self._speak(OPENING_GAME)
        elif kind == KIND_WATCH:
            self._speak(OPENING_WATCH)
        # 游戏不在这里问攻略：那件事 game.GameGuideService 在做，再冒一遍就是同一件事
        # 问两遍，见模块 docstring。专注与悠闲什么都不冒。
        #
        # **社交（v2-social-scene-boundary）也什么都不冒**：这一档的活是"让位"——置顶的是
        # 聊天窗口时，场景开场词（我陪你看电影 / 我陪你一起玩游戏啦 / 我陪你一起工作）一律
        # 不许飘，把话头留给社交那条线（它自己冒"要帮你措辞吗"）。用户拍板的硬规则：
        # 只要置顶的是聊天窗口，就优先处理社交回复。这一档进得来、说得出口的只有"让位"
        # 这一件事，所以这里真的一行都不发。

    def _speak(self, text: str) -> None:
        """冒一句（只显示不出声）。**红线不跳步**：写死的句子也过一遍危机词。"""
        if crisis.is_crisis(text):
            logger.warning("场景态台词 %r 命中危机词，跳过", text)
            return
        self._bus.pet_speak_proactive.emit(text)

    # ---------- 工作态那一问 ----------

    def _ask_notes(self) -> None:
        """开场白说完之后问一句"要不要我帮你整理笔记"，并摆出轻按钮。"""
        if self._kind != KIND_WORK or self._awaiting or self._accepted:
            return
        if not self._can_offer():
            return
        if crisis.is_crisis(ASK_NOTES):
            logger.warning("工作态问句命中危机词，跳过")
            return
        self._awaiting = True
        logger.debug("工作态：问一句要不要整理笔记")
        self._bus.pet_speak.emit(ASK_NOTES)
        self._bus.scene_offer.emit(KIND_WORK)

    def answer(self, reply: str) -> bool:
        """用户点了轻按钮。返回这次是不是这一层接下了。

        「好呀」→ 这一场允许随时整理笔记（`notes_allowed`），装配层顺手抓一拍；
        别的（「不用了」）→ 这一场先别陪着，装配层把宠物收起来。
        """
        if not self._awaiting:
            return False
        self._awaiting = False
        kind = self._kind
        if self._is_yes(reply):
            self._accepted = True
            logger.debug("场景态：用户点头，这一场随时整理笔记")
            return True
        logger.debug("场景态：用户摇头，这一场先收起来")
        self.declined.emit(kind)
        return True

    @staticmethod
    def _is_yes(reply: str) -> bool:
        text = (reply or "").strip()
        return any(word in text for word in YES_WORDS)


__all__ = [
    "ASK_DELAY_MS", "ASK_NOTES", "OPENING_GAME", "OPENING_WATCH", "OPENING_WORK",
    "SceneStateService", "is_work_page",
]
