from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from ocoopa_monitor.config import Settings
from ocoopa_monitor.db import Database
from ocoopa_monitor.fetchers.search_api import BraveSearchFetcher, REGULAR_MEDIA_OUTLET_SOCIAL_QUERY
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.models import RawItem, SourceConfig, utcnow
from ocoopa_monitor.pipeline import MonitorPipeline
from ocoopa_monitor.public_social_intake import build_manual_public_social_item
from ocoopa_monitor.sources import sources_for_settings


def _settings(path: str) -> Settings:
    return Settings(
        db_path=path, db_url="", alert_channel="generic", alert_webhook_url="",
        alert_webhook_secret="", alert_at_mobiles="", alert_rate_limit_per_minute=20,
        alert_cooldown_hours=6, alert_ack_timeout_minutes=30, review_token="",
        llm_provider="rule", llm_model="rule", llm_api_key="", llm_base_url="https://example.com",
        serpapi_api_key="", brave_search_api_key="", gnews_api_key="",
        high_lane_interval_minutes=15, regular_lane_interval_minutes=60,
        p0_health_threshold_minutes=120, backfill_days=180, request_timeout_seconds=1,
    )


class StaticFetcher:
    def __init__(self, items):
        self.items = items

    def fetch(self, source, keywords, since=None):
        return self.items


