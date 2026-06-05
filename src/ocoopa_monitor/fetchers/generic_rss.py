from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from urllib.request import Request, urlopen

from ..models import RawItem, SourceConfig
from ..normalize import normalize_text
from .base import Fetcher


class GenericRSSFetcher(Fetcher):
    def __init__(self, timeout_seconds: int = 20):
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        request = Request(source.url, headers={"User-Agent": "OcoopaMonitor/0.1"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            xml_bytes = response.read()
        root = ET.fromstring(xml_bytes)
        channel = root.find("channel")
        if channel is None:
            return []
        items: List[RawItem] = []
        for node in channel.findall("item"):
            title = normalize_text(_node_text(node, "title"))
            link = normalize_text(_node_text(node, "link"))
            description = normalize_text(_node_text(node, "description"))
            published_at = _parse_rss_date(_node_text(node, "pubDate"))
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
                    raw_text=f"{title}\n{description}",
                    published_at=published_at,
                    author_or_publisher=source.source_name,
                    language="en",
                    country_or_market="US",
                    tos_method="rss",
                )
            )
        return items


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
