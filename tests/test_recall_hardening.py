from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from ocoopa_monitor.analysis import AnalysisService
from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database
from ocoopa_monitor.delivery import DeliveryClient
from ocoopa_monitor.fetchers.generic_rss import GenericRSSFetcher
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.models import AnalysisResult, Mention, RawItem, SourceConfig, utcnow
from ocoopa_monitor.normalize import topic_key
from ocoopa_monitor.outbox import DeliveryOutboxWorker
from ocoopa_monitor.pipeline import MonitorPipeline
from ocoopa_monitor.recall import RecallRegistryService, is_current_recall, recall_digest_payload
from ocoopa_monitor.reports import DailyReportService


def settings(db_path: str) -> Settings:
    return Settings(
        db_path=db_path,
        db_url="",
        alert_channel="generic",
        alert_webhook_url="",
        alert_webhook_secret="",
        alert_at_mobiles="",
        alert_rate_limit_per_minute=20,
        alert_cooldown_hours=6,
        alert_ack_timeout_minutes=30,
        review_token="token",
        llm_provider="rule",
        llm_model="deepseek-v4-flash",
        llm_api_key="",
        llm_base_url="https://api.deepseek.com",
        serpapi_api_key="",
        brave_search_api_key="",
        gnews_api_key="",
        high_lane_interval_minutes=15,
        regular_lane_interval_minutes=60,
        p0_health_threshold_minutes=120,
        backfill_days=180,
        request_timeout_seconds=1,
    )


class StaticFetcher:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def fetch(self, source, keywords, since=None):
        if self.error:
            raise RuntimeError(self.error)
        return self.items


class FlakyChannel:
    def __init__(self):
        self.calls = 0

    def send_alert(self, payload):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary webhook failure")
        return "test://delivered"


class CapturingChannel:
    def __init__(self):
        self.payloads = []

    def send_alert(self, payload):
        self.payloads.append(payload)
        return "test://delivered"


class RoutineRecallAnalysisService:
    def analyze(self, mention):
        return AnalysisResult(
            mention_id=mention.id,
            model_provider="test",
            model_name="routine-recall",
            prompt_version="test",
            sentiment="negative",
            risk_level="yellow",
            category="recall",
            summary_zh=f"召回传播跟踪：{mention.title}",
            key_quotes=[],
            key_quote_offsets=[],
            requires_escalation=False,
            escalation_reason="",
            confidence=0.91,
            evidence_check_passed=True,
            evidence_check_notes="test",
            needs_human_review=False,
        )


class DowngradingProvider:
    provider_name = "test"
    model_name = "downgrader"

    def analyze(self, mention, rule_decision):
        return {
            "sentiment": "neutral",
            "risk_level": "green",
            "category": "other",
            "summary_zh": "Ocoopa wrongful death lawsuit after fire.",
            "key_quotes": ["wrongful death lawsuit"],
            "requires_escalation": False,
            "escalation_reason": "",
            "confidence": 0.9,
        }


