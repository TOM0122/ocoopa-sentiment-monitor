from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .delivery import DeliveryClient, short_markdown_link
from .pipeline import MonitorPipeline
from .outbox import DeliveryOutboxWorker
from .reports import DailyReportService
from .recall import RecallRegistryService
from .source_health import SourceHealthMonitor

LOGGER = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL_MINUTES = 30


@dataclass
class SchedulerState:
    last_high_run: Optional[datetime] = None
    last_regular_run: Optional[datetime] = None
    last_daily_report_date: Optional[str] = None
    last_health_run: Optional[datetime] = None


class SimpleScheduler:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.state = SchedulerState()
        self.delivery = DeliveryClient.from_settings(settings)
        self.health = SourceHealthMonitor(db)
        self._alerted_unhealthy: set = set()

    def run_forever(self, poll_seconds: int = 30) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        LOGGER.info("starting scheduler")
        self._ensure_bootstrap()
        while True:
            try:
                self.tick()
            except Exception:
                LOGGER.exception("scheduler tick failed; loop will continue")
            time.sleep(poll_seconds)

    def _ensure_bootstrap(self) -> None:
        """Guarantee backfill-before-realtime ordering on every (re)start.

        On a fresh DB this runs a silent backfill across both lanes and marks
        the system bootstrapped, so real-time alerts never fire on pre-existing
        content. On an already-bootstrapped DB (e.g. a redeploy) it is a no-op,
        so historical data is not re-ingested and no alert storm occurs.
        """
        if self.db.is_bootstrapped():
            LOGGER.info("already bootstrapped; enabling real-time alerts")
            return
        LOGGER.info(
            "cold start detected: running silent backfill (%s days) before real-time alerts",
            self.settings.backfill_days,
        )
        pipeline = MonitorPipeline(self.db, self.settings, delivery_client=self.delivery)
        stats = pipeline.bootstrap(self.settings.backfill_days)
        LOGGER.info("bootstrap complete: %s; real-time alerts enabled", stats)

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        pipeline = MonitorPipeline(self.db, self.settings, delivery_client=self.delivery)
        recall_service = RecallRegistryService(self.db, pipeline)
        if not self.db.get_state("recall_registry_backfill_v1"):
            result = recall_service.backfill_existing(assume_group_reported=True)
            self.db.set_state("recall_registry_backfill_v1", now.isoformat())
            LOGGER.info("recall registry rollout backfill=%s", result)
        queued = recall_service.queue_pending(10)
        if queued:
            LOGGER.info("queued %s pending recall mentions for group sync", queued)
        delivery_stats = DeliveryOutboxWorker(self.db, self.delivery).drain()
        if delivery_stats["attempted"]:
            LOGGER.info("delivery outbox result=%s", delivery_stats)
        if self._due(self.state.last_high_run, self.settings.high_lane_interval_minutes, now):
            LOGGER.info("running high-sensitivity lane")
            LOGGER.info("high lane result=%s", pipeline.run_lane("high"))
            self.state.last_high_run = now
        if self._due(self.state.last_regular_run, self.settings.regular_lane_interval_minutes, now):
            LOGGER.info("running regular lane")
            LOGGER.info("regular lane result=%s", pipeline.run_lane("regular"))
            self.state.last_regular_run = now
        self._maybe_health_check(now)
        self._maybe_escalate_unacked(now)
        self._maybe_daily_report(now)
        self.db.set_state("scheduler_heartbeat_at", now.isoformat())

    def _maybe_escalate_unacked(self, now: datetime) -> None:
        """Re-page the on-call once for any red alert left unacked past the timeout.

        Reviewing an alert (CLI or web) acks it, so a handled alert never
        escalates. Escalated alerts are marked so they are not re-paged again.
        """
        cutoff = now - timedelta(minutes=self.settings.alert_ack_timeout_minutes)
        pending = self.db.pending_red_alerts_older_than(cutoff)
        if not pending:
            return
        lines = [
            f"### 【红色告警未处理】以下红色告警超过 {self.settings.alert_ack_timeout_minutes} 分钟无人复核：",
            "",
        ]
        for a in pending:
            lines.append(
                f"- #{a['alert_id']} {a.get('title')}\n"
                f"  原文：{short_markdown_link(a.get('source_url'))}"
            )
        try:
            self.delivery.send_text("Ocoopa 红色告警未处理升级", "\n".join(lines), suppress_at=False)
        except Exception:
            LOGGER.exception("failed to deliver escalation; will retry next tick")
            return
        for a in pending:
            self.db.mark_alert_escalated(a["alert_id"])
        LOGGER.warning("escalated unacked red alerts: %s", [a["alert_id"] for a in pending])

    def _maybe_daily_report(self, now: datetime) -> None:
        local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        if local_now.hour < 9:
            return
        report_date = local_now.date().isoformat()
        persisted = self.db.get_state("last_daily_report_date")
        if self.state.last_daily_report_date == report_date or persisted == report_date:
            return
        LOGGER.info("generating + delivering daily report")
        report = DailyReportService(self.db, self.delivery).generate("Asia/Shanghai")
        LOGGER.info("daily report delivery_status=%s", report.get("delivery_status"))
        if report.get("delivery_status") == "failed":
            LOGGER.warning("daily report delivery failed; scheduler will retry")
            return
        self.state.last_daily_report_date = report_date
        self.db.set_state("last_daily_report_date", report_date)

    def _maybe_health_check(self, now: datetime) -> None:
        if not self._due(self.state.last_health_run, HEALTH_CHECK_INTERVAL_MINUTES, now):
            return
        self.state.last_health_run = now
        unhealthy = {s["source_name"]: s for s in self.health.check()}
        # Drop recovered sources so a future failure re-alerts.
        self._alerted_unhealthy &= set(unhealthy)
        # Only P0 sources page the team; P1 commercial-API quota failures are expected.
        new_p0 = [
            s
            for name, s in unhealthy.items()
            if name not in self._alerted_unhealthy and str(s.get("priority")) == "P0"
        ]
        if not new_p0:
            return
        for s in new_p0:
            self._alerted_unhealthy.add(s["source_name"])
        lines = ["### 【源健康告警】以下 P0 抓取源失联/连续失败，可能正在漏报：", ""]
        for s in new_p0:
            lines.append(
                f"- {s['source_name']} status={s.get('health_status')} "
                f"last_success={s.get('last_success_at')} failures={s.get('consecutive_failures')}"
            )
        try:
            self.delivery.send_text("Ocoopa 源健康告警", "\n".join(lines), suppress_at=False)
            LOGGER.warning("source-health alert sent for %s", [s["source_name"] for s in new_p0])
        except Exception:
            LOGGER.exception("failed to deliver source-health alert")

    @staticmethod
    def _due(last_run: Optional[datetime], interval_minutes: int, now: datetime) -> bool:
        if last_run is None:
            return True
        return (now - last_run).total_seconds() >= interval_minutes * 60
