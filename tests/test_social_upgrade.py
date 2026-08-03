from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from ocoopa_monitor.analysis_web import compute_dashboard, render_dashboard
from ocoopa_monitor.analysis_details import (
    build_detail_view,
    normalize_detail_metric,
    normalize_story_role,
    render_analysis_details,
)
from ocoopa_monitor.config import Settings
from ocoopa_monitor.console_web import rows_to_csv
from ocoopa_monitor.db import Database
from ocoopa_monitor.delivery import DeliveryClient, GenericWebhookChannel
from ocoopa_monitor.fetchers.brandwatch import BrandwatchMentionsFetcher
from ocoopa_monitor.models import AnalysisResult, Mention, SourceConfig, utcnow
from ocoopa_monitor.pipeline import MonitorPipeline
from ocoopa_monitor.session_auth import issue_session, verify_csrf, verify_session
from ocoopa_monitor.social import assess_public_mention, crossed_surge_threshold, infer_platform, response_guidance
from ocoopa_monitor.sources import sources_for_settings
from ocoopa_monitor.syndication import assess_syndication
from ocoopa_monitor.topics import enrich_topic_fields


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
    def test_recall_reposts_are_standard_but_new_first_person_claim_is_substantive(self):
        release_text = (
            "OCOOPA recall 26-659 covers 1.5 million rechargeable hand warmers due to fire and burn hazards. "
            "The CPSC release reports one death and directs consumers to stop using affected models."
        )
        news = self._mention("https://news.example.com/ocoopa-recall", release_text)
        news.source_type = "news"
        news.platform = "news"
        news.content_type = "article"
        assessment = assess_public_mention(news)
        spread = assess_syndication(news, assessment.novelty_type)
        self.assertEqual(assessment.notification_priority, "standard")
        self.assertEqual(assessment.novelty_type, "known_recall_repost")
        self.assertEqual(spread.role, "news_repost")
        self.assertEqual(spread.cluster_key, "story:recall-26-659:official-release")
        self.assertEqual(spread.cluster_label, "召回 26-659 官方新闻稿传播簇")

        official = self._mention(
            "https://www.cpsc.gov/Recalls/2026/OCOOPA-Direct-Recalls-Hand-Warmers",
            release_text,
        )
        official.source_type = "news"
        official.platform = "news"
        official.content_type = "article"
        official_spread = assess_syndication(official, "known_recall_repost")
        self.assertEqual(official_spread.role, "official_source")
        self.assertEqual(official_spread.cluster_key, spread.cluster_key)

        claim = self._mention(
            "https://www.tiktok.com/@person/video/26",
            "My OCOOPA UT3053 from recall 26-659 caught fire yesterday and burned my hand.",
        )
        claim_assessment = assess_public_mention(claim)
        claim_spread = assess_syndication(claim, claim_assessment.novelty_type)
        self.assertEqual(claim_assessment.notification_priority, "urgent")
        self.assertEqual(claim_spread.role, "substantive_update")
        self.assertNotEqual(claim_spread.cluster_key, spread.cluster_key)

    def test_dashboard_and_export_separate_links_clusters_and_substantive_signals(self):
        when = datetime(2026, 8, 1, 2, tzinfo=timezone.utc)
        release = "OCOOPA recall 26-659 for UT3053 hand warmers due to fire and burn hazards; one death was reported."
        rows = [
            {"title": release, "raw_text": release, "source_url": "https://www.cpsc.gov/Recalls/2026/OCOOPA", "event_fingerprint": "official", "source_type": "news", "source_name": "cpsc", "platform": "news", "content_type": "article", "fetched_at": when, "published_at": when, "risk_level": "red", "sentiment": "negative", "category": "recall", "campaign": "recall_26_659", "novelty_type": "known_recall_repost"},
            {"title": release, "raw_text": release, "source_url": "https://news.example.com/repost", "event_fingerprint": "news-copy", "source_type": "news", "source_name": "news", "platform": "news", "content_type": "article", "fetched_at": when, "published_at": when, "risk_level": "yellow", "sentiment": "neutral", "category": "recall", "campaign": "recall_26_659", "novelty_type": "known_recall_repost"},
            {"title": release, "raw_text": release, "source_url": "https://x.com/outlet/status/1", "event_fingerprint": "social-copy", "source_type": "search", "source_name": "brave", "platform": "x", "content_type": "post", "fetched_at": when, "published_at": when, "risk_level": "yellow", "sentiment": "neutral", "category": "recall", "campaign": "recall_26_659", "novelty_type": "known_recall_repost"},
            {"title": "My OCOOPA UT3053 caught fire", "raw_text": "My OCOOPA UT3053 from recall 26-659 caught fire and burned my hand.", "source_url": "https://reddit.com/r/test/1", "event_fingerprint": "new-claim", "source_type": "social", "source_name": "reddit", "platform": "reddit", "content_type": "post", "fetched_at": when, "published_at": when, "risk_level": "red", "sentiment": "negative", "category": "user_complaint", "campaign": "recall_26_659", "novelty_type": "new_high_risk_claim", "notification_priority": "urgent"},
            {"title": "My OCOOPA UT3053 caught fire", "raw_text": "My OCOOPA UT3053 from recall 26-659 caught fire and burned my hand.", "source_url": "https://news.example.com/user-claim-copy", "event_fingerprint": "new-claim", "source_type": "news", "source_name": "news", "platform": "news", "content_type": "article", "fetched_at": when, "published_at": when, "risk_level": "red", "sentiment": "negative", "category": "user_complaint", "campaign": "recall_26_659", "novelty_type": "new_high_risk_claim", "notification_priority": "urgent"},
        ]
        stats = compute_dashboard(rows, [], 7, now=datetime(2026, 8, 1, 8, tzinfo=timezone.utc))
        self.assertEqual(stats["valid_mentions"], 5)
        self.assertEqual(stats["story_clusters"], 2)
        self.assertEqual(stats["syndicated_mentions"], 2)
        self.assertEqual(stats["substantive_updates"], 1)
        self.assertEqual(sum(day["cluster_total"] for day in stats["daily"]), 2)
        html = render_dashboard(stats, 7)
        self.assertIn("独立传播簇", html)
        self.assertIn("新增实质信号", html)
        self.assertNotIn("需人工介入", html)
        self.assertNotIn('intervention=human', html)
        self.assertIn('story_role=news_repost', html)
        self.assertNotIn("回应状态", html)
        self.assertNotIn("高传播帖子榜", html)
        exported = rows_to_csv(rows)
        self.assertIn("story_cluster_key", exported.splitlines()[0])
        self.assertIn("story_cluster_label", exported.splitlines()[0])
        self.assertIn("news_repost", exported)

        detail = build_detail_view(rows, metric="clusters", page=1, page_size=20)
        self.assertEqual(
            detail["counts"],
            {"links": 5, "clusters": 2, "syndicated": 2, "substantive": 1, "parent_posts": 1},
        )
        self.assertEqual(detail["item_kind"], "cluster")
        self.assertEqual(sum(cluster["link_count"] for cluster in detail["items"]), 5)
        self.assertEqual(normalize_detail_metric("invalid"), "links")
        detail_html = render_analysis_details(
            detail, 7, filters={"platform": "x", "campaign": "recall_26_659"}, csrf_token="csrf-value"
        )
        self.assertIn('<details class="cluster-card">', detail_html)
        self.assertIn("查看原文证据", detail_html)
        self.assertIn("进入复核页", detail_html)
        self.assertIn("平台：x", detail_html)
        self.assertIn('name="csrf" value="csrf-value"', detail_html)
        self.assertNotIn("story:recall-26-659", detail_html)
        self.assertEqual(build_detail_view(rows, metric="links", page=2, page_size=20)["page"], 1)

        focused_detail = render_analysis_details(
            build_detail_view([{**rows[0], "id": 51}], metric="links"),
            7,
            filters={
                "platform": "x",
                "campaign": "recall_26_659",
                "story_role": "official_source",
            },
        )
        self.assertIn("view=library", focused_detail)
        self.assertIn("focus_mention_id=51", focused_detail)
        self.assertIn("return_to=", focused_detail)
        self.assertIn('name="story_role"', focused_detail)
        self.assertIn('value="official_source" selected', focused_detail)
        self.assertEqual(normalize_story_role("news_repost"), "news_repost")
        self.assertEqual(normalize_story_role("unknown-role"), "")

        stats["filters"] = {"platform": "x", "campaign": "recall_26_659"}
        filtered_dashboard = render_dashboard(stats, 7)
        self.assertIn("/review/analysis/details?metric=links&amp;days=7&amp;platform=x&amp;campaign=recall_26_659", filtered_dashboard.replace("&", "&amp;"))

    def test_false_positive_is_retained_for_audit_but_excluded_from_dashboard(self):
        when = datetime(2026, 8, 1, 2, tzinfo=timezone.utc)
        rows = [
            {
                "title": "OCOOPA recall 26-659 news repost", "raw_text": "OCOOPA recall 26-659",
                "source_url": "https://news.example.com/recall", "event_fingerprint": "valid",
                "source_name": "news", "platform": "web", "content_type": "article",
                "fetched_at": when, "first_seen_at": when, "risk_level": "yellow",
                "sentiment": "neutral", "campaign": "recall_26_659", "review_status": "confirmed",
            },
            {
                "title": "Unrelated Ozark Trail stove lawsuit", "raw_text": "Unrelated camping stove lawsuit",
                "source_url": "https://aboutlawsuits.example/ozark", "event_fingerprint": "false",
                "source_name": "aboutlawsuits", "platform": "web", "content_type": "article",
                "fetched_at": when, "first_seen_at": when, "risk_level": "red",
                "sentiment": "negative", "campaign": "brand_major_risk", "review_status": "false_positive",
                "incident_status": "resolved",
            },
        ]
        stats = compute_dashboard(rows, [], 7, now=datetime(2026, 8, 1, 8, tzinfo=timezone.utc))
        self.assertEqual(stats["valid_mentions"], 1)
        self.assertEqual(stats["excluded_false_positives"], 1)
        self.assertEqual(stats["risk"].get("red", 0), 0)
        csv_text = rows_to_csv(rows)
        self.assertIn("statistical_inclusion", csv_text.splitlines()[0])
        self.assertIn("已排除误报", csv_text)

    def test_news_repost_defaults_to_monitoring_and_parent_post_to_manual_comment_review(self):
        from ocoopa_monitor.interventions import intervention_status, requires_human_intervention
        from ocoopa_monitor.review_web import render_review_page

        news = {"platform": "web", "content_type": "article", "recommended_action": "monitor"}
        parent = {
            "platform": "facebook", "content_type": "post", "recommended_action": "monitor",
            "source_url": "https://facebook.com/outlet/posts/1",
        }
        self.assertEqual(intervention_status(news), "仅监测")
        self.assertFalse(requires_human_intervention(news))
        self.assertEqual(intervention_status(parent), "人工查看评论")
        self.assertTrue(requires_human_intervention(parent))
        html = render_review_page([], filters={"view": "library", "quality": "all"})
        self.assertIn("全量证据库", html)
        self.assertIn("待处置队列", html)
        self.assertIn("已排除误报", html)

    def test_intervention_route_is_persisted_separately_from_response_record(self):
        with tempfile.NamedTemporaryFile() as tmp:
            db = Database(tmp.name)
            db.init()
            mention = self._mention("https://facebook.com/outlet/posts/2", "OCOOPA recall article")
            mention.platform, mention.content_type = "facebook", "post"
            db.upsert_mention(mention)
            db.ensure_mention_action(mention.id, {})
            self.assertTrue(
                db.update_mention_intervention(
                    mention.id, "人工查看评论", "无需回应", "", "人工打开母帖查看评论", "reviewer"
                )
            )
            rows = db.fetch_mentions_between(
                datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 1, tzinfo=timezone.utc)
            )
            self.assertEqual(rows[0]["intervention_status"], "人工查看评论")
            self.assertEqual(rows[0]["response_status"], "无需回应")

    def test_analysis_details_paginate_and_reject_unsafe_evidence_urls(self):
        when = datetime(2026, 8, 1, 2, tzinfo=timezone.utc)
        rows = [
            {
                "id": index,
                "title": f"Independent OCOOPA safety discussion {index}",
                "raw_text": f"Independent OCOOPA product safety discussion with enough detail {index}",
                "source_url": "javascript:alert(1)" if index == 25 else f"https://example.com/{index}",
                "event_fingerprint": f"event-{index}",
                "source_type": "social",
                "source_name": "test",
                "platform": "reddit",
                "content_type": "post",
                "fetched_at": when,
                "published_at": when,
                "risk_level": "yellow",
                "campaign": "brand_major_risk",
            }
            for index in range(1, 26)
        ]
        view = build_detail_view(rows, metric="links", page=2, page_size=20)
        self.assertEqual(view["total_items"], 25)
        self.assertEqual(view["page"], 2)
        self.assertEqual(len(view["items"]), 5)
        html = render_analysis_details(view, 30, filters={"platform": "reddit"})
        self.assertIn("第 2 / 2 页", html)
        self.assertIn("← 上一页", html)
        self.assertNotIn("javascript:alert", html)

    def test_parent_post_worklist_and_topic_axis_do_not_treat_recall_death_as_lawsuit(self):
        recall_post = {
            "id": 1,
            "title": "Boston station reports OCOOPA recall after fires and one death",
            "raw_text": "CPSC recall 26-659 reports fires, burns and one death. The station posted this update on Facebook.",
            "source_url": "https://www.facebook.com/Boston25News/posts/123",
            "event_fingerprint": "official-release",
            "source_type": "search",
            "source_name": "brave",
            "platform": "facebook",
            "content_type": "post",
            "fetched_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "first_seen_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "risk_level": "red",
            "campaign": "recall_26_659",
        }
        legal = enrich_topic_fields({
            **recall_post,
            "id": 2,
            "title": "OCOOPA class action lawsuit filed",
            "raw_text": "A class action lawsuit was filed over OCOOPA hand warmer fires.",
            "source_url": "https://news.example.com/lawsuit",
            "platform": "news",
            "content_type": "article",
        })
        self.assertEqual(enrich_topic_fields(recall_post)["topic_primary"], "recall_regulatory")
        self.assertEqual(legal["topic_primary"], "legal_action")
        view = build_detail_view([recall_post, legal], metric="parent_posts")
        self.assertEqual(view["total_items"], 1)
        html = render_analysis_details(view, 7, filters={})
        self.assertIn("人工查看评论", html)
        self.assertIn("不抓取评论", html)
        self.assertNotIn("Meta Developer", html)

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
        self.assertNotEqual(first.duplicate_group_id, second.duplicate_group_id)
        self.assertEqual(infer_platform(parent, "social"), "facebook")

        recall_text = "OCOOPA recall 26-659 covers UT3053 hand warmers due to fire and burn hazards."
        news = pipeline._build_mention(
            RawItem("news", "news_one", "https://news.example.com/a", recall_text, recall_text),
            ["OCOOPA", "26-659"], False,
        )
        social = pipeline._build_mention(
            RawItem("search", "brave_social", "https://x.com/outlet/status/1", recall_text, recall_text),
            ["OCOOPA", "26-659"], False,
        )
        self.assertEqual(news.duplicate_group_id, "story:recall-26-659:official-release")
        self.assertEqual(news.duplicate_group_id, social.duplicate_group_id)

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
        self.assertIn("按系统首次发现时间统计", html)
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

    def test_recall_batch_copy_reports_spread_without_inventing_new_facts(self):
        payload = DeliveryClient(GenericWebhookChannel()).mention_batch_payload(
            [
                {"campaign": "recall_26_659", "story_cluster_key": "story:recall-26-659:official-release", "story_role": "news_repost", "story_role_label": "新闻转载", "is_syndicated": True, "has_substantive_update": False, "platform": "news", "title": "Recall copy", "summary_zh": "媒体转载召回信息", "source_url": "https://news.example.com/1", "notification_priority": "standard"},
                {"campaign": "recall_26_659", "story_cluster_key": "story:recall-26-659:official-release", "story_role": "social_amplification", "story_role_label": "社媒扩散", "is_syndicated": True, "has_substantive_update": False, "platform": "x", "title": "Social copy", "summary_zh": "媒体社媒账号同步", "source_url": "https://x.com/outlet/1", "notification_priority": "standard"},
            ],
            "standard",
        )
        self.assertIn("召回传播总览", payload["title"])
        self.assertIn("独立传播簇：1 个", payload["text"])
        self.assertIn("自动规则未识别到", payload["text"])
        self.assertTrue(payload["suppress_at"])

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
