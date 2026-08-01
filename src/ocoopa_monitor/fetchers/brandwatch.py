from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from ..models import RawItem, SourceConfig
from ..normalize import normalize_text
from ..social import infer_content_type, infer_platform
from .base import Fetcher


class BrandwatchMentionsFetcher(Fetcher):
    """Read-only Brandwatch Mentions API connector.

    The caller supplies an overlapping `since`; provider/resource identity in
    the database makes retries idempotent. The connector is inert until all
    credentials are present and never requests publishing scopes.
    """

    def __init__(
        self,
        token: str = "",
        project_id: str = "",
        query_id: str = "",
        base_url: str = "https://api.brandwatch.com",
        timeout_seconds: int = 20,
        max_retries: int = 3,
    ):
        self.token = token
        self.project_id = project_id
        self.query_id = query_id
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    @property
    def configured(self) -> bool:
        return bool(self.token and self.project_id and self.query_id)

    def fetch(
        self,
        source: SourceConfig,
        keywords: Iterable[str],
        since: Optional[datetime] = None,
    ) -> List[RawItem]:
        if not self.configured:
            return []
        base_params: Dict[str, str] = {
            "queryId": self.query_id,
            "pageSize": "100",
            "orderBy": "added",
            "orderDirection": "asc",
        }
        streams = [base_params]
        if since:
            # Added and updated are separate incremental streams. Combining the
            # two filters can turn provider-side semantics into an AND and miss
            # older mentions whose engagement was updated recently.
            streams = [
                {**base_params, "sinceAdded": _iso(since)},
                {**base_params, "sinceUpdated": _iso(since), "orderBy": "updated"},
            ]
        by_resource: Dict[str, RawItem] = {}
        for params in streams:
            for item in self._fetch_pages(source, params):
                if item.provider_item_id:
                    by_resource[item.provider_item_id] = item
        return list(by_resource.values())

    def _fetch_pages(self, source: SourceConfig, params: Dict[str, str]) -> List[RawItem]:
        items: List[RawItem] = []
        cursor = ""
        seen_cursors = set()
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            data = self._get(page_params)
            records = data.get("results") or data.get("mentions") or []
            for record in records:
                item = self._to_item(source, record)
                if item is not None:
                    items.append(item)
            cursor = str(data.get("nextCursor") or data.get("nextPageCursor") or "")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return items

    def _get(self, params: Dict[str, str]) -> Dict[str, object]:
        url = f"{self.base_url}/projects/{self.project_id}/data/mentions?{urlencode(params)}"
        request = Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.max_retries:
                    raise
                retry_after = exc.headers.get("Retry-After")
                delay = min(30, int(retry_after)) if retry_after and retry_after.isdigit() else 2 ** attempt
                time.sleep(delay)
        return {}

    @staticmethod
    def _to_item(source: SourceConfig, record: Dict[str, object]) -> Optional[RawItem]:
        resource_id = str(record.get("resourceId") or record.get("id") or "").strip()
        if not resource_id:
            return None
        parent_url = _first_text(record, "parentUrl", "parentURL", "threadUrl", "threadURL")
        public_url = _first_text(record, "url", "originalUrl", "displayUrl") or parent_url
        if not public_url:
            public_url = f"https://app.brandwatch.com/mention/{resource_id}"
        title = normalize_text(_first_text(record, "title", "name") or f"Brandwatch mention {resource_id}")
        body = normalize_text(_first_text(record, "fullText", "text", "content", "snippet"))
        platform = normalize_text(
            _first_text(record, "platform", "contentSource", "contentSourceName", "pageType", "domain")
        ) or infer_platform(public_url, "social")
        platform = _normalize_platform(platform, public_url)
        content_type = normalize_text(
            _first_text(record, "contentType", "engagementType", "threadEntryType", "subtype")
        ) or infer_content_type(public_url, platform)
        if content_type.casefold() in {"comment", "reply"}:
            content_type = "comment"
        metrics = _platform_metrics(record, platform)
        return RawItem(
            source_type="social",
            source_name=source.source_name,
            source_url=public_url,
            title=title,
            raw_text=f"{title}\n{body}",
            published_at=_parse_time(_first_text(record, "date", "published", "publishedAt")),
            author_or_publisher=_author(record),
            language=_first_text(record, "language", "languageCode") or None,
            country_or_market=_first_text(record, "country", "countryCode") or None,
            tos_method="licensed_api",
            platform=platform,
            content_type=content_type.lower(),
            provider="brandwatch",
            provider_item_id=resource_id,
            parent_url=parent_url,
            discovery_method="licensed_api",
            coverage_tier="licensed",
            provider_added_at=_parse_time(_first_text(record, "added", "addedAt", "updated")),
            view_count=metrics["view_count"],
            like_count=metrics["like_count"],
            comment_count=metrics["comment_count"],
            share_count=metrics["share_count"],
        )


def _author(record: Dict[str, object]) -> Optional[str]:
    author = record.get("author")
    if isinstance(author, dict):
        return normalize_text(str(author.get("name") or author.get("username") or "")) or None
    return normalize_text(str(
        record.get("authorName") or record.get("authorUsername") or record.get("fullname") or author or ""
    )) or None


def _platform_metrics(record: Dict[str, object], platform: str) -> Dict[str, Optional[int]]:
    prefixes = {
        "tiktok": {
            "like": ("tiktokLikes",), "comment": ("tiktokComments",), "share": ("tiktokShares",),
        },
        "instagram": {
            "like": ("instagramLikeCount",), "comment": ("instagramCommentCount",), "share": (),
        },
        "facebook": {
            "like": ("facebookLikes",), "comment": ("facebookComments",), "share": ("facebookShares",),
        },
        "x": {
            "like": ("twitterLikeCount",), "comment": ("twitterReplyCount",), "share": ("twitterRetweets",),
        },
    }.get(platform, {"like": (), "comment": (), "share": ()})
    return {
        # Do not treat Brandwatch reach/impressions as observed views.
        "view_count": _metric(record, "viewCount", "views", "videoViews"),
        "like_count": _metric(record, *prefixes["like"], "likeCount", "likes"),
        "comment_count": _metric(record, *prefixes["comment"], "commentCount", "comments"),
        "share_count": _metric(record, *prefixes["share"], "shareCount", "shares", "retweets"),
    }


def _metric(record: Dict[str, object], *keys: str) -> Optional[int]:
    for key in keys:
        value = record.get(key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return None


def _first_text(record: Dict[str, object], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_platform(value: str, url: str) -> str:
    normalized = value.casefold().replace("twitter", "x").replace("youtube", "youtube")
    for token in ("tiktok", "instagram", "facebook", "youtube", "reddit"):
        if token in normalized:
            return token
    if normalized in {"x", "tweet"}:
        return "x"
    for token, platform in (
        ("forum", "forum"), ("review", "review"), ("news", "news"),
        ("blog", "blog"), ("qq", "qq"), ("tumblr", "tumblr"),
    ):
        if token in normalized:
            return platform
    return infer_platform(url, "social")
