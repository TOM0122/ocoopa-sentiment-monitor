"""Safe intake for public social-parent-post links supplied by an operator.

This module never fetches a social-network page.  It records only the public
URL and text the operator can already see, so a missed search-index result can
be retained without bypassing a platform's access controls.
"""
from __future__ import annotations

from typing import Iterable, Tuple
from urllib.parse import unquote, urlsplit

from .models import RawItem
from .normalize import normalize_text
from .social import infer_content_type, infer_platform


SUPPORTED_SOCIAL_PLATFORMS = {"facebook", "instagram", "tiktok", "youtube", "x", "reddit"}
MAX_TITLE_CHARS = 500
MAX_EXCERPT_CHARS = 4000


def build_manual_public_social_item(
    source_url: str,
    title: str = "",
    evidence_excerpt: str = "",
) -> Tuple[RawItem, str]:
    """Create a clearly-labelled, evidence-bounded social parent-post record.

    A URL-only entry is accepted only if its *visible path* itself contains an
    OCOOPA recall/safety cue.  Otherwise the operator must paste a visible
    title or excerpt, preventing an arbitrary social URL from becoming a
    fabricated OCOOPA mention.
    """
    url = str(source_url or "").strip()
    if len(url) > 2000:
        raise ValueError("公开链接过长，请使用页面原始链接而非跟踪/分享拼接链接")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("请提供完整的 http(s) 公开社媒链接")
    platform = infer_platform(url)
    if platform not in SUPPORTED_SOCIAL_PLATFORMS:
        raise ValueError("仅支持 Facebook、Instagram、TikTok、YouTube、X 或 Reddit 的公开链接")
    content_type = infer_content_type(url, platform)
    if content_type == "comment":
        raise ValueError("请补录媒体账号的母帖链接，不直接补录评论链接")

    clean_title = normalize_text(str(title or ""))[:MAX_TITLE_CHARS]
    clean_excerpt = normalize_text(str(evidence_excerpt or ""))[:MAX_EXCERPT_CHARS]
    path_hint = _visible_path_hint(parts.path)
    evidence = "\n".join(value for value in (clean_title, clean_excerpt, path_hint) if value)
    if not _has_recall_context(evidence):
        raise ValueError("链接路径未呈现 OCOOPA 召回语境；请粘贴原帖可见标题或摘要后再补录")

    publisher = _publisher_label(parts.path, platform)
    display_title = clean_title or path_hint or f"人工提供的 {platform.upper()} 公开母帖"
    raw_text = "\n".join(
        value for value in (
            clean_title,
            clean_excerpt,
            f"公开链接路径可见文本：{path_hint}" if path_hint else "",
            "记录边界：由人工提供公开链接；系统未直抓平台正文或评论。",
        ) if value
    )
    return (
        RawItem(
            source_type="social",
            source_name="manual_public_social_intake",
            source_url=url,
            title=display_title,
            raw_text=raw_text,
            author_or_publisher=publisher,
            language="en",
            country_or_market="US",
            tos_method="human_supplied_public_link",
            platform=platform,
            content_type=content_type,
            provider="manual_public_social",
            provider_item_id=url,
            discovery_method="manual_public_link",
            coverage_tier="manual_supplied",
        ),
        publisher,
    )


def _visible_path_hint(path: str) -> str:
    value = unquote(path or "").replace("/", " ").replace("-", " ").replace("_", " ")
    return normalize_text(value)[:MAX_TITLE_CHARS]


def _publisher_label(path: str, platform: str) -> str:
    pieces = [piece for piece in path.split("/") if piece]
    handle = pieces[0] if pieces else ""
    return f"{platform.upper()} / {handle}（人工提供公开母帖）" if handle else f"{platform.upper()}（人工提供公开母帖）"


def _has_recall_context(text: str) -> bool:
    folded = text.casefold()
    brand = "ocoopa" in folded or "ocopa" in folded
    safety = any(term in folded for term in ("recall", "26-659", "hand warmer", "fire", "burn", "death", "lawsuit"))
    return brand and safety
