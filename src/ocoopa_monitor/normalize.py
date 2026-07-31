from __future__ import annotations

import hashlib
import re
from html import unescape
from typing import Iterable, List
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid"}


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = parse_qs(parsed.query, keep_blank_values=False)
    filtered = {
        key: values[-1]
        for key, values in query.items()
        if key not in TRACKING_KEYS and not key.startswith(TRACKING_PREFIXES)
    }
    clean_query = urlencode(sorted(filtered.items()))
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((scheme, netloc, path, "", clean_query, ""))


def normalize_text(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def content_hash(title: str, raw_text: str) -> str:
    normalized = normalize_text(f"{title} {raw_text}").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def event_fingerprint(title: str, raw_text: str, matched_keywords: Iterable[str]) -> str:
    text = normalize_text(f"{title} {raw_text}").lower()
    tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text)
    important = [token for token in tokens if len(token) > 2][:24]
    keyword_part = "|".join(sorted({kw.lower() for kw in matched_keywords}))
    base = " ".join(important) + "|" + keyword_part
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


RISK_TOPIC_TAGS = {
    "brand": ["ocoopa", "ocopa"],
    "death": ["death", "died", "fatal", "wrongful death", "致死", "死亡", "重伤"],
    "fire": ["fire", "burn", "overheat", "explode", "explosion", "起火", "爆炸", "烧伤"],
    "lawsuit": ["lawsuit", "class action", "product liability", "settlement", "诉讼", "集体诉讼"],
    "recall": ["recall", "cpsc", "召回"],
}

MODEL_PATTERN = re.compile(r"\b(?:UT\d{4}|ZLS-\d+[A-Z]?|H01(?:\s*\(PD\))?)\b", re.IGNORECASE)
RECALL_NUMBER_PATTERN = re.compile(r"\b\d{2}-\d{3}\b")
LOCATION_PATTERN = re.compile(
    r"\b(?:in|near|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b"
)


def topic_key(title: str, raw_text: str, matched_keywords: Iterable[str]) -> str:
    """Stable signature for cross-source reports about the same event.

    Risk tags retain cross-outlet clustering. Strong identifiers (recall number,
    model, or an explicit title location) split distinct incidents so two fires
    of the same general kind are not silently merged.
    """
    combined = normalize_text(f"{title} {raw_text}")
    text = combined.lower()
    tags = [tag for tag, needles in RISK_TOPIC_TAGS.items() if any(n in text for n in needles)]
    if tags:
        identifiers = {item.upper().replace(" ", "") for item in MODEL_PATTERN.findall(combined)}
        identifiers.update(RECALL_NUMBER_PATTERN.findall(combined))
        location_match = LOCATION_PATTERN.search(normalize_text(title))
        if location_match:
            identifiers.add("loc:" + location_match.group(1).lower().replace(" ", "-"))
        parts = sorted(set(tags))
        if identifiers:
            parts.extend(sorted(identifiers))
        return "|".join(parts)
    keys = sorted({kw.lower() for kw in matched_keywords})[:3]
    return "kw:" + "|".join(keys) if keys else "untagged"


def excerpt(text: str, limit: int = 600) -> str:
    clean = normalize_text(text)
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "..."


def find_keywords(text: str, terms: Iterable[str]) -> List[str]:
    lowered = text.lower()
    matches = []
    for term in terms:
        term_clean = term.strip()
        if term_clean and term_clean.lower() in lowered:
            matches.append(term_clean)
    return sorted(set(matches), key=str.lower)


def google_news_rss_url(query: str) -> str:
    encoded = quote_plus(query)
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
