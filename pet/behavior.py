"""
行为库：猫咪的悠闲姿态调度（对应指令 2，按指令 7 修正二调整了默认表现）。

只收"有姿势图、看得出差别"的 8 个姿态；缺图的 5 个（舔毛 / 伸懒腰 / 打哈欠 / 甩尾巴 / 踩奶）
先不进库——没有对应图只能靠轻微形变假装，做出来和"发呆"几乎一样，等美术图到位再加。

默认节奏（用户选定的"安静型"）：
- 一个动作演完，静止 20~60 秒再演下一个；
- 坐下 / 趴下 / 打盹 / 发呆是主力（原地不动，不干扰你看屏幕）；
- 溜达 / 蹦跳 / 追尾巴 / 扑空会挪位置，演完要冷却 4~8 分钟，实际大约 5~10 分钟才动一次，一次只走一小段；
- 深夜（23:00~06:00）会动的动作权重压到 0.3，打盹 / 趴着提到 1.6；
- 同一动作不连着演两次；
- 番茄钟专注中只演安静动作，且不做位移。

本模块只管"什么时候演哪个"（发信号），怎么演交给 ui/pet_window.py。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

from pet.persona import preset_of

logger = logging.getLogger(__name__)

# 深夜时段（跨零点）：这段时间猫咪更安静
NIGHT_FROM = 23
NIGHT_TO = 6
NIGHT_MOVING_FACTOR = 0.3    # 深夜会动的动作权重
NIGHT_REST_FACTOR = 1.6      # 深夜打盹 / 趴着的权重


class Behavior(Enum):
    """悠闲姿态。取值同时是性格配置里 behavior 字典的键。"""

    WANDER = "wander"            # 溜达
    HOP = "hop"                 # 蹦跳
    SIT = "sit"                 # 坐下
    LIE = "lie"                 # 趴下
    SLEEP = "sleep"             # 打盹
    LOOK = "look"               # 发呆
    CHASE_TAIL = "chase_tail"   # 追尾巴
    POUNCE = "pounce"           # 扑空


@dataclass(frozen=True)
class BehaviorMeta:
    duration: tuple[float, float]   # 持续时长范围（秒）
    weight: float                   # 基础权重（指令 2 第 2 节的表）
    quiet: bool                     # 是否"安静动作"（专注时可以演）
    moves: bool                     # 是否会挪位置


META: dict[Behavior, BehaviorMeta] = {
    Behavior.WANDER: BehaviorMeta((1.5, 3.0), 20, False, True),
    Behavior.HOP: BehaviorMeta((1.5, 3.0), 12, False, True),
    Behavior.SIT: BehaviorMeta((6.0, 14.0), 15, True, False),
    Behavior.LIE: BehaviorMeta((8.0, 16.0), 12, True, False),
    Behavior.SLEEP: BehaviorMeta((10.0, 30.0), 12, True, False),
    Behavior.LOOK: BehaviorMeta((4.0, 10.0), 8, True, False),
    Behavior.CHASE_TAIL: BehaviorMeta((2.0, 4.0), 6, False, True),
    Behavior.POUNCE: BehaviorMeta((1.0, 2.0), 5, False, True),
}

# 深夜要加权的"安静休息"动作
REST_BEHAVIORS = (Behavior.SLEEP, Behavior.LIE)


class BehaviorScheduler(QObject):
    """按权重挑悠闲姿态并管节奏（不碰界面）。"""

    behavior_started = Signal(object, int)   # (Behavior, 时长毫秒)
    behavior_finished = Signal(object)       # (Behavior)

    GAP_MS = (20_000, 60_000)            # 两个动作之间的静止时长
    FIRST_GAP_MS = (5_000, 20_000)       # 启动后第一个动作的等待
    AFTER_TOUCH_MS = 30_000              # 用户刚碰过宠物，先安静一会儿
    MOVING_COOLDOWN_MS = (4 * 60_000, 8 * 60_000)   # 会动的动作之间的冷却
    # 强制姿态的"时长"（v2-scene-state）：够长就好，到点会自己续上（见 _on_timeout）。
    # 取 10 分钟是因为一场工作/看片/游戏本来就可能坐很久，不需要在这一层设上限——
    # 真正的解除条件是"用户切走了页面"，由装配层喊 release()，不靠这个时长。
    FORCED_HOLD_MS = 10 * 60_000

    def __init__(self, persona_key: str = "", parent: QObject | None = None):
        super().__init__(parent)
        self._persona_key = persona_key
        self._quiet = False
        self._paused = True          # 装配完由 start() 打开
        self._current: Behavior | None = None
        self._last: Behavior | None = None
        self._moving_ready_at = 0.0
        self._forced: Behavior | None = None   # 被强制保持的姿态（场景态，v2-scene-state）

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)

    # ---------- 对外 ----------

    @property
    def current(self) -> Behavior | None:
        return self._current

    @property
    def forced(self) -> Behavior | None:
        """这会儿被强制保持的姿态（`None` = 没强制，走悠闲调度）。"""
        return self._forced

    def force(self, behavior: Behavior) -> None:
        """强制演某个姿态并**保持**（v2-scene-state 的"坐起陪着"）。

        跟 `set_quiet` 不是一回事：那个只是把"能挑的池子"缩小，宠物照样自己换姿势；
        这个把调度整个让开——`_play_next` 不再挑，`_on_timeout` 也不换，一直演这一个，
        直到装配层喊 `release()`。用户碰了宠物（`interrupt`）也不放：场景态里"坐起来
        陪着"是这一场的样子，不是一次动作。

        已经在强制同一个姿态时什么都不做（场景态重复进同一档不该打断/重演一次）。
        """
        if self._forced is behavior:
            return
        self._finish_current()
        self._forced = behavior
        self._emit_forced()

    def release(self) -> None:
        """解除强制，回到悠闲调度（用户切走了页面）。"""
        if self._forced is None:
            return
        self._forced = None
        self._finish_current()
        if not self._paused:
            self._schedule_gap(self.FIRST_GAP_MS)

    def _emit_forced(self) -> None:
        """把强制姿态发出去（进入时一次，之后每 `FORCED_HOLD_MS` 续一次）。"""
        behavior = self._forced
        if behavior is None:
            return
        self._current = behavior
        logger.debug("强制姿态: %s（保持，等装配层解除）", behavior.value)
        self.behavior_started.emit(behavior, self.FORCED_HOLD_MS)
        if not self._paused:
            self._timer.start(self.FORCED_HOLD_MS)

    def start(self) -> None:
        """装配完成后开始调度（一开始先安静一会儿再动）。"""
        self.set_paused(False)

    def set_paused(self, paused: bool) -> None:
        """暂停 / 恢复（说话中、被拖、腾空、隐藏时暂停；恢复后会重新排下一个动作）。"""
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        if paused:
            self._finish_current()
            self._timer.stop()
        elif self._forced is not None:
            self._emit_forced()          # 恢复显示：强制姿态接着保持
        else:
            self._schedule_gap(self.FIRST_GAP_MS)

    def set_quiet(self, quiet: bool) -> None:
        """专注模式：只演安静动作，正在挪位置的动作立刻停下。

        强制姿态（场景态坐起）期间整个让开：安静不安静的池子规则管不着"它这会儿
        就该坐着"这件事——专注已经把场景态打回 idle 了（装配层会喊 release），
        这两条路不会同时成立。
        """
        self._quiet = bool(quiet)
        if self._forced is not None:
            return
        if not self._quiet or self._current is None:
            return
        if not META[self._current].quiet:
            self._finish_current()
            self._schedule_gap(self.GAP_MS)

    def interrupt(self) -> None:
        """用户碰了宠物：立刻停下当前动作，安静一会儿再恢复。

        强制姿态期间只算"碰了一下"，不放掉姿态：场景态里"坐起来陪着"是这一场的
        样子，用户逗它一下不该把它变回趴着。
        """
        self._finish_current()
        if self._forced is not None:
            self._emit_forced()
            return
        if not self._paused:
            self._schedule_gap((self.AFTER_TOUCH_MS, self.AFTER_TOUCH_MS))

    def apply_persona(self, persona_key: str) -> None:
        """换性格：行为权重随之改变（下一次挑动作时生效）。"""
        self._persona_key = persona_key
        logger.info("行为权重跟着性格切到「%s」", persona_key or "默认")

    # ---------- 调度 ----------

    def _on_timeout(self) -> None:
        if self._forced is not None:
            self._emit_forced()          # 强制姿态到点了：接着保持（解除只认 release()）
            return
        if self._current is not None:
            self._finish_current()
            self._schedule_gap(self.GAP_MS)
        else:
            self._play_next()

    def _schedule_gap(self, gap_ms: tuple[int, int]) -> None:
        if self._paused:
            return
        self._timer.start(random.randint(int(gap_ms[0]), int(gap_ms[1])))

    def _finish_current(self) -> None:
        if self._current is None:
            return
        finished = self._current
        self._current = None
        self._last = finished
        self._timer.stop()
        self.behavior_finished.emit(finished)

    def _play_next(self) -> None:
        if self._forced is not None:
            self._emit_forced()          # 被强制时永远不挑下一个（兜住任何一条进来的路）
            return
        pool = self._pool()
        if not pool:
            # 理论上不会发生（安静动作有 4 个），真发生了就歇一轮
            self._schedule_gap(self.GAP_MS)
            return
        behavior = random.choices(
            [item[0] for item in pool], weights=[item[1] for item in pool]
        )[0]
        meta = META[behavior]
        if meta.moves:
            low, high = self.MOVING_COOLDOWN_MS
            self._moving_ready_at = time.monotonic() + random.randint(low, high) / 1000.0
        duration_ms = random.randint(int(meta.duration[0] * 1000), int(meta.duration[1] * 1000))
        self._current = behavior
        logger.debug("开始悠闲姿态: %s（%d 秒）", behavior.value, duration_ms // 1000)
        self.behavior_started.emit(behavior, duration_ms)
        self._timer.start(duration_ms)

    def _pool(self, ignore_last: bool = False) -> list[tuple[Behavior, float]]:
        """当前可选的（行为, 权重），已按静音 / 重复 / 冷却过滤。"""
        now = time.monotonic()
        hour = datetime.now().hour
        pool: list[tuple[Behavior, float]] = []
        for behavior, meta in META.items():
            if behavior == self._last and not ignore_last:
                continue
            if self._quiet and not meta.quiet:
                continue
            if meta.moves and now < self._moving_ready_at:
                continue
            weight = meta.weight * self._time_factor(behavior, meta, hour) * self._persona_factor(behavior)
            if weight > 0:
                pool.append((behavior, weight))
        if not pool and not ignore_last:
            # 只剩"刚演过的那个"能选时允许重复，别把节奏卡死
            return self._pool(ignore_last=True)
        return pool

    @staticmethod
    def _time_factor(behavior: Behavior, meta: BehaviorMeta, hour: int) -> float:
        """时段权重：深夜偏安静，白天照常。"""
        if not (hour >= NIGHT_FROM or hour < NIGHT_TO):
            return 1.0
        if meta.moves:
            return NIGHT_MOVING_FACTOR
        return NIGHT_REST_FACTOR if behavior in REST_BEHAVIORS else 1.0

    def _persona_factor(self, behavior: Behavior) -> float:
        """性格带来的行为倾向（指令 3 第 4 节）。"""
        preset = preset_of(self._persona_key)
        if preset is None:
            return 1.0
        return preset.behavior.get(behavior.value, 1.0)
