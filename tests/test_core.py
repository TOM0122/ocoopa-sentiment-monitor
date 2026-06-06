from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError

from ocoopa_monitor.analysis import AnalysisService
from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database, dt_to_str
from ocoopa_monitor.delivery import DeliveryClient, DingTalkRobotChannel
from ocoopa_monitor.doctor import run_doctor
from ocoopa_monitor.evidence import EvidenceChecker
from ocoopa_monitor.fetchers.base import Fetcher
from ocoopa_monitor.fetchers.search_api import BraveSearchFetcher, SerpAPIFetcher
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.llm import MAX_LLM_RAW_TEXT_CHARS, DeepSeekProvider
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
        brave_search_api_key="",
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

    def test_evidence_checker_accepts_bilingual_entity_aliases(self):
        result = EvidenceChecker().check(
            raw_text="Amazon and Ocoopa were named in a California wrongful death lawsuit. CPSC records were cited.",
            source_url="https://example.com",
            summary_zh="亚马逊和 Ocoopa 涉及加州过失致死诉讼，美国消费品安全委员会记录被提及。",
            escalation_reason="过失致死; 诉讼; CPSC",
            key_quotes=["California wrongful death lawsuit"],
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

    def test_deepseek_provider_truncates_raw_text_and_retries_retryable_errors(self):
        captured = {"calls": 0, "raw_text": ""}

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

        def fake_urlopen(request, timeout=20):
            captured["calls"] += 1
            body = request.data.decode("utf-8")
            payload = __import__("json").loads(body)
            user_payload = __import__("json").loads(payload["messages"][1]["content"])
            captured["raw_text"] = user_payload["input"]["raw_text"]
            if captured["calls"] == 1:
                raise HTTPError(request.full_url, 429, "rate limited", {"Retry-After": "0"}, None)
            return FakeResponse()

        mention = RawItem(
            source_type="news",
            source_name="unit",
            source_url="https://example.com",
            title="Ocoopa lawsuit",
            raw_text="Ocoopa lawsuit " + ("x" * (MAX_LLM_RAW_TEXT_CHARS + 2000)),
        )
        db, tmp = self.make_db()
        pipeline = MonitorPipeline(db, settings(tmp.name), fetchers={"static": StaticFetcher([mention])})
        stored = pipeline._build_mention(mention, ["Ocoopa lawsuit"], backfill=False)
        stored.id = 1
        decision = RiskRuleEngine().evaluate(stored.title, stored.raw_text, stored.matched_keywords)
        with patch("ocoopa_monitor.llm.urlopen", side_effect=fake_urlopen), patch("ocoopa_monitor.llm.time.sleep"):
            payload = DeepSeekProvider(api_key="secret").analyze(stored, decision)
        self.assertEqual(payload["risk_level"], "red")
        self.assertEqual(captured["calls"], 2)
        self.assertLessEqual(len(captured["raw_text"]), MAX_LLM_RAW_TEXT_CHARS + len("\n[TRUNCATED]"))
        self.assertTrue(captured["raw_text"].endswith("[TRUNCATED]"))

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

    def test_production_doctor_requires_deepseek_and_dingtalk_secrets(self):
        report = run_doctor(settings("/tmp/test.db"), production=True)
        self.assertFalse(report.ok)
        self.assertTrue(any("OCOOPA_LLM_PROVIDER=deepseek" in error for error in report.errors))
        self.assertTrue(any("OCOOPA_ALERT_CHANNEL=dingtalk" in error for error in report.errors))
        self.assertFalse(report.settings_summary["llm_api_key_configured"])

    def test_production_doctor_passes_with_required_secret_flags(self):
        base = settings("/tmp/test.db")
        prod_settings = type(base)(
            db_path=base.db_path,
            alert_channel="dingtalk",
            alert_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=xxx",
            alert_webhook_secret="secret",
            alert_at_mobiles="13800000000",
            alert_rate_limit_per_minute=20,
            llm_provider="deepseek",
            llm_model="deepseek-v4-flash",
            llm_api_key="key",
            llm_base_url="https://api.deepseek.com",
            serpapi_api_key="serp",
            brave_search_api_key="brave",
            gnews_api_key="gnews",
            high_lane_interval_minutes=base.high_lane_interval_minutes,
            regular_lane_interval_minutes=base.regular_lane_interval_minutes,
            p0_health_threshold_minutes=base.p0_health_threshold_minutes,
            backfill_days=180,
            request_timeout_seconds=base.request_timeout_seconds,
        )
        report = run_doctor(prod_settings, production=True)
        self.assertTrue(report.ok)
        self.assertEqual(report.errors, [])

    def test_brave_search_uses_single_high_sensitivity_boolean_query(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'{"web":{"results":[{"title":"Ocoopa lawsuit",'
                    b'"url":"https://example.com","description":"fire","age":"1 day ago"}]}}'
                )

        def fake_urlopen(request, timeout=20):
            captured["url"] = request.full_url
            captured["token"] = request.headers.get("X-subscription-token")
            return FakeResponse()

        source = SourceConfig(
            "brave_high_search",
            "search",
            "P0",
            "high",
            "brave_search",
            "https://api.search.brave.com/res/v1/web/search",
        )
        with patch("ocoopa_monitor.fetchers.search_api.urlopen", side_effect=fake_urlopen):
            items = BraveSearchFetcher(api_key="secret").fetch(source, ["Ocoopa lawsuit"])
        self.assertEqual(len(items), 1)
        self.assertIn("Ocoopa+%28fire+OR+death+OR+lawsuit+OR+recall+OR+CPSC+OR+%22class+action%22%29", captured["url"])
        self.assertIn("freshness=pd", captured["url"])
        self.assertEqual(captured["token"], "secret")

    def test_brave_regular_query_includes_category_level_hand_warmer_terms(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"web":{"results":[]}}'

        def fake_urlopen(request, timeout=20):
            captured["url"] = request.full_url
            return FakeResponse()

        source = SourceConfig(
            "brave_regular_search",
            "search",
            "P1",
            "regular",
            "brave_search",
            "https://api.search.brave.com/res/v1/web/search",
        )
        with patch("ocoopa_monitor.fetchers.search_api.urlopen", side_effect=fake_urlopen):
            BraveSearchFetcher(api_key="secret").fetch(source, ["hand warmer fire"])
        self.assertIn("%22hand+warmer%22", captured["url"])
        self.assertIn("lawsuit", captured["url"])
        self.assertIn("freshness=py", captured["url"])

    def test_search_api_query_hit_is_retained_without_exact_brand_in_snippet(self):
        db, tmp = self.make_db()
        item = RawItem(
            source_type="search",
            source_name="brave_high_search",
            source_url="https://example.com/amazon-hand-warmer-lawsuit",
            title="Amazon hand warmers lawsuit claims defective products sparked fire",
            raw_text="Lawsuit claims defective hand warmers sparked a fatal fire.",
            published_at=utcnow(),
            tos_method="api",
        )
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([item])},
        )
        result = pipeline.run_lane("high")
        self.assertEqual(result["mentions_processed"], 1)
        self.assertEqual(result["items_matched"], 1)
        self.assertEqual(result["items_filtered_no_keywords"], 0)
        with db.connect() as conn:
            mention = conn.execute("SELECT matched_keywords FROM mentions").fetchone()
        self.assertIn("search_api_query_hit", mention["matched_keywords"])
        self.assertNotIn("Ocoopa", mention["matched_keywords"])

        duplicate_result = pipeline.run_lane("high")
        self.assertEqual(duplicate_result["mentions_processed"], 0)
        self.assertEqual(duplicate_result["items_duplicate_skipped"], 1)

    def test_init_migrates_existing_sqlite_alert_schema(self):
        tmp = tempfile.NamedTemporaryFile(delete=True)
        db = Database(tmp.name)
        with db.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mention_id INTEGER NOT NULL,
                    incident_group_id INTEGER,
                    risk_level TEXT NOT NULL,
                    alert_reason TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    confidence REAL NOT NULL,
                    evidence_check_passed INTEGER NOT NULL,
                    needs_human_review INTEGER NOT NULL,
                    sent_to TEXT,
                    sent_at TEXT,
                    ack_status TEXT NOT NULL DEFAULT 'pending',
                    muted_until TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
        db.init()
        with db.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        self.assertIn("delivery_latency_seconds", columns)


if __name__ == "__main__":
    unittest.main()
