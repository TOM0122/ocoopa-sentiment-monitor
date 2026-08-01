from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import urlparse

from .recall import is_current_recall


SOCIAL_PLATFORMS = {"tiktok", "instagram", "facebook", "youtube", "x", "reddit", "social", "review"}
OFFICIAL_HOSTS = {"cpsc.gov", "ocoopa.com"}
FIRST_PERSON_PATTERN = re.compile(r"\b(?:i|i'm|i've|me|my|mine|we|our|us)\b|(?:^|[\s，。！；])我(?:的|们)?", re.IGNORECASE)
INCIDENT_PATTERN = re.compile(
    r"caught fire|started a fire|burn(?:ed|t)?|overheat(?:ed|ing)?|explod(?:e|ed|ing)|"
    r"smoke|hospital|emergency room|injur(?:y|ed)|died|death|起火|着火|烧伤|过热|爆炸|冒烟|住院|受伤|死亡",
    re.IGNORECASE,
)
NEW_ACTION_PATTERN = re.compile(
    r"lawsuit (?:was )?filed|filed (?:a )?lawsuit|class action (?:was )?filed|"
    r"investigation (?:was )?(?:opened|launched)|recall (?:was )?(?:expanded|widened)|"
    r"additional death|second death|new (?:death|injury|fire)|civil penalty|criminal charge|"
    r"提起诉讼|立案调查|扩大召回|新增死亡|新增受伤|行政处罚",
    re.IGNORECASE,
)

ROLE_LABELS = {
    "official_source": "官方事实源",
    "news_repost": "新闻转载",
    "social_amplification": "社媒扩散",
    "substantive_update": "新增实质信号",
    "independent_mention": "独立讨论",
}


@dataclass(frozen=True)
class SyndicationAssessment:
    cluster_key: str
    cluster_label: str
    role: str
    role_label: str
    is_syndicated: bool
    has_substantive_update: bool


def has_substantive_signal(title: str, raw_text: str, novelty_type: str = "") -> bool:
    text = " ".join(f"{title}\n{raw_text}".split())
    if novelty_type == "new_high_risk_claim":
        return True
    return bool(
        NEW_ACTION_PATTERN.search(text)
        or (FIRST_PERSON_PATTERN.search(text) and INCIDENT_PATTERN.search(text))
    )


def assess_syndication(item: Any, novelty_type: str = "") -> SyndicationAssessment:
    title = str(_value(item, "title") or "")
    raw_text = str(_value(item, "raw_text") or _value(item, "text_excerpt") or "")
    source_url = str(_value(item, "source_url") or _value(item, "canonical_url") or "")
    platform = str(_value(item, "platform") or "web").casefold()
    source_type = str(_value(item, "source_type") or "").casefold()
    content_type = str(_value(item, "content_type") or "article").casefold()
    fingerprint = str(_value(item, "event_fingerprint") or "")
    current_recall = is_current_recall(title, raw_text)
    substantive = has_substantive_signal(title, raw_text, novelty_type)
    host = (urlparse(source_url).hostname or "").casefold().removeprefix("www.")
    official = any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_HOSTS)

    if current_recall and official:
        role = "official_source"
        substantive = False
    elif substantive:
        role = "substantive_update"
    elif current_recall and platform in SOCIAL_PLATFORMS:
        role = "social_amplification"
    elif current_recall and (content_type == "article" or source_type in {"news", "search", "legal"}):
        role = "news_repost"
    else:
        role = "independent_mention"

    if current_recall and role != "substantive_update":
        cluster_key = "story:recall-26-659:official-release"
        cluster_label = "召回 26-659 官方新闻稿传播簇"
    else:
        identity = fingerprint or source_url or f"{title}\n{raw_text}"
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()
        prefix = "story:recall-26-659:signal" if current_recall else "story:independent"
        cluster_key = f"{prefix}:{digest}"
        cluster_label = "召回 26-659 新增实质信号" if current_recall else "独立舆情事件"
    return SyndicationAssessment(
        cluster_key=cluster_key,
        cluster_label=cluster_label,
        role=role,
        role_label=ROLE_LABELS[role],
        is_syndicated=role in {"news_repost", "social_amplification"},
        has_substantive_update=role == "substantive_update",
    )


def with_syndication_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(row)
    assessment = assess_syndication(enriched, str(enriched.get("novelty_type") or ""))
    enriched.update(
        {
            "story_cluster_key": assessment.cluster_key,
            "story_cluster_label": assessment.cluster_label,
            "story_role": assessment.role,
            "story_role_label": assessment.role_label,
            "is_syndicated": assessment.is_syndicated,
            "has_substantive_update": assessment.has_substantive_update,
        }
    )
    return enriched


def _value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    try:
        return item[name]
    except (KeyError, TypeError):
        return getattr(item, name, None)
