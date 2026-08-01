from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Keyword:
    term: str
    category: str
    lane: str
    active: bool = True


@dataclass
class SourceConfig:
    source_name: str
    source_type: str
    priority: str
    lane: str
    method: str
    url: str
    active: bool = True
    alert_threshold_minutes: int = 120
    id: Optional[int] = None


@dataclass
class RawItem:
    source_type: str
    source_name: str
    source_url: str
    title: str
    raw_text: str
    published_at: Optional[datetime] = None
    author_or_publisher: Optional[str] = None
    language: Optional[str] = None
    country_or_market: Optional[str] = None
    tos_method: str = "rss"
    platform: str = "web"
    content_type: str = "article"
    provider: str = ""
    provider_item_id: str = ""
    parent_url: str = ""
    discovery_method: str = "public_feed"
    coverage_tier: str = "public_index"
    provider_added_at: Optional[datetime] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None


@dataclass
class Mention:
    source_type: str
    source_name: str
    source_url: str
    canonical_url: str
    title: str
    raw_text: str
    fetched_at: datetime
    first_seen_at: datetime
    matched_keywords: List[str]
    content_hash: str
    event_fingerprint: str
    author_or_publisher: Optional[str] = None
    published_at: Optional[datetime] = None
    language: Optional[str] = None
    country_or_market: Optional[str] = None
    text_excerpt: str = ""
    duplicate_group_id: Optional[str] = None
    is_new: bool = True
    is_updated: bool = False
    backfill: bool = False
    tos_method: str = "rss"
    fetch_status: str = "ok"
    fetch_error: Optional[str] = None
    platform: str = "web"
    content_type: str = "article"
    provider: str = ""
    provider_item_id: str = ""
    parent_url: str = ""
    discovery_method: str = "public_feed"
    coverage_tier: str = "public_index"
    provider_added_at: Optional[datetime] = None
    discovery_latency_seconds: Optional[int] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None
    id: Optional[int] = None


@dataclass
class AnalysisResult:
    mention_id: int
    model_provider: str
    model_name: str
    prompt_version: str
    sentiment: str
    risk_level: str
    category: str
    summary_zh: str
    key_quotes: List[str]
    key_quote_offsets: List[Dict[str, int]]
    requires_escalation: bool
    escalation_reason: str
    confidence: float
    evidence_check_passed: bool
    evidence_check_notes: str
    needs_human_review: bool
    campaign: str = "brand_major_risk"
    relevance: float = 0.0
    novelty_type: str = "new_mention"
    notification_priority: str = "standard"
    recommended_action: str = "monitor"
    analysis_created_at: datetime = field(default_factory=utcnow)
    review_status: str = "unreviewed"
    id: Optional[int] = None


@dataclass
class Alert:
    mention_id: int
    incident_group_id: Optional[int]
    risk_level: str
    alert_reason: str
    dedupe_key: str
    confidence: float
    evidence_check_passed: bool
    needs_human_review: bool
    sent_to: Optional[str] = None
    sent_at: Optional[datetime] = None
    ack_status: str = "pending"
    muted_until: Optional[datetime] = None
    id: Optional[int] = None


@dataclass
class DailyReport:
    report_date: str
    timezone: str
    total_mentions: int
    new_mentions: int
    backfill_mentions: int
    sentiment_distribution: Dict[str, int]
    source_distribution: Dict[str, int]
    risk_distribution: Dict[str, int]
    top_risks: List[Dict[str, Any]]
    trend_vs_yesterday: Dict[str, Any]
    recommended_actions: List[str]
    generated_text_zh: str
    delivery_status: str = "pending"
    id: Optional[int] = None
