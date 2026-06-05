from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta

from ocoopa_monitor.analysis import AnalysisService
from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database, dt_to_str
from ocoopa_monitor.evidence import EvidenceChecker
from ocoopa_monitor.fetchers.base import Fetcher
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.models import RawItem, SourceConfig, utcnow
from ocoopa_monitor.pipeline import MonitorPipeline
from ocoopa_monitor.risk import RiskRuleEngine


class StaticFetcher(Fetcher):
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def fetch(self, source, keywords, since=None):
        if self.error:
            raise RuntimeError(self.error)
        return self.items


def settings(db_path):
    return Settings(
        db_path=db_path,
        alert_webhook_url="",
        high_lane_interval_minutes=15,
        regular_lane_interval_minutes=60,
        p0_health_threshold_minutes=120,
        backfill_days=60,
        request_timeout_seconds=1,
    )


class CoreTests(unittest.TestCase):
    def make_db(self):
        tmp = tempfile.NamedTemporaryFile(delete=True)
        db = Database(tmp.name)
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(
            [
                SourceConfig(
                    source_name="test_high",
                    source_type="news",
                    priority="P0",
                    lane="high",
                    method="static",
                    url="test://high",
                    alert_threshold_minutes=120,
                )
            ]
        )
        return db, tmp

    def test_backfill_does_not_create_alert(self):
        db, tmp = self.make_db()
        item = RawItem(
            source_type="news",
            source_name="test_high",
            source_url="https://example.com/ocoopa-lawsuit",
            title="Ocoopa wrongful death lawsuit filed",
            raw_text="Ocoopa wrongful death lawsuit filed after reported hand warmer fire.",
            published_at=utcnow() - timedelta(days=5),
        )
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([item])},
        )
        result = pipeline.run_lane("high", backfill=True, since_days=30)
        self.assertEqual(result["mentions_processed"], 1)
        self.assertEqual(result["alerts_created"], 0)
        with db.connect() as conn:
            mentions = conn.execute("SELECT backfill FROM mentions").fetchall()
            alerts = conn.execute("SELECT * FROM alerts").fetchall()
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["backfill"], 1)
        self.assertEqual(len(alerts), 0)

    def test_red_realtime_creates_alert_with_review_when_evidence_low_confidence(self):
        db, tmp = self.make_db()
        item = RawItem(
            source_type="news",
            source_name="test_high",
            source_url="https://example.com/ocoopa-fire",
            title="Ocoopa fire lawsuit",
            raw_text="Ocoopa fire lawsuit alleges a rechargeable hand warmer caused injuries.",
            published_at=utcnow(),
        )
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([item])},
        )
        result = pipeline.run_lane("high")
        self.assertEqual(result["alerts_created"], 1)
        with db.connect() as conn:
            alert = conn.execute("SELECT * FROM alerts").fetchone()
        self.assertEqual(alert["risk_level"], "red")

    def test_evidence_checker_drops_ungrounded_quote(self):
        result = EvidenceChecker().check(
            raw_text="Ocoopa recall mentioned by CPSC in source text.",
            source_url="https://example.com",
            summary_zh="Ocoopa recall",
            escalation_reason="recall",
            key_quotes=["this exact quote is absent"],
            confidence=0.9,
            risk_level="red",
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.valid_quotes, [])
        self.assertTrue(result.needs_human_review)

    def test_negated_recall_is_not_red(self):
        decision = RiskRuleEngine().evaluate(
            "Ocoopa no recall notice",
            "There is no recall and no lawsuit in this product discussion.",
            ["Ocoopa recall"],
        )
        self.assertNotEqual(decision.risk_level, "red")

    def test_source_health_flags_stale_p0(self):
        db, tmp = self.make_db()
        with db.connect() as conn:
            conn.execute(
                "UPDATE source_health SET last_success_at=?, health_status='ok' WHERE source_name='test_high'",
                (dt_to_str(utcnow() - timedelta(hours=3)),),
            )
        unhealthy = db.unhealthy_sources()
        self.assertEqual(len(unhealthy), 1)
        self.assertEqual(unhealthy[0]["source_name"], "test_high")

    def test_fetcher_failure_is_isolated(self):
        tmp = tempfile.NamedTemporaryFile(delete=True)
        db = Database(tmp.name)
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(
            [
                SourceConfig("bad", "news", "P0", "high", "bad", "test://bad", alert_threshold_minutes=120),
                SourceConfig("good", "news", "P0", "high", "good", "test://good", alert_threshold_minutes=120),
            ]
        )
        good_item = RawItem(
            source_type="news",
            source_name="good",
            source_url="https://example.com/good",
            title="Ocoopa CPSC recall",
            raw_text="Ocoopa CPSC recall signal in public source.",
            published_at=utcnow(),
        )
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"bad": StaticFetcher(error="boom"), "good": StaticFetcher([good_item])},
        )
        result = pipeline.run_lane("high")
        self.assertEqual(result["sources_failed"], 1)
        self.assertEqual(result["mentions_processed"], 1)


if __name__ == "__main__":
    unittest.main()
