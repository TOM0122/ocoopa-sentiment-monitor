from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from ocoopa_monitor.analysis import AnalysisService
from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database, PostgresDatabase, create_database, dt_to_str
from ocoopa_monitor.delivery import DeliveryClient, DingTalkRobotChannel
from ocoopa_monitor.doctor import run_doctor
from ocoopa_monitor.evidence import EvidenceChecker
from ocoopa_monitor.fetchers.base import Fetcher
from ocoopa_monitor.fetchers.search_api import BraveSearchFetcher, SerpAPIFetcher
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.llm import DeepSeekProvider
from ocoopa_monitor.models import AnalysisResult, RawItem, SourceConfig, utcnow
from ocoopa_monitor.pipeline import MonitorPipeline
from ocoopa_monitor.reports import DailyReportService
from ocoopa_monitor.risk import RiskRuleEngine
from ocoopa_monitor.scheduler import SimpleScheduler
from ocoopa_monitor.sources import DEFAULT_SOURCES


class StaticFetcher(Fetcher):
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def fetch(self, source, keywords, since=None):
        if self.error:
            raise RuntimeError(self.error)
        return self.items


class NeedsReviewRedAnalysisService:
    def analyze(self, mention):
        return AnalysisResult(
            mention_id=mention.id,
            model_provider="test",
            model_name="needs-review-red",
            prompt_version="test",
            sentiment="negative",
            risk_level="red",
            category="lawsuit",
            summary_zh="需人工核实的红色风险",
            key_quotes=[],
            key_quote_offsets=[],
            requires_escalation=True,
            escalation_reason="low_confidence_red_signal",
            confidence=0.42,
            evidence_check_passed=False,
            evidence_check_notes="low_confidence; summary_not_grounded",
            needs_human_review=True,
        )


