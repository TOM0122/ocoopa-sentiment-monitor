from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import RawItem, SourceConfig
from ..normalize import normalize_text
from .base import Fetcher

HIGH_SENSITIVITY_QUERY = 'Ocoopa (fire OR death OR lawsuit OR recall OR CPSC OR "class action")'
REGULAR_QUERY = (
    '("Ocoopa" OR "Ocopa" OR "rechargeable hand warmer") '
    '(fire OR death OR lawsuit OR recall OR burn OR overheat)'
)


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
        query = HIGH_SENSITIVITY_QUERY if source.lane == "high" else REGULAR_QUERY
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
        query = HIGH_SENSITIVITY_QUERY if source.lane == "high" else REGULAR_QUERY
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
