"""
番茄钟（对应指令 1）。

分两层：
- PomodoroTimer：纯计时状态机，负责 专注/短休/长休 的循环、暂停/继续/重置/跳过，
  只发信号，不碰数据库、不碰界面。
- PomodoroService：把计时器接到业务上——会话落库、专注期间让提醒静音、
  阶段切换时让宠物弹屏说话。

循环：专注 → 短休 → 专注 → … 每完成 N 个专注（N 可配）插入一次长休。
每个阶段结束都会记一条 PomodoroSession，所以统计不会因为关程序而丢。

计时基准是墙钟而不是"每拍减一秒"：剩余时间 = 截止时刻 - 现在。这样系统卡顿、
电脑睡眠几小时之后，醒来下一拍就能自动修正到正确的剩余时间，不会累积误差。
当前阶段的状态会写到 data/pomodoro_state.json：关掉程序再打开，按截止时刻接着算，
退出到重启之间的时间也算在里头，一路推进到这一轮跑完（长休结束）为止。
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from core.config import Config, PomodoroSection
from core.events import EventBus
from data.dao import PomodoroSessionDAO
from scheduler.jobs import ReminderJobs

logger = logging.getLogger(__name__)

TICK_MS = 1000   # 界面刷新频率；计时精度由墙钟决定，与它无关
STATE_FILE = "cache/pomodoro_state.json"   # 进行中阶段的状态存档，供重启后接着走


class Phase(Enum):
    IDLE = "idle"
    FOCUS = "focus"
    SHORT_BREAK = "short_break"
    LONG_BREAK = "long_break"


PHASE_TITLES = {
    Phase.IDLE: "空闲",
    Phase.FOCUS: "专注",
    Phase.SHORT_BREAK: "短休",
    Phase.LONG_BREAK: "长休",
}

# 阶段结束原因：走完 / 被跳过 / 被中止（重置或退出程序）
END_COMPLETED = "completed"
END_SKIPPED = "skipped"
END_ABORTED = "aborted"

# 启动恢复的结果
RESTORE_NONE = "none"        # 没有可接着走的阶段
RESTORE_RUNNING = "running"  # 接着跑（程序关着的时候还没到点）
RESTORE_PAUSED = "paused"    # 上次关程序时是暂停态
RESTORE_EXPIRED = "expired"  # 程序关着的时候这一阶段就走完了，已结算并停在下一阶段


class PomodoroTimer(QObject):
    """番茄钟计时状态机（墙钟驱动，不怕卡顿与睡眠，关程序可接着走）。"""

    tick = Signal(int, object)                    # 剩余秒, 当前阶段
    phase_started = Signal(object, int)           # 阶段, 计划秒
    phase_ended = Signal(object, int, int, str)   # 阶段, 计划秒, 实际秒, 结束原因
    state_changed = Signal(object)                # 阶段（暂停时仍是当前阶段）

    def __init__(
        self,
        pomodoro: PomodoroSection,
        parent: QObject | None = None,
        state_path: Path | str | None = None,
    ):
        super().__init__(parent)
        self._cfg = pomodoro
        self._phase = Phase.IDLE
        self._planned = 0
        self._remaining = 0
        self._elapsed = 0
        self._paused = False
        self._completed_focus = 0   # 本轮已完成的专注个数，用来判断何时长休

        # 墙钟状态
        self._deadline = 0.0          # 运行中：本阶段结束的绝对时间戳
        self._paused_remaining = 0    # 暂停中：冻结住的剩余秒

        self._state_path = Path(state_path) if state_path is not None else None

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    # ---------- 只读状态 ----------

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def planned(self) -> int:
        return self._planned

    @property
    def elapsed(self) -> int:
        return self._elapsed

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def is_active(self) -> bool:
        return self._phase != Phase.IDLE

    @property
    def is_focusing(self) -> bool:
        """是否正处于专注阶段（暂停中也算，因为它还没结束）。"""
        return self._phase == Phase.FOCUS

    @property
    def completed_focus(self) -> int:
        return self._completed_focus

    # ---------- 配置与外部状态 ----------

    def set_config(self, pomodoro: PomodoroSection) -> None:
        """更新参数，从下一个阶段开始生效（不打断当前计时）。"""
        self._cfg = pomodoro

    def set_completed_focus_count(self, count: int) -> None:
        """用今日已完成的番茄数初始化轮次计数，避免重启后长休节奏错乱。"""
        self._completed_focus = max(0, count)

    # ---------- 操作 ----------

    def start_focus(self) -> None:
        if self._phase != Phase.IDLE:
            return
        self._begin(Phase.FOCUS)

    def pause(self) -> None:
        if self._phase == Phase.IDLE or self._paused:
            return
        self._paused = True
        self._timer.stop()
        self._paused_remaining = self._compute_remaining()   # 冻住剩余（按墙钟算，不是拍数）
        self._remaining = self._paused_remaining
        self._elapsed = self._planned - self._remaining
        self.state_changed.emit(self._phase)
        self.save_state()

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        self._deadline = time.time() + self._paused_remaining   # 用冻结的剩余重算截止时刻
        self._paused_remaining = 0
        self._timer.start()
        self.state_changed.emit(self._phase)
        self.save_state()

    def skip(self) -> None:
        """跳过当前阶段，直接进入下一个阶段。"""
        if self._phase == Phase.IDLE:
            return
        self._finish(END_SKIPPED)
        self._advance()

    def reset(self) -> None:
        """中止当前阶段并回到空闲。"""
        if self._phase == Phase.IDLE:
            return
        self._finish(END_ABORTED)
        self._go_idle()

    def prepare_shutdown(self) -> None:
        """退出程序：不改变计时状态，只把当前状态落盘。

        退出期间墙上的时钟照走，下次启动按截止时刻接着算——也就是"退出到重启这段时间
        也算在内"，而不是把它当成暂停冻住。
        """
        self._timer.stop()
        self.save_state()

    # ---------- 内部 ----------

    def _go_idle(self) -> None:
        """回到空闲：一轮跑完、或用户重置时走这里。"""
        self._timer.stop()
        self._phase = Phase.IDLE
        self._planned = 0
        self._remaining = 0
        self._elapsed = 0
        self._paused = False
        self._deadline = 0.0
        self._paused_remaining = 0
        self.state_changed.emit(self._phase)
        self.save_state()

    def _duration(self, phase: Phase) -> int:
        """本阶段时长（秒）。配置里写的是分钟，这里换算成秒。"""
        minutes = {
            Phase.FOCUS: self._cfg.focus_min,
            Phase.SHORT_BREAK: self._cfg.short_break_min,
            Phase.LONG_BREAK: self._cfg.long_break_min,
        }[phase]
        return int(minutes) * 60

    def _compute_remaining(self) -> int:
        """按墙钟算剩余秒。

        用 ceil 而不是四舍五入：只有时间真的走完才会显示到 0，
        否则剩余 0.4 秒时就会被判成"已结束"，阶段会提前半秒收尾。
        """
        return max(0, math.ceil(self._deadline - time.time()))

    def _begin(self, phase: Phase, elapsed_ahead: float = 0.0) -> None:
        """开始一个阶段。

        elapsed_ahead 是"已经过去"的秒数，只有补算离线时间时才用：
        把这段已过去的时长顺延到本阶段的截止时刻上，离线时间才不会被白白丢掉。
        """
        self._phase = phase
        self._planned = self._duration(phase)
        self._deadline = time.time() + self._planned - elapsed_ahead
        self._remaining = self._compute_remaining()
        self._elapsed = self._planned - self._remaining
        self._paused = False
        self._paused_remaining = 0
        self.phase_started.emit(phase, self._planned)
        self.tick.emit(self._remaining, phase)   # 立即刷一次界面，不等第一拍
        self._timer.start()
        self.state_changed.emit(phase)
        self.save_state()

    def _on_tick(self) -> None:
        self._remaining = self._compute_remaining()   # 每拍重算，误差不累积
        self._elapsed = self._planned - self._remaining
        if self._remaining <= 0:
            self._finish(END_COMPLETED)
            self._advance()
            return
        self.tick.emit(self._remaining, self._phase)

    def _finish(self, status: str) -> None:
        if self._phase == Phase.IDLE:
            return
        self._timer.stop()
        finished = self._phase
        # 只有走完的专注才计入轮次（跳过/中止不算一个番茄）
        if finished == Phase.FOCUS and status == END_COMPLETED:
            self._completed_focus += 1
        self.phase_ended.emit(finished, self._planned, self._elapsed, status)

    def _advance(self, elapsed_ahead: float = 0.0) -> None:
        """进入下一阶段。

        长休走完就代表"这一轮番茄跑完了"，回到空闲等用户自己点开始，不再自动开下一轮
        （否则人不在电脑前时会一直空跑）。elapsed_ahead 用于补算离线时间。
        """
        if self._phase == Phase.FOCUS:
            every = max(1, int(self._cfg.long_break_every))
            nxt = Phase.LONG_BREAK if self._completed_focus % every == 0 else Phase.SHORT_BREAK
            self._begin(nxt, elapsed_ahead)
        elif self._phase == Phase.LONG_BREAK:
            self._go_idle()
        else:
            self._begin(Phase.FOCUS, elapsed_ahead)

    # ---------- 存档 / 恢复 ----------

    def save_state(self) -> None:
        """把当前阶段状态落到磁盘（阶段切换、暂停/继续、重置、退出时都会调用）。"""
        if self._state_path is None:
            return
        data = {
            "phase": self._phase.value,
            "planned": self._planned,
            "paused": self._paused,
            "deadline": self._deadline,                  # 运行中：绝对截止时刻
            "paused_remaining": self._paused_remaining,   # 暂停中：冻结的剩余秒
            "completed_focus": self._completed_focus,     # 决定下一次休息是短休还是长休
            "saved_at": time.time(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.parent / (self._state_path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_path)   # 原子替换，避免写到一半被打断而留下坏文件
        except OSError as exc:
            logger.warning("保存番茄钟状态失败: %s", exc)

    def restore(self) -> str:
        """启动时接着上次的阶段继续，返回 RESTORE_* 中的一种结果。"""
        if self._state_path is None or not self._state_path.exists():
            return RESTORE_NONE
        try:
            with self._state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("读取番茄钟状态失败: %s", exc)
            return RESTORE_NONE

        saved_at = float(data.get("saved_at", 0))
        if datetime.fromtimestamp(saved_at).date() != datetime.now().date():
            # 隔天的状态不恢复，否则昨天没走完的番茄会在今天突然冒出来
            return RESTORE_NONE

        self._completed_focus = max(0, int(data.get("completed_focus", 0)))
        phase = Phase(data.get("phase", Phase.IDLE.value))
        if phase == Phase.IDLE:
            return RESTORE_NONE

        self._phase = phase
        self._planned = int(data.get("planned", self._duration(phase)))

        if data.get("paused"):
            self._paused = True
            self._paused_remaining = max(0, int(data.get("paused_remaining", 0)))
            self._remaining = self._paused_remaining
            self._elapsed = self._planned - self._remaining
            self.state_changed.emit(self._phase)
            return RESTORE_PAUSED

        self._deadline = float(data.get("deadline", 0))
        remaining = self._compute_remaining()
        if remaining > 0:
            self._remaining = remaining
            self._elapsed = self._planned - remaining
            self._timer.start()
            self.tick.emit(self._remaining, self._phase)
            self.state_changed.emit(self._phase)
            return RESTORE_RUNNING

        # 程序关着的时候这一段就到点了：按墙钟一路往前推进（离线这段时间也算进去），
        # 每段照常结算，多出来的时长顺延给下一段；直到追上一个还没到点的阶段，
        # 或者这一轮已经跑完（长休结束）就停在空闲。
        while self.is_active and self._compute_remaining() <= 0:
            overdue = time.time() - self._deadline   # 这一段已经超时多少秒
            self._remaining = 0
            self._elapsed = self._planned
            self._finish(END_COMPLETED)
            self._advance(overdue)
        if self.is_active:
            self.tick.emit(self._remaining, self._phase)
            self.state_changed.emit(self._phase)
        return RESTORE_EXPIRED


class PomodoroService(QObject):
    """把番茄钟接到业务：落库、静音其他提醒、宠物播报。"""

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        session_dao: PomodoroSessionDAO,
        jobs: ReminderJobs,
        user_id: int,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._dao = session_dao
        self._jobs = jobs
        self._user_id = user_id

        self.timer = PomodoroTimer(
            config.pomodoro, self, state_path=config.data_path(STATE_FILE)
        )
        self._session_id: int | None = None
        # 恢复阶段时不要弹屏、不要响提示音（那是给"刚刚发生"准备的，不是给"启动时"）
        self._recovering = False

        self.timer.phase_started.connect(self._on_phase_started)
        self.timer.phase_ended.connect(self._on_phase_ended)
        self.timer.state_changed.connect(self._on_state_changed)

    # ---------- 启动 / 退出 ----------

    def sync_on_startup(self) -> None:
        """启动时接着上次没走完的阶段，并把数据库里悬空的会话记录结算掉。"""
        dangling = self._dao.latest_running()
        if dangling is not None:
            # 先认领这条记录，恢复出来的阶段结束时才能落到同一行上
            self._session_id = dangling.id

        self._recovering = True
        try:
            outcome = self.timer.restore()
        finally:
            self._recovering = False

        if dangling is not None and outcome == RESTORE_NONE:
            # 没有可用的计时状态（隔天了、存档坏了）：按墙钟估算实际时长后收尾
            elapsed = int((datetime.now() - dangling.started_at).total_seconds())
            self._dao.finish(
                dangling.id, min(dangling.planned_sec, max(0, elapsed)), END_ABORTED
            )
            self._session_id = None

        # 用今日已完成番茄数恢复轮次（含刚结算的那条），保证长休节奏连续
        self.timer.set_completed_focus_count(self._dao.today_completed_focus_count())
        self._bus.pomodoro_stats_changed.emit()
        if outcome != RESTORE_NONE:
            self._announce_recovery(outcome)

    def shutdown(self) -> None:
        """退出时只把状态落盘，不结算、不暂停。

        数据库里那条记录保持"运行中"，下次启动按墙钟接着算（退出到重启的时间也算在内），
        这样既不会丢统计，也不会把还没走完的番茄误算成完成。
        """
        self.timer.prepare_shutdown()
        if self._session_id is not None:
            # 已经专注的时长先写进这条未结算的记录，避免关程序损失统计
            self._dao.update_actual(self._session_id, self.timer.elapsed)
        self._jobs.focus_mode = False

    # ---------- 参数热更新 ----------

    def apply_config(self) -> None:
        self.timer.set_config(self._config.pomodoro)
        # 参数改了要立刻反映到"专注中屏蔽提醒"这个开关上
        self._sync_focus_mode(self.timer.is_focusing)

    # ---------- 计时器回调 ----------

    def _on_phase_started(self, phase: Phase, planned_sec: int) -> None:
        record = self._dao.start(self._user_id, phase.value, planned_sec)
        self._session_id = record.id
        self._sync_focus_mode(phase == Phase.FOCUS)
        if not self._recovering:
            self._announce_phase_start(phase, planned_sec)
        self._bus.pomodoro_phase_changed.emit(phase.value)

    def _on_phase_ended(self, phase: Phase, planned_sec: int, actual_sec: int, status: str) -> None:
        if self._session_id is not None:
            self._dao.finish(self._session_id, actual_sec, status)
            self._session_id = None
        self._bus.pomodoro_stats_changed.emit()
        if status == END_COMPLETED and not self._recovering:
            self._beep()
            if phase == Phase.LONG_BREAK:
                # 长休走完 = 这一轮番茄结束了，计时停在这里等用户自己开下一轮
                self._bus.summon_pet.emit()
                self._bus.pet_speak_proactive.emit("这一轮番茄都跑完啦，好好歇会儿～想再来一轮就点开始。")

    def _on_state_changed(self, phase: Phase) -> None:
        # 暂停/继续/重置都会走到这里，据此同步静音开关：
        # 否则从专注里直接重置回空闲时，focus_mode 会残留为 True，其他提醒一直不响。
        self._sync_focus_mode(phase == Phase.FOCUS)
        self._bus.pomodoro_phase_changed.emit(phase.value)

    # ---------- 内部 ----------

    def _sync_focus_mode(self, focusing: bool) -> None:
        """专注期间让其他提醒静音（可在设置页关掉）。"""
        self._jobs.focus_mode = bool(focusing and self._config.pomodoro.mute_other_reminders)

    def _announce_phase_start(self, phase: Phase, planned_sec: int) -> None:
        minutes = planned_sec // 60
        if phase == Phase.FOCUS:
            self._bus.pet_speak.emit(f"我陪你安静专注 {minutes} 分钟，不吵你。")
        elif phase == Phase.SHORT_BREAK:
            self._bus.summon_pet.emit()
            self._bus.pet_speak_proactive.emit("这个番茄走完啦，起来动动、喝口水～")
        else:
            self._bus.summon_pet.emit()
            self._bus.pet_speak_proactive.emit(f"这一轮的番茄都走完啦，好好休息 {minutes} 分钟～")

    def _announce_recovery(self, outcome: str) -> None:
        """启动时用一句话交代"番茄钟现在是什么状态"，避免用户一脸懵。"""
        if not self.timer.is_active:
            self._bus.pet_speak_proactive.emit("你不在的时候这一轮已经跑完啦，想再来一轮就点开始～")
            return
        title = PHASE_TITLES[self.timer.phase]
        if outcome == RESTORE_PAUSED:
            self._bus.pet_speak_proactive.emit(f"刚才的{title}还留着，点继续就能接着走～")
        elif outcome == RESTORE_RUNNING:
            self._bus.pet_speak_proactive.emit(f"接着刚才的{title}继续～")
        else:
            self._bus.pet_speak_proactive.emit(
                f"你不在的时候前面几段已经走完啦，现在到「{title}」，接着走～"
            )

    def _beep(self) -> None:
        if not self._config.pomodoro.notify_sound:
            return
        try:
            QApplication.beep()
        except Exception as exc:  # 提示音失败不该影响计时
            logger.warning("提示音播放失败: %s", exc)
