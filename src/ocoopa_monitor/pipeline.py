from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .analysis import AnalysisService
from .config import Settings
from .db import Database
from .delivery import DeliveryClient
from .fetchers import (
    BraveSearchFetcher,
    CPSCRecallFetcher,
    Fetcher,
    GenericRSSFetcher,
    GNewsFetcher,
    GoogleNewsRSSFetcher,
    SerpAPIFetcher,
)
from .llm import RuleOnlyProvider, provider_from_settings
from .models import Mention, RawItem, SourceConfig, utcnow
from .normalize import canonicalize_url, content_hash, event_fingerprint, excerpt, find_keywords, normalize_text


class MonitorPipeline:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        analysis_service: Optional[AnalysisService] = None,
        delivery_client: Optional[DeliveryClient] = None,
        fetchers: Optional[Dict[str, Fetcher]] = None,
    ):
        self.db = db
        self.settings = settings
        self.analysis_service = analysis_service or self._analysis_service_from_settings(settings)
        self.delivery_client = delivery_client or DeliveryClient.from_settings(settings)
        self.fetchers = fetchers or {
            "rss": GoogleNewsRSSFetcher(settings.request_timeout_seconds),
            "api": CPSCRecallFetcher(settings.request_timeout_seconds),
            "generic_rss": GenericRSSFetcher(settings.request_timeout_seconds),
            "serpapi": SerpAPIFetcher(settings.serpapi_api_key, settings.request_timeout_seconds),
            "brave_search": BraveSearchFetcher(settings.brave_search_api_key, settings.request_timeout_seconds),
            "gnews": GNewsFetcher(settings.gnews_api_key, settings.request_timeout_seconds),
        }

    def run_lane(
        self,
        lane: str,
        backfill: bool = False,
        since_days: Optional[int] = None,
    ) -> Dict[str, int]:
        since = None
        if since_days is not None:
            since = utcnow() - timedelta(days=since_days)
        keywords = self.db.get_keywords(lane)
        keyword_terms = [keyword.term for keyword in keywords]
        stats = {
            "sources_attempted": 0,
            "sources_failed": 0,
            "items_fetched": 0,
            "items_filtered_since": 0,
            "items_filtered_no_keywords": 0,
            "items_matched": 0,
            "items_duplicate_skipped": 0,
            "mentions_processed": 0,
            "alerts_created": 0,
        }
        for source in self.db.get_sources(lane):
            stats["sources_attempted"] += 1
            self.db.record_source_attempt(source)
            try:
                fetcher = self._fetcher_for(source)
                raw_items = fetcher.fetch(source, keyword_terms, since=since)
                stats["items_fetched"] += len(raw_items)
                for raw_item in raw_items:
                    query_terms = list(keyword_terms)
                    if raw_item.source_name.startswith(("google_news", "serpapi", "gnews", "brave")):
                        query_terms.extend(self._source_query_terms(raw_item))
                    mention = self._build_mention(raw_item, query_terms, backfill=backfill)
                    if since and mention.published_at and mention.published_at < since:
                        stats["items_filtered_since"] += 1
                        continue
                    if not mention.matched_keywords:
                        stats["items_filtered_no_keywords"] += 1
                        continue
                    stats["items_matched"] += 1
                    stored = self.db.upsert_mention(mention)
                    if not stored.is_new and not stored.is_updated:
                        stats["items_duplicate_skipped"] += 1
                        continue
                    analysis = self.analysis_service.analyze(stored)
                    self.db.insert_analysis(analysis)
                    incident_group_id = self.db.upsert_incident_group(stored, analysis)
                    stats["mentions_processed"] += 1
                    if self._should_alert(stored, analysis, backfill):
                        if self._create_alert(stored, analysis, incident_group_id):
                            stats["alerts_created"] += 1
                self.db.record_source_success(source)
            except Exception as exc:
                stats["sources_failed"] += 1
                self.db.record_source_failure(source, str(exc))
        return stats

    def _fetcher_for(self, source: SourceConfig) -> Fetcher:
        if source.method not in self.fetchers:
            raise ValueError(f"No fetcher configured for method={source.method}")
        return self.fetchers[source.method]

    @staticmethod
    def _analysis_service_from_settings(settings: Settings) -> AnalysisService:
        try:
            provider = provider_from_settings(settings)
        except Exception:
            provider = RuleOnlyProvider()
        return AnalysisService(provider=provider, fallback_provider=RuleOnlyProvider())

    def _build_mention(self, raw_item: RawItem, keyword_terms: Iterable[str], backfill: bool) -> Mention:
        now = utcnow()
        title = normalize_text(raw_item.title)
        raw_text = normalize_text(raw_item.raw_text)
        canonical = canonicalize_url(raw_item.source_url)
        matched = find_keywords(f"{title}\n{raw_text}", keyword_terms)
        if raw_item.source_name.startswith(("brave", "serpapi", "gnews")):
            matched.append("search_api_query_hit")
            matched = sorted(set(matched), key=str.lower)
        digest = content_hash(title, raw_text)
        fingerprint = event_fingerprint(title, raw_text, matched)
        return Mention(
            source_type=raw_item.source_type,
            source_name=raw_item.source_name,
            source_url=raw_item.source_url,
            canonical_url=canonical,
            title=title or canonical,
            author_or_publisher=raw_item.author_or_publisher,
            published_at=_aware(raw_item.published_at),
            fetched_at=now,
            first_seen_at=now,
            language=raw_item.language,
            country_or_market=raw_item.country_or_market,
            raw_text=raw_text or title,
            text_excerpt=excerpt(raw_text or title),
            matched_keywords=matched,
            content_hash=digest,
            event_fingerprint=fingerprint,
            duplicate_group_id=fingerprint,
            backfill=backfill,
            tos_method=raw_item.tos_method,
        )

    @staticmethod
    def _source_query_terms(raw_item: RawItem) -> List[str]:
        text = f"{raw_item.title}\n{raw_item.raw_text}".lower()
        derived = []
        for token in ["ocoopa", "ocopa", "lawsuit", "fire", "death", "recall", "cpsc", "class action"]:
            if token in text:
                derived.append(token)
        if raw_item.source_type in {"search", "news"} and raw_item.source_name.startswith(
            ("brave", "serpapi", "gnews")
        ):
            derived.append("search_api_query_hit")
        return derived

    @staticmethod
    def _should_alert(mention: Mention, analysis, backfill: bool) -> bool:
        if backfill or mention.backfill:
            return False
        return analysis.risk_level == "red" and analysis.requires_escalation

    def _create_alert(self, mention: Mention, analysis, incident_group_id: int) -> bool:
        if mention.id is None:
            return False
        dedupe_key = f"{mention.event_fingerprint}:red"
        if self.db.alert_exists(dedupe_key):
            return False
        sent_at = utcnow()
        delivery_latency_seconds = self._delivery_latency_seconds(mention, sent_at)
        payload = self.delivery_client.alert_payload(
            title=mention.title,
            url=mention.source_url,
            risk_level=analysis.risk_level,
            reason=analysis.escalation_reason,
            confidence=analysis.confidence,
            evidence_check_passed=analysis.evidence_check_passed,
            needs_human_review=analysis.needs_human_review,
            sent_at=sent_at,
            delivery_latency_seconds=delivery_latency_seconds,
        )
        sent_to = self.delivery_client.send_alert(payload)
        self.db.insert_alert(
            mention_id=mention.id,
            incident_group_id=incident_group_id,
            risk_level=analysis.risk_level,
            alert_reason=analysis.escalation_reason,
            dedupe_key=dedupe_key,
            confidence=analysis.confidence,
            evidence_check_passed=analysis.evidence_check_passed,
            needs_human_review=analysis.needs_human_review,
            delivery_latency_seconds=delivery_latency_seconds,
            sent_to=sent_to,
            sent_at=sent_at,
        )
        return True

    @staticmethod
    def _delivery_latency_seconds(mention: Mention, sent_at: datetime) -> Optional[int]:
        start = mention.published_at or mention.first_seen_at
        if not start:
            return None
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return max(0, int((sent_at - start.astimezone(timezone.utc)).total_seconds()))


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
