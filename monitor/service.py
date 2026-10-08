"""
前台应用监控服务（对应指令 5 / 指令 6）。

链路：定时采样前台应用 → 会话归并 → 落库 → 久坐/分心判断 → 让宠物开口。

几条设计约定：
- 只记应用进程名与类别，不读窗口标题（标题里可能有文档名、网址）；
- 采样在界面线程用 QTimer 跑，每 3 秒一次，开销只有一次进程名查询；
- 宠物自己的窗口不算用户活动（点猫咪的时候不记）；
- "人离开"（超过 idle_min 没有键鼠操作）期间不计时，这一段在最后一次操作那一刻就结掉；
- 人离开、跨天、关程序都会把当前这一段落库，所以统计不会漏。

数据全在本机，可在设置页一键清空；久坐/分心提醒可单独关掉（关了照样记录）。
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from datetime import datetime, time as dtime, timedelta

from PySide6.QtCore import QObject, QTimer

from core.config import Config, in_quiet_hours
from core.events import EventBus
from core.worker import run_async
from data.dao import ActivitySessionDAO, AppCategoryDAO
from monitor import foreground
from monitor.category import friendly_name, resolve_category
from monitor.report import DayReport
from scheduler.jobs import ReminderJobs

logger = logging.getLogger(__name__)

SEDENTARY_LINE = "盯{name} {minutes} 分钟啦，起来动动~"   # 指令 6 第 3 节的原文
DISTRACT_LINE = "主人换来换去的，是分心了嘛?"


class ActivityService(QObject):
    """前台应用采样与时间分析。"""

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        session_dao: ActivitySessionDAO,
        category_dao: AppCategoryDAO,
        jobs: ReminderJobs,
        comment_chain,
        user_id: int,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._dao = session_dao
        self._category_dao = category_dao
        self._jobs = jobs
        self._comment_chain = comment_chain
        self._user_id = user_id

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._apply_interval()

        self._pids = foreground.ProcNames()
        self._overrides: dict[str, str] = {}

        # 当前这一段（同一个应用连续使用）
        self._current_proc: str | None = None
        self._current_category = ""
        self._current_start = 0.0     # 这一段是从什么时候开始连续的（久坐判定用它）
        self._flushed_at = 0.0        # 这一段已经落库到哪一刻（中途刷新统计时用）

        self._switches: deque[float] = deque()   # 最近窗口内的应用切换时刻，用于判分心
        self._last_sedentary = 0.0
        self._last_distract = 0.0
        self._today = datetime.now().date()

    # ---------- 启停与热更新 ----------

    def start(self) -> None:
        self._reload_categories()
        self.cleanup_history()
        if not self._config.activity.enabled:
            logger.info("前台监控已关闭，不采样")
            return
        if not foreground.available():
            logger.warning("当前环境无法采样前台应用（需要 Windows + psutil）")
            return
        self._timer.start()

    def apply_config(self) -> None:
        """设置页保存后热更新：开关、采样间隔、名单、分类立刻生效。"""
        self._apply_interval()
        self._reload_categories()
        if not self._config.activity.enabled:
            self._stop()
        elif foreground.available() and not self._timer.isActive():
            self._timer.start()

    def shutdown(self) -> None:
        self._stop()

    def cleanup_history(self) -> int:
        """删掉超过保留天数的记录（默认保留 14 天）。"""
        days = max(1, int(self._config.activity.retention_days))
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days
        )
        removed = self._dao.cleanup_before(cutoff)
        if removed:
            logger.info("清理了 %d 条过期活动记录", removed)
        return removed

    def clear_history(self) -> None:
        """一键清空历史（隐私要求）。当前这一段从此刻重新开始计，避免旧的时长又被写回去。"""
        self._dao.clear_all()
        if self._current_proc is not None:
            self._current_start = time.time()
            self._flushed_at = self._current_start

    @property
    def recording(self) -> bool:
        """是否正在记录（界面用来显示开关状态）。"""
        return self._timer.isActive()

    @property
    def user_present(self) -> bool:
        """现在有没有人在用电脑（主动搭话前先看一眼，人不在就别自言自语）。"""
        return self._current_proc is not None

    @property
    def current_proc(self) -> str:
        """当前前台应用名（陪玩时告诉模型"用户在玩什么"）。取不到就是空串。"""
        return self._current_proc or ""

    # ---------- 采样 ----------

    def _tick(self) -> None:
        now = time.time()
        self._roll_over_if_new_day(now)

        cfg = self._config.activity
        idle = foreground.idle_seconds()
        if idle >= cfg.idle_min * 60:
            # 人离开了：这一段在最后一次操作那一刻就结束，离开的时间不算使用
            self._leave_desk(now - idle)
            return

        pid = foreground.foreground_pid()
        proc = self._pids.name_of(pid)
        if proc is None or pid == os.getpid() or self._is_ignored(proc):
            # 宠物自己、读不到进程、或在"不记录"名单里：都不算用户活动
            self._close_segment(now, reset_switches=True)
            return

        if proc != self._current_proc:
            self._close_segment(now)
            self._current_proc = proc
            self._current_category = resolve_category(proc, self._overrides)
            self._current_start = now
            self._flushed_at = now
            self._note_switch(now)
            return

        # 同一个应用：用户可能刚改过分类，跟着刷新
        self._current_category = resolve_category(proc, self._overrides)
        self._check_sedentary(now, proc)

    def _note_switch(self, now: float) -> None:
        """记一次应用切换；窗口内切得太多就是分心。"""
        cfg = self._config.activity
        self._switches.append(now)
        while self._switches and self._switches[0] < now - cfg.distract_window_sec:
            self._switches.popleft()
        if len(self._switches) < cfg.distract_switches:
            return
        self._switches.clear()   # 提醒过就重新数，免得冷却期一过立刻又弹
        if now - self._last_distract < cfg.distract_cooldown_min * 60:
            return
        self._last_distract = now
        logger.info("判定为分心：%d 秒内切换 %d 次", cfg.distract_window_sec, cfg.distract_switches)
        self._remind(DISTRACT_LINE)

    def _check_sedentary(self, now: float, proc: str) -> None:
        """盯着同一个应用太久就提醒起来动动（每隔 repeat 分钟最多一次）。"""
        cfg = self._config.activity
        if now - self._current_start < cfg.sedentary_min * 60:
            return
        if now - self._last_sedentary < cfg.sedentary_repeat_min * 60:
            return
        self._last_sedentary = now
        logger.info("判定为久坐：%s 连续使用 %d 分钟", proc, cfg.sedentary_min)
        self._remind(SEDENTARY_LINE.format(name=friendly_name(proc), minutes=cfg.sedentary_min))

    def _remind(self, text: str) -> None:
        """久坐/分心提示：把宠物叫到前面说一句。免打扰时段和专注期间不打扰。"""
        if not self._config.activity.remind:
            return
        if self._jobs.focus_mode:
            return
        if in_quiet_hours(self._config.schedule.quiet_hours):
            return
        self._bus.summon_pet.emit()
        self._bus.pet_speak_proactive.emit(text)

    # ---------- 会话归并 ----------

    def _leave_desk(self, end_ts: float) -> None:
        self._close_segment(end_ts, reset_switches=True)

    def _flush_segment(self) -> None:
        """把当前这一段"已经过去的时间"先落库，但不结束它。

        今日活动报告和主动搭话要看统计，得先把手上这段记进去；
        但不能像切换应用那样把这一段结束掉——那会顺手把"同一应用连续用了多久"清零，
        而久坐判定正是看这个时长，于是每刷新一次统计就把久坐提醒往后推一次。
        """
        if self._current_proc is None:
            return
        now = time.time()
        if now <= self._flushed_at:
            return
        self._record(self._current_proc, self._current_category, self._flushed_at, now)
        self._flushed_at = now

    def _close_segment(self, end_ts: float, reset_switches: bool = False) -> None:
        """结束当前这一段并把时间写进库（应用切换、人离开、跨天、关程序都会走这里）。"""
        if reset_switches:
            self._switches.clear()
        if self._current_proc is None:
            return
        # 只记"上次落库之后"的那一段，前面已经记过的部分不重复写
        self._record(self._current_proc, self._current_category, self._flushed_at, end_ts)
        self._current_proc = None
        self._current_start = 0.0
        self._flushed_at = 0.0

    def _record(self, proc: str, category: str, start_ts: float, end_ts: float) -> None:
        """写一段记录；跨零点的那段按午夜切开，保证"今日用时"按天算得准。"""
        if end_ts <= start_ts:
            return
        cursor = datetime.fromtimestamp(start_ts)
        end_dt = datetime.fromtimestamp(end_ts)
        while cursor.date() != end_dt.date():
            midnight = datetime.combine(cursor.date() + timedelta(days=1), dtime.min)
            self._dao.record(self._user_id, proc, category, cursor, midnight)
            cursor = midnight
        self._dao.record(self._user_id, proc, category, cursor, end_dt)

    def _roll_over_if_new_day(self, now: float) -> None:
        """跨天：先把昨天那一段结掉（按午夜切开），再清过期数据。"""
        day = datetime.fromtimestamp(now).date()
        if day == self._today:
            return
        self._close_segment(now, reset_switches=True)
        self._today = day
        self.cleanup_history()

    # ---------- 今日活动 ----------

    def today_report(self) -> DayReport:
        totals = self._dao.today_top_apps(limit=50)
        apps = [
            (friendly_name(proc), resolve_category(proc, self._overrides), seconds)
            for proc, seconds in totals
        ]
        by_category: dict[str, int] = {}
        for _name, category, seconds in apps:
            by_category[category] = by_category.get(category, 0) + seconds
        longest_row = self._dao.today_longest_segment()
        return DayReport(
            day=datetime.now().date(),
            total_sec=self._dao.today_total_seconds(),
            apps=apps,
            categories=sorted(by_category.items(), key=lambda item: item[1], reverse=True),
            longest=(friendly_name(longest_row[0]), longest_row[1]) if longest_row else None,
        )

    def today_summary_text(self, top: int = 3) -> str:
        """今日使用情况摘要（给主动搭话当上下文），先把当前这一段落库再统计。"""
        self._flush_segment()
        return self.today_report().render(top=top)

    def announce_today(self) -> None:
        """聊天里让它报「今日活动」：先报今日排行，配了 Key 再补一句人设点评。

        排行立刻弹（不等模型）；点评到了把"排行 + 点评"重新发一遍——
        气泡只有一个，重发才能两段一起看见，也不会各说一半。
        """
        self._flush_segment()   # 正在用的这一段落库，排行里才有它
        report = self.today_report()
        text = report.render()
        self._bus.summon_pet.emit()
        self._bus.activity_report.emit(text)
        if report.empty or not self._config.get_api_key():
            return
        run_async(
            self._comment_chain.comment,
            text,
            on_done=lambda comment: self._bus.activity_report.emit(f"{text}\n\n{comment}"),
            on_failed=lambda exc: logger.warning("今日活动点评失败: %s", exc),
        )

    # ---------- 内部 ----------

    def _stop(self) -> None:
        self._timer.stop()
        self._close_segment(time.time(), reset_switches=True)

    def _apply_interval(self) -> None:
        self._timer.setInterval(max(1, int(self._config.activity.sample_sec)) * 1000)

    def _reload_categories(self) -> None:
        self._overrides = self._category_dao.get_map()

    def _is_ignored(self, proc: str) -> bool:
        """在"不记录"名单里的应用（密码管理器、私聊这类敏感程序）。"""
        return proc.lower() in {name.lower() for name in self._config.activity.blacklist}
