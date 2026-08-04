from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import RawItem, SourceConfig
from ..normalize import normalize_text
from ..social import infer_content_type, infer_platform
from .base import Fetcher

HIGH_SENSITIVITY_QUERY = (
    '("OCOOPA" OR "Shenzhen Street Cat Technology") '
    '(fire OR death OR lawsuit OR recall OR CPSC OR "class action" OR "26-659" '
    'OR UT3053 OR UT3056 OR "ZLS-118" OR H01)'
)
# Provider limits differ: Brave accepts at most 400 characters / 50 words,
# while GNews accepts at most 200 characters. Keep provider-specific queries so
# one broad social discovery expression cannot silently disable both sources.
REGULAR_SOCIAL_QUERY = (
    '("OCOOPA" OR "hand warmer" OR "26-659" OR UT3053 OR UT3056 OR "ZLS-118" OR H01) '
    '(recall OR fire OR burn OR death OR lawsuit OR complaint) '
    '(site:tiktok.com OR site:instagram.com OR site:facebook.com OR site:youtube.com '
    'OR site:x.com OR site:reddit.com OR site:trustpilot.com)'
)
# The public parent posts supplied by operations identify a small, auditable
# outlet roster. Each account receives its own daily query rather than
# competing for the ten result slots of one combined query. A path-level site
# restriction covers Facebook's /posts, /photos and /videos URL variants
# without attempting to fetch Facebook itself.
MEDIA_OUTLET_SOCIAL_TARGETS = {
    "brave_media_outlet_social_whionews": ("WHIO News", "whionews"),
    "brave_media_outlet_social_wpri12": ("WPRI 12", "WPRI12"),
    "brave_media_outlet_social_kens5": ("KENS 5", "kens5"),
    "brave_media_outlet_social_kare11": ("KARE 11", "KARE11"),
    "brave_media_outlet_social_boston25news": ("Boston 25 News", "Boston25News"),
}


def is_media_outlet_social_source(source_name: str) -> bool:
    return source_name in MEDIA_OUTLET_SOCIAL_TARGETS


def media_outlet_target_label(source_name: str) -> str:
    return MEDIA_OUTLET_SOCIAL_TARGETS[source_name][0]


def media_outlet_query(source_name: str) -> str:
    """Return the bounded public-index query for one known media account."""
    _, account = MEDIA_OUTLET_SOCIAL_TARGETS[source_name]
    return (
        f'site:facebook.com/{account}/ ("OCOOPA" OR "26-659" OR "hand warmer") '
        '(recall OR fire OR burn OR death)'
    )


# Compatibility export retained for integrations that import the former
# constant. The scheduler now runs the source-specific query above.
REGULAR_MEDIA_OUTLET_SOCIAL_QUERY = media_outlet_query("brave_media_outlet_social_whionews")
REGULAR_NEWS_QUERY = (
    '("OCOOPA" OR "Shenzhen Street Cat Technology" OR "26-659" OR UT3053 OR UT3056 '
    'OR "ZLS-118" OR H01) '
    '(recall OR fire OR burn OR death OR lawsuit OR complaint OR refund)'
)
# Compatibility alias for callers that imported the former shared query.
REGULAR_QUERY = REGULAR_SOCIAL_QUERY


