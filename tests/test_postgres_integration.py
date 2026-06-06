from __future__ import annotations

import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
