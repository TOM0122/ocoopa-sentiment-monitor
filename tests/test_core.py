from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from ocoopa_monitor.analysis import AnalysisService
from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database, dt_to_str
from ocoopa_monitor.delivery import DeliveryClient, DingTalkRobotChannel
from ocoopa_monitor.evidence import EvidenceChecker
from ocoopa_monitor.fetchers.base import Fetcher
from ocoopa_monitor.fetchers.search_api import SerpAPIFetcher
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.llm import DeepSeekProvider
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
            alert_channel="generic",
            alert_webhook_url="",
            alert_webhook_secret="",
            alert_at_mobiles="",
            alert_rate_limit_per_minute=20,
            llm_provider="rule",
            llm_model="deepseek-v4-flash",
            llm_api_key="",
            llm_base_url="https://api.deepseek.com",
            serpapi_api_key="",
            gnews_api_key="",
            high_lane_interval_minutes=15,
            regular_lane_interval_minutes=60,
            p0_health_threshold_minutes=120,
            backfill_days=180,
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
        self.assertIsNotNone(alert["delivery_latency_seconds"])

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

    def test_evidence_checker_flags_ungrounded_chinese_entity(self):
        result = EvidenceChecker().check(
            raw_text="Ocoopa recall mentioned by CPSC in source text after a fire report.",
            source_url="https://example.com",
            summary_zh="Ocoopa 暖手宝被加州法院召回。",
            escalation_reason="召回",
            key_quotes=["Ocoopa recall mentioned by CPSC"],
            confidence=0.91,
            risk_level="red",
        )
        self.assertFalse(result.passed)
        self.assertIn("summary_not_grounded", result.notes)
        self.assertTrue(result.needs_human_review)

    def test_evidence_checker_accepts_chinese_risk_terms_grounded_by_english_raw(self):
        result = EvidenceChecker().check(
            raw_text="Ocoopa wrongful death lawsuit after a hand warmer fire.",
            source_url="https://example.com",
            summary_zh="Ocoopa 涉及过失致死诉讼和起火风险。",
            escalation_reason="过失致死; 诉讼; 起火",
            key_quotes=["Ocoopa wrongful death lawsuit"],
            confidence=0.91,
            risk_level="red",
        )
        self.assertTrue(result.passed)

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

    def test_deepseek_provider_parses_schema_json(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'{"choices":[{"message":{"content":"{\\"sentiment\\":\\"negative\\",'
                    b'\\"risk_level\\":\\"red\\",\\"category\\":\\"lawsuit\\",'
                    b'\\"summary_zh\\":\\"Ocoopa lawsuit\\",'
                    b'\\"key_quotes\\":[\\"Ocoopa lawsuit\\"],'
                    b'\\"requires_escalation\\":true,'
                    b'\\"escalation_reason\\":\\"lawsuit\\",\\"confidence\\":0.9}"}}]}'
                )

        mention = RawItem(
            source_type="news",
            source_name="unit",
            source_url="https://example.com",
            title="Ocoopa lawsuit",
            raw_text="Ocoopa lawsuit",
        )
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(db, settings(tmp.name), fetchers={"static": StaticFetcher([mention])})
        stored = pipeline._build_mention(mention, ["Ocoopa lawsuit"], backfill=False)
        stored.id = 1
        decision = RiskRuleEngine().evaluate(stored.title, stored.raw_text, stored.matched_keywords)
        with patch("ocoopa_monitor.llm.urlopen", return_value=FakeResponse()):
            payload = DeepSeekProvider(api_key="secret").analyze(stored, decision)
        self.assertEqual(payload["risk_level"], "red")

    def test_dingtalk_channel_builds_signed_markdown_request(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"errcode":0,"errmsg":"ok"}'

        def fake_urlopen(request, timeout=10):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse()

        channel = DingTalkRobotChannel(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=abc",
            secret="ding-secret",
            at_mobiles=["13800000000"],
            rate_limit_per_minute=20,
        )
        payload = DeliveryClient(channel).alert_payload(
            title="Ocoopa fire lawsuit",
            url="https://example.com",
            risk_level="red",
            reason="lawsuit",
            confidence=0.9,
            evidence_check_passed=True,
            needs_human_review=False,
            sent_at=utcnow(),
            delivery_latency_seconds=42,
        )
        with patch("ocoopa_monitor.delivery.urlopen", side_effect=fake_urlopen):
            sent_to = DeliveryClient(channel).send_alert(payload)
        self.assertEqual(sent_to, "https://oapi.dingtalk.com/robot/send?access_token=abc")
        self.assertIn("timestamp=", captured["url"])
        self.assertIn("sign=", captured["url"])
        self.assertIn('"msgtype": "markdown"', captured["body"])
        self.assertIn("13800000000", captured["body"])

    def test_serpapi_uses_single_high_sensitivity_boolean_query(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"organic_results":[{"title":"Ocoopa lawsuit","link":"https://example.com","snippet":"fire"}]}'

        def fake_urlopen(request, timeout=20):
            captured["url"] = request.full_url
            return FakeResponse()

        source = SourceConfig("serpapi_high_search", "search", "P0", "high", "serpapi", "https://serpapi.com")
        with patch("ocoopa_monitor.fetchers.search_api.urlopen", side_effect=fake_urlopen):
            items = SerpAPIFetcher(api_key="secret").fetch(source, ["Ocoopa lawsuit"])
        self.assertEqual(len(items), 1)
        self.assertIn("Ocoopa+%28fire+OR+death+OR+lawsuit+OR+recall+OR+CPSC+OR+%22class+action%22%29", captured["url"])


if __name__ == "__main__":
    unittest.main()
