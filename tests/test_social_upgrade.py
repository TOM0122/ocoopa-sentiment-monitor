from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from ocoopa_monitor.analysis_web import compute_dashboard, render_dashboard
from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database
from ocoopa_monitor.delivery import DeliveryClient, GenericWebhookChannel
from ocoopa_monitor.fetchers.brandwatch import BrandwatchMentionsFetcher
from ocoopa_monitor.models import AnalysisResult, Mention, SourceConfig, utcnow
from ocoopa_monitor.pipeline import MonitorPipeline
from ocoopa_monitor.session_auth import issue_session, verify_csrf, verify_session
from ocoopa_monitor.social import assess_public_mention, crossed_surge_threshold, infer_platform, response_guidance
from ocoopa_monitor.sources import sources_for_settings


def _settings(path: str, **overrides) -> Settings:
    values = dict(
        db_path=path, db_url="", alert_channel="generic", alert_webhook_url="",
        alert_webhook_secret="", alert_at_mobiles="", alert_rate_limit_per_minute=20,
        alert_cooldown_hours=6, alert_ack_timeout_minutes=30, review_token="code",
        llm_provider="rule", llm_model="rule", llm_api_key="", llm_base_url="https://example.com",
        serpapi_api_key="", brave_search_api_key="", gnews_api_key="",
        high_lane_interval_minutes=15, regular_lane_interval_minutes=60,
        p0_health_threshold_minutes=120, backfill_days=180, request_timeout_seconds=1,
    )
    values.update(overrides)
    return Settings(**values)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SocialUpgradeTests(unittest.TestCase):
    def test_signed_session_expiry_tamper_and_csrf(self):
        token = issue_session("independent", ttl_hours=1, now=100)
        self.assertTrue(verify_session(token, "independent", now=200))
        self.assertFalse(verify_session(token, "independent", now=4000))
        self.assertFalse(verify_session(token + "x", "independent", now=200))
        self.assertTrue(verify_csrf("same", "same"))
        self.assertFalse(verify_csrf("same", "different"))

    def test_public_relevance_and_safe_response_boundary(self):
        mention = self._mention(
            "https://www.tiktok.com/@person/video/1",
            "My OCOOPA hand warmer caught fire and I went to the hospital. I need help with a refund.",
        )
        assessment = assess_public_mention(mention)
        self.assertTrue(assessment.valid)
        self.assertEqual(assessment.notification_priority, "urgent")
        guidance = response_guidance(mention, self._analysis(1))
        self.assertIn("ocooparecalls@ocoopa.cc", guidance["draft_original"])
        self.assertNotIn("liable", guidance["draft_original"].lower())
        spam = self._mention(
            "https://instagram.com/p/deal",
            "OCOOPA coupon promo code affiliate deal only, buy this hand warmer today",
        )
        self.assertFalse(assess_public_mention(spam).valid)
        own = self._mention(
            "https://instagram.com/p/official",
            "OCOOPA official recall 26-659 safety notice with affected model details",
        )
        own.author_or_publisher = "@OCOOPA"
        self.assertFalse(assess_public_mention(own).valid)
        official_alias = self._mention(
            "https://youtube.com/watch?v=official",
            "OCOOPA recall 26-659 safety notice with affected model details",
        )
        official_alias.author_or_publisher = "North America Product Safety Official"
        self.assertFalse(assess_public_mention(official_alias).valid)

    def test_platform_and_comment_identity_keep_same_parent_comments_distinct(self):
        tmp = tempfile.NamedTemporaryFile()
        db = Database(tmp.name)
        db.init()
        pipeline = MonitorPipeline(db, _settings(tmp.name), fetchers={})
        parent = "https://www.facebook.com/post/123"
        from ocoopa_monitor.models import RawItem

        first = pipeline._build_mention(
            RawItem("social", "brandwatch_mentions", parent, "OCOOPA complaint", "OCOOPA hand warmer overheated and support was contacted", platform="facebook", content_type="comment", provider="brandwatch", provider_item_id="c1", parent_url=parent),
            ["OCOOPA"], False,
        )
        second = pipeline._build_mention(
            RawItem("social", "brandwatch_mentions", parent, "OCOOPA complaint", "OCOOPA hand warmer overheated and refund was requested", platform="facebook", content_type="comment", provider="brandwatch", provider_item_id="c2", parent_url=parent),
            ["OCOOPA"], False,
        )
        db.upsert_mention(first)
        db.upsert_mention(second)
        self.assertNotEqual(first.canonical_url, second.canonical_url)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(infer_platform(parent, "social"), "facebook")

    def test_brandwatch_paginates_and_maps_resource_identity(self):
        pages = [
            {"results": [{"resourceId": "r1", "url": "https://x.com/a/status/1", "text": "OCOOPA fire complaint with meaningful details", "added": "2026-08-01T00:05:00Z", "views": 12000}], "nextCursor": "next"},
            {"results": [{"resourceId": "r2", "parentUrl": "https://facebook.com/p/1", "text": "OCOOPA overheating complaint with meaningful details", "contentType": "comment"}]},
        ]

        requested_urls = []

        def fake_open(request, timeout=20):
            requested_urls.append(request.full_url)
            return FakeResponse(pages[1] if "cursor=next" in request.full_url else pages[0])

        fetcher = BrandwatchMentionsFetcher("token", "project", "query", timeout_seconds=1)
        source = SourceConfig("brandwatch_mentions", "social", "P1", "licensed", "brandwatch", "brandwatch://mentions")
        with patch("ocoopa_monitor.fetchers.brandwatch.urlopen", side_effect=fake_open):
            rows = fetcher.fetch(source, ["OCOOPA"], datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.assertEqual([row.provider_item_id for row in rows], ["r1", "r2"])
        self.assertEqual(rows[0].platform, "x")
        self.assertEqual(rows[0].view_count, 12000)
        self.assertEqual(rows[1].parent_url, "https://facebook.com/p/1")
        self.assertTrue(any("sinceAdded=" in url and "sinceUpdated=" not in url for url in requested_urls))
        self.assertTrue(any("sinceUpdated=" in url and "sinceAdded=" not in url for url in requested_urls))

    def test_notification_ledger_is_idempotent_and_dashboard_has_new_metrics(self):
        tmp = tempfile.NamedTemporaryFile()
        db = Database(tmp.name)
        db.init()
        mention = self._mention("https://reddit.com/r/a/1", "OCOOPA hand warmer overheated and refund was requested with detailed context")
        mention.platform = "reddit"
        mention.view_count = 12000
        mention.id = None
        db.upsert_mention(mention)
        analysis = self._analysis(mention.id)
        analysis.campaign = "brand_major_risk"
        analysis.notification_priority = "urgent"
        db.insert_analysis(analysis)
        db.upsert_incident_group(mention, analysis)
        db.ensure_mention_action(mention.id, response_guidance(mention, analysis))
        payload = {"title": "batch", "text": "[链接](https://reddit.com/r/a/1)", "suppress_at": False}
        self.assertTrue(db.enqueue_mention_batch([mention.id], "urgent", payload))
        self.assertFalse(db.enqueue_mention_batch([mention.id], "urgent", payload))
        job = db.list_due_deliveries(1)[0]
        db.mark_delivery_sent(job["id"], "test://dingtalk", utcnow())
        rows = db.fetch_mentions_between(datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 1, tzinfo=timezone.utc))
        health = db.list_source_health()
        stats = compute_dashboard(rows, [], 7, source_health=health)
        self.assertEqual(stats["social_mentions"], 1)
        self.assertEqual(stats["urgent_mentions"], 1)
        self.assertEqual(stats["surge_mentions"], 1)
        self.assertIsNotNone(stats["delivery_p95_seconds"])
        html = render_dashboard(stats, 7)
        self.assertIn("平台覆盖矩阵", html)
        self.assertIn("采集源健康", html)
        self.assertIn("内容发布", html)
        self.assertNotIn("?token=", html)

    def test_mixed_batch_does_not_hide_verified_urgent_at(self):
        payload = DeliveryClient(GenericWebhookChannel()).mention_batch_payload(
            [
                {
                    "title": "verified emergency",
                    "notification_priority": "urgent",
                    "needs_human_review": False,
                },
                {
                    "title": "unverified routine item",
                    "notification_priority": "standard",
                    "needs_human_review": True,
                },
            ],
            "urgent",
        )
        self.assertFalse(payload["suppress_at"])
        self.assertFalse(payload["needs_human_review"])
        self.assertTrue(payload["contains_human_review"])
        self.assertIn("需人工核实", payload["text"])

    def test_surge_thresholds_and_brandwatch_feature_flag(self):
        self.assertEqual(crossed_surge_threshold({"view_count": 9000}, {"view_count": 10000}), "views_10000")
        self.assertEqual(
            crossed_surge_threshold(
                {"like_count": 30, "comment_count": 10, "share_count": 10},
                {"like_count": 130, "comment_count": 20, "share_count": 10},
            ),
            "interaction_growth_3x",
        )
        disabled = sources_for_settings(_settings(":memory:"))
        self.assertFalse(next(source for source in disabled if source.method == "brandwatch").active)
        enabled = sources_for_settings(
            _settings(":memory:", brandwatch_api_token="t", brandwatch_project_id="p", brandwatch_query_id="q")
        )
        self.assertTrue(next(source for source in enabled if source.method == "brandwatch").active)

    @staticmethod
    def _mention(url: str, text: str) -> Mention:
        now = utcnow()
        return Mention(
            source_type="social", source_name="test", source_url=url, canonical_url=url,
            title=text[:80], raw_text=text, fetched_at=now, first_seen_at=now,
            matched_keywords=["OCOOPA"], content_hash=text, event_fingerprint="event-" + str(abs(hash(text))),
            platform=infer_platform(url, "social"), content_type="post",
        )

    @staticmethod
    def _analysis(mention_id: int) -> AnalysisResult:
        return AnalysisResult(
            mention_id=mention_id, model_provider="rule", model_name="rule", prompt_version="v",
            sentiment="negative", risk_level="yellow", category="user_complaint", summary_zh="用户投诉产品过热",
            key_quotes=[], key_quote_offsets=[], requires_escalation=False, escalation_reason="",
            confidence=0.8, evidence_check_passed=True, evidence_check_notes="", needs_human_review=False,
        )


if __name__ == "__main__":
    unittest.main()
