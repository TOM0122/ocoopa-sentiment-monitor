from __future__ import annotations

import os
import unittest
from datetime import timedelta
from uuid import uuid4

from ocoopa_monitor.db import PostgresDatabase
from ocoopa_monitor.keywords import DEFAULT_KEYWORDS
from ocoopa_monitor.models import AnalysisResult, Mention, SourceConfig, utcnow


@unittest.skipUnless(os.getenv("OCOOPA_TEST_POSTGRES_URL"), "set OCOOPA_TEST_POSTGRES_URL to run Postgres smoke tests")
class PostgresIntegrationTests(unittest.TestCase):
    def test_postgres_runtime_smoke_path(self):
        db = PostgresDatabase(os.environ["OCOOPA_TEST_POSTGRES_URL"])
        suffix = uuid4().hex
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS[:2])
        db.seed_sources(
            [
                SourceConfig(
                    source_name=f"pg_smoke_source_{suffix}",
                    source_type="news",
                    priority="P0",
                    lane="high",
                    method="static",
                    url=f"test://pg-smoke-{suffix}",
                )
            ]
        )
        mention = db.upsert_mention(
            Mention(
                source_type="news",
                source_name=f"pg_smoke_source_{suffix}",
                source_url=f"https://example.com/pg-smoke-{suffix}",
                canonical_url=f"https://example.com/pg-smoke-{suffix}",
                title="Ocoopa CPSC recall smoke test",
                raw_text="Ocoopa CPSC recall smoke test.",
                fetched_at=utcnow(),
                first_seen_at=utcnow(),
                text_excerpt="Ocoopa CPSC recall smoke test.",
                matched_keywords=["Ocoopa CPSC"],
                content_hash=f"pg-smoke-hash-{suffix}",
                event_fingerprint=f"pg-smoke-fingerprint-{suffix}",
                tos_method="api",
                platform="reddit",
                content_type="comment",
                provider="brandwatch",
                provider_item_id=f"pg-resource-{suffix}",
                parent_url=f"https://reddit.com/r/ocoopa/comments/{suffix}",
                discovery_method="licensed_api",
                coverage_tier="licensed",
                view_count=10000,
                like_count=500,
            )
        )
        analysis = db.insert_analysis(
            AnalysisResult(
                mention_id=mention.id or 0,
                model_provider="rule",
                model_name="rule-only",
                prompt_version="test",
                sentiment="negative",
                risk_level="red",
                category="recall",
                summary_zh="Ocoopa CPSC recall smoke test.",
                key_quotes=["Ocoopa CPSC recall smoke test."],
                key_quote_offsets=[{"start": 0, "end": 31}],
                requires_escalation=True,
                escalation_reason="CPSC recall",
                confidence=0.9,
                evidence_check_passed=True,
                evidence_check_notes="",
                needs_human_review=False,
                analysis_created_at=utcnow(),
                campaign="recall_26_659",
                relevance=1.0,
                novelty_type="known_recall_repost",
                notification_priority="standard",
                recommended_action="review_for_response",
            )
        )
        group_id = db.upsert_incident_group(mention, analysis)
        alert_id = db.insert_alert(
            mention_id=mention.id or 0,
            incident_group_id=group_id,
            risk_level="red",
            alert_reason="CPSC recall",
            dedupe_key=f"pg-smoke-fingerprint-{suffix}:red",
            confidence=0.9,
            evidence_check_passed=True,
            needs_human_review=False,
            delivery_latency_seconds=12,
            sent_to="test",
            sent_at=utcnow(),
        )
        self.assertGreater(alert_id, 0)
        self.assertTrue(db.alert_exists(f"pg-smoke-fingerprint-{suffix}:red"))
        db.record_interaction_snapshot(mention)
        db.ensure_mention_action(
            mention.id or 0,
            {"intervention_reason": "test", "draft_original": "draft", "draft_zh": "草稿", "legal_risk_note": "review"},
        )
        self.assertTrue(db.update_mention_action(mention.id or 0, "建议回应", "", "note", "tester"))
        self.assertTrue(db.enqueue_mention_batch([mention.id or 0], "standard", {"text": "test"}))
        rows = db.fetch_mentions_between(mention.fetched_at - timedelta(minutes=1), utcnow())
        row = next(item for item in rows if item["id"] == mention.id)
        self.assertEqual(row["provider_item_id"], f"pg-resource-{suffix}")
        self.assertEqual(row["campaign"], "recall_26_659")
        self.assertEqual(row["response_status"], "建议回应")

    def test_init_adds_missing_columns_to_existing_tables(self):
        # CREATE TABLE IF NOT EXISTS never alters an existing table; init() must
        # apply idempotent column migrations (regression: g.muted_until missing).
        db = PostgresDatabase(os.environ["OCOOPA_TEST_POSTGRES_URL"])
        db.init()
        with db.connect() as conn:
            conn.execute("ALTER TABLE incident_groups DROP COLUMN IF EXISTS muted_until")
        db.init()
        with db.connect() as conn:
            row = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='incident_groups' AND column_name='muted_until'"
            ).fetchone()
            social_tables = conn.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_name IN ('mention_metrics','mention_actions','mention_notifications')"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(social_tables["n"], 3)
        db.list_recent_alerts(1)  # the review query that failed in production now works


if __name__ == "__main__":
    unittest.main()
