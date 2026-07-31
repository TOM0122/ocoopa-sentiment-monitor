from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from urllib.request import Request, urlopen

from ..models import RawItem, SourceConfig
from ..normalize import google_news_rss_url, normalize_text
from .base import Fetcher


class GoogleNewsRSSFetcher(Fetcher):
    def __init__(self, timeout_seconds: int = 20):
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        items: List[RawItem] = []
        errors: List[str] = []
        queries = self._queries(source.lane, list(keywords))
        for query in queries:
            try:
                items.extend(self._fetch_query(source, query, since))
            except Exception as exc:  # fetcher isolation happens one level up; query isolation keeps source useful.
                errors.append(f"{query}: {exc}")
        if errors and not items:
            raise RuntimeError("; ".join(errors))
        unique = {}
        for item in items:
            key = (
                item.title.lower(),
                item.author_or_publisher or "",
                item.published_at.isoformat() if item.published_at else item.source_url,
            )
            unique[key] = item
        return list(unique.values())

    def _fetch_query(self, source: SourceConfig, query: str, since: Optional[datetime]) -> List[RawItem]:
        url = google_news_rss_url(query)
        request = Request(url, headers={"User-Agent": "OcoopaMonitor/0.1 (+internal compliance monitoring)"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            xml_bytes = response.read()
        root = ET.fromstring(xml_bytes)
        parsed_items: List[RawItem] = []
        channel = root.find("channel")
        if channel is None:
            return parsed_items
        for node in channel.findall("item"):
            title = normalize_text(_node_text(node, "title"))
            link = normalize_text(_node_text(node, "link"))
            description = normalize_text(_node_text(node, "description"))
            publisher = normalize_text(_node_text(node, "source"))
            published_at = _parse_rss_date(_node_text(node, "pubDate"))
            if since and published_at and published_at < since:
                continue
            if not link:
                continue
            parsed_items.append(
                RawItem(
                    source_type=source.source_type,
                    source_name=source.source_name,
                    source_url=link,
                    title=title or link,
                    raw_text=f"{title}\n{description}",
                    published_at=published_at,
                    author_or_publisher=publisher or "Google News RSS",
                    language="en",
                    country_or_market="US",
                    tos_method="rss",
                )
            )
        return parsed_items

    @staticmethod
    def _queries(lane: str, keywords: List[str]) -> List[str]:
        current_recall_terms = [
            '"OCOOPA" "26-659"',
            '"OCOOPA" ("UT3053" OR "UT3056" OR "ZLS-118" OR "H01") recall',
            '"Shenzhen Street Cat Technology" recall',
            '"OCOOPA" ("1.5 million" OR "1,480 reports" OR "350 burn injuries")',
        ]
        fallback_high_terms = [
            "Ocoopa lawsuit",
            "Ocoopa wrongful death",
            "Ocoopa fire",
            "Ocoopa recall",
            "Ocoopa CPSC",
            "Ocoopa class action",
            "Amazon Ocoopa delisted",
        ]
        high_terms = [
            term
            for term in keywords
            if "ocoopa" in term.lower() or "ocopa" in term.lower() or "amazon ocoopa" in term.lower()
        ]
        if not high_terms:
            high_terms = fallback_high_terms
        if lane == "high":
            return _unique(current_recall_terms + high_terms)[:16]
        category_terms = [
            term
            for term in keywords
            if "hand warmer" in term.lower() or "暖手宝" in term
        ]
        if not category_terms:
            category_terms = [
                "hand warmer fire death lawsuit",
                "rechargeable hand warmer recall",
                "electric hand warmer burn",
            ]
        return _unique(current_recall_terms + high_terms + category_terms)[:24]


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _node_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return child.text if child is not None and child.text else ""


def _parse_rss_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    parsed = email.utils.parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
