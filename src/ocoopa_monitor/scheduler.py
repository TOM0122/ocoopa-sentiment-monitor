from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import Settings
from .db import Database
from .pipeline import MonitorPipeline
from .reports import DailyReportService

LOGGER = logging.getLogger(__name__)


@dataclass
class SchedulerState:
    last_high_run: Optional[datetime] = None
    last_regular_run: Optional[datetime] = None
    last_daily_report_date: Optional[str] = None


class SimpleScheduler:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.state = SchedulerState()

    def run_forever(self, poll_seconds: int = 30) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        LOGGER.info("starting scheduler")
        while True:
            self.tick()
            time.sleep(poll_seconds)

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        pipeline = MonitorPipeline(self.db, self.settings)
        if self._due(self.state.last_high_run, self.settings.high_lane_interval_minutes, now):
            LOGGER.info("running high-sensitivity lane")
            LOGGER.info("high lane result=%s", pipeline.run_lane("high"))
            self.state.last_high_run = now
        if self._due(self.state.last_regular_run, self.settings.regular_lane_interval_minutes, now):
            LOGGER.info("running regular lane")
            LOGGER.info("regular lane result=%s", pipeline.run_lane("regular"))
            self.state.last_regular_run = now
        self._maybe_daily_report(now)

    def _maybe_daily_report(self, now: datetime) -> None:
        # Beijing 09:00 is 01:00 UTC. This keeps the scheduler dependency-free.
        report_date = now.date().isoformat()
        if now.hour == 1 and now.minute < 10 and self.state.last_daily_report_date != report_date:
            LOGGER.info("generating daily report")
            DailyReportService(self.db).generate("Asia/Shanghai")
            self.state.last_daily_report_date = report_date

    @staticmethod
    def _due(last_run: Optional[datetime], interval_minutes: int, now: datetime) -> bool:
        if last_run is None:
            return True
        return (now - last_run).total_seconds() >= interval_minutes * 60