class SerpAPIFetcher(Fetcher):
    def __init__(self, api_key: str = "", timeout_seconds: int = 20):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        if not self.api_key:
            return []
        query = _query_for_source(source)
        params = urlencode(
            {
                "engine": "google",
                "q": query,
                "api_key": self.api_key,
                "num": "10",
                "hl": "en",
                "gl": "us",
            }
        )
        request = Request(
            f"https://serpapi.com/search.json?{params}",
            headers={"User-Agent": "OcoopaMonitor/0.1"},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        items: List[RawItem] = []
        for result in data.get("organic_results", []):
            title = normalize_text(str(result.get("title") or ""))
            link = normalize_text(str(result.get("link") or ""))
            snippet = normalize_text(str(result.get("snippet") or ""))
            if not link:
                continue
            items.append(
                RawItem(
                    source_type=source.source_type,
                    source_name=source.source_name,
                    source_url=link,
                    title=title or link,
                    raw_text=f"{title}\n{snippet}",
                    published_at=None,
                    author_or_publisher="SerpAPI Google Search",
                    language="en",
                    country_or_market="US",
                    tos_method="api",
                    platform=infer_platform(link, source.source_type),
                    content_type=infer_content_type(link, infer_platform(link, source.source_type)),
                    provider="serpapi",
                    provider_item_id=link,
                    discovery_method="public_index",
                    coverage_tier="public_index",
                )
            )
        return items


class GNewsFetcher(Fetcher):
    def __init__(self, api_key: str = "", timeout_seconds: int = 20):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        if not self.api_key:
            return []
        query = HIGH_SENSITIVITY_QUERY if source.lane == "high" else REGULAR_NEWS_QUERY
        params = urlencode(
            {
                "q": query,
                "lang": "en",
                "country": "us",
                "max": "10",
                "apikey": self.api_key,
            }
        )
        request = Request(
            f"https://gnews.io/api/v4/search?{params}",
            headers={"User-Agent": "OcoopaMonitor/0.1"},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        items: List[RawItem] = []
        for article in data.get("articles", []):
            title = normalize_text(str(article.get("title") or ""))
            link = normalize_text(str(article.get("url") or ""))
            description = normalize_text(str(article.get("description") or ""))
            content = normalize_text(str(article.get("content") or ""))
            published_at = _parse_iso(str(article.get("publishedAt") or ""))
            if since and published_at and published_at < since:
                continue
            if not link:
                continue
            items.append(
                RawItem(
                    source_type=source.source_type,
                    source_name=source.source_name,
                    source_url=link,
                    title=title or link,
                    raw_text=f"{title}\n{description}\n{content}",
                    published_at=published_at,
                    author_or_publisher=str(article.get("source", {}).get("name") or "GNews"),
                    language="en",
                    country_or_market="US",
                    tos_method="api",
                )
            )
        return items


class BraveSearchFetcher(Fetcher):
    def __init__(self, api_key: str = "", timeout_seconds: int = 20):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        if not self.api_key:
            raise RuntimeError("Brave Search API key is not configured")
        query = _query_for_source(source)
        params = urlencode(
            {
                "q": query,
                "count": "10",
                "country": "us",
                "search_lang": "en",
                "safesearch": "moderate",
                "freshness": "pd" if source.lane == "high" else "py",
            }
        )
        request = Request(
            f"https://api.search.brave.com/res/v1/web/search?{params}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
                "User-Agent": "OcoopaMonitor/0.1",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        results = data.get("web", {}).get("results", [])
        items: List[RawItem] = []
        for result in results:
            title = normalize_text(str(result.get("title") or ""))
            link = normalize_text(str(result.get("url") or ""))
            description = normalize_text(str(result.get("description") or ""))
            age = normalize_text(str(result.get("age") or ""))
            if not link:
                continue
            platform = infer_platform(link, source.source_type)
            content_type = infer_content_type(link, platform)
            if is_media_outlet_social_source(source.source_name) and content_type == "comment":
                continue
            items.append(
                RawItem(
                    source_type=source.source_type,
                    source_name=source.source_name,
                    source_url=link,
                    title=title or link,
                    raw_text=f"{title}\n{description}\n{age}",
                    published_at=None,
                    author_or_publisher="Brave Search",
                    language="en",
                    country_or_market="US",
                    tos_method="api",
                    platform=platform,
                    content_type=content_type,
                    provider="brave",
                    provider_item_id=link,
                    discovery_method=(
                        "public_index_media_targeted"
                        if is_media_outlet_social_source(source.source_name) else "public_index"
                    ),
                    coverage_tier="public_index",
                )
            )
        return items


def _query_for_source(source: SourceConfig) -> str:
    if source.lane == "high":
        return HIGH_SENSITIVITY_QUERY
    if is_media_outlet_social_source(source.source_name):
        return media_outlet_query(source.source_name)
    return REGULAR_SOCIAL_QUERY


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
