from __future__ import annotations

import json
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database
from ocoopa_monitor.evidence import EvidenceChecker
from ocoopa_monitor.fetchers.search_api import BraveSearchFetcher
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.llm import MAX_LLM_RAW_TEXT_CHARS, DeepSeekProvider
from ocoopa_monitor.models import RawItem, SourceConfig, utcnow
from ocoopa_monitor.pipeline import MonitorPipeline
from ocoopa_monitor.risk import RiskRuleEngine


class StaticFetcher:
    def __init__(self, items):
        self.items = items

    def fetch(self, source, keywords, since=None):
        return self.items


def settings(db_path):
    return Settings(
        db_path=db_path,
        db_url="",
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


class ThirdRoundOptimizationTests(unittest.TestCase):
    def make_db(self):
        tmp = tempfile.NamedTemporaryFile(delete=True)
        db = Database(tmp.name)
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(
            [
                SourceConfig(
                    source_name="test_high",
                    source_type="search",
                    priority="P0",
                    lane="high",
                    method="static",
                    url="test://high",
                    alert_threshold_minutes=120,
                )
            ]
        )
        return db, tmp

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

    def test_search_api_query_hit_does_not_inject_synthetic_brand_keyword(self):
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
        pipeline = MonitorPipeline(db, settings(tmp.name), fetchers={"static": StaticFetcher([item])})
        result = pipeline.run_lane("high")
        self.assertEqual(result["mentions_processed"], 1)
        with db.connect() as conn:
            mention = conn.execute("SELECT matched_keywords FROM mentions").fetchone()
        self.assertIn("search_api_query_hit", mention["matched_keywords"])
        self.assertNotIn("Ocoopa", mention["matched_keywords"])

    def test_brave_queries_use_high_freshness_and_regular_category_terms(self):
        captured = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"web":{"results":[]}}'

        def fake_urlopen(request, timeout=20):
            captured.append(request.full_url)
            return FakeResponse()

        high = SourceConfig(
            "brave_high_search",
            "search",
            "P0",
            "high",
            "brave_search",
            "https://api.search.brave.com/res/v1/web/search",
        )
        regular = SourceConfig(
            "brave_regular_search",
            "search",
            "P1",
            "regular",
            "brave_search",
            "https://api.search.brave.com/res/v1/web/search",
        )
        with patch("ocoopa_monitor.fetchers.search_api.urlopen", side_effect=fake_urlopen):
            BraveSearchFetcher(api_key="secret").fetch(high, ["Ocoopa lawsuit"])
            BraveSearchFetcher(api_key="secret").fetch(regular, ["hand warmer fire"])
        self.assertIn("freshness=pd", captured[0])
        self.assertIn("freshness=py", captured[1])
        self.assertIn("%22hand+warmer%22", captured[1])
        self.assertIn("lawsuit", captured[1])

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
            payload = json.loads(request.data.decode("utf-8"))
            user_payload = json.loads(payload["messages"][1]["content"])
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
        decision = RiskRuleEngine().evaluate(stored.title, stored.raw_text, stored.matched_keywords)
        with patch("ocoopa_monitor.llm.urlopen", side_effect=fake_urlopen), patch("ocoopa_monitor.llm.time.sleep"):
            payload = DeepSeekProvider(api_key="secret").analyze(stored, decision)
        self.assertEqual(payload["risk_level"], "red")
        self.assertEqual(captured["calls"], 2)
        self.assertLessEqual(len(captured["raw_text"]), MAX_LLM_RAW_TEXT_CHARS + len("\n[TRUNCATED]"))
        self.assertTrue(captured["raw_text"].endswith("[TRUNCATED]"))


if __name__ == "__main__":
    unittest.main()