class PublicSocialIntakeTests(unittest.TestCase):
    def test_known_facebook_parent_links_are_accepted_as_evidence_bounded_posts(self):
        links = [
            "https://www.facebook.com/whionews/photos/a-massive-recall-of-ocoopa-hand-warmers-follows-reports-of-fires-and-one-fatalit/1362260672753064/",
            "https://www.facebook.com/WPRI12/posts/recall-ocoopa-direct-is-recalling-about-15-million-rechargeable-handwarmers-foll/1492882272869986/",
            "https://www.facebook.com/kens5/photos/recall-alert-%EF%B8%8F-ocoopa-rechargeable-hand-warmers-have-been-recalled-after-numerou/1451946533631614/",
            "https://www.facebook.com/KARE11/photos/recall-alert-%EF%B8%8F-ocoopa-rechargeable-hand-warmers-have-been-recalled-after-numerou/1623322899837477/",
        ]
        for link in links:
            item, publisher = build_manual_public_social_item(link)
            self.assertEqual(item.platform, "facebook")
            self.assertEqual(item.content_type, "post")
            self.assertEqual(item.discovery_method, "manual_public_link")
            self.assertEqual(item.coverage_tier, "manual_supplied")
            self.assertIn("人工提供公开母帖", publisher)
            self.assertIn("系统未直抓平台正文或评论", item.raw_text)

    def test_review_page_exposes_csrf_protected_manual_intake(self):
        from ocoopa_monitor.review_web import render_review_page

        html = render_review_page([], csrf_token="csrf-test", return_to="/review?view=library")
        self.assertIn('action="/review/public-social-intake"', html)
        self.assertIn('name="csrf" value="csrf-test"', html)
        self.assertIn("补录公开社媒母帖", html)
        self.assertIn("不抓平台正文或评论", html)

    def test_manual_intake_rejects_comment_and_contextless_links(self):
        with self.assertRaisesRegex(ValueError, "母帖"):
            build_manual_public_social_item("https://www.facebook.com/example/comments/123")
        with self.assertRaisesRegex(ValueError, "标题或摘要"):
            build_manual_public_social_item("https://www.facebook.com/example/posts/123")
        with self.assertRaisesRegex(ValueError, "http"):
            build_manual_public_social_item("https://token@example.com/ocoopa/recall")

    def test_manual_intake_is_silent_backfill_but_joins_recall_cluster(self):
        with tempfile.NamedTemporaryFile() as tmp:
            db = Database(tmp.name)
            db.init()
            db.seed_keywords(DEFAULT_KEYWORDS)
            pipeline = MonitorPipeline(db, _settings(tmp.name))
            stored = pipeline.ingest_manual_public_social(
                "https://www.facebook.com/WPRI12/posts/recall-ocoopa-direct-is-recalling-about-15-million-rechargeable-handwarmers-foll/1492882272869986/"
            )
            self.assertTrue(stored.backfill)
            self.assertEqual(stored.platform, "facebook")
            self.assertEqual(stored.content_type, "post")
            rows = db.fetch_mentions_between(utcnow() - timedelta(days=1), utcnow() + timedelta(days=1))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["campaign"], "recall_26_659")
            self.assertEqual(rows[0]["notification_status"], None)

    def test_targeted_media_query_is_auto_configured_and_skips_comments(self):
        settings = _settings("/tmp/irrelevant.db")
        names = {source.source_name for source in sources_for_settings(settings)}
        self.assertIn("brave_media_outlet_social", names)
        captured = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"web": {"results": [
                    {"title": "WPRI OCOOPA recall", "url": "https://www.facebook.com/WPRI12/posts/123", "description": "OCOOPA recall"},
                    {"title": "comment", "url": "https://www.facebook.com/WPRI12/comments/123", "description": "OCOOPA recall"},
                ]}}).encode("utf-8")

        def fake_urlopen(request, timeout=20):
            captured.append(request.full_url)
            return Response()

        source = SourceConfig(
            "brave_media_outlet_social", "social", "P1", "regular", "brave_search", "https://api.search.brave.com/res/v1/web/search"
        )
        with patch("ocoopa_monitor.fetchers.search_api.urlopen", side_effect=fake_urlopen):
            items = BraveSearchFetcher(api_key="secret").fetch(source, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].discovery_method, "public_index_media_targeted")
        self.assertIn("WPRI", captured[0])
        self.assertLessEqual(len(REGULAR_MEDIA_OUTLET_SOCIAL_QUERY), 400)
        self.assertLessEqual(len(REGULAR_MEDIA_OUTLET_SOCIAL_QUERY.split()), 50)

    def test_targeted_source_waits_four_hours_between_automatic_runs(self):
        with tempfile.NamedTemporaryFile() as tmp:
            db = Database(tmp.name)
            db.init()
            db.seed_keywords(DEFAULT_KEYWORDS)
            source = SourceConfig(
                "brave_media_outlet_social", "social", "P1", "regular", "static", "test://media"
            )
            db.seed_sources([source])
            pipeline = MonitorPipeline(db, _settings(tmp.name), fetchers={"static": StaticFetcher([])})
            first = pipeline.run_lane("regular")
            second = pipeline.run_lane("regular")
            self.assertEqual(first["sources_attempted"], 1)
            self.assertEqual(second["sources_attempted"], 0)
            self.assertEqual(second["sources_deferred"], 1)

    def test_first_targeted_source_run_is_silent_before_realtime_delivery(self):
        with tempfile.NamedTemporaryFile() as tmp:
            db = Database(tmp.name)
            db.init()
            db.seed_keywords(DEFAULT_KEYWORDS)
            db.mark_bootstrapped()
            db.set_state("public_social_backfill_completed_at", utcnow().isoformat())
            db.seed_sources([
                SourceConfig("brave_media_outlet_social", "social", "P1", "regular", "static", "test://media")
            ])
            item = RawItem(
                source_type="social", source_name="brave_media_outlet_social",
                source_url="https://www.facebook.com/WPRI12/posts/recall-ocoopa-hand-warmers-26-659",
                title="WPRI OCOOPA hand warmer recall 26-659", raw_text="OCOOPA hand warmer recall 26-659",
                platform="facebook", content_type="post", provider="brave", provider_item_id="wpri-1",
                discovery_method="public_index_media_targeted", coverage_tier="public_index",
            )
            result = MonitorPipeline(
                db, _settings(tmp.name), fetchers={"static": StaticFetcher([item])}
            ).run_lane("regular")
            rows = db.fetch_mentions_between(utcnow() - timedelta(days=1), utcnow() + timedelta(days=1))
            self.assertEqual(result["mention_batches_queued"], 0)
            self.assertTrue(rows[0]["backfill"])
            self.assertIsNotNone(db.get_state("media_outlet_social_backfill_completed_at"))


if __name__ == "__main__":
    unittest.main()
