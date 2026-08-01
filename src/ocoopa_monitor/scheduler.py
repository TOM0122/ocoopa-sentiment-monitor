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
from .models import utcnow
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
    last_licensed_run: Optional[datetime] = None
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
            if not self.db.get_state("public_social_backfill_completed_at"):
                LOGGER.info("public social discovery upgrade: running silent 30-day backfill")
                self._run_upgrade_backfill("regular", "public_social_backfill_completed_at")
                self.state.last_regular_run = utcnow()
            licensed_sources = self.db.get_sources("licensed")
            if licensed_sources and not self.db.get_state("brandwatch_backfill_completed_at"):
                LOGGER.info("licensed source enabled: running silent 30-day backfill")
                self._run_upgrade_backfill("licensed", "brandwatch_backfill_completed_at")
                self.state.last_licensed_run = utcnow()
            LOGGER.info("already bootstrapped; each lane will enable real-time after its own backfill")
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
            if not self.db.get_state("public_social_backfill_completed_at"):
                LOGGER.info("retrying silent public-social backfill")
                self._run_upgrade_backfill("regular", "public_social_backfill_completed_at")
            else:
                LOGGER.info("running regular lane")
                LOGGER.info("regular lane result=%s", pipeline.run_lane("regular"))
            self.state.last_regular_run = now
        if self._due(self.state.last_licensed_run, self.settings.licensed_lane_interval_minutes, now):
            if self.db.get_sources("licensed"):
                if not self.db.get_state("brandwatch_backfill_completed_at"):
                    LOGGER.info("retrying silent licensed-source backfill")
                    self._run_upgrade_backfill("licensed", "brandwatch_backfill_completed_at")
                else:
                    LOGGER.info("running licensed social lane")
                    LOGGER.info("licensed lane result=%s", pipeline.run_lane("licensed"))
            self.state.last_licensed_run = now
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
        # P0 failures page the owner. Licensed providers also notify because a
        # silent outage directly invalidates social coverage, but do not @.
        candidates = [
            s
            for name, s in unhealthy.items()
            if name not in self._alerted_unhealthy
            and (str(s.get("priority")) == "P0" or str(s.get("lane")) == "licensed")
        ]
        if not candidates:
            return
        for group, suppress_at in (
            ([s for s in candidates if str(s.get("priority")) == "P0"], False),
            ([s for s in candidates if str(s.get("priority")) != "P0"], True),
        ):
            if not group:
                continue
            label = "P0" if not suppress_at else "持牌社媒"
            lines = [f"【源健康告警】以下 {label} 采集源失联/连续失败，可能正在漏报：", ""]
            for source in group:
                lines.append(
                    f"- {source['source_name']} status={source.get('health_status')} "
                    f"last_success={source.get('last_success_at')} failures={source.get('consecutive_failures')}"
                )
            try:
                self.delivery.send_text("Ocoopa 源健康告警", "\n".join(lines), suppress_at=suppress_at)
            except Exception:
                LOGGER.exception("failed to deliver source-health alert")
                continue
            self._alerted_unhealthy.update(source["source_name"] for source in group)
            LOGGER.warning("source-health alert sent for %s", [source["source_name"] for source in group])

    def _run_upgrade_backfill(self, lane: str, state_key: str) -> bool:
        result = MonitorPipeline(self.db, self.settings, delivery_client=self.delivery).run_lane(
            lane, backfill=True, since_days=30
        )
        if not result["sources_attempted"] or result["sources_failed"]:
            # Keep this lane silent and retry on its normal cadence, while the
            # P0 high lane and the rest of the scheduler continue operating.
            LOGGER.warning("%s backfill incomplete; will retry without blocking scheduler: %s", lane, result)
            return False
        self.db.set_state(state_key, utcnow().isoformat())
        return True

    @staticmethod
    def _due(last_run: Optional[datetime], interval_minutes: int, now: datetime) -> bool:
        if last_run is None:
            return True
        return (now - last_run).total_seconds() >= interval_minutes * 60
