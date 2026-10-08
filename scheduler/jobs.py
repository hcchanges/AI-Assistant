"""
提醒任务。

职责：把提醒登记到调度器、到点判断是否该打扰（免打扰时段 / 每小时频率上限）、
并把"到点"事件通过事件总线抛给界面层。

注意：本模块的方法可能在调度线程里执行，因此只做数据与事件，不直接碰界面。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from core.config import Config, in_quiet_hours
from core.events import EventBus
from data.dao import ReminderDAO
from data.models import Reminder
from scheduler.engine import SchedulerEngine

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_MUTED = "muted"
STATUS_MISSED = "missed"


class ReminderJobs:
    def __init__(
        self,
        config: Config,
        reminder_dao: ReminderDAO,
        engine: SchedulerEngine,
        bus: EventBus,
    ):
        self._config = config
        self._dao = reminder_dao
        self._engine = engine
        self._bus = bus
        # 专注模式（番茄钟）：为 True 时其他提醒一律静音，由番茄钟服务切换
        self.focus_mode = False

    # ---------- 登记与取消 ----------

    def schedule(self, reminder: Reminder) -> None:
        self._engine.add_reminder(reminder.id, reminder.trigger_at, self.fire)

    def cancel(self, reminder_id: int) -> None:
        self._engine.cancel_reminder(reminder_id)
        self._dao.delete(reminder_id)
        self._bus.reminder_changed.emit()

    # ---------- 启动时对齐 ----------

    def sync_on_startup(self) -> None:
        """软件启动时：待触发的重新登记；关机期间已经错过的标记为"已错过"。"""
        now = datetime.now()
        changed = False
        for reminder in self._dao.pending():
            if reminder.trigger_at <= now:
                self._dao.set_status(reminder.id, STATUS_MISSED)
                logger.info("提醒 #%d 在软件未运行期间到点，标记为已错过", reminder.id)
                changed = True
            else:
                self.schedule(reminder)
        if changed:
            self._bus.reminder_changed.emit()

    # ---------- 到点触发 ----------

    def fire(self, reminder_id: int) -> None:
        reminder = self._dao.get(reminder_id)
        if reminder is None or reminder.status != STATUS_PENDING:
            return

        now = datetime.now()
        if self.focus_mode:
            self._dao.set_status(reminder_id, STATUS_MUTED)
            logger.info("提醒 #%d 落在番茄钟专注期间，已静音", reminder_id)
        elif self._in_quiet_hours(now):
            self._dao.set_status(reminder_id, STATUS_MUTED)
            logger.info("提醒 #%d 落在免打扰时段，已静音", reminder_id)
        elif self._exceeded_rate(now):
            self._dao.set_status(reminder_id, STATUS_MUTED)
            logger.info("提醒 #%d 超出每小时打扰上限，已静音", reminder_id)
        else:
            self._dao.set_status(reminder_id, STATUS_FIRED, fired_at=now)
            logger.info("提醒 #%d 触发: %s", reminder_id, reminder.content)
            self._bus.reminder_due.emit(reminder_id)
        self._bus.reminder_changed.emit()

    # ---------- 免打扰判断 ----------

    def _in_quiet_hours(self, now: datetime) -> bool:
        return in_quiet_hours(self._config.schedule.quiet_hours, now)

    def _exceeded_rate(self, now: datetime) -> bool:
        since = now - timedelta(hours=1)
        return self._dao.count_fired_since(since) >= self._config.schedule.max_notify_per_hour
