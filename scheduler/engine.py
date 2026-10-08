"""
APScheduler 封装。

随宠物常驻后台运行；每个提醒是一个一次性（date）任务，id 用 reminder:{id} 便于取消。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# 电脑休眠/卡顿导致晚点几分钟触发时，仍然允许补触发
MISFIRE_GRACE_SECONDS = 600


class SchedulerEngine:
    def __init__(self):
        self._scheduler = BackgroundScheduler()

    def start(self) -> None:
        self._scheduler.start()
        logger.info("定时调度已启动")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("定时调度已停止")

    @staticmethod
    def job_id(reminder_id: int) -> str:
        return f"reminder:{reminder_id}"

    def add_reminder(self, reminder_id: int, trigger_at: datetime, callback: Callable[[int], None]) -> None:
        self._scheduler.add_job(
            callback,
            trigger="date",
            run_date=trigger_at,
            args=[reminder_id],
            id=self.job_id(reminder_id),
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )

    def cancel_reminder(self, reminder_id: int) -> None:
        job = self._scheduler.get_job(self.job_id(reminder_id))
        if job is not None:
            job.remove()