def settings(db_path):
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
        review_token="",
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
    def make_db(self, bootstrap: bool = True):
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
        if bootstrap:
            db.mark_bootstrapped()
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

    def test_dingtalk_does_not_at_mobiles_for_human_review_red_alert(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"errcode":0,"errmsg":"ok"}'

        def fake_urlopen(request, timeout=10):
            captured["body"] = request.data.decode("utf-8")
            return FakeResponse()

        channel = DingTalkRobotChannel(
            webhook_url="https://oapi.dingtalk.com/robot/send?access_token=abc",
            secret="ding-secret",
            at_mobiles=["13800000000"],
            rate_limit_per_minute=20,
        )
        payload = DeliveryClient(channel).alert_payload(
            title="Ocoopa low confidence red",
            url="https://example.com",
            risk_level="red",
            reason="low_confidence_red_signal",
            confidence=0.42,
            evidence_check_passed=False,
            needs_human_review=True,
            sent_at=utcnow(),
            delivery_latency_seconds=42,
        )
        with patch("ocoopa_monitor.delivery.urlopen", side_effect=fake_urlopen):
            DeliveryClient(channel).send_alert(payload)
        self.assertIn("需人工核实", captured["body"])
        self.assertIn('"atMobiles": []', captured["body"])
        self.assertNotIn("13800000000", captured["body"])

    def test_needs_review_red_alert_is_not_silently_withheld(self):
        db, tmp = self.make_db()
        item = RawItem(
            source_type="news",
            source_name="test_high",
            source_url="https://example.com/ocoopa-unverified-red",
            title="Ocoopa unverified red risk",
            raw_text="Ocoopa lawsuit signal with limited evidence.",
            published_at=utcnow(),
        )
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            analysis_service=NeedsReviewRedAnalysisService(),
            fetchers={"static": StaticFetcher([item])},
        )
        result = pipeline.run_lane("high")
        self.assertEqual(result["alerts_created"], 1)
        with db.connect() as conn:
            alert = conn.execute("SELECT * FROM alerts").fetchone()
        self.assertEqual(alert["risk_level"], "red")
        self.assertEqual(alert["evidence_check_passed"], 0)
        self.assertEqual(alert["needs_human_review"], 1)

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
        self.assertTrue(any("OCOOPA_DB_URL" in error or "DATABASE_URL" in error for error in report.errors))
        self.assertTrue(any("OCOOPA_LLM_PROVIDER=deepseek" in error for error in report.errors))
        self.assertTrue(any("OCOOPA_ALERT_CHANNEL=dingtalk" in error for error in report.errors))
        self.assertFalse(report.settings_summary["llm_api_key_configured"])

    def test_production_doctor_passes_with_required_secret_flags(self):
        base = settings("/tmp/test.db")
        prod_settings = type(base)(
            db_path=base.db_path,
            db_url="postgresql://user:pass@example.com:5432/db",
            alert_channel="dingtalk",
            alert_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=xxx",
            alert_webhook_secret="secret",
            alert_at_mobiles="13800000000",
            alert_rate_limit_per_minute=20,
            alert_cooldown_hours=6,
            alert_ack_timeout_minutes=30,
            review_token="",
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
        self.assertEqual(report.settings_summary["db_backend"], "postgres")
        self.assertTrue(report.settings_summary["db_url_configured"])

    def test_database_factory_prefers_postgres_url(self):
        base = settings("/tmp/local.db")
        pg_settings = type(base)(
            db_path=base.db_path,
            db_url="postgresql://user:pass@example.com:5432/db",
            alert_channel=base.alert_channel,
            alert_webhook_url=base.alert_webhook_url,
            alert_webhook_secret=base.alert_webhook_secret,
            alert_at_mobiles=base.alert_at_mobiles,
            alert_rate_limit_per_minute=base.alert_rate_limit_per_minute,
            alert_cooldown_hours=base.alert_cooldown_hours,
            alert_ack_timeout_minutes=base.alert_ack_timeout_minutes,
            review_token=base.review_token,
            llm_provider=base.llm_provider,
            llm_model=base.llm_model,
            llm_api_key=base.llm_api_key,
            llm_base_url=base.llm_base_url,
            serpapi_api_key=base.serpapi_api_key,
            brave_search_api_key=base.brave_search_api_key,
            gnews_api_key=base.gnews_api_key,
            high_lane_interval_minutes=base.high_lane_interval_minutes,
            regular_lane_interval_minutes=base.regular_lane_interval_minutes,
            p0_health_threshold_minutes=base.p0_health_threshold_minutes,
            backfill_days=base.backfill_days,
            request_timeout_seconds=base.request_timeout_seconds,
        )
        self.assertIsInstance(create_database(pg_settings), PostgresDatabase)
        self.assertIsInstance(create_database(base), Database)

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
        self.assertEqual(captured["token"], "secret")

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

    def _red_item(self, url: str) -> RawItem:
        return RawItem(
            source_type="news",
            source_name="test_high",
            source_url=url,
            title="Ocoopa wrongful death lawsuit filed",
            raw_text="Ocoopa wrongful death lawsuit filed after a hand warmer fire.",
            published_at=utcnow(),
        )

    def test_realtime_alert_suppressed_until_bootstrap(self):
        db, tmp = self.make_db(bootstrap=False)
        self.assertFalse(db.is_bootstrapped())
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self._red_item("https://example.com/a")])},
        )
        result = pipeline.run_lane("high")
        # Pre-bootstrap: ingested + analyzed, but no alert fired.
        self.assertEqual(result["mentions_processed"], 1)
        self.assertEqual(result["alerts_created"], 0)
        self.assertEqual(result["alerts_suppressed_pre_bootstrap"], 1)
        with db.connect() as conn:
            self.assertEqual(len(conn.execute("SELECT 1 FROM alerts").fetchall()), 0)
            self.assertEqual(len(conn.execute("SELECT 1 FROM mentions").fetchall()), 1)
        # After bootstrap, a genuinely new item alerts.
        db.mark_bootstrapped()
        pipeline2 = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self._red_item("https://example.com/b")])},
        )
        result2 = pipeline2.run_lane("high")
        self.assertEqual(result2["alerts_created"], 1)

    def test_bootstrap_runs_silently_and_enables_alerts(self):
        db, tmp = self.make_db(bootstrap=False)
        pipeline = MonitorPipeline(
            db,
            settings(tmp.name),
            fetchers={"static": StaticFetcher([self._red_item("https://example.com/seed")])},
        )
        stats = pipeline.bootstrap(180)
        self.assertTrue(db.is_bootstrapped())
        self.assertEqual(stats["high"]["alerts_created"], 0)
        with db.connect() as conn:
            self.assertEqual(len(conn.execute("SELECT 1 FROM alerts").fetchall()), 0)
            backfilled = conn.execute("SELECT backfill FROM mentions").fetchall()
        self.assertTrue(all(row["backfill"] == 1 for row in backfilled))

    def test_high_lane_uses_only_free_sources(self):
        metered = {"brave_search", "serpapi", "gnews"}
        free = {"rss", "api", "generic_rss"}
        high_methods = {s.method for s in DEFAULT_SOURCES if s.lane == "high"}
        self.assertFalse(high_methods & metered, f"high lane must be free-only, got {high_methods}")
        self.assertTrue(free >= high_methods, f"unexpected high-lane method: {high_methods - free}")
        # Commercial APIs still present, but only on the hourly regular lane.
        regular_methods = {s.method for s in DEFAULT_SOURCES if s.lane == "regular"}
        self.assertIn("brave_search", regular_methods)
        self.assertIn("gnews", regular_methods)

    def test_legal_lead_gen_source_is_configured(self):
        legal = [s for s in DEFAULT_SOURCES if s.source_type == "legal"]
        self.assertTrue(legal, "expected a legal lead-gen source for class-action recruitment")
        src = legal[0]
        self.assertEqual(src.lane, "high")
        self.assertEqual(src.method, "generic_rss")
        self.assertTrue(src.url.startswith("https://"))

    def test_seed_sources_deactivates_removed_source(self):
        tmp = tempfile.NamedTemporaryFile(delete=True)
        db = Database(tmp.name)
        db.init()
        db.seed_sources(
            [
                SourceConfig("keep_me", "news", "P0", "high", "rss", "x://keep", alert_threshold_minutes=120),
                SourceConfig("drop_me", "search", "P1", "regular", "brave_search", "x://drop", alert_threshold_minutes=360),
            ]
        )
        self.assertEqual({s.source_name for s in db.get_sources()}, {"keep_me", "drop_me"})
        db.seed_sources(
            [SourceConfig("keep_me", "news", "P0", "high", "rss", "x://keep", alert_threshold_minutes=120)]
        )
        self.assertEqual({s.source_name for s in db.get_sources()}, {"keep_me"})

    def test_false_positive_suppresses_future_alerts(self):
        db, tmp = self.make_db()
        p1 = MonitorPipeline(
            db, settings(tmp.name),
            fetchers={"static": StaticFetcher([self._red_item("https://example.com/fp-a")])},
        )
        self.assertEqual(p1.run_lane("high")["alerts_created"], 1)
        fp = db.get_fingerprint_by_alert(db.list_recent_alerts(1)[0]["alert_id"])
        self.assertTrue(db.review_incident(fp, "false_positive", None))
        self.assertTrue(db.is_incident_suppressed(fp))
        # Same event (same fingerprint, new URL) must no longer alert.
        p2 = MonitorPipeline(
            db, settings(tmp.name),
            fetchers={"static": StaticFetcher([self._red_item("https://example.com/fp-b")])},
        )
        r2 = p2.run_lane("high")
        self.assertEqual(r2["alerts_created"], 0)
        self.assertEqual(r2["alerts_suppressed_muted"], 1)
        with db.connect() as conn:
            statuses = {row["review_status"] for row in conn.execute("SELECT review_status FROM analysis_results")}
        self.assertIn("false_positive", statuses)

    def test_mute_window_and_confirmed(self):
        db, tmp = self.make_db()
        p = MonitorPipeline(
            db, settings(tmp.name),
            fetchers={"static": StaticFetcher([self._red_item("https://example.com/m-a")])},
        )
        p.run_lane("high")
        fp = db.get_fingerprint_by_alert(db.list_recent_alerts(1)[0]["alert_id"])
        db.review_incident(fp, "muted", utcnow() + timedelta(days=7))
        self.assertTrue(db.is_incident_suppressed(fp))
        # Expired mute no longer suppresses.
        db.review_incident(fp, "muted", utcnow() - timedelta(days=1))
        self.assertFalse(db.is_incident_suppressed(fp))
        # Confirmed keeps alerting (not suppressed).
        db.review_incident(fp, "confirmed", None)
        self.assertFalse(db.is_incident_suppressed(fp))

    def test_review_incident_unknown_fingerprint_returns_false(self):
        db, _ = self.make_db()
        self.assertFalse(db.review_incident("nonexistent-fp", "muted", None))

    def test_daily_report_delivers_to_channel(self):
        db, tmp = self.make_db()
        captured = []

        class FakeDelivery:
            def send_text(self, title, text, suppress_at=True):
                captured.append((title, suppress_at))
                return "dingtalk"

        rep = DailyReportService(db, FakeDelivery()).generate("Asia/Shanghai")
        self.assertEqual(rep["delivery_status"], "delivered")
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0][1])  # suppress_at: routine push, no @
        # No client -> stays stored.
        self.assertEqual(DailyReportService(db).generate("Asia/Shanghai")["delivery_status"], "stored")

    def test_scheduler_pages_p0_unhealthy_once_and_excludes_p1(self):
        db, tmp = self.make_db()
        db.seed_sources(
            [
                SourceConfig("p0_news", "news", "P0", "high", "static", "x://p0", alert_threshold_minutes=120),
                SourceConfig("p1_brave", "search", "P1", "regular", "brave_search", "x://p1", alert_threshold_minutes=360),
            ]
        )
        with db.connect() as conn:
            conn.execute(
                "UPDATE source_health SET last_success_at=?, health_status='failing', consecutive_failures=3",
                (dt_to_str(utcnow() - timedelta(hours=5)),),
            )
        captured = []

        class FakeDelivery:
            def send_text(self, title, text, suppress_at=True):
                captured.append(text)
                return "x"

        sch = SimpleScheduler(db, settings(tmp.name))
        sch.delivery = FakeDelivery()
        now = utcnow()
        sch._maybe_health_check(now)
        self.assertEqual(len(captured), 1)
        self.assertIn("p0_news", captured[0])
        self.assertNotIn("p1_brave", captured[0])  # P1 quota failures are not paged
        # Re-check: already alerted -> no duplicate page.
        sch._maybe_health_check(now + timedelta(minutes=31))
        self.assertEqual(len(captured), 1)

    def test_keyword_cli_db_add_disable_and_hot_filter(self):
        db, _ = self.make_db()
        db.upsert_keyword("Ocoopa HR-12X fire", "model", "high")
        self.assertIn("Ocoopa HR-12X fire", {k.term for k in db.get_keywords("high")})
        self.assertTrue(db.set_keyword_active("Ocoopa HR-12X fire", False))
        self.assertNotIn("Ocoopa HR-12X fire", {k.term for k in db.get_keywords("high")})
        self.assertIn("Ocoopa HR-12X fire", {k.term for k in db.list_keywords_all()})
        self.assertFalse(db.set_keyword_active("does-not-exist", True))

    def _red_news(self, url: str, title: str, source_type: str = "news") -> RawItem:
        return RawItem(
            source_type=source_type,
            source_name="test_high",
            source_url=url,
            title=title,
            raw_text=f"{title} — Ocoopa wrongful death lawsuit after a hand warmer fire.",
            published_at=utcnow(),
        )

    def _run_one(self, db, tmp, item):
        return MonitorPipeline(
            db, settings(tmp.name), fetchers={"static": StaticFetcher([item])}
        ).run_lane("high")

    def test_topic_key_clusters_same_event_across_outlets(self):
        from ocoopa_monitor.normalize import topic_key

        a = topic_key("Ocoopa death lawsuit filed", "wrongful death after fire", ["Ocoopa"])
        b = topic_key("Family sues Ocoopa after fatal fire", "lawsuit and death", ["Ocoopa"])
        self.assertEqual(a, b)
        self.assertNotEqual(a, topic_key("Ocoopa recall notice", "cpsc recall", ["Ocoopa recall"]))

    def test_cross_source_cooldown_dedups_same_topic(self):
        db, tmp = self.make_db()
        r1 = self._run_one(db, tmp, self._red_news("https://a.com/1", "Ocoopa death lawsuit filed in California"))
        self.assertEqual(r1["alerts_created"], 1)
        # Different outlet + different article, SAME topic + same source_type -> suppressed.
        r2 = self._run_one(db, tmp, self._red_news("https://b.com/2", "Family sues Ocoopa after fatal fire"))
        self.assertEqual(r2["alerts_created"], 0)
        self.assertEqual(r2["alerts_suppressed_cooldown"], 1)

    def test_new_source_type_breaks_through_cooldown(self):
        db, tmp = self.make_db()
        self._run_one(db, tmp, self._red_news("https://a.com/1", "Ocoopa death lawsuit filed"))
        # Same topic, but a NEW source type (CPSC) must break through the cooldown.
        r = self._run_one(
            db, tmp, self._red_news("https://cpsc.gov/x", "Ocoopa death lawsuit official", source_type="cpsc")
        )
        self.assertEqual(r["alerts_created"], 1)

    def test_review_web_token_gate(self):
        from ocoopa_monitor.review_web import token_ok

        self.assertTrue(token_ok("", "anything"))  # open when unset
        self.assertTrue(token_ok("s3cret", "s3cret"))
        self.assertFalse(token_ok("s3cret", "wrong"))
        self.assertFalse(token_ok("s3cret", ""))

    def test_review_web_apply_mark_and_render(self):
        from ocoopa_monitor.review_web import apply_mark, render_review_page

        db, tmp = self.make_db()
        self._run_one(db, tmp, self._red_news("https://a.com/1", "Ocoopa death lawsuit filed"))
        aid = db.list_recent_alerts(1)[0]["alert_id"]
        ok, _ = apply_mark(db, aid, "false_positive")
        self.assertTrue(ok)
        self.assertTrue(db.is_incident_suppressed(db.get_fingerprint_by_alert(aid)))
        self.assertFalse(apply_mark(db, aid, "bogus")[0])
        self.assertFalse(apply_mark(db, 999999, "muted")[0])
        html = render_review_page(db.list_recent_alerts(10), token="t")
        self.assertIn("/review/mark", html)
        self.assertIn("误报", html)

    def _make_aged_red_alert(self, db, tmp):
        self._run_one(db, tmp, self._red_news("https://a.com/1", "Ocoopa death lawsuit filed"))
        aid = db.list_recent_alerts(1)[0]["alert_id"]
        with db.connect() as conn:
            conn.execute(
                "UPDATE alerts SET sent_at=? WHERE id=?",
                (dt_to_str(utcnow() - timedelta(hours=1)), aid),
            )
        return aid

    def test_unacked_red_alert_escalates_once(self):
        db, tmp = self.make_db()
        aid = self._make_aged_red_alert(db, tmp)
        captured = []

        class FakeDelivery:
            def send_text(self, title, text, suppress_at=True):
                captured.append(text)
                return "x"

        sch = SimpleScheduler(db, settings(tmp.name))
        sch.delivery = FakeDelivery()
        sch._maybe_escalate_unacked(utcnow())
        self.assertEqual(len(captured), 1)
        self.assertIn(str(aid), captured[0])
        # Already escalated -> not paged again.
        sch._maybe_escalate_unacked(utcnow())
        self.assertEqual(len(captured), 1)

    def test_review_ack_prevents_escalation(self):
        from ocoopa_monitor.review_web import apply_mark

        db, tmp = self.make_db()
        aid = self._make_aged_red_alert(db, tmp)
        self.assertTrue(apply_mark(db, aid, "confirmed")[0])  # ack via review
        captured = []

        class FakeDelivery:
            def send_text(self, title, text, suppress_at=True):
                captured.append(text)
                return "x"

        sch = SimpleScheduler(db, settings(tmp.name))
        sch.delivery = FakeDelivery()
        sch._maybe_escalate_unacked(utcnow())
        self.assertEqual(len(captured), 0)

    def test_console_dashboard_search_export(self):
        from ocoopa_monitor.console_web import (
            compute_dashboard,
            filter_rows,
            render_dashboard,
            render_search,
            rows_to_csv,
        )

        db, tmp = self.make_db()
        self._run_one(db, tmp, self._red_news("https://a.com/1", "Ocoopa death lawsuit filed"))
        rows = db.fetch_mentions_between(utcnow() - timedelta(days=1), utcnow() + timedelta(days=1))
        stats = compute_dashboard(rows, db.list_recent_alerts(100))
        self.assertGreaterEqual(stats["total"], 1)
        self.assertIn("red", stats["risk"])
        self.assertTrue(stats["top"])
        # search filtering
        self.assertTrue(filter_rows(rows, q="ocoopa"))
        self.assertEqual(filter_rows(rows, q="zzz-no-match"), [])
        self.assertTrue(all(r["risk_level"] == "red" for r in filter_rows(rows, risk="red")))
        # CSV export
        csv_text = rows_to_csv(rows)
        self.assertIn("source_url", csv_text.splitlines()[0])
        self.assertIn("https://a.com/1", csv_text)
        # HTML renders
        self.assertIn("舆情看板", render_dashboard(stats, 30, "t"))
        self.assertIn("检索", render_search(filter_rows(rows), "", "", 30, "t"))


if __name__ == "__main__":
    unittest.main()
