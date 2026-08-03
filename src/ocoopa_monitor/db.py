from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .models import AnalysisResult, Keyword, Mention, SourceConfig, utcnow


def dt_to_str(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def str_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


BOOTSTRAP_KEY = "backfill_completed_at"


def create_database(settings) -> "Database":
    if getattr(settings, "db_url", ""):
        return PostgresDatabase(settings.db_url)
    return Database(settings.db_path)


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SQLITE_SCHEMA)
            self._migrate_sqlite(conn)

    def _migrate_sqlite(self, conn: sqlite3.Connection) -> None:
        self._add_column_if_missing(conn, "alerts", "delivery_latency_seconds", "INTEGER")
        self._add_column_if_missing(conn, "incident_groups", "muted_until", "TEXT")
        for column, column_type in (
            ("platform", "TEXT NOT NULL DEFAULT 'web'"),
            ("content_type", "TEXT NOT NULL DEFAULT 'article'"),
            ("provider", "TEXT NOT NULL DEFAULT ''"),
            ("provider_item_id", "TEXT NOT NULL DEFAULT ''"),
            ("parent_url", "TEXT NOT NULL DEFAULT ''"),
            ("discovery_method", "TEXT NOT NULL DEFAULT 'public_feed'"),
            ("coverage_tier", "TEXT NOT NULL DEFAULT 'public_index'"),
            ("provider_added_at", "TEXT"),
            ("discovery_latency_seconds", "INTEGER"),
            ("view_count", "INTEGER"),
            ("like_count", "INTEGER"),
            ("comment_count", "INTEGER"),
            ("share_count", "INTEGER"),
        ):
            self._add_column_if_missing(conn, "mentions", column, column_type)
        for column, column_type in (
            ("campaign", "TEXT NOT NULL DEFAULT 'brand_major_risk'"),
            ("relevance", "REAL NOT NULL DEFAULT 0"),
            ("novelty_type", "TEXT NOT NULL DEFAULT 'new_mention'"),
            ("notification_priority", "TEXT NOT NULL DEFAULT 'standard'"),
            ("recommended_action", "TEXT NOT NULL DEFAULT 'monitor'"),
        ):
            self._add_column_if_missing(conn, "analysis_results", column, column_type)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mentions_provider_item "
            "ON mentions(provider, provider_item_id) "
            "WHERE provider <> '' AND provider_item_id <> ''"
        )

    @staticmethod
    def _add_column_if_missing(
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    def seed_keywords(self, keywords: Iterable[Keyword]) -> None:
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO keywords(term, category, lane, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(term) DO UPDATE SET
                    category=excluded.category,
                    lane=excluded.lane,
                    updated_at=excluded.updated_at
                """,
                [(kw.term, kw.category, kw.lane, int(kw.active), now, now) for kw in keywords],
            )

    def get_keywords(self, lane: Optional[str] = None) -> List[Keyword]:
        sql = "SELECT term, category, lane, active FROM keywords WHERE active=1"
        params: Sequence[Any] = ()
        if lane == "high":
            sql += " AND lane='high'"
        elif lane == "regular":
            sql += " AND lane IN ('high', 'regular')"
        sql += " ORDER BY term"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Keyword(row["term"], row["category"], row["lane"], bool(row["active"])) for row in rows]

    def list_keywords_all(self) -> List[Keyword]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT term, category, lane, active FROM keywords ORDER BY active DESC, lane, term"
            ).fetchall()
        return [Keyword(row["term"], row["category"], row["lane"], bool(row["active"])) for row in rows]

    def upsert_keyword(self, term: str, category: str, lane: str, active: bool = True) -> None:
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO keywords(term, category, lane, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(term) DO UPDATE SET
                    category=excluded.category, lane=excluded.lane,
                    active=excluded.active, updated_at=excluded.updated_at
                """,
                (term, category, lane, int(active), now, now),
            )

    def set_keyword_active(self, term: str, active: bool) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE keywords SET active=?, updated_at=? WHERE term=?",
                (int(active), dt_to_str(utcnow()), term),
            )
            return cur.rowcount > 0

    def seed_sources(self, sources: Iterable[SourceConfig]) -> None:
        now = dt_to_str(utcnow())
        source_list = list(sources)
        with self.connect() as conn:
            for source in source_list:
                conn.execute(
                    """
                    INSERT INTO source_configs(
                        source_name, source_type, priority, lane, method, url, active,
                        alert_threshold_minutes, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_name) DO UPDATE SET
                        source_type=excluded.source_type,
                        priority=excluded.priority,
                        lane=excluded.lane,
                        method=excluded.method,
                        url=excluded.url,
                        active=excluded.active,
                        alert_threshold_minutes=excluded.alert_threshold_minutes,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source.source_name,
                        source.source_type,
                        source.priority,
                        source.lane,
                        source.method,
                        source.url,
                        int(source.active),
                        source.alert_threshold_minutes,
                        now,
                        now,
                    ),
                )
                source_row = conn.execute(
                    "SELECT id FROM source_configs WHERE source_name=?",
                    (source.source_name,),
                ).fetchone()
                source_id = int(source_row["id"])
                conn.execute(
                    """
                    INSERT INTO source_health(
                        source_id, source_name, priority, lane, health_status, alert_threshold_minutes
                    )
                    VALUES (?, ?, ?, ?, 'unknown', ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        source_name=excluded.source_name,
                        priority=excluded.priority,
                        lane=excluded.lane,
                        alert_threshold_minutes=excluded.alert_threshold_minutes
                    """,
                    (
                        source_id,
                        source.source_name,
                        source.priority,
                        source.lane,
                        source.alert_threshold_minutes,
                    ),
                )
            # Deactivate any source no longer in the seed list so config stays
            # the single source of truth (e.g. retired commercial-API lanes).
            keep = [s.source_name for s in source_list]
            if keep:
                placeholders = ",".join("?" for _ in keep)
                conn.execute(
                    f"UPDATE source_configs SET active=0, updated_at=? "
                    f"WHERE source_name NOT IN ({placeholders})",
                    (now, *keep),
                )

    # --- system_state (bootstrap / cold-start ordering) ---
    def get_state(self, key: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO system_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, dt_to_str(utcnow())),
            )

    def is_bootstrapped(self) -> bool:
        return self.get_state(BOOTSTRAP_KEY) is not None

    def mark_bootstrapped(self) -> None:
        self.set_state(BOOTSTRAP_KEY, dt_to_str(utcnow()) or "")

    def get_sources(self, lane: Optional[str] = None) -> List[SourceConfig]:
        sql = "SELECT * FROM source_configs WHERE active=1"
        params: Sequence[Any] = ()
        if lane:
            sql += " AND lane=?"
            params = (lane,)
        sql += " ORDER BY priority, source_name"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            SourceConfig(
                source_name=row["source_name"],
                source_type=row["source_type"],
                priority=row["priority"],
                lane=row["lane"],
                method=row["method"],
                url=row["url"],
                active=bool(row["active"]),
                alert_threshold_minutes=int(row["alert_threshold_minutes"]),
                id=int(row["id"]),
            )
            for row in rows
        ]

    def record_source_attempt(self, source: SourceConfig) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE source_health SET last_attempt_at=? WHERE source_id=?",
                (dt_to_str(utcnow()), source.id),
            )

    def record_source_success(self, source: SourceConfig) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_health
                SET last_success_at=?, last_attempt_at=?, consecutive_failures=0,
                    last_error=NULL, health_status='ok'
                WHERE source_id=?
                """,
                (dt_to_str(utcnow()), dt_to_str(utcnow()), source.id),
            )

    def record_source_failure(self, source: SourceConfig, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_health
                SET last_attempt_at=?, consecutive_failures=consecutive_failures + 1,
                    last_error=?, health_status='failing'
                WHERE source_id=?
                """,
                (dt_to_str(utcnow()), error[:1000], source.id),
            )

    def unhealthy_sources(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or utcnow()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT h.* FROM source_health h "
                "JOIN source_configs c ON c.id=h.source_id WHERE c.active=1"
            ).fetchall()
        unhealthy = []
        for row in rows:
            last_success = str_to_dt(row["last_success_at"])
            threshold = int(row["alert_threshold_minutes"])
            status = row["health_status"]
            stale = last_success is None or (now - last_success).total_seconds() > threshold * 60
            if row["priority"] == "P0" and stale:
                unhealthy.append(dict(row))
            elif status == "failing" and int(row["consecutive_failures"]) > 0:
                unhealthy.append(dict(row))
        return unhealthy

    def list_source_health(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT h.* FROM source_health h "
                "JOIN source_configs c ON c.id=h.source_id "
                "WHERE c.active=1 ORDER BY h.priority, h.source_name"
            ).fetchall()]

    def upsert_mention(self, mention: Mention) -> Mention:
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            existing = None
            if mention.provider and mention.provider_item_id:
                existing = conn.execute(
                    "SELECT id, content_hash, first_seen_at FROM mentions "
                    "WHERE provider=? AND provider_item_id=?",
                    (mention.provider, mention.provider_item_id),
                ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT id, content_hash, first_seen_at FROM mentions WHERE canonical_url=?",
                    (mention.canonical_url,),
                ).fetchone()
            if existing:
                mention.id = int(existing["id"])
                mention.is_new = False
                mention.is_updated = existing["content_hash"] != mention.content_hash
                mention.first_seen_at = str_to_dt(existing["first_seen_at"]) or mention.first_seen_at
                conn.execute(
                    """
                    UPDATE mentions SET
                        title=?, raw_text=?, text_excerpt=?, matched_keywords=?,
                        content_hash=?, event_fingerprint=?, duplicate_group_id=?, is_new=0, is_updated=?,
                        backfill=backfill AND ?, fetched_at=?, platform=?, content_type=?,
                        provider=?, provider_item_id=?, parent_url=?, discovery_method=?,
                        coverage_tier=?, provider_added_at=?,
                        discovery_latency_seconds=COALESCE(discovery_latency_seconds, ?),
                        view_count=?, like_count=?, comment_count=?, share_count=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        mention.title,
                        mention.raw_text,
                        mention.text_excerpt,
                        json.dumps(mention.matched_keywords, ensure_ascii=False),
                        mention.content_hash,
                        mention.event_fingerprint,
                        mention.duplicate_group_id,
                        int(mention.is_updated),
                        int(mention.backfill),
                        dt_to_str(mention.fetched_at),
                        mention.platform,
                        mention.content_type,
                        mention.provider,
                        mention.provider_item_id,
                        mention.parent_url,
                        mention.discovery_method,
                        mention.coverage_tier,
                        dt_to_str(mention.provider_added_at),
                        mention.discovery_latency_seconds,
                        mention.view_count,
                        mention.like_count,
                        mention.comment_count,
                        mention.share_count,
                        now,
                        mention.id,
                    ),
                )
                return mention

            cur = conn.execute(
                """
                INSERT INTO mentions(
                    source_type, source_name, source_url, canonical_url, title,
                    author_or_publisher, published_at, fetched_at, first_seen_at,
                    language, country_or_market, raw_text, text_excerpt, matched_keywords,
                    content_hash, event_fingerprint, duplicate_group_id, is_new, is_updated,
                    backfill, tos_method, fetch_status, fetch_error, platform, content_type,
                    provider, provider_item_id, parent_url, discovery_method, coverage_tier,
                    provider_added_at, discovery_latency_seconds, view_count, like_count,
                    comment_count, share_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mention.source_type,
                    mention.source_name,
                    mention.source_url,
                    mention.canonical_url,
                    mention.title,
                    mention.author_or_publisher,
                    dt_to_str(mention.published_at),
                    dt_to_str(mention.fetched_at),
                    dt_to_str(mention.first_seen_at),
                    mention.language,
                    mention.country_or_market,
                    mention.raw_text,
                    mention.text_excerpt,
                    json.dumps(mention.matched_keywords, ensure_ascii=False),
                    mention.content_hash,
                    mention.event_fingerprint,
                    mention.duplicate_group_id,
                    int(mention.is_new),
                    int(mention.is_updated),
                    int(mention.backfill),
                    mention.tos_method,
                    mention.fetch_status,
                    mention.fetch_error,
                    mention.platform,
                    mention.content_type,
                    mention.provider,
                    mention.provider_item_id,
                    mention.parent_url,
                    mention.discovery_method,
                    mention.coverage_tier,
                    dt_to_str(mention.provider_added_at),
                    mention.discovery_latency_seconds,
                    mention.view_count,
                    mention.like_count,
                    mention.comment_count,
                    mention.share_count,
                    now,
                    now,
                ),
            )
            mention.id = int(cur.lastrowid)
        return mention

    def insert_analysis(self, result: AnalysisResult) -> AnalysisResult:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO analysis_results(
                    mention_id, model_provider, model_name, prompt_version, sentiment,
                    risk_level, category, summary_zh, key_quotes, key_quote_offsets,
                    requires_escalation, escalation_reason, confidence,
                    evidence_check_passed, evidence_check_notes, needs_human_review,
                    analysis_created_at, review_status, campaign, relevance, novelty_type,
                    notification_priority, recommended_action
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.mention_id,
                    result.model_provider,
                    result.model_name,
                    result.prompt_version,
                    result.sentiment,
                    result.risk_level,
                    result.category,
                    result.summary_zh,
                    json.dumps(result.key_quotes, ensure_ascii=False),
                    json.dumps(result.key_quote_offsets, ensure_ascii=False),
                    int(result.requires_escalation),
                    result.escalation_reason,
                    result.confidence,
                    int(result.evidence_check_passed),
                    result.evidence_check_notes,
                    int(result.needs_human_review),
                    dt_to_str(result.analysis_created_at),
                    result.review_status,
                    result.campaign,
                    result.relevance,
                    result.novelty_type,
                    result.notification_priority,
                    result.recommended_action,
                ),
            )
            result.id = int(cur.lastrowid)
        return result

    def upsert_incident_group(self, mention: Mention, analysis: AnalysisResult) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, mention_count, risk_level_max FROM incident_groups WHERE fingerprint=?",
                (mention.event_fingerprint,),
            ).fetchone()
            if existing:
                risk_level = max_risk(str(existing["risk_level_max"]), analysis.risk_level)
                conn.execute(
                    """
                    UPDATE incident_groups
                    SET last_seen_at=?, risk_level_max=?, mention_count=mention_count + ?,
                        source_count=(
                            SELECT COUNT(DISTINCT source_name)
                            FROM mentions
                            WHERE event_fingerprint=?
                        )
                    WHERE id=?
                    """,
                    (
                        dt_to_str(mention.fetched_at),
                        risk_level,
                        1 if mention.is_new else 0,
                        mention.event_fingerprint,
                        existing["id"],
                    ),
                )
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO incident_groups(
                    fingerprint, primary_topic, first_seen_at, last_seen_at, risk_level_max,
                    mention_count, source_count, representative_mention_id, status, notes
                )
                VALUES (?, ?, ?, ?, ?, 1, 1, ?, 'active', NULL)
                """,
                (
                    mention.event_fingerprint,
                    mention.title[:300],
                    dt_to_str(mention.first_seen_at),
                    dt_to_str(mention.fetched_at),
                    analysis.risk_level,
                    mention.id,
                ),
            )
            return int(cur.lastrowid)

    def alert_exists(self, dedupe_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM alerts WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        return row is not None

    def insert_alert(
        self,
        mention_id: int,
        incident_group_id: Optional[int],
        risk_level: str,
        alert_reason: str,
        dedupe_key: str,
        confidence: float,
        evidence_check_passed: bool,
        needs_human_review: bool,
        delivery_latency_seconds: Optional[int],
        sent_to: Optional[str],
        sent_at: Optional[datetime],
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alerts(
                    mention_id, incident_group_id, risk_level, alert_reason, dedupe_key,
                    confidence, evidence_check_passed, needs_human_review,
                    delivery_latency_seconds, sent_to, sent_at, ack_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    mention_id,
                    incident_group_id,
                    risk_level,
                    alert_reason,
                    dedupe_key,
                    confidence,
                    int(evidence_check_passed),
                    int(needs_human_review),
                    delivery_latency_seconds,
                    sent_to,
                    dt_to_str(sent_at),
                    dt_to_str(utcnow()),
                ),
            )
            return int(cur.lastrowid)

    def insert_alert_with_outbox(
        self,
        *,
        mention_id: int,
        incident_group_id: Optional[int],
        risk_level: str,
        alert_reason: str,
        dedupe_key: str,
        confidence: float,
        evidence_check_passed: bool,
        needs_human_review: bool,
        delivery_latency_seconds: Optional[int],
        payload: Dict[str, Any],
    ) -> int:
        """Atomically persist the alert and its delivery job.

        The alert dedupe key is also the outbox dedupe key, so a transport
        failure can be retried without either losing or duplicating the page.
        """
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alerts(
                    mention_id, incident_group_id, risk_level, alert_reason, dedupe_key,
                    confidence, evidence_check_passed, needs_human_review,
                    delivery_latency_seconds, sent_to, sent_at, ack_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'pending', ?)
                """,
                (
                    mention_id,
                    incident_group_id,
                    risk_level,
                    alert_reason,
                    dedupe_key,
                    confidence,
                    int(evidence_check_passed),
                    int(needs_human_review),
                    delivery_latency_seconds,
                    now,
                ),
            )
            alert_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO delivery_outbox(
                    kind, dedupe_key, entity_type, entity_id, payload_json,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                )
                VALUES ('red_alert', ?, 'alert', ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (dedupe_key, alert_id, json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            return alert_id

    def enqueue_delivery(
        self,
        *,
        kind: str,
        dedupe_key: str,
        entity_type: str,
        entity_id: int,
        payload: Dict[str, Any],
    ) -> bool:
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO delivery_outbox(
                    kind, dedupe_key, entity_type, entity_id, payload_json,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    kind,
                    dedupe_key,
                    entity_type,
                    entity_id,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
            return cur.rowcount > 0

    def list_due_deliveries(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE status='pending' AND next_attempt_at <= ?
                ORDER BY CASE kind WHEN 'red_alert' THEN 0 ELSE 1 END, id
                LIMIT ?
                """,
                (dt_to_str(utcnow()), limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def mark_delivery_sent(self, job_id: int, sent_to: str, sent_at: datetime) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT entity_type, entity_id, payload_json FROM delivery_outbox WHERE id=?",
                (job_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE delivery_outbox
                SET status='sent', delivered_at=?, last_error=NULL, updated_at=?
                WHERE id=?
                """,
                (dt_to_str(sent_at), dt_to_str(sent_at), job_id),
            )
            if row and row["entity_type"] == "alert":
                conn.execute(
                    "UPDATE alerts SET sent_to=?, sent_at=? WHERE id=?",
                    (sent_to, dt_to_str(sent_at), row["entity_id"]),
                )
                conn.execute(
                    """
                    UPDATE recall_mentions
                    SET sync_status='synced', synced_at=?, updated_at=?
                    WHERE mention_id=(SELECT mention_id FROM alerts WHERE id=?)
                    """,
                    (dt_to_str(sent_at), dt_to_str(sent_at), row["entity_id"]),
                )
            if row and row["entity_type"] == "recall_mention":
                conn.execute(
                    """
                    UPDATE recall_mentions
                    SET sync_status='synced', synced_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (dt_to_str(sent_at), dt_to_str(sent_at), row["entity_id"]),
                )
            if row and row["entity_type"] == "recall_batch":
                payload = json.loads(row["payload_json"])
                record_ids = [
                    int(record_id)
                    for record_id in payload.get("_recall_record_ids", [])
                    if str(record_id).isdigit()
                ]
                if record_ids:
                    placeholders = ",".join("?" for _ in record_ids)
                    conn.execute(
                        f"""
                        UPDATE recall_mentions
                        SET sync_status='synced', synced_at=?, updated_at=?
                        WHERE id IN ({placeholders})
                        """,
                        (dt_to_str(sent_at), dt_to_str(sent_at), *record_ids),
                    )
            if row and row["entity_type"] == "mention_batch":
                payload = json.loads(row["payload_json"])
                mention_ids = [int(value) for value in payload.get("_mention_ids", []) if str(value).isdigit()]
                if mention_ids:
                    placeholders = ",".join("?" for _ in mention_ids)
                    conn.execute(
                        f"UPDATE mention_notifications SET delivery_status='sent', delivered_at=?, updated_at=? WHERE mention_id IN ({placeholders})",
                        (dt_to_str(sent_at), dt_to_str(sent_at), *mention_ids),
                    )
                    conn.execute(
                        f"UPDATE recall_mentions SET sync_status='synced', synced_at=?, updated_at=? WHERE mention_id IN ({placeholders})",
                        (dt_to_str(sent_at), dt_to_str(sent_at), *mention_ids),
                    )
                    conn.execute(
                        f"UPDATE alerts SET sent_to=?, sent_at=? WHERE mention_id IN ({placeholders}) AND sent_at IS NULL",
                        (sent_to, dt_to_str(sent_at), *mention_ids),
                    )

    def supersede_pending_recall_updates(self) -> int:
        """Retire legacy one-item jobs without marking their registry rows synced."""
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE delivery_outbox
                SET status='superseded', updated_at=?
                WHERE status='pending' AND kind='recall_update'
                """,
                (now,),
            )
            return cur.rowcount

    def has_pending_delivery(self, kind: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM delivery_outbox WHERE status='pending' AND kind=? LIMIT 1",
                (kind,),
            ).fetchone()
        return row is not None

    def mark_delivery_failed(self, job_id: int, error: str, next_attempt_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE delivery_outbox
                SET attempt_count=attempt_count+1, last_error=?, next_attempt_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    error[:1000],
                    dt_to_str(next_attempt_at),
                    dt_to_str(utcnow()),
                    job_id,
                ),
            )

    def upsert_recall_mention(
        self,
        mention_id: int,
        *,
        origin: str = "external",
        sync_status: str = "pending",
        group_synced_at: Optional[datetime] = None,
    ) -> int:
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO recall_mentions(
                    campaign_key, mention_id, origin, sync_status, group_synced_at,
                    first_discovered_at, last_seen_at, created_at, updated_at
                )
                VALUES ('ocoopa-cpsc-26-659', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mention_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    origin=CASE
                        WHEN recall_mentions.origin='group_import' THEN recall_mentions.origin
                        ELSE excluded.origin
                    END,
                    sync_status=CASE
                        WHEN recall_mentions.sync_status='synced' THEN recall_mentions.sync_status
                        ELSE excluded.sync_status
                    END,
                    group_synced_at=COALESCE(recall_mentions.group_synced_at, excluded.group_synced_at),
                    updated_at=excluded.updated_at
                """,
                (
                    mention_id,
                    origin,
                    sync_status,
                    dt_to_str(group_synced_at),
                    now,
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM recall_mentions WHERE mention_id=?", (mention_id,)
            ).fetchone()
            return int(row["id"])

    def list_recall_mentions(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.id AS recall_record_id, r.campaign_key, r.origin, r.sync_status,
                       r.first_discovered_at, r.last_seen_at, r.group_synced_at, r.synced_at,
                       m.id AS mention_id, m.source_type, m.source_name, m.source_url,
                       m.title, m.author_or_publisher, m.published_at, m.fetched_at,
                       m.text_excerpt, m.matched_keywords, m.backfill, m.event_fingerprint,
                       a.sentiment, a.risk_level, a.category, a.summary_zh,
                       a.confidence, a.evidence_check_passed, a.needs_human_review,
                       EXISTS(
                           SELECT 1 FROM alerts alert WHERE alert.mention_id=m.id
                       ) AS has_alert,
                       EXISTS(
                           SELECT 1 FROM mention_notifications n WHERE n.mention_id=m.id
                       ) AS has_mention_notification
                FROM recall_mentions r
                JOIN mentions m ON m.id=r.mention_id
                LEFT JOIN analysis_results a ON a.id=(
                    SELECT id FROM analysis_results
                    WHERE mention_id=m.id ORDER BY id DESC LIMIT 1
                )
                ORDER BY COALESCE(m.published_at, m.fetched_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- M2 human feedback loop (review CLI) ---
    def is_incident_suppressed(self, event_fingerprint: str, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, muted_until FROM incident_groups WHERE fingerprint=?",
                (event_fingerprint,),
            ).fetchone()
        return _incident_suppressed(row["status"], str_to_dt(row["muted_until"]), now) if row else False

    def review_incident(self, event_fingerprint: str, status: str, muted_until: Optional[datetime]) -> bool:
        incident_status, review_status = _review_to_status(status)
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE incident_groups SET status=?, muted_until=? WHERE fingerprint=?",
                (incident_status, dt_to_str(muted_until), event_fingerprint),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "UPDATE analysis_results SET review_status=? "
                "WHERE mention_id IN (SELECT id FROM mentions WHERE event_fingerprint=?)",
                (review_status, event_fingerprint),
            )
        return True

    def get_fingerprint_by_alert(self, alert_id: int) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT m.event_fingerprint AS fp FROM alerts a "
                "JOIN mentions m ON m.id=a.mention_id WHERE a.id=?",
                (alert_id,),
            ).fetchone()
        return row["fp"] if row else None

    def list_recent_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, a.risk_level, a.needs_human_review, a.ack_status,
                       a.sent_at, m.event_fingerprint, m.title, m.source_url,
                       g.status AS incident_status, g.muted_until
                FROM alerts a
                JOIN mentions m ON m.id=a.mention_id
                LEFT JOIN incident_groups g ON g.fingerprint=m.event_fingerprint
                ORDER BY a.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_alerts_between(self, start_at: datetime, end_at: datetime) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, a.risk_level, a.needs_human_review, a.ack_status,
                       a.sent_at, a.created_at, m.event_fingerprint, m.title, m.source_url,
                       g.status AS incident_status, g.muted_until
                FROM alerts a
                JOIN mentions m ON m.id=a.mention_id
                LEFT JOIN incident_groups g ON g.fingerprint=m.event_fingerprint
                WHERE a.created_at >= ? AND a.created_at < ?
                ORDER BY a.created_at DESC, a.id DESC
                """,
                (dt_to_str(start_at), dt_to_str(end_at)),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Tier 2 cross-source alert cooldown ---
    def get_topic_alert(self, topic_key: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT last_alert_at, alerted_source_types, risk_level_max FROM alert_topics WHERE topic_key=?",
                (topic_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "last_alert_at": str_to_dt(row["last_alert_at"]),
            "alerted_source_types": set(json.loads(row["alerted_source_types"] or "[]")),
            "risk_level_max": row["risk_level_max"],
        }

    def record_topic_alert(self, topic_key: str, source_type: str, risk_level: str, now: datetime) -> None:
        existing = self.get_topic_alert(topic_key)
        types = existing["alerted_source_types"] if existing else set()
        types.add(source_type)
        rmax = max_risk(existing["risk_level_max"], risk_level) if existing else risk_level
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_topics(topic_key, last_alert_at, alerted_source_types, risk_level_max, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(topic_key) DO UPDATE SET
                    last_alert_at=excluded.last_alert_at,
                    alerted_source_types=excluded.alerted_source_types,
                    risk_level_max=excluded.risk_level_max,
                    updated_at=excluded.updated_at
                """,
                (topic_key, dt_to_str(now), json.dumps(sorted(types)), rmax, dt_to_str(utcnow())),
            )

    # --- Tier 2 red-alert ack / unacked escalation ---
    def ack_alert(self, alert_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE alerts SET ack_status='acked' WHERE id=? AND ack_status!='acked'",
                (alert_id,),
            )
            return cur.rowcount > 0

    def mark_alert_escalated(self, alert_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE alerts SET ack_status='escalated' WHERE id=?", (alert_id,))

    def pending_red_alerts_older_than(self, cutoff: datetime) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, a.sent_at, m.title, m.source_url
                FROM alerts a JOIN mentions m ON m.id=a.mention_id
                WHERE a.risk_level='red' AND a.ack_status='pending' AND a.sent_at IS NOT NULL
                      AND a.sent_at < ?
                ORDER BY a.id
                """,
                (dt_to_str(cutoff),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_interaction_snapshot(self, mention: Mention) -> Optional[Dict[str, Any]]:
        if mention.id is None or all(
            value is None
            for value in (mention.view_count, mention.like_count, mention.comment_count, mention.share_count)
        ):
            return None
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT view_count, like_count, comment_count, share_count, captured_at "
                "FROM mention_metrics WHERE mention_id=? ORDER BY id DESC LIMIT 1",
                (mention.id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO mention_metrics(mention_id, view_count, like_count, comment_count, share_count, captured_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mention.id, mention.view_count, mention.like_count, mention.comment_count, mention.share_count, dt_to_str(mention.fetched_at)),
            )
        return dict(previous) if previous else None

    def ensure_mention_action(self, mention_id: int, guidance: Dict[str, str]) -> None:
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mention_actions(
                    mention_id, intervention_reason, draft_original, draft_zh,
                    legal_risk_note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mention_id) DO NOTHING
                """,
                (
                    mention_id,
                    guidance.get("intervention_reason", ""),
                    guidance.get("draft_original", ""),
                    guidance.get("draft_zh", ""),
                    guidance.get("legal_risk_note", ""),
                    now,
                    now,
                ),
            )

    def update_mention_action(
        self, mention_id: int, status: str, response_url: str, internal_note: str, operator: str
    ) -> bool:
        if status not in MENTION_ACTION_STATUSES:
            raise ValueError(f"invalid mention action status: {status}")
        now = dt_to_str(utcnow())
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE mention_actions SET status=?, response_url=?, internal_note=?,
                    operator=?, acted_at=?, updated_at=? WHERE mention_id=?
                """,
                (status, response_url[:2000], internal_note[:4000], operator[:200], now, now, mention_id),
            )
            return cur.rowcount > 0

    def enqueue_mention_batch(
        self, mention_ids: List[int], priority: str, payload: Dict[str, Any]
    ) -> bool:
        ids = sorted(set(int(value) for value in mention_ids))
        if not ids:
            return False
        now = dt_to_str(utcnow())
        dedupe_key = "mentions:first:" + ",".join(str(value) for value in ids)
        with self.connect() as conn:
            new_ids = []
            for mention_id in ids:
                cur = conn.execute(
                    """
                    INSERT INTO mention_notifications(
                        mention_id, notification_priority, delivery_status,
                        outbox_dedupe_key, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, ?, ?)
                    ON CONFLICT(mention_id) DO NOTHING
                    """,
                    (mention_id, priority, dedupe_key, now, now),
                )
                if cur.rowcount:
                    new_ids.append(mention_id)
            if not new_ids:
                return False
            batch_key = "mentions:first:" + ",".join(str(value) for value in new_ids)
            payload = dict(payload)
            payload["_mention_ids"] = new_ids
            conn.execute(
                """
                INSERT INTO delivery_outbox(
                    kind, dedupe_key, entity_type, entity_id, payload_json,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                ) VALUES ('mention_batch', ?, 'mention_batch', ?, ?, 'pending', 0, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (batch_key, new_ids[0], json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            conn.executemany(
                "UPDATE mention_notifications SET outbox_dedupe_key=? WHERE mention_id=?",
                [(batch_key, mention_id) for mention_id in new_ids],
            )
        return True

    def list_recent_incidents_detailed(self, limit: int = 50) -> List[Dict[str, Any]]:
        incidents = self.list_recent_incidents(limit)
        with self.connect() as conn:
            for incident in incidents:
                rows = conn.execute(
                    """
                    SELECT m.*, a.summary_zh, a.risk_level, a.campaign, a.relevance,
                           a.novelty_type, a.notification_priority, a.recommended_action,
                           x.status AS response_status, x.response_url, x.internal_note,
                           x.operator, x.intervention_reason, x.draft_original, x.draft_zh,
                           x.legal_risk_note, x.acted_at
                    FROM mentions m
                    LEFT JOIN analysis_results a ON a.id=(SELECT id FROM analysis_results WHERE mention_id=m.id ORDER BY id DESC LIMIT 1)
                    LEFT JOIN mention_actions x ON x.mention_id=m.id
                    WHERE m.event_fingerprint=? ORDER BY COALESCE(m.published_at, m.fetched_at) DESC
                    """,
                    (incident["fingerprint"],),
                ).fetchall()
                incident["mentions"] = [dict(row) for row in rows]
        return incidents

    # --- Incident-level review (red/yellow events, even if never alerted) ---
    def list_recent_incidents(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT g.id AS incident_id, g.fingerprint, g.primary_topic, g.risk_level_max,
                       g.status, g.muted_until, g.mention_count, g.source_count, g.last_seen_at,
                       m.title, m.source_url,
                       a.needs_human_review, a.summary_zh, a.evidence_check_passed
                FROM incident_groups g
                LEFT JOIN mentions m ON m.id = g.representative_mention_id
                LEFT JOIN analysis_results a ON a.id = (
                    SELECT id FROM analysis_results
                    WHERE mention_id = g.representative_mention_id ORDER BY id DESC LIMIT 1
                )
                WHERE g.risk_level_max IN ('red', 'yellow')
                ORDER BY g.last_seen_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_fingerprint_by_incident(self, incident_id: int) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM incident_groups WHERE id=?", (incident_id,)
            ).fetchone()
        return row["fingerprint"] if row else None

    def ack_alerts_by_fingerprint(self, event_fingerprint: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE alerts SET ack_status='acked' WHERE ack_status!='acked' "
                "AND mention_id IN (SELECT id FROM mentions WHERE event_fingerprint=?)",
                (event_fingerprint,),
            )

    def fetch_mentions_for_day(self, date_prefix: str) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT m.*, a.sentiment, a.risk_level, a.category, a.summary_zh,
                       a.escalation_reason, a.confidence, a.evidence_check_passed,
                       a.needs_human_review, a.campaign, a.relevance, a.novelty_type,
                       a.notification_priority, a.recommended_action,
                       x.status AS response_status, x.acted_at,
                       n.delivery_status AS notification_status,
                       n.created_at AS notification_created_at,
                       n.delivered_at AS notification_delivered_at
                FROM mentions m
                LEFT JOIN analysis_results a ON a.id=(
                    SELECT id FROM analysis_results
                    WHERE mention_id=m.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN mention_actions x ON x.mention_id=m.id
                LEFT JOIN mention_notifications n ON n.mention_id=m.id
                WHERE substr(m.first_seen_at, 1, 10)=?
                ORDER BY
                    CASE a.risk_level WHEN 'red' THEN 1 WHEN 'yellow' THEN 2 ELSE 3 END,
                    m.first_seen_at DESC
                """,
                (date_prefix,),
            ).fetchall()

    def fetch_mentions_between(self, start_at: datetime, end_at: datetime) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT m.*, a.sentiment, a.risk_level, a.category, a.summary_zh,
                       a.escalation_reason, a.confidence, a.evidence_check_passed,
                       a.needs_human_review, a.campaign, a.relevance, a.novelty_type,
                       a.notification_priority, a.recommended_action,
                       x.status AS response_status, x.acted_at,
                       n.delivery_status AS notification_status,
                       n.created_at AS notification_created_at,
                       n.delivered_at AS notification_delivered_at
                FROM mentions m
                LEFT JOIN analysis_results a ON a.id=(
                    SELECT id FROM analysis_results
                    WHERE mention_id=m.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN mention_actions x ON x.mention_id=m.id
                LEFT JOIN mention_notifications n ON n.mention_id=m.id
                WHERE m.first_seen_at >= ? AND m.first_seen_at < ?
                ORDER BY
                    CASE a.risk_level WHEN 'red' THEN 1 WHEN 'yellow' THEN 2 ELSE 3 END,
                    m.first_seen_at DESC
                """,
                (dt_to_str(start_at), dt_to_str(end_at)),
            ).fetchall()

    def insert_daily_report(self, report: Dict[str, Any]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO daily_reports(
                    report_date, timezone, total_mentions, new_mentions, backfill_mentions,
                    sentiment_distribution, source_distribution, risk_distribution,
                    top_risks, trend_vs_yesterday, recommended_actions,
                    generated_text_zh, delivery_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_date, timezone) DO UPDATE SET
                    total_mentions=excluded.total_mentions,
                    new_mentions=excluded.new_mentions,
                    backfill_mentions=excluded.backfill_mentions,
                    sentiment_distribution=excluded.sentiment_distribution,
                    source_distribution=excluded.source_distribution,
                    risk_distribution=excluded.risk_distribution,
                    top_risks=excluded.top_risks,
                    trend_vs_yesterday=excluded.trend_vs_yesterday,
                    recommended_actions=excluded.recommended_actions,
                    generated_text_zh=excluded.generated_text_zh,
                    delivery_status=excluded.delivery_status,
                    created_at=excluded.created_at
                """,
                (
                    report["report_date"],
                    report["timezone"],
                    report["total_mentions"],
                    report["new_mentions"],
                    report["backfill_mentions"],
                    json.dumps(report["sentiment_distribution"], ensure_ascii=False),
                    json.dumps(report["source_distribution"], ensure_ascii=False),
                    json.dumps(report["risk_distribution"], ensure_ascii=False),
                    json.dumps(report["top_risks"], ensure_ascii=False),
                    json.dumps(report["trend_vs_yesterday"], ensure_ascii=False),
                    json.dumps(report["recommended_actions"], ensure_ascii=False),
                    report["generated_text_zh"],
                    report["delivery_status"],
                    dt_to_str(utcnow()),
                ),
            )
            return int(cur.lastrowid)


class PostgresDatabase:
    def __init__(self, db_url: str):
        self.db_url = db_url

    @contextmanager
    def connect(self):
        psycopg, dict_row, _ = self._pg_modules()
        conn = psycopg.connect(self.db_url, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # Columns added to tables after their initial creation. CREATE TABLE IF NOT
    # EXISTS never alters an existing table, so apply these idempotently on init.
    _PG_COLUMN_MIGRATIONS = (
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS delivery_latency_seconds INTEGER",
        "ALTER TABLE incident_groups ADD COLUMN IF NOT EXISTS muted_until TIMESTAMPTZ",
    )

    def init(self) -> None:
        with self.connect() as conn:
            for migration in _postgres_migration_sqls():
                for statement in migration.split(";"):
                    statement = statement.strip()
                    if statement:
                        conn.execute(statement)
            for statement in self._PG_COLUMN_MIGRATIONS:
                conn.execute(statement)

    def seed_keywords(self, keywords: Iterable[Keyword]) -> None:
        now = utcnow()
        with self.connect() as conn:
            for kw in keywords:
                conn.execute(
                    """
                    INSERT INTO keywords(term, category, lane, active, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT(term) DO UPDATE SET
                        category=excluded.category,
                        lane=excluded.lane,
                        updated_at=excluded.updated_at
                    """,
                    (kw.term, kw.category, kw.lane, kw.active, now, now),
                )

    def get_keywords(self, lane: Optional[str] = None) -> List[Keyword]:
        sql = "SELECT term, category, lane, active FROM keywords WHERE active=TRUE"
        params: List[Any] = []
        if lane == "high":
            sql += " AND lane=%s"
            params.append("high")
        elif lane == "regular":
            sql += " AND lane IN ('high', 'regular')"
        sql += " ORDER BY term"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Keyword(row["term"], row["category"], row["lane"], bool(row["active"])) for row in rows]

    def list_keywords_all(self) -> List[Keyword]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT term, category, lane, active FROM keywords ORDER BY active DESC, lane, term"
            ).fetchall()
        return [Keyword(row["term"], row["category"], row["lane"], bool(row["active"])) for row in rows]

    def upsert_keyword(self, term: str, category: str, lane: str, active: bool = True) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO keywords(term, category, lane, active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(term) DO UPDATE SET
                    category=excluded.category, lane=excluded.lane,
                    active=excluded.active, updated_at=excluded.updated_at
                """,
                (term, category, lane, active, now, now),
            )

    def set_keyword_active(self, term: str, active: bool) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE keywords SET active=%s, updated_at=%s WHERE term=%s",
                (active, utcnow(), term),
            )
            return cur.rowcount > 0

    def seed_sources(self, sources: Iterable[SourceConfig]) -> None:
        now = utcnow()
        source_list = list(sources)
        with self.connect() as conn:
            for source in source_list:
                source_row = conn.execute(
                    """
                    INSERT INTO source_configs(
                        source_name, source_type, priority, lane, method, url, active,
                        alert_threshold_minutes, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(source_name) DO UPDATE SET
                        source_type=excluded.source_type,
                        priority=excluded.priority,
                        lane=excluded.lane,
                        method=excluded.method,
                        url=excluded.url,
                        active=excluded.active,
                        alert_threshold_minutes=excluded.alert_threshold_minutes,
                        updated_at=excluded.updated_at
                    RETURNING id
                    """,
                    (
                        source.source_name,
                        source.source_type,
                        source.priority,
                        source.lane,
                        source.method,
                        source.url,
                        source.active,
                        source.alert_threshold_minutes,
                        now,
                        now,
                    ),
                ).fetchone()
                source_id = int(source_row["id"])
                conn.execute(
                    """
                    INSERT INTO source_health(
                        source_id, source_name, priority, lane, health_status, alert_threshold_minutes
                    )
                    VALUES (%s, %s, %s, %s, 'unknown', %s)
                    ON CONFLICT(source_id) DO UPDATE SET
                        source_name=excluded.source_name,
                        priority=excluded.priority,
                        lane=excluded.lane,
                        alert_threshold_minutes=excluded.alert_threshold_minutes
                    """,
                    (
                        source_id,
                        source.source_name,
                        source.priority,
                        source.lane,
                        source.alert_threshold_minutes,
                    ),
                )
            keep = [s.source_name for s in source_list]
            if keep:
                placeholders = ",".join("%s" for _ in keep)
                conn.execute(
                    f"UPDATE source_configs SET active=FALSE, updated_at=%s "
                    f"WHERE source_name NOT IN ({placeholders})",
                    (now, *keep),
                )

    # --- system_state (bootstrap / cold-start ordering) ---
    def get_state(self, key: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO system_state(key, value, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utcnow()),
            )

    def is_bootstrapped(self) -> bool:
        return self.get_state(BOOTSTRAP_KEY) is not None

    def mark_bootstrapped(self) -> None:
        self.set_state(BOOTSTRAP_KEY, (dt_to_str(utcnow()) or ""))

    def get_sources(self, lane: Optional[str] = None) -> List[SourceConfig]:
        sql = "SELECT * FROM source_configs WHERE active=TRUE"
        params: List[Any] = []
        if lane:
            sql += " AND lane=%s"
            params.append(lane)
        sql += " ORDER BY priority, source_name"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            SourceConfig(
                source_name=row["source_name"],
                source_type=row["source_type"],
                priority=row["priority"],
                lane=row["lane"],
                method=row["method"],
                url=row["url"],
                active=bool(row["active"]),
                alert_threshold_minutes=int(row["alert_threshold_minutes"]),
                id=int(row["id"]),
            )
            for row in rows
        ]

    def record_source_attempt(self, source: SourceConfig) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE source_health SET last_attempt_at=%s WHERE source_id=%s",
                (utcnow(), source.id),
            )

    def record_source_success(self, source: SourceConfig) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_health
                SET last_success_at=%s, last_attempt_at=%s, consecutive_failures=0,
                    last_error=NULL, health_status='ok'
                WHERE source_id=%s
                """,
                (now, now, source.id),
            )

    def record_source_failure(self, source: SourceConfig, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE source_health
                SET last_attempt_at=%s, consecutive_failures=consecutive_failures + 1,
                    last_error=%s, health_status='failing'
                WHERE source_id=%s
                """,
                (utcnow(), error[:1000], source.id),
            )

    def unhealthy_sources(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or utcnow()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT h.* FROM source_health h "
                "JOIN source_configs c ON c.id=h.source_id WHERE c.active=TRUE"
            ).fetchall()
        unhealthy = []
        for row in rows:
            last_success = str_to_dt(row["last_success_at"])
            threshold = int(row["alert_threshold_minutes"])
            status = row["health_status"]
            stale = last_success is None or (now - last_success).total_seconds() > threshold * 60
            if row["priority"] == "P0" and stale:
                unhealthy.append(dict(row))
            elif status == "failing" and int(row["consecutive_failures"]) > 0:
                unhealthy.append(dict(row))
        return unhealthy

    def list_source_health(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT h.* FROM source_health h "
                "JOIN source_configs c ON c.id=h.source_id "
                "WHERE c.active=TRUE ORDER BY h.priority, h.source_name"
            ).fetchall()]

    def upsert_mention(self, mention: Mention) -> Mention:
        _, _, Json = self._pg_modules()
        now = utcnow()
        with self.connect() as conn:
            existing = None
            if mention.provider and mention.provider_item_id:
                existing = conn.execute(
                    "SELECT id, content_hash, first_seen_at FROM mentions "
                    "WHERE provider=%s AND provider_item_id=%s",
                    (mention.provider, mention.provider_item_id),
                ).fetchone()
            if existing is None:
                existing = conn.execute(
                    "SELECT id, content_hash, first_seen_at FROM mentions WHERE canonical_url=%s",
                    (mention.canonical_url,),
                ).fetchone()
            if existing:
                mention.id = int(existing["id"])
                mention.is_new = False
                mention.is_updated = existing["content_hash"] != mention.content_hash
                mention.first_seen_at = str_to_dt(existing["first_seen_at"]) or mention.first_seen_at
                conn.execute(
                    """
                    UPDATE mentions SET
                        title=%s, raw_text=%s, text_excerpt=%s, matched_keywords=%s,
                        content_hash=%s, event_fingerprint=%s, duplicate_group_id=%s, is_new=FALSE, is_updated=%s,
                        backfill=backfill AND %s, fetched_at=%s, platform=%s, content_type=%s,
                        provider=%s, provider_item_id=%s, parent_url=%s, discovery_method=%s,
                        coverage_tier=%s, provider_added_at=%s,
                        discovery_latency_seconds=COALESCE(discovery_latency_seconds, %s),
                        view_count=%s, like_count=%s, comment_count=%s, share_count=%s, updated_at=%s
                    WHERE id=%s
                    """,
                    (
                        mention.title,
                        mention.raw_text,
                        mention.text_excerpt,
                        Json(mention.matched_keywords),
                        mention.content_hash,
                        mention.event_fingerprint,
                        mention.duplicate_group_id,
                        mention.is_updated,
                        mention.backfill,
                        mention.fetched_at,
                        mention.platform,
                        mention.content_type,
                        mention.provider,
                        mention.provider_item_id,
                        mention.parent_url,
                        mention.discovery_method,
                        mention.coverage_tier,
                        mention.provider_added_at,
                        mention.discovery_latency_seconds,
                        mention.view_count,
                        mention.like_count,
                        mention.comment_count,
                        mention.share_count,
                        now,
                        mention.id,
                    ),
                )
                return mention
            row = conn.execute(
                """
                INSERT INTO mentions(
                    source_type, source_name, source_url, canonical_url, title,
                    author_or_publisher, published_at, fetched_at, first_seen_at,
                    language, country_or_market, raw_text, text_excerpt, matched_keywords,
                    content_hash, event_fingerprint, duplicate_group_id, is_new, is_updated,
                    backfill, tos_method, fetch_status, fetch_error, platform, content_type,
                    provider, provider_item_id, parent_url, discovery_method, coverage_tier,
                    provider_added_at, discovery_latency_seconds, view_count, like_count,
                    comment_count, share_count, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    mention.source_type,
                    mention.source_name,
                    mention.source_url,
                    mention.canonical_url,
                    mention.title,
                    mention.author_or_publisher,
                    mention.published_at,
                    mention.fetched_at,
                    mention.first_seen_at,
                    mention.language,
                    mention.country_or_market,
                    mention.raw_text,
                    mention.text_excerpt,
                    Json(mention.matched_keywords),
                    mention.content_hash,
                    mention.event_fingerprint,
                    mention.duplicate_group_id,
                    mention.is_new,
                    mention.is_updated,
                    mention.backfill,
                    mention.tos_method,
                    mention.fetch_status,
                    mention.fetch_error,
                    mention.platform,
                    mention.content_type,
                    mention.provider,
                    mention.provider_item_id,
                    mention.parent_url,
                    mention.discovery_method,
                    mention.coverage_tier,
                    mention.provider_added_at,
                    mention.discovery_latency_seconds,
                    mention.view_count,
                    mention.like_count,
                    mention.comment_count,
                    mention.share_count,
                    now,
                    now,
                ),
            ).fetchone()
            mention.id = int(row["id"])
        return mention

    def insert_analysis(self, result: AnalysisResult) -> AnalysisResult:
        _, _, Json = self._pg_modules()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO analysis_results(
                    mention_id, model_provider, model_name, prompt_version, sentiment,
                    risk_level, category, summary_zh, key_quotes, key_quote_offsets,
                    requires_escalation, escalation_reason, confidence,
                    evidence_check_passed, evidence_check_notes, needs_human_review,
                    analysis_created_at, review_status, campaign, relevance, novelty_type,
                    notification_priority, recommended_action
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    result.mention_id,
                    result.model_provider,
                    result.model_name,
                    result.prompt_version,
                    result.sentiment,
                    result.risk_level,
                    result.category,
                    result.summary_zh,
                    Json(result.key_quotes),
                    Json(result.key_quote_offsets),
                    result.requires_escalation,
                    result.escalation_reason,
                    result.confidence,
                    result.evidence_check_passed,
                    result.evidence_check_notes,
                    result.needs_human_review,
                    result.analysis_created_at,
                    result.review_status,
                    result.campaign,
                    result.relevance,
                    result.novelty_type,
                    result.notification_priority,
                    result.recommended_action,
                ),
            ).fetchone()
            result.id = int(row["id"])
        return result

    def upsert_incident_group(self, mention: Mention, analysis: AnalysisResult) -> int:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, mention_count, risk_level_max FROM incident_groups WHERE fingerprint=%s",
                (mention.event_fingerprint,),
            ).fetchone()
            if existing:
                risk_level = max_risk(str(existing["risk_level_max"]), analysis.risk_level)
                conn.execute(
                    """
                    UPDATE incident_groups
                    SET last_seen_at=%s, risk_level_max=%s, mention_count=mention_count + %s,
                        source_count=(
                            SELECT COUNT(DISTINCT source_name)
                            FROM mentions
                            WHERE event_fingerprint=%s
                        )
                    WHERE id=%s
                    """,
                    (
                        mention.fetched_at,
                        risk_level,
                        1 if mention.is_new else 0,
                        mention.event_fingerprint,
                        existing["id"],
                    ),
                )
                return int(existing["id"])
            row = conn.execute(
                """
                INSERT INTO incident_groups(
                    fingerprint, primary_topic, first_seen_at, last_seen_at, risk_level_max,
                    mention_count, source_count, representative_mention_id, status, notes
                )
                VALUES (%s, %s, %s, %s, %s, 1, 1, %s, 'active', NULL)
                RETURNING id
                """,
                (
                    mention.event_fingerprint,
                    mention.title[:300],
                    mention.first_seen_at,
                    mention.fetched_at,
                    analysis.risk_level,
                    mention.id,
                ),
            ).fetchone()
            return int(row["id"])

    def alert_exists(self, dedupe_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM alerts WHERE dedupe_key=%s", (dedupe_key,)).fetchone()
        return row is not None

    def insert_alert(
        self,
        mention_id: int,
        incident_group_id: Optional[int],
        risk_level: str,
        alert_reason: str,
        dedupe_key: str,
        confidence: float,
        evidence_check_passed: bool,
        needs_human_review: bool,
        delivery_latency_seconds: Optional[int],
        sent_to: Optional[str],
        sent_at: Optional[datetime],
    ) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO alerts(
                    mention_id, incident_group_id, risk_level, alert_reason, dedupe_key,
                    confidence, evidence_check_passed, needs_human_review,
                    delivery_latency_seconds, sent_to, sent_at, ack_status, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (
                    mention_id,
                    incident_group_id,
                    risk_level,
                    alert_reason,
                    dedupe_key,
                    confidence,
                    evidence_check_passed,
                    needs_human_review,
                    delivery_latency_seconds,
                    sent_to,
                    sent_at,
                    utcnow(),
                ),
            ).fetchone()
            return int(row["id"])

    def insert_alert_with_outbox(
        self,
        *,
        mention_id: int,
        incident_group_id: Optional[int],
        risk_level: str,
        alert_reason: str,
        dedupe_key: str,
        confidence: float,
        evidence_check_passed: bool,
        needs_human_review: bool,
        delivery_latency_seconds: Optional[int],
        payload: Dict[str, Any],
    ) -> int:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO alerts(
                    mention_id, incident_group_id, risk_level, alert_reason, dedupe_key,
                    confidence, evidence_check_passed, needs_human_review,
                    delivery_latency_seconds, sent_to, sent_at, ack_status, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, 'pending', %s)
                RETURNING id
                """,
                (
                    mention_id, incident_group_id, risk_level, alert_reason, dedupe_key,
                    confidence, evidence_check_passed, needs_human_review,
                    delivery_latency_seconds, now,
                ),
            ).fetchone()
            alert_id = int(row["id"])
            conn.execute(
                """
                INSERT INTO delivery_outbox(
                    kind, dedupe_key, entity_type, entity_id, payload_json,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                )
                VALUES ('red_alert', %s, 'alert', %s, %s, 'pending', 0, %s, %s, %s)
                """,
                (dedupe_key, alert_id, json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            return alert_id

    def enqueue_delivery(
        self, *, kind: str, dedupe_key: str, entity_type: str,
        entity_id: int, payload: Dict[str, Any],
    ) -> bool:
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO delivery_outbox(
                    kind, dedupe_key, entity_type, entity_id, payload_json,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, 'pending', 0, %s, %s, %s)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    kind, dedupe_key, entity_type, entity_id,
                    json.dumps(payload, ensure_ascii=False), now, now, now,
                ),
            )
            return cur.rowcount > 0

    def list_due_deliveries(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE status='pending' AND next_attempt_at <= %s
                ORDER BY CASE kind WHEN 'red_alert' THEN 0 ELSE 1 END, id LIMIT %s
                """,
                (utcnow(), limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw = item.pop("payload_json")
            item["payload"] = json.loads(raw) if isinstance(raw, str) else raw
            result.append(item)
        return result

    def mark_delivery_sent(self, job_id: int, sent_to: str, sent_at: datetime) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT entity_type, entity_id, payload_json FROM delivery_outbox WHERE id=%s",
                (job_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE delivery_outbox
                SET status='sent', delivered_at=%s, last_error=NULL, updated_at=%s
                WHERE id=%s
                """,
                (sent_at, sent_at, job_id),
            )
            if row and row["entity_type"] == "alert":
                conn.execute(
                    "UPDATE alerts SET sent_to=%s, sent_at=%s WHERE id=%s",
                    (sent_to, sent_at, row["entity_id"]),
                )
                conn.execute(
                    """
                    UPDATE recall_mentions
                    SET sync_status='synced', synced_at=%s, updated_at=%s
                    WHERE mention_id=(SELECT mention_id FROM alerts WHERE id=%s)
                    """,
                    (sent_at, sent_at, row["entity_id"]),
                )
            if row and row["entity_type"] == "recall_mention":
                conn.execute(
                    "UPDATE recall_mentions SET sync_status='synced', synced_at=%s, updated_at=%s WHERE id=%s",
                    (sent_at, sent_at, row["entity_id"]),
                )
            if row and row["entity_type"] == "recall_batch":
                raw_payload = row["payload_json"]
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                record_ids = [
                    int(record_id)
                    for record_id in payload.get("_recall_record_ids", [])
                    if str(record_id).isdigit()
                ]
                if record_ids:
                    conn.execute(
                        """
                        UPDATE recall_mentions
                        SET sync_status='synced', synced_at=%s, updated_at=%s
                        WHERE id = ANY(%s)
                        """,
                        (sent_at, sent_at, record_ids),
                    )
            if row and row["entity_type"] == "mention_batch":
                raw_payload = row["payload_json"]
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                mention_ids = [int(value) for value in payload.get("_mention_ids", []) if str(value).isdigit()]
                if mention_ids:
                    conn.execute(
                        "UPDATE mention_notifications SET delivery_status='sent', delivered_at=%s, updated_at=%s WHERE mention_id = ANY(%s)",
                        (sent_at, sent_at, mention_ids),
                    )
                    conn.execute(
                        "UPDATE recall_mentions SET sync_status='synced', synced_at=%s, updated_at=%s WHERE mention_id = ANY(%s)",
                        (sent_at, sent_at, mention_ids),
                    )
                    conn.execute(
                        "UPDATE alerts SET sent_to=%s, sent_at=%s WHERE mention_id = ANY(%s) AND sent_at IS NULL",
                        (sent_to, sent_at, mention_ids),
                    )

    def supersede_pending_recall_updates(self) -> int:
        """Retire legacy one-item jobs without marking their registry rows synced."""
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE delivery_outbox
                SET status='superseded', updated_at=%s
                WHERE status='pending' AND kind='recall_update'
                """,
                (utcnow(),),
            )
            return cur.rowcount

    def has_pending_delivery(self, kind: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM delivery_outbox WHERE status='pending' AND kind=%s LIMIT 1",
                (kind,),
            ).fetchone()
        return row is not None

    def mark_delivery_failed(self, job_id: int, error: str, next_attempt_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE delivery_outbox
                SET attempt_count=attempt_count+1, last_error=%s,
                    next_attempt_at=%s, updated_at=%s
                WHERE id=%s
                """,
                (error[:1000], next_attempt_at, utcnow(), job_id),
            )

    def upsert_recall_mention(
        self,
        mention_id: int,
        *,
        origin: str = "external",
        sync_status: str = "pending",
        group_synced_at: Optional[datetime] = None,
    ) -> int:
        now = utcnow()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO recall_mentions(
                    campaign_key, mention_id, origin, sync_status, group_synced_at,
                    first_discovered_at, last_seen_at, created_at, updated_at
                )
                VALUES ('ocoopa-cpsc-26-659', %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(mention_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    origin=CASE WHEN recall_mentions.origin='group_import'
                                THEN recall_mentions.origin ELSE excluded.origin END,
                    sync_status=CASE WHEN recall_mentions.sync_status='synced'
                                     THEN recall_mentions.sync_status ELSE excluded.sync_status END,
                    group_synced_at=COALESCE(recall_mentions.group_synced_at, excluded.group_synced_at),
                    updated_at=excluded.updated_at
                RETURNING id
                """,
                (mention_id, origin, sync_status, group_synced_at, now, now, now, now),
            ).fetchone()
            return int(row["id"])

    def list_recall_mentions(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.id AS recall_record_id, r.campaign_key, r.origin, r.sync_status,
                       r.first_discovered_at, r.last_seen_at, r.group_synced_at, r.synced_at,
                       m.id AS mention_id, m.source_type, m.source_name, m.source_url,
                       m.title, m.author_or_publisher, m.published_at, m.fetched_at,
                       m.text_excerpt, m.matched_keywords, m.backfill, m.event_fingerprint,
                       a.sentiment, a.risk_level, a.category, a.summary_zh,
                       a.confidence, a.evidence_check_passed, a.needs_human_review,
                       EXISTS(
                           SELECT 1 FROM alerts alert WHERE alert.mention_id=m.id
                       ) AS has_alert,
                       EXISTS(
                           SELECT 1 FROM mention_notifications n WHERE n.mention_id=m.id
                       ) AS has_mention_notification
                FROM recall_mentions r
                JOIN mentions m ON m.id=r.mention_id
                LEFT JOIN analysis_results a ON a.id=(
                    SELECT id FROM analysis_results
                    WHERE mention_id=m.id ORDER BY id DESC LIMIT 1
                )
                ORDER BY COALESCE(m.published_at, m.fetched_at) DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- M2 human feedback loop (review CLI) ---
    def is_incident_suppressed(self, event_fingerprint: str, now: Optional[datetime] = None) -> bool:
        now = now or utcnow()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status, muted_until FROM incident_groups WHERE fingerprint=%s",
                (event_fingerprint,),
            ).fetchone()
        return _incident_suppressed(row["status"], row["muted_until"], now) if row else False

    def review_incident(self, event_fingerprint: str, status: str, muted_until: Optional[datetime]) -> bool:
        incident_status, review_status = _review_to_status(status)
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE incident_groups SET status=%s, muted_until=%s WHERE fingerprint=%s",
                (incident_status, muted_until, event_fingerprint),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "UPDATE analysis_results SET review_status=%s "
                "WHERE mention_id IN (SELECT id FROM mentions WHERE event_fingerprint=%s)",
                (review_status, event_fingerprint),
            )
        return True

    def get_fingerprint_by_alert(self, alert_id: int) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT m.event_fingerprint AS fp FROM alerts a "
                "JOIN mentions m ON m.id=a.mention_id WHERE a.id=%s",
                (alert_id,),
            ).fetchone()
        return row["fp"] if row else None

    def list_recent_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, a.risk_level, a.needs_human_review, a.ack_status,
                       a.sent_at, m.event_fingerprint, m.title, m.source_url,
                       g.status AS incident_status, g.muted_until
                FROM alerts a
                JOIN mentions m ON m.id=a.mention_id
                LEFT JOIN incident_groups g ON g.fingerprint=m.event_fingerprint
                ORDER BY a.id DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fetch_alerts_between(self, start_at: datetime, end_at: datetime) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, a.risk_level, a.needs_human_review, a.ack_status,
                       a.sent_at, a.created_at, m.event_fingerprint, m.title, m.source_url,
                       g.status AS incident_status, g.muted_until
                FROM alerts a
                JOIN mentions m ON m.id=a.mention_id
                LEFT JOIN incident_groups g ON g.fingerprint=m.event_fingerprint
                WHERE a.created_at >= %s AND a.created_at < %s
                ORDER BY a.created_at DESC, a.id DESC
                """,
                (start_at, end_at),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Tier 2 cross-source alert cooldown ---
    def get_topic_alert(self, topic_key: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT last_alert_at, alerted_source_types, risk_level_max FROM alert_topics WHERE topic_key=%s",
                (topic_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "last_alert_at": row["last_alert_at"],
            "alerted_source_types": set(json.loads(row["alerted_source_types"] or "[]")),
            "risk_level_max": row["risk_level_max"],
        }

    def record_topic_alert(self, topic_key: str, source_type: str, risk_level: str, now: datetime) -> None:
        existing = self.get_topic_alert(topic_key)
        types = existing["alerted_source_types"] if existing else set()
        types.add(source_type)
        rmax = max_risk(existing["risk_level_max"], risk_level) if existing else risk_level
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_topics(topic_key, last_alert_at, alerted_source_types, risk_level_max, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(topic_key) DO UPDATE SET
                    last_alert_at=excluded.last_alert_at,
                    alerted_source_types=excluded.alerted_source_types,
                    risk_level_max=excluded.risk_level_max,
                    updated_at=excluded.updated_at
                """,
                (topic_key, now, json.dumps(sorted(types)), rmax, utcnow()),
            )

    # --- Tier 2 red-alert ack / unacked escalation ---
    def ack_alert(self, alert_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE alerts SET ack_status='acked' WHERE id=%s AND ack_status<>'acked'",
                (alert_id,),
            )
            return cur.rowcount > 0

    def mark_alert_escalated(self, alert_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE alerts SET ack_status='escalated' WHERE id=%s", (alert_id,))

    def pending_red_alerts_older_than(self, cutoff: datetime) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id AS alert_id, a.sent_at, m.title, m.source_url
                FROM alerts a JOIN mentions m ON m.id=a.mention_id
                WHERE a.risk_level='red' AND a.ack_status='pending' AND a.sent_at IS NOT NULL
                      AND a.sent_at < %s
                ORDER BY a.id
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_interaction_snapshot(self, mention: Mention) -> Optional[Dict[str, Any]]:
        if mention.id is None or all(
            value is None
            for value in (mention.view_count, mention.like_count, mention.comment_count, mention.share_count)
        ):
            return None
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT view_count, like_count, comment_count, share_count, captured_at "
                "FROM mention_metrics WHERE mention_id=%s ORDER BY id DESC LIMIT 1",
                (mention.id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO mention_metrics(mention_id, view_count, like_count, comment_count, share_count, captured_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (mention.id, mention.view_count, mention.like_count, mention.comment_count, mention.share_count, mention.fetched_at),
            )
        return dict(previous) if previous else None

    def ensure_mention_action(self, mention_id: int, guidance: Dict[str, str]) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mention_actions(
                    mention_id, intervention_reason, draft_original, draft_zh,
                    legal_risk_note, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(mention_id) DO NOTHING
                """,
                (mention_id, guidance.get("intervention_reason", ""), guidance.get("draft_original", ""),
                 guidance.get("draft_zh", ""), guidance.get("legal_risk_note", ""), now, now),
            )

    def update_mention_action(
        self, mention_id: int, status: str, response_url: str, internal_note: str, operator: str
    ) -> bool:
        if status not in MENTION_ACTION_STATUSES:
            raise ValueError(f"invalid mention action status: {status}")
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE mention_actions SET status=%s, response_url=%s, internal_note=%s, "
                "operator=%s, acted_at=%s, updated_at=%s WHERE mention_id=%s",
                (status, response_url[:2000], internal_note[:4000], operator[:200], now, now, mention_id),
            )
            return cur.rowcount > 0

    def enqueue_mention_batch(
        self, mention_ids: List[int], priority: str, payload: Dict[str, Any]
    ) -> bool:
        ids = sorted(set(int(value) for value in mention_ids))
        if not ids:
            return False
        now = utcnow()
        provisional_key = "mentions:first:" + ",".join(str(value) for value in ids)
        with self.connect() as conn:
            new_ids = []
            for mention_id in ids:
                cur = conn.execute(
                    """
                    INSERT INTO mention_notifications(
                        mention_id, notification_priority, delivery_status,
                        outbox_dedupe_key, created_at, updated_at
                    ) VALUES (%s, %s, 'pending', %s, %s, %s)
                    ON CONFLICT(mention_id) DO NOTHING
                    """,
                    (mention_id, priority, provisional_key, now, now),
                )
                if cur.rowcount:
                    new_ids.append(mention_id)
            if not new_ids:
                return False
            batch_key = "mentions:first:" + ",".join(str(value) for value in new_ids)
            public_payload = dict(payload)
            public_payload["_mention_ids"] = new_ids
            conn.execute(
                """
                INSERT INTO delivery_outbox(
                    kind, dedupe_key, entity_type, entity_id, payload_json,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                ) VALUES ('mention_batch', %s, 'mention_batch', %s, %s, 'pending', 0, %s, %s, %s)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (batch_key, new_ids[0], json.dumps(public_payload, ensure_ascii=False), now, now, now),
            )
            conn.execute(
                "UPDATE mention_notifications SET outbox_dedupe_key=%s WHERE mention_id = ANY(%s)",
                (batch_key, new_ids),
            )
        return True

    def list_recent_incidents_detailed(self, limit: int = 50) -> List[Dict[str, Any]]:
        incidents = self.list_recent_incidents(limit)
        with self.connect() as conn:
            for incident in incidents:
                rows = conn.execute(
                    """
                    SELECT m.*, a.summary_zh, a.risk_level, a.campaign, a.relevance,
                           a.novelty_type, a.notification_priority, a.recommended_action,
                           x.status AS response_status, x.response_url, x.internal_note,
                           x.operator, x.intervention_reason, x.draft_original, x.draft_zh,
                           x.legal_risk_note, x.acted_at
                    FROM mentions m
                    LEFT JOIN analysis_results a ON a.id=(SELECT id FROM analysis_results WHERE mention_id=m.id ORDER BY id DESC LIMIT 1)
                    LEFT JOIN mention_actions x ON x.mention_id=m.id
                    WHERE m.event_fingerprint=%s ORDER BY COALESCE(m.published_at, m.fetched_at) DESC
                    """,
                    (incident["fingerprint"],),
                ).fetchall()
                incident["mentions"] = [dict(row) for row in rows]
        return incidents

    # --- Incident-level review (red/yellow events, even if never alerted) ---
    def list_recent_incidents(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT g.id AS incident_id, g.fingerprint, g.primary_topic, g.risk_level_max,
                       g.status, g.muted_until, g.mention_count, g.source_count, g.last_seen_at,
                       m.title, m.source_url,
                       a.needs_human_review, a.summary_zh, a.evidence_check_passed
                FROM incident_groups g
                LEFT JOIN mentions m ON m.id = g.representative_mention_id
                LEFT JOIN analysis_results a ON a.id = (
                    SELECT id FROM analysis_results
                    WHERE mention_id = g.representative_mention_id ORDER BY id DESC LIMIT 1
                )
                WHERE g.risk_level_max IN ('red', 'yellow')
                ORDER BY g.last_seen_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_fingerprint_by_incident(self, incident_id: int) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM incident_groups WHERE id=%s", (incident_id,)
            ).fetchone()
        return row["fingerprint"] if row else None

    def ack_alerts_by_fingerprint(self, event_fingerprint: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE alerts SET ack_status='acked' WHERE ack_status<>'acked' "
                "AND mention_id IN (SELECT id FROM mentions WHERE event_fingerprint=%s)",
                (event_fingerprint,),
            )

    def fetch_mentions_between(self, start_at: datetime, end_at: datetime) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT m.*, a.sentiment, a.risk_level, a.category, a.summary_zh,
                       a.escalation_reason, a.confidence, a.evidence_check_passed,
                       a.needs_human_review, a.campaign, a.relevance, a.novelty_type,
                       a.notification_priority, a.recommended_action,
                       x.status AS response_status, x.acted_at,
                       n.delivery_status AS notification_status,
                       n.created_at AS notification_created_at,
                       n.delivered_at AS notification_delivered_at
                FROM mentions m
                LEFT JOIN analysis_results a ON a.id=(
                    SELECT id FROM analysis_results
                    WHERE mention_id=m.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN mention_actions x ON x.mention_id=m.id
                LEFT JOIN mention_notifications n ON n.mention_id=m.id
                WHERE m.first_seen_at >= %s AND m.first_seen_at < %s
                ORDER BY
                    CASE a.risk_level WHEN 'red' THEN 1 WHEN 'yellow' THEN 2 ELSE 3 END,
                    m.first_seen_at DESC
                """,
                (start_at, end_at),
            ).fetchall()

    def fetch_mentions_for_day(self, date_prefix: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT m.*, a.sentiment, a.risk_level, a.category, a.summary_zh,
                       a.escalation_reason, a.confidence, a.evidence_check_passed,
                       a.needs_human_review, a.campaign, a.relevance, a.novelty_type,
                       a.notification_priority, a.recommended_action,
                       x.status AS response_status, x.acted_at,
                       n.delivery_status AS notification_status,
                       n.created_at AS notification_created_at,
                       n.delivered_at AS notification_delivered_at
                FROM mentions m
                LEFT JOIN analysis_results a ON a.id=(
                    SELECT id FROM analysis_results
                    WHERE mention_id=m.id ORDER BY id DESC LIMIT 1
                )
                LEFT JOIN mention_actions x ON x.mention_id=m.id
                LEFT JOIN mention_notifications n ON n.mention_id=m.id
                WHERE m.first_seen_at >= %s::date AND m.first_seen_at < (%s::date + INTERVAL '1 day')
                ORDER BY
                    CASE a.risk_level WHEN 'red' THEN 1 WHEN 'yellow' THEN 2 ELSE 3 END,
                    m.first_seen_at DESC
                """,
                (date_prefix, date_prefix),
            ).fetchall()

    def insert_daily_report(self, report: Dict[str, Any]) -> int:
        _, _, Json = self._pg_modules()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO daily_reports(
                    report_date, timezone, total_mentions, new_mentions, backfill_mentions,
                    sentiment_distribution, source_distribution, risk_distribution,
                    top_risks, trend_vs_yesterday, recommended_actions,
                    generated_text_zh, delivery_status, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(report_date, timezone) DO UPDATE SET
                    total_mentions=excluded.total_mentions,
                    new_mentions=excluded.new_mentions,
                    backfill_mentions=excluded.backfill_mentions,
                    sentiment_distribution=excluded.sentiment_distribution,
                    source_distribution=excluded.source_distribution,
                    risk_distribution=excluded.risk_distribution,
                    top_risks=excluded.top_risks,
                    trend_vs_yesterday=excluded.trend_vs_yesterday,
                    recommended_actions=excluded.recommended_actions,
                    generated_text_zh=excluded.generated_text_zh,
                    delivery_status=excluded.delivery_status,
                    created_at=excluded.created_at
                RETURNING id
                """,
                (
                    report["report_date"],
                    report["timezone"],
                    report["total_mentions"],
                    report["new_mentions"],
                    report["backfill_mentions"],
                    Json(report["sentiment_distribution"]),
                    Json(report["source_distribution"]),
                    Json(report["risk_distribution"]),
                    Json(report["top_risks"]),
                    Json(report["trend_vs_yesterday"]),
                    Json(report["recommended_actions"]),
                    report["generated_text_zh"],
                    report["delivery_status"],
                    utcnow(),
                ),
            ).fetchone()
            return int(row["id"])

    @staticmethod
    def _pg_modules():
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError("Postgres runtime requires psycopg. Install package with postgres support.") from exc
        return psycopg, dict_row, Jsonb


def _postgres_migration_sqls() -> List[str]:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    return [path.read_text(encoding="utf-8") for path in sorted(migrations_dir.glob("*.sql"))]


def max_risk(left: str, right: str) -> str:
    order = {"green": 0, "yellow": 1, "red": 2}
    return left if order.get(left, 0) >= order.get(right, 0) else right


REVIEW_STATUSES = ("confirmed", "false_positive", "muted")
MENTION_ACTION_STATUSES = ("待判断", "建议回应", "已回应", "无需回应", "升级 PR/法务")


def _review_to_status(status: str):
    mapping = {
        "confirmed": ("monitoring", "confirmed"),
        "false_positive": ("resolved", "false_positive"),
        "muted": ("muted", "muted"),
    }
    if status not in mapping:
        raise ValueError(f"invalid review status: {status} (expected one of {REVIEW_STATUSES})")
    return mapping[status]


def _incident_suppressed(status, muted_until, now) -> bool:
    """A confirmed false positive (resolved) is suppressed forever; a muted
    incident is suppressed until muted_until (None = indefinitely)."""
    if status == "resolved":
        return True
    if status == "muted":
        return muted_until is None or muted_until > now
    return False


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_topics (
    topic_key TEXT PRIMARY KEY,
    last_alert_at TEXT,
    alerted_source_types TEXT NOT NULL DEFAULT '[]',
    risk_level_max TEXT NOT NULL DEFAULT 'green',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_events (
    event_key TEXT PRIMARY KEY,
    first_alerted_at TEXT NOT NULL,
    last_alerted_at TEXT NOT NULL,
    alert_count INTEGER NOT NULL DEFAULT 1,
    seen_source_types TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    lane TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    priority TEXT NOT NULL,
    lane TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    alert_threshold_minutes INTEGER NOT NULL DEFAULT 120,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id INTEGER PRIMARY KEY REFERENCES source_configs(id) ON DELETE CASCADE,
    source_name TEXT NOT NULL,
    priority TEXT NOT NULL,
    lane TEXT NOT NULL,
    last_success_at TEXT,
    last_attempt_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    health_status TEXT NOT NULL DEFAULT 'unknown',
    alert_threshold_minutes INTEGER NOT NULL DEFAULT 120
);

CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author_or_publisher TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    language TEXT,
    country_or_market TEXT,
    raw_text TEXT NOT NULL,
    text_excerpt TEXT,
    matched_keywords TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    event_fingerprint TEXT NOT NULL,
    duplicate_group_id TEXT,
    is_new INTEGER NOT NULL DEFAULT 1,
    is_updated INTEGER NOT NULL DEFAULT 0,
    backfill INTEGER NOT NULL DEFAULT 0,
    tos_method TEXT NOT NULL,
    fetch_status TEXT NOT NULL DEFAULT 'ok',
    fetch_error TEXT,
    platform TEXT NOT NULL DEFAULT 'web',
    content_type TEXT NOT NULL DEFAULT 'article',
    provider TEXT NOT NULL DEFAULT '',
    provider_item_id TEXT NOT NULL DEFAULT '',
    parent_url TEXT NOT NULL DEFAULT '',
    discovery_method TEXT NOT NULL DEFAULT 'public_feed',
    coverage_tier TEXT NOT NULL DEFAULT 'public_index',
    provider_added_at TEXT,
    discovery_latency_seconds INTEGER,
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mentions_event_fingerprint ON mentions(event_fingerprint);
CREATE INDEX IF NOT EXISTS idx_mentions_backfill ON mentions(backfill);
CREATE INDEX IF NOT EXISTS idx_mentions_fetched_at ON mentions(fetched_at);
CREATE INDEX IF NOT EXISTS idx_mentions_first_seen_at ON mentions(first_seen_at);

CREATE TABLE IF NOT EXISTS analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mention_id INTEGER NOT NULL REFERENCES mentions(id) ON DELETE CASCADE,
    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    category TEXT NOT NULL,
    summary_zh TEXT NOT NULL,
    key_quotes TEXT NOT NULL,
    key_quote_offsets TEXT NOT NULL,
    requires_escalation INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    evidence_check_passed INTEGER NOT NULL DEFAULT 0,
    evidence_check_notes TEXT,
    needs_human_review INTEGER NOT NULL DEFAULT 0,
    analysis_created_at TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed'
    ,campaign TEXT NOT NULL DEFAULT 'brand_major_risk'
    ,relevance REAL NOT NULL DEFAULT 0
    ,novelty_type TEXT NOT NULL DEFAULT 'new_mention'
    ,notification_priority TEXT NOT NULL DEFAULT 'standard'
    ,recommended_action TEXT NOT NULL DEFAULT 'monitor'
);

CREATE INDEX IF NOT EXISTS idx_analysis_mention_id ON analysis_results(mention_id);
CREATE INDEX IF NOT EXISTS idx_analysis_risk_level ON analysis_results(risk_level);

CREATE TABLE IF NOT EXISTS incident_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    primary_topic TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    risk_level_max TEXT NOT NULL,
    mention_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    representative_mention_id INTEGER REFERENCES mentions(id),
    status TEXT NOT NULL DEFAULT 'active',
    muted_until TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mention_id INTEGER NOT NULL REFERENCES mentions(id) ON DELETE CASCADE,
    incident_group_id INTEGER REFERENCES incident_groups(id),
    risk_level TEXT NOT NULL,
    alert_reason TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    confidence REAL NOT NULL,
    evidence_check_passed INTEGER NOT NULL,
    needs_human_review INTEGER NOT NULL,
    delivery_latency_seconds INTEGER,
    sent_to TEXT,
    sent_at TEXT,
    ack_status TEXT NOT NULL DEFAULT 'pending',
    muted_until TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_key TEXT NOT NULL,
    mention_id INTEGER NOT NULL UNIQUE REFERENCES mentions(id) ON DELETE CASCADE,
    origin TEXT NOT NULL DEFAULT 'external',
    sync_status TEXT NOT NULL DEFAULT 'pending',
    first_discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    group_synced_at TEXT,
    synced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recall_mentions_sync_status ON recall_mentions(sync_status);

CREATE TABLE IF NOT EXISTS delivery_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delivery_outbox_due
ON delivery_outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS mention_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mention_id INTEGER NOT NULL REFERENCES mentions(id) ON DELETE CASCADE,
    view_count INTEGER,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mention_metrics_mention_time
ON mention_metrics(mention_id, captured_at);

CREATE TABLE IF NOT EXISTS mention_actions (
    mention_id INTEGER PRIMARY KEY REFERENCES mentions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT '待判断',
    response_url TEXT NOT NULL DEFAULT '',
    internal_note TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    intervention_reason TEXT NOT NULL DEFAULT '',
    draft_original TEXT NOT NULL DEFAULT '',
    draft_zh TEXT NOT NULL DEFAULT '',
    legal_risk_note TEXT NOT NULL DEFAULT '',
    acted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mention_notifications (
    mention_id INTEGER PRIMARY KEY REFERENCES mentions(id) ON DELETE CASCADE,
    notification_priority TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    outbox_dedupe_key TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    total_mentions INTEGER NOT NULL,
    new_mentions INTEGER NOT NULL,
    backfill_mentions INTEGER NOT NULL,
    sentiment_distribution TEXT NOT NULL,
    source_distribution TEXT NOT NULL,
    risk_distribution TEXT NOT NULL,
    top_risks TEXT NOT NULL,
    trend_vs_yesterday TEXT NOT NULL,
    recommended_actions TEXT NOT NULL,
    generated_text_zh TEXT NOT NULL,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    UNIQUE(report_date, timezone)
);
"""