class RecallHardeningTests(unittest.TestCase):
    def make_db(self, bootstrapped=True):
        tmp = tempfile.NamedTemporaryFile(delete=True)
        db = Database(tmp.name)
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(
            [SourceConfig("test_high", "news", "P0", "high", "static", "test://high")]
        )
        if bootstrapped:
            db.mark_bootstrapped()
        return db, tmp

    @staticmethod
    def recall_item(url="https://example.com/recall"):
        return RawItem(
            source_type="news",
            source_name="test_high",
            source_url=url,
            title="OCOOPA hand warmer recall 26-659 covers model UT3053",
            raw_text="CPSC recall cites fire and burn hazards for OCOOPA model UT3053.",
            published_at=utcnow(),
        )

    def test_llm_cannot_downgrade_rule_red(self):
        mention = Mention(
            source_type="news",
            source_name="test",
            source_url="https://example.com",
            canonical_url="https://example.com/",
            title="Ocoopa wrongful death lawsuit",
            raw_text="Ocoopa wrongful death lawsuit after a hand warmer fire.",
            fetched_at=utcnow(),
            first_seen_at=utcnow(),
            matched_keywords=["Ocoopa wrongful death", "Ocoopa lawsuit"],
            content_hash="hash",
            event_fingerprint="fp",
            id=1,
        )
        result = AnalysisService(provider=DowngradingProvider()).analyze(mention)
        self.assertEqual(result.risk_level, "red")
        self.assertTrue(result.requires_escalation)
        self.assertIn("llm_downgrade_blocked", result.evidence_check_notes)

    def test_distinct_incident_locations_do_not_share_topic(self):
        new_york = topic_key(
            "Ocoopa hand warmer fire in New York", "burn injury", ["Ocoopa fire"]
        )
        texas = topic_key("Ocoopa hand warmer fire in Texas", "burn injury", ["Ocoopa fire"])
        self.assertNotEqual(new_york, texas)

    def test_bootstrap_stays_disabled_when_p0_fails(self):
        db, tmp = self.make_db(bootstrapped=False)
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher(error="source unavailable")},
        )
        with self.assertRaises(RuntimeError):
            pipeline.bootstrap(30)
        self.assertFalse(db.is_bootstrapped())

    def test_bootstrap_does_not_mark_unconfigured_optional_lanes_complete(self):
        db, tmp = self.make_db(bootstrapped=False)
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([])},
        )
        pipeline.bootstrap(30)
        self.assertTrue(db.is_bootstrapped())
        self.assertIsNone(db.get_state("public_social_backfill_completed_at"))
        self.assertIsNone(db.get_state("brandwatch_backfill_completed_at"))

    def test_outbox_retries_without_losing_first_discovery_notice(self):
        db, tmp = self.make_db()
        channel = FlakyChannel()
        delivery = DeliveryClient(channel)
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            delivery_client=delivery,
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        first = pipeline.run_lane("high")
        self.assertEqual(first["mention_batches_queued"], 1)
        self.assertEqual(first["deliveries_failed"], 1)
        with db.connect() as conn:
            notice = conn.execute(
                "SELECT delivery_status, delivered_at FROM mention_notifications"
            ).fetchone()
            conn.execute(
                "UPDATE delivery_outbox SET next_attempt_at=?",
                ((utcnow() - timedelta(seconds=1)).isoformat(),),
            )
        self.assertEqual(notice["delivery_status"], "pending")
        self.assertIsNone(notice["delivered_at"])
        retry = DeliveryOutboxWorker(db, delivery).drain()
        self.assertEqual(retry["sent"], 1)
        with db.connect() as conn:
            notice = conn.execute(
                "SELECT delivery_status, delivered_at FROM mention_notifications"
            ).fetchone()
            count = conn.execute("SELECT COUNT(*) AS n FROM mention_notifications").fetchone()["n"]
            kinds = [
                row["kind"]
                for row in conn.execute("SELECT kind FROM delivery_outbox ORDER BY id").fetchall()
            ]
        self.assertEqual(notice["delivery_status"], "sent")
        self.assertIsNotNone(notice["delivered_at"])
        self.assertEqual(count, 1)
        self.assertEqual(kinds, ["mention_batch"])

    def test_recall_registry_tracks_and_exports_new_external_item(self):
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        result = pipeline.run_lane("high")
        self.assertEqual(result["recall_mentions_registered"], 1)
        rows = db.list_recall_mentions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["campaign_key"], "ocoopa-cpsc-26-659")
        with tempfile.NamedTemporaryFile(suffix=".csv") as output:
            count = RecallRegistryService(db, pipeline).export_csv(output.name)
            self.assertEqual(count, 1)

    def test_routine_recall_items_are_delivered_as_one_summary_and_marked_synced(self):
        db, tmp = self.make_db()
        items = [
            self.recall_item("https://example.com/recall?article=1&tracking=very-long"),
            RawItem(
                source_type="social",
                source_name="test_high",
                source_url="https://example.com/recall?article=2&tracking=very-long",
                title="OCOOPA recall 26-659 discussion covers model UT3056",
                raw_text="Discussion of the CPSC fire and burn hazard recall for OCOOPA UT3056.",
                published_at=utcnow(),
            ),
        ]
        channel = CapturingChannel()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            analysis_service=RoutineRecallAnalysisService(),
            delivery_client=DeliveryClient(channel),
            fetchers={"static": StaticFetcher(items)},
        )

        result = pipeline.run_lane("high")

        self.assertEqual(result["mention_batches_queued"], 1)
        self.assertEqual(result["recall_updates_queued"], 0)
        self.assertEqual(len(channel.payloads), 1)
        payload = channel.payloads[0]
        self.assertNotIn("_recall_record_ids", payload)
        self.assertIn("#### 总览", payload["text"])
        self.assertIn("#### 重点信息", payload["text"])
        self.assertIn("#### 下一步", payload["text"])
        self.assertIn("[链接](https://example.com/", payload["text"])
        self.assertNotIn("原文：https://", payload["text"])
        self.assertLess(len(payload["text"]), 8000)
        self.assertTrue(all(row["sync_status"] == "synced" for row in db.list_recall_mentions()))
        with db.connect() as conn:
            jobs = conn.execute(
                "SELECT kind, status FROM delivery_outbox ORDER BY id"
            ).fetchall()
        self.assertEqual([(row["kind"], row["status"]) for row in jobs], [("mention_batch", "sent")])

    def test_pending_digest_is_not_duplicated_and_syncs_only_after_success(self):
        db, tmp = self.make_db()
        channel = FlakyChannel()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            analysis_service=RoutineRecallAnalysisService(),
            delivery_client=DeliveryClient(channel),
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        first = pipeline.run_lane("high")
        self.assertEqual(first["deliveries_failed"], 1)
        self.assertEqual(db.list_recall_mentions()[0]["sync_status"], "pending")
        service = RecallRegistryService(db, pipeline)
        self.assertEqual(service.queue_pending(), 0)
        with db.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM delivery_outbox WHERE kind='mention_batch'"
                ).fetchone()["n"],
                1,
            )
            conn.execute(
                "UPDATE delivery_outbox SET next_attempt_at=?",
                ((utcnow() - timedelta(seconds=1)).isoformat(),),
            )
        self.assertEqual(DeliveryOutboxWorker(db, DeliveryClient(channel)).drain()["sent"], 1)
        self.assertEqual(db.list_recall_mentions()[0]["sync_status"], "synced")

    def test_legacy_single_item_jobs_are_superseded_into_summary(self):
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        pipeline.run_lane("high", backfill=True)
        row = db.list_recall_mentions()[0]
        with db.connect() as conn:
            conn.execute("UPDATE mentions SET backfill=0 WHERE id=?", (row["mention_id"],))
        db.enqueue_delivery(
            kind="recall_update",
            dedupe_key="legacy-recall-item",
            entity_type="recall_mention",
            entity_id=row["recall_record_id"],
            payload={"title": "旧单条", "text": "旧单条内容"},
        )

        queued = RecallRegistryService(db, pipeline).queue_pending()

        self.assertEqual(queued, 1)
        with db.connect() as conn:
            jobs = conn.execute(
                "SELECT kind, status FROM delivery_outbox ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [(job["kind"], job["status"]) for job in jobs],
            [("recall_update", "superseded"), ("recall_digest", "pending")],
        )

    def test_backfill_recall_is_not_replayed_in_digest(self):
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        pipeline.run_lane("high", backfill=True)
        self.assertEqual(RecallRegistryService(db, pipeline).queue_pending(), 0)

    def test_human_suppressed_recall_is_not_reintroduced_by_digest(self):
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        pipeline.run_lane("high", backfill=True)
        row = db.list_recall_mentions()[0]
        with db.connect() as conn:
            conn.execute("UPDATE mentions SET backfill=0 WHERE id=?", (row["mention_id"],))
        self.assertTrue(db.review_incident(row["event_fingerprint"], "false_positive", None))
        self.assertEqual(RecallRegistryService(db, pipeline).queue_pending(), 0)

    def test_digest_and_daily_report_use_summary_template_and_short_link_label(self):
        url = "https://example.com/a/very/long/path?with=many&query=parameters"
        row = {
            "recall_record_id": 1,
            "risk_level": "yellow",
            "source_type": "news",
            "source_name": "Example News",
            "title": "OCOOPA recall coverage",
            "summary_zh": "召回报道摘要",
            "source_url": url,
            "needs_human_review": False,
        }
        digest = recall_digest_payload([row])["text"]
        daily = DailyReportService._render(
            report_date="2026-07-31",
            total=1,
            new_mentions=1,
            backfill_mentions=0,
            sentiment={"negative": 1},
            sources={"Example News": 1},
            risks={"yellow": 1},
            top_risks=[
                {
                    "title": row["title"],
                    "source": row["source_name"],
                    "url": url,
                    "risk_level": "yellow",
                    "summary_zh": row["summary_zh"],
                    "evidence_check_passed": True,
                    "needs_human_review": False,
                }
            ],
            recommended_actions=["继续监控。"],
        )
        for text in (digest, daily):
            self.assertIn("#### 总览", text)
            self.assertIn("#### 核心结论", text)
            self.assertIn(f"[链接]({url})", text)
            self.assertNotIn(f"来源：{url}", text)
        alert = DeliveryClient(CapturingChannel()).alert_payload(
            title="OCOOPA recall",
            url=url,
            risk_level="red",
            reason="test",
            confidence=0.9,
            evidence_check_passed=True,
            needs_human_review=False,
            sent_at=utcnow(),
        )
        self.assertIn(f"原文：[链接]({url})", alert["text"])
        self.assertNotIn(f"原文：{url}", alert["text"])

    def test_group_import_is_registered_as_already_synced(self):
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher()},
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", encoding="utf-8") as source:
            source.write(
                "platform,source_name,source_url,title,content,published_at,group_synced_at\n"
                "reddit,dingtalk_group_import,https://reddit.com/r/x/1,"
                "OCOOPA recall 26-659 model UT3053,"
                "CPSC fire and burn hazard recall,2026-07-30T12:00:00Z,"
                "2026-07-30T20:15:00+08:00\n"
            )
            source.flush()
            stats = RecallRegistryService(db, pipeline).import_group(source.name)
        self.assertEqual(stats["imported"], 1)
        row = db.list_recall_mentions()[0]
        self.assertEqual(row["origin"], "group_import")
        self.assertEqual(row["sync_status"], "synced")

    def test_rollout_backfill_registers_existing_mentions_without_replaying(self):
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        pipeline.run_lane("high", backfill=True)
        service = RecallRegistryService(db, pipeline)
        result = service.backfill_existing(assume_group_reported=True)
        self.assertEqual(result["registered"], 1)
        row = db.list_recall_mentions()[0]
        self.assertEqual(row["sync_status"], "synced")
        self.assertEqual(service.queue_pending(), 0)

    def test_latest_analysis_join_does_not_duplicate_daily_rows(self):
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self.recall_item()])},
        )
        pipeline.run_lane("high", backfill=True)
        with db.connect() as conn:
            mention_id = int(conn.execute("SELECT id FROM mentions").fetchone()["id"])
        db.insert_analysis(
            AnalysisResult(
                mention_id=mention_id,
                model_provider="test",
                model_name="second-analysis",
                prompt_version="test",
                sentiment="negative",
                risk_level="red",
                category="recall",
                summary_zh="第二次分析",
                key_quotes=[],
                key_quote_offsets=[],
                requires_escalation=True,
                escalation_reason="recall",
                confidence=0.9,
                evidence_check_passed=True,
                evidence_check_notes="test",
                needs_human_review=False,
            )
        )
        rows = db.fetch_mentions_between(utcnow() - timedelta(days=1), utcnow() + timedelta(days=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary_zh"], "第二次分析")

    def test_reddit_atom_feed_is_parsed(self):
        atom = b"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>OCOOPA recall 26-659 model UT3053</title>
            <link href="https://reddit.com/r/test/comments/abc"/>
            <published>2026-07-30T12:00:00Z</published>
            <author><name>reddit-user</name></author>
            <content>CPSC fire and burn hazard recall</content>
          </entry>
        </feed>"""

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return atom

        source = SourceConfig(
            "reddit_recall_atom", "social", "P1", "high", "generic_rss", "https://reddit.test"
        )
        with patch("ocoopa_monitor.fetchers.generic_rss.urlopen", return_value=FakeResponse()):
            items = GenericRSSFetcher().fetch(source, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].author_or_publisher, "reddit-user")
        self.assertTrue(is_current_recall(items[0].title, items[0].raw_text))

    def test_disabled_seed_keyword_remains_disabled(self):
        db, _ = self.make_db()
        term = DEFAULT_KEYWORDS[0].term
        self.assertTrue(db.set_keyword_active(term, False))
        db.seed_keywords(DEFAULT_KEYWORDS)
        states = {kw.term: kw.active for kw in db.list_keywords_all()}
        self.assertFalse(states[term])


if __name__ == "__main__":
    unittest.main()
