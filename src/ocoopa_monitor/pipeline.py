from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .analysis import AnalysisService
from .config import Settings
from .db import Database, max_risk
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
from .outbox import DeliveryOutboxWorker
from .recall import is_current_recall, recall_update_payload
from .normalize import (
    canonicalize_url,
    content_hash,
    event_fingerprint,
    excerpt,
    find_keywords,
    normalize_text,
    topic_key,
)


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
        # Real-time alerts only fire once the cold-start backfill is complete.
        # A fresh / un-bootstrapped DB ingests and analyzes silently so the first
        # deploy never produces an alert storm from pre-existing content.
        realtime_enabled = (not backfill) and self.db.is_bootstrapped()
        stats = {
            "sources_attempted": 0,
            "sources_failed": 0,
            "p0_sources_attempted": 0,
            "p0_sources_failed": 0,
            "items_fetched": 0,
            "items_filtered_since": 0,
            "items_filtered_no_keywords": 0,
            "items_matched": 0,
            "items_duplicate_skipped": 0,
            "mentions_processed": 0,
            "alerts_created": 0,
            "alerts_suppressed_pre_bootstrap": 0,
            "alerts_suppressed_muted": 0,
            "alerts_suppressed_cooldown": 0,
            "recall_mentions_registered": 0,
            "recall_updates_queued": 0,
            "deliveries_sent": 0,
            "deliveries_failed": 0,
        }
        for source in self.db.get_sources(lane):
            stats["sources_attempted"] += 1
            if source.priority == "P0":
                stats["p0_sources_attempted"] += 1
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
                    recall_record_id = None
                    if is_current_recall(stored.title, stored.raw_text):
                        recall_record_id = self.db.upsert_recall_mention(stored.id)
                        stats["recall_mentions_registered"] += 1
                    if self._is_red_escalation(analysis) and not realtime_enabled:
                        stats["alerts_suppressed_pre_bootstrap"] += 1
                    alert_created = False
                    if self._should_alert(stored, analysis, backfill, realtime_enabled):
                        if self.db.is_incident_suppressed(stored.event_fingerprint):
                            # Human marked this incident false-positive or muted.
                            stats["alerts_suppressed_muted"] += 1
                        elif not self._topic_allows_alert(stored, analysis):
                            # Same event already paged within the cooldown window
                            # (cross-source de-dup): suppress the duplicate page.
                            stats["alerts_suppressed_cooldown"] += 1
                        elif self._create_alert(stored, analysis, incident_group_id):
                            stats["alerts_created"] += 1
                            alert_created = True
                    if recall_record_id and realtime_enabled and not alert_created:
                        queued = self.db.enqueue_delivery(
                            kind="recall_update",
                            dedupe_key=f"recall:{stored.id}",
                            entity_type="recall_mention",
                            entity_id=recall_record_id,
                            payload=recall_update_payload(stored, analysis),
                        )
                        stats["recall_updates_queued"] += int(queued)
                self.db.record_source_success(source)
            except Exception as exc:
                stats["sources_failed"] += 1
                if source.priority == "P0":
                    stats["p0_sources_failed"] += 1
                self.db.record_source_failure(source, str(exc))
        delivery_stats = DeliveryOutboxWorker(self.db, self.delivery_client).drain()
        stats["deliveries_sent"] = delivery_stats["sent"]
        stats["deliveries_failed"] = delivery_stats["failed"]
        return stats

    def bootstrap(self, since_days: int) -> Dict[str, Dict[str, int]]:
        """Cold-start: silently backfill both lanes, then enable real-time alerts.

        All ingested content is marked backfill=true and produces no alerts;
        once both lanes finish, the bootstrap flag is set so subsequent runs
        alert only on genuinely new post-backfill content.
        """
        stats: Dict[str, Dict[str, int]] = {}
        for lane in ("high", "regular"):
            stats[lane] = self.run_lane(lane, backfill=True, since_days=since_days)
        high = stats["high"]
        if high["p0_sources_attempted"] == 0 or high["p0_sources_failed"] > 0:
            raise RuntimeError(
                "bootstrap incomplete: every high-lane P0 source must complete successfully; "
                f"attempted={high['p0_sources_attempted']} failed={high['p0_sources_failed']}"
            )
        self.db.mark_bootstrapped()
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
    def _is_red_escalation(analysis) -> bool:
        return analysis.risk_level == "red" and analysis.requires_escalation

    @staticmethod
    def _should_alert(mention: Mention, analysis, backfill: bool, realtime_enabled: bool) -> bool:
        if backfill or mention.backfill or not realtime_enabled:
            return False
        return MonitorPipeline._is_red_escalation(analysis)

    def _topic_allows_alert(self, mention: Mention, analysis) -> bool:
        """Cross-source de-dup: one page per event topic per cooldown window.

        Breaks through the cooldown when a new source TYPE first appears on the
        topic (first CPSC / legal / mainstream-media report) or when risk
        escalates — preserving the lifeline 'media follow-up / CPSC' signals.
        """
        topic = topic_key(mention.title, mention.raw_text, mention.matched_keywords)
        state = self.db.get_topic_alert(topic)
        if state is None:
            return True
        last = state["last_alert_at"]
        cooldown = timedelta(hours=self.settings.alert_cooldown_hours)
        if last is None or (utcnow() - _aware(last)) > cooldown:
            return True
        if mention.source_type not in state["alerted_source_types"]:
            return True
        return max_risk(analysis.risk_level, state["risk_level_max"]) != state["risk_level_max"]

    def _create_alert(self, mention: Mention, analysis, incident_group_id: int) -> bool:
        if mention.id is None:
            return False
        dedupe_key = f"{mention.event_fingerprint}:red"
        if self.db.alert_exists(dedupe_key):
            return False
        queued_at = utcnow()
        delivery_latency_seconds = self._delivery_latency_seconds(mention, queued_at)
        payload = self.delivery_client.alert_payload(
            title=mention.title,
            url=mention.source_url,
            risk_level=analysis.risk_level,
            reason=analysis.escalation_reason,
            confidence=analysis.confidence,
            evidence_check_passed=analysis.evidence_check_passed,
            needs_human_review=analysis.needs_human_review,
            sent_at=queued_at,
            delivery_latency_seconds=delivery_latency_seconds,
        )
        self.db.insert_alert_with_outbox(
            mention_id=mention.id,
            incident_group_id=incident_group_id,
            risk_level=analysis.risk_level,
            alert_reason=analysis.escalation_reason,
            dedupe_key=dedupe_key,
            confidence=analysis.confidence,
            evidence_check_passed=analysis.evidence_check_passed,
            needs_human_review=analysis.needs_human_review,
            delivery_latency_seconds=delivery_latency_seconds,
            payload=payload,
        )
        self.db.record_topic_alert(
            topic_key(mention.title, mention.raw_text, mention.matched_keywords),
            mention.source_type,
            analysis.risk_level,
            queued_at,
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
