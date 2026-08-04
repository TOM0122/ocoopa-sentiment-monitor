from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from .analysis import AnalysisService
from .config import Settings
from .db import Database, max_risk
from .delivery import DeliveryClient
from .fetchers import (
    BrandwatchMentionsFetcher,
    BraveSearchFetcher,
    CPSCRecallFetcher,
    Fetcher,
    GenericRSSFetcher,
    GNewsFetcher,
    GoogleNewsRSSFetcher,
    SerpAPIFetcher,
)
from .fetchers.search_api import is_media_outlet_social_source, media_outlet_target_label
from .llm import RuleOnlyProvider, provider_from_settings
from .models import Mention, RawItem, SourceConfig, utcnow
from .outbox import DeliveryOutboxWorker
from .recall import RecallRegistryService, is_current_recall
from .social import (
    assess_public_mention,
    crossed_surge_threshold,
    discovery_latency_seconds,
    enrich_analysis,
    infer_content_type,
    infer_platform,
    response_guidance,
)
from .syndication import assess_syndication
from .public_social_intake import build_manual_public_social_item
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
            "brandwatch": BrandwatchMentionsFetcher(
                token=settings.brandwatch_api_token,
                project_id=settings.brandwatch_project_id,
                query_id=settings.brandwatch_query_id,
                base_url=settings.brandwatch_base_url,
                timeout_seconds=settings.request_timeout_seconds,
            ),
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
        if lane == "regular" and not self.db.get_state("public_social_backfill_completed_at"):
            realtime_enabled = False
        if lane == "licensed" and not self.db.get_state("brandwatch_backfill_completed_at"):
            realtime_enabled = False
        notice_groups: Dict[str, List[Dict[str, object]]] = {}
        stats = {
            "sources_attempted": 0,
            "sources_failed": 0,
            "p0_sources_attempted": 0,
            "p0_sources_failed": 0,
            "items_fetched": 0,
            "items_filtered_since": 0,
            "items_filtered_no_keywords": 0,
            "items_filtered_irrelevant": 0,
            "items_matched": 0,
            "items_duplicate_skipped": 0,
            "mentions_processed": 0,
            "alerts_created": 0,
            "alerts_suppressed_pre_bootstrap": 0,
            "alerts_suppressed_muted": 0,
            "alerts_suppressed_cooldown": 0,
            "recall_mentions_registered": 0,
            "recall_updates_queued": 0,
            "mention_batches_queued": 0,
            "surge_alerts_queued": 0,
            "deliveries_sent": 0,
            "deliveries_failed": 0,
        }
        for source in self.db.get_sources(lane):
            if not self._source_due(source, backfill):
                stats.setdefault("sources_deferred", 0)
                stats["sources_deferred"] += 1
                continue
            source_backfill = backfill or self._source_requires_silent_backfill(source)
            source_realtime_enabled = realtime_enabled and not source_backfill
            stats["sources_attempted"] += 1
            if source.priority == "P0":
                stats["p0_sources_attempted"] += 1
            self.db.record_source_attempt(source)
            try:
                fetcher = self._fetcher_for(source)
                source_stats = {
                    "result_count": 0,
                    "matched_count": 0,
                    "stored_count": 0,
                    "filtered_count": 0,
                    "duplicate_count": 0,
                    "query_label": self._source_query_label(source),
                }
                source_since = since
                if source.method == "brandwatch" and source_since is None:
                    cursor = self.db.get_state(f"source_cursor:{source.source_name}")
                    if cursor:
                        source_since = _aware(datetime.fromisoformat(cursor.replace("Z", "+00:00")))
                        if source_since:
                            source_since -= timedelta(minutes=5)
                raw_items = fetcher.fetch(source, keyword_terms, since=source_since)
                source_stats["result_count"] = len(raw_items)
                stats["items_fetched"] += len(raw_items)
                for raw_item in raw_items:
                    query_terms = list(keyword_terms)
                    if raw_item.source_name.startswith(("google_news", "serpapi", "gnews", "brave")):
                        query_terms.extend(self._source_query_terms(raw_item))
                    mention = self._build_mention(raw_item, query_terms, backfill=source_backfill)
                    if since and mention.published_at and mention.published_at < since:
                        stats["items_filtered_since"] += 1
                        source_stats["filtered_count"] += 1
                        continue
                    if not mention.matched_keywords:
                        stats["items_filtered_no_keywords"] += 1
                        source_stats["filtered_count"] += 1
                        continue
                    assessment = assess_public_mention(mention)
                    if (
                        source.source_type == "social"
                        or (source.source_type == "search" and mention.platform not in {"web", "news"})
                    ) and not assessment.valid:
                        stats["items_filtered_irrelevant"] += 1
                        source_stats["filtered_count"] += 1
                        continue
                    stats["items_matched"] += 1
                    source_stats["matched_count"] += 1
                    stored = self.db.upsert_mention(mention)
                    previous_metrics = self.db.record_interaction_snapshot(stored)
                    surge_reason = (
                        crossed_surge_threshold(
                            previous_metrics,
                            {
                                "view_count": stored.view_count,
                                "like_count": stored.like_count,
                                "comment_count": stored.comment_count,
                                "share_count": stored.share_count,
                            },
                        )
                        if previous_metrics
                        else None
                    )
                    if surge_reason and source_realtime_enabled and stored.id is not None:
                        surge_payload = self.delivery_client.alert_payload(
                            title=stored.title,
                            url=stored.source_url,
                            risk_level="red",
                            reason=f"传播突增：{surge_reason}",
                            confidence=1.0,
                            evidence_check_passed=True,
                            needs_human_review=False,
                            sent_at=utcnow(),
                            delivery_latency_seconds=stored.discovery_latency_seconds,
                            notification_priority="urgent",
                        )
                        if self.db.enqueue_delivery(
                            kind="surge_alert",
                            dedupe_key=f"surge:{stored.id}:{surge_reason}",
                            entity_type="mention",
                            entity_id=stored.id,
                            payload=surge_payload,
                        ):
                            stats["surge_alerts_queued"] += 1
                    if not stored.is_new and not stored.is_updated:
                        stats["items_duplicate_skipped"] += 1
                        source_stats["duplicate_count"] += 1
                        continue
                    source_stats["stored_count"] += 1
                    analysis = enrich_analysis(stored, self.analysis_service.analyze(stored))
                    self.db.insert_analysis(analysis)
                    self.db.ensure_mention_action(stored.id, response_guidance(stored, analysis))
                    incident_group_id = self.db.upsert_incident_group(stored, analysis)
                    stats["mentions_processed"] += 1
                    if is_current_recall(stored.title, stored.raw_text):
                        self.db.upsert_recall_mention(stored.id)
                        stats["recall_mentions_registered"] += 1
                    if self._is_red_escalation(analysis) and not source_realtime_enabled:
                        stats["alerts_suppressed_pre_bootstrap"] += 1
                    # Every effective public mention enters the first-discovery
                    # ledger immediately, including routine recall reposts. The
                    # recall digest reads the same ledger and therefore skips
                    # these rows instead of sending a second notification.
                    batch_candidate = assessment.valid and source_realtime_enabled and stored.is_new
                    incident_suppressed = self.db.is_incident_suppressed(stored.event_fingerprint)
                    if batch_candidate and incident_suppressed:
                        stats["alerts_suppressed_muted"] += 1
                    elif batch_candidate:
                        syndication = assess_syndication(stored, analysis.novelty_type)
                        notification_group = (
                            "recall_26_659"
                            if is_current_recall(stored.title, stored.raw_text)
                            else topic_key(stored.title, stored.raw_text, stored.matched_keywords)
                        )
                        notice_groups.setdefault(notification_group, []).append(
                            {
                                "mention_id": stored.id,
                                "title": stored.title,
                                "source_url": stored.source_url,
                                "platform": stored.platform,
                                "content_type": stored.content_type,
                                "summary_zh": analysis.summary_zh,
                                "campaign": analysis.campaign,
                                "recommended_action": analysis.recommended_action,
                                "notification_priority": analysis.notification_priority,
                                "needs_human_review": analysis.needs_human_review,
                                "story_cluster_key": syndication.cluster_key,
                                "story_cluster_label": syndication.cluster_label,
                                "story_role": syndication.role,
                                "story_role_label": syndication.role_label,
                                "is_syndicated": syndication.is_syndicated,
                                "has_substantive_update": syndication.has_substantive_update,
                                "view_count": stored.view_count,
                                "like_count": stored.like_count,
                                "comment_count": stored.comment_count,
                                "share_count": stored.share_count,
                                "_mention": stored,
                                "_analysis": analysis,
                                "_incident_group_id": incident_group_id,
                            }
                        )
                    elif self._should_alert(stored, analysis, source_backfill, source_realtime_enabled):
                        if self.db.is_incident_suppressed(stored.event_fingerprint):
                            # Human marked this incident false-positive or muted.
                            stats["alerts_suppressed_muted"] += 1
                        elif not self._topic_allows_alert(stored, analysis):
                            # Same event already paged within the cooldown window
                            # (cross-source de-dup): suppress the duplicate page.
                            stats["alerts_suppressed_cooldown"] += 1
                        elif self._create_alert(stored, analysis, incident_group_id):
                            stats["alerts_created"] += 1
                self.db.record_source_success(source, source_stats)
                if is_media_outlet_social_source(source.source_name):
                    self.db.set_state(f"source_last_run:{source.source_name}", utcnow().isoformat())
                    if source_backfill:
                        self.db.set_state(
                            f"media_outlet_social_backfill_completed_at:{source.source_name}",
                            utcnow().isoformat(),
                        )
                if source.method == "brandwatch":
                    self.db.set_state(f"source_cursor:{source.source_name}", utcnow().isoformat())
            except Exception as exc:
                stats["sources_failed"] += 1
                if source.priority == "P0":
                    stats["p0_sources_failed"] += 1
                self.db.record_source_failure(source, str(exc))
        for rows in notice_groups.values():
            priority = "urgent" if any(row["notification_priority"] == "urgent" for row in rows) else "standard"
            urgent_alert_row = next(
                (
                    row for row in rows
                    if row["notification_priority"] == "urgent"
                    and self._is_red_escalation(row["_analysis"])
                ),
                None,
            )
            urgent_alert_allowed = False
            if urgent_alert_row:
                urgent_alert_allowed = self._topic_allows_alert(
                    urgent_alert_row["_mention"], urgent_alert_row["_analysis"]
                )
                if not urgent_alert_allowed:
                    stats["alerts_suppressed_cooldown"] += 1
            payload = self.delivery_client.mention_batch_payload(rows, priority)
            mention_ids = [int(row["mention_id"]) for row in rows if row.get("mention_id") is not None]
            if self.db.enqueue_mention_batch(mention_ids, priority, payload):
                stats["mention_batches_queued"] += 1
                if urgent_alert_row and urgent_alert_allowed:
                    mention = urgent_alert_row["_mention"]
                    analysis = urgent_alert_row["_analysis"]
                    dedupe_key = f"{mention.event_fingerprint}:red"
                    if not self.db.alert_exists(dedupe_key):
                        self.db.insert_alert(
                            mention_id=mention.id,
                            incident_group_id=int(urgent_alert_row["_incident_group_id"]),
                            risk_level=analysis.risk_level,
                            alert_reason=analysis.escalation_reason,
                            dedupe_key=dedupe_key,
                            confidence=analysis.confidence,
                            evidence_check_passed=analysis.evidence_check_passed,
                            needs_human_review=analysis.needs_human_review,
                            delivery_latency_seconds=self._delivery_latency_seconds(mention, utcnow()),
                            sent_to=None,
                            sent_at=None,
                        )
                        self.db.record_topic_alert(
                            topic_key(mention.title, mention.raw_text, mention.matched_keywords),
                            mention.source_type,
                            analysis.risk_level,
                            utcnow(),
                        )
                        stats["alerts_created"] += 1
        if realtime_enabled:
            stats["recall_updates_queued"] = RecallRegistryService(self.db, self).queue_pending(20)
        delivery_stats = DeliveryOutboxWorker(self.db, self.delivery_client).drain()
        stats["deliveries_sent"] = delivery_stats["sent"]
        stats["deliveries_failed"] = delivery_stats["failed"]
        return stats

    def ingest_manual_public_social(
        self, source_url: str, title: str = "", evidence_excerpt: str = ""
    ) -> Mention:
        """Silently preserve an operator-supplied public parent-post link.

        The person providing the link already knows about it, so this is an
        evidence-library backfill, never an automatic DingTalk notification.
        """
        raw_item, _ = build_manual_public_social_item(source_url, title, evidence_excerpt)
        keyword_terms = [keyword.term for keyword in self.db.get_keywords("regular")]
        mention = self._build_mention(raw_item, keyword_terms, backfill=True)
        assessment = assess_public_mention(mention)
        if not assessment.valid:
            raise ValueError("补录内容未满足 OCOOPA 召回/安全舆情口径；请补充原帖可见标题或摘要")
        stored = self.db.upsert_mention(mention)
        self.db.record_interaction_snapshot(stored)
        if not stored.is_new and not stored.is_updated:
            return stored
        analysis = enrich_analysis(stored, self.analysis_service.analyze(stored))
        self.db.insert_analysis(analysis)
        self.db.ensure_mention_action(stored.id, response_guidance(stored, analysis))
        self.db.upsert_incident_group(stored, analysis)
        if is_current_recall(stored.title, stored.raw_text):
            self.db.upsert_recall_mention(stored.id)
        return stored

    def _source_due(self, source: SourceConfig, backfill: bool) -> bool:
        if backfill or not is_media_outlet_social_source(source.source_name):
            return True
        raw = self.db.get_state(f"source_last_run:{source.source_name}")
        if not raw:
            return True
        try:
            last_run = _aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            return True
        return last_run is None or utcnow() - last_run >= timedelta(hours=24)

    def _source_requires_silent_backfill(self, source: SourceConfig) -> bool:
        """New public-index sources must never replay indexed history as live."""
        return (
            is_media_outlet_social_source(source.source_name)
            and not self.db.get_state(
                f"media_outlet_social_backfill_completed_at:{source.source_name}"
            )
        )

    @staticmethod
    def _source_query_label(source: SourceConfig) -> str:
        if is_media_outlet_social_source(source.source_name):
            return f"Facebook 定向：{media_outlet_target_label(source.source_name)}"
        if source.source_name == "brave_regular_search":
            return "公开社媒广泛索引"
        return source.source_name

    def bootstrap(self, since_days: int) -> Dict[str, Dict[str, int]]:
        """Cold-start: silently backfill both lanes, then enable real-time alerts.

        All ingested content is marked backfill=true and produces no alerts;
        once both lanes finish, the bootstrap flag is set so subsequent runs
        alert only on genuinely new post-backfill content.
        """
        stats: Dict[str, Dict[str, int]] = {}
        for lane in ("high", "regular", "licensed"):
            lane_days = min(since_days, 30) if lane == "licensed" else since_days
            stats[lane] = self.run_lane(lane, backfill=True, since_days=lane_days)
        high = stats["high"]
        if high["p0_sources_attempted"] == 0 or high["p0_sources_failed"] > 0:
            raise RuntimeError(
                "bootstrap incomplete: every high-lane P0 source must complete successfully; "
                f"attempted={high['p0_sources_attempted']} failed={high['p0_sources_failed']}"
            )
        self.db.mark_bootstrapped()
        if stats["regular"]["sources_attempted"] and stats["regular"]["sources_failed"] == 0:
            self.db.set_state("public_social_backfill_completed_at", utcnow().isoformat())
        if stats["licensed"]["sources_attempted"] and stats["licensed"]["sources_failed"] == 0:
            self.db.set_state("brandwatch_backfill_completed_at", utcnow().isoformat())
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
        platform = raw_item.platform or infer_platform(raw_item.source_url, raw_item.source_type)
        content_type = raw_item.content_type or infer_content_type(raw_item.source_url, platform)
        canonical = canonicalize_url(raw_item.source_url)
        if raw_item.provider and raw_item.provider_item_id and content_type == "comment" and (
            not raw_item.parent_url or canonicalize_url(raw_item.parent_url) == canonical
        ):
            canonical = f"{raw_item.provider}://{raw_item.provider_item_id}"
        matched = find_keywords(f"{title}\n{raw_text}", keyword_terms)
        if raw_item.source_name.startswith(("brave", "serpapi", "gnews")):
            matched.append("search_api_query_hit")
            matched = sorted(set(matched), key=str.lower)
        digest = content_hash(title, raw_text)
        fingerprint = event_fingerprint(title, raw_text, matched)
        mention = Mention(
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
            platform=platform,
            content_type=content_type,
            provider=raw_item.provider,
            provider_item_id=raw_item.provider_item_id,
            parent_url=raw_item.parent_url,
            discovery_method=raw_item.discovery_method,
            coverage_tier=raw_item.coverage_tier,
            provider_added_at=_aware(raw_item.provider_added_at),
            discovery_latency_seconds=discovery_latency_seconds(
                raw_item.published_at, raw_item.provider_added_at, now
            ),
            view_count=raw_item.view_count,
            like_count=raw_item.like_count,
            comment_count=raw_item.comment_count,
            share_count=raw_item.share_count,
        )
        mention.duplicate_group_id = assess_syndication(mention).cluster_key
        return mention

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
            notification_priority=analysis.notification_priority,
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
