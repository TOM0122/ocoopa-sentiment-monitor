from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import RawItem, SourceConfig
from ..normalize import normalize_text
from .base import Fetcher


class CPSCRecallFetcher(Fetcher):
    """Fetch public CPSC recall data through the saferproducts.gov recall endpoint."""

    def __init__(self, timeout_seconds: int = 20):
        self.timeout_seconds = timeout_seconds

    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        items: List[RawItem] = []
        query_terms = ["Ocoopa", "hand warmer", "rechargeable hand warmer"]
        errors: List[str] = []
        for term in query_terms:
            try:
                items.extend(self._fetch_term(source, term, since))
            except Exception as exc:
                errors.append(f"{term}: {exc}")
        if errors and not items:
            raise RuntimeError("; ".join(errors))
        return items

    def _fetch_term(self, source: SourceConfig, term: str, since: Optional[datetime]) -> List[RawItem]:
        sep = "&" if "?" in source.url else "?"
        url = f"{source.url}{sep}{urlencode({'ProductName': term})}"
        request = Request(url, headers={"User-Agent": "OcoopaMonitor/0.1 (+internal compliance monitoring)"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8")
        data = json.loads(payload)
        if isinstance(data, dict):
            records = data.get("Recalls") or data.get("recalls") or data.get("results") or []
        else:
            records = data
        items: List[RawItem] = []
        for record in records:
            title = normalize_text(str(record.get("Title") or record.get("RecallTitle") or record.get("Name") or "CPSC recall"))
            description = normalize_text(
                " ".join(
                    str(record.get(key) or "")
                    for key in [
                        "Description",
                        "RecallDescription",
                        "Hazard",
                        "Remedy",
                        "Products",
                        "ProductDescription",
                    ]
                )
            )
            recall_id = record.get("RecallID") or record.get("RecallNumber") or title
            url_value = (
                record.get("URL")
                or record.get("RecallURL")
                or f"https://www.cpsc.gov/Recalls?search_api_fulltext={term}"
            )
            published_at = _parse_cpsc_date(
                str(record.get("RecallDate") or record.get("Date") or record.get("PublishDate") or "")
            )
            if since and published_at and published_at < since:
                continue
            items.append(
                RawItem(
                    source_type=source.source_type,
                    source_name=source.source_name,
                    source_url=str(url_value),
                    title=title,
                    raw_text=f"{title}\n{description}\nRecall ID: {recall_id}",
                    published_at=published_at,
                    author_or_publisher="CPSC",
                    language="en",
                    country_or_market="US",
                    tos_method="api",
                )
            )
        return items


def _parse_cpsc_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
