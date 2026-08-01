from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse

from .models import AnalysisResult, Mention
from .recall import is_current_recall


PLATFORM_DOMAINS = {
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "x.com": "x",
    "twitter.com": "x",
    "reddit.com": "reddit",
    "trustpilot.com": "review",
    "sitejabber.com": "review",
}

SAFETY_TERMS = {
    "recall", "recalled", "cpsc", "fire", "burn", "burned", "burnt",
    "overheat", "overheating", "explod", "smoke", "injur", "death", "died",
    "lawsuit", "legal", "refund", "stop using", "26-659", "ut3053", "ut3056",
    "zls-118", "h01", "召回", "起火", "烧伤", "死亡", "退款", "停用", "诉讼",
}
BRAND_TERMS = {"ocoopa", "ocopa", "shenzhen street cat technology"}
SPAM_TERMS = {"coupon", "promo code", "discount code", "affiliate", "sponsored", "deal only"}
URGENT_TERMS = {
    "i was burned", "caught fire", "started a fire", "hospital", "emergency room",
    "died", "death", "fatal", "lawsuit filed", "class action filed", "cpsc announced",
    "起火", "烧伤", "住院", "死亡", "提起诉讼", "监管调查",
}
SOCIAL_PLATFORMS = {"tiktok", "instagram", "facebook", "youtube", "x", "reddit", "social"}
RECALL_PAGE_URL = "https://www.ocoopa.com/pages/product-recalls"
RECALL_SUPPORT_EMAIL = "ocooparecalls@ocoopa.cc"


@dataclass(frozen=True)
class PublicAssessment:
    valid: bool
    campaign: str
    relevance: float
    novelty_type: str
    notification_priority: str
    recommended_action: str
    reason: str


def infer_platform(url: str, source_type: str = "") -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    for domain, platform in PLATFORM_DOMAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return platform
    if source_type == "news":
        return "news"
    if source_type == "legal":
        return "legal"
    if source_type == "social":
        return "social"
    return "web"


def infer_content_type(url: str, platform: str) -> str:
    path = urlparse(url).path.lower()
    if "/comments/" in path or "/comment/" in path:
        return "comment"
    if platform in {"tiktok", "youtube"} or "/reel/" in path or "/video" in path:
        return "video"
    if platform in {"instagram", "facebook", "x", "reddit", "social"}:
        return "post"
    if platform == "review":
        return "review"
    return "article"


def assess_public_mention(mention: Mention) -> PublicAssessment:
    text = f"{mention.title}\n{mention.raw_text}".casefold()
    has_brand = any(term in text for term in BRAND_TERMS)
    has_safety = any(term in text for term in SAFETY_TERMS)
    exact_recall = is_current_recall(mention.title, mention.raw_text)
    substantial = len(" ".join(text.split())) >= 35 or exact_recall
    spam = any(term in text for term in SPAM_TERMS) and not exact_recall
    author = " ".join(str(mention.author_or_publisher or "").casefold().split())
    own_account = mention.platform in SOCIAL_PLATFORMS and (
        "ocoopa" in author or "official" in author
    )
    valid = (exact_recall or (has_brand and has_safety and substantial)) and not spam and not own_account
    campaign = "recall_26_659" if exact_recall else "brand_major_risk"
    urgent = any(term in text for term in URGENT_TERMS)
    novelty = "new_high_risk_claim" if urgent else ("known_recall_repost" if exact_recall else "new_mention")
    priority = "urgent" if urgent else "standard"
    if urgent:
        action = "escalate_pr_legal" if any(term in text for term in ("death", "died", "lawsuit", "诉讼", "死亡")) else "suggest_response"
    elif mention.platform in {"tiktok", "instagram", "facebook", "youtube", "x", "reddit", "review", "social"}:
        action = "review_for_response"
    else:
        action = "monitor"
    relevance = 1.0 if exact_recall else (0.85 if has_brand and has_safety else 0.0)
    reason = "匹配召回编号或受影响型号" if exact_recall else "品牌与安全/法律语境共同出现"
    if own_account:
        reason = "官号自身内容不属于公开舆情发现范围"
    elif spam:
        reason = "优惠券、联盟或促销内容"
    elif not substantial:
        reason = "内容过短，缺少可核实的实质信息"
    elif not valid:
        reason = "未同时满足品牌与安全/法律语境"
    return PublicAssessment(valid, campaign, relevance, novelty, priority, action, reason)


def enrich_analysis(mention: Mention, analysis: AnalysisResult) -> AnalysisResult:
    assessment = assess_public_mention(mention)
    analysis.campaign = assessment.campaign
    analysis.relevance = assessment.relevance
    analysis.novelty_type = assessment.novelty_type
    analysis.notification_priority = assessment.notification_priority
    analysis.recommended_action = assessment.recommended_action
    interactions = engagement_total((mention.like_count, mention.comment_count, mention.share_count))
    if (mention.view_count is not None and mention.view_count >= 10000) or (
        interactions is not None and interactions >= 500
    ):
        analysis.notification_priority = "urgent"
        analysis.recommended_action = "review_high_reach"
    if (
        analysis.risk_level == "red"
        and analysis.requires_escalation
        and assessment.novelty_type != "known_recall_repost"
    ):
        analysis.notification_priority = "urgent"
        if analysis.recommended_action == "monitor":
            analysis.recommended_action = "escalate_pr_legal"
    return analysis


def response_guidance(mention: Mention, analysis: AnalysisResult) -> Dict[str, str]:
    assessment = assess_public_mention(mention)
    sensitive = assessment.notification_priority == "urgent"
    if sensitive:
        original = (
            "We’re sorry to hear about this. Please stop using the product and contact OCOOPA support "
            f"at {RECALL_SUPPORT_EMAIL} or use the official recall page ({RECALL_PAGE_URL}) "
            "so the safety team can review the details. "
            "If anyone needs medical attention, please contact local emergency services."
        )
        zh = f"很遗憾得知这一情况。请停止使用产品，并通过 {RECALL_SUPPORT_EMAIL} 或官方召回页面联系 OCOOPA，由安全团队核查具体情况；如有人需要医疗救助，请联系当地紧急服务。"
        risk = "仅表达关切并引导至官方渠道；不得确认因果、责任、赔偿或诉讼结论，提交 PR/法务复核。"
    else:
        original = (
            f"Thank you for flagging this. Please review OCOOPA’s official recall page ({RECALL_PAGE_URL}) "
            f"for the affected models and next steps. For help confirming a model, contact {RECALL_SUPPORT_EMAIL}."
        )
        zh = f"感谢反馈。请查阅 OCOOPA 官方召回页面，确认受影响型号及后续步骤；如需协助确认型号，请联系 {RECALL_SUPPORT_EMAIL}。"
        risk = "发布前核对原帖语境、型号与当前已确认召回事实；不要扩展未确认数字或承诺处理结果。"
    return {
        "intervention_reason": assessment.reason,
        "draft_original": original,
        "draft_zh": zh,
        "legal_risk_note": risk,
    }


def discovery_latency_seconds(
    published_at: Optional[datetime], provider_added_at: Optional[datetime], fetched_at: datetime
) -> Optional[int]:
    # Publication-to-system latency is the operational coverage boundary the
    # dashboard needs. provider_added_at is retained separately for diagnosing
    # whether delay came from the supplier or our polling loop.
    value = published_at or provider_added_at
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((fetched_at - value.astimezone(timezone.utc)).total_seconds()))


def engagement_total(values: Iterable[Optional[int]]) -> Optional[int]:
    known = [int(value) for value in values if value is not None]
    return sum(known) if known else None


def crossed_surge_threshold(
    previous: Optional[Dict[str, int]], current: Dict[str, Optional[int]]
) -> Optional[str]:
    views = current.get("view_count")
    interactions = engagement_total(
        (current.get("like_count"), current.get("comment_count"), current.get("share_count"))
    )
    previous = previous or {}
    old_views = int(previous.get("view_count") or 0)
    old_interactions = sum(int(previous.get(key) or 0) for key in ("like_count", "comment_count", "share_count"))
    for threshold in (100000, 50000, 10000):
        if views is not None and old_views < threshold <= int(views):
            return f"views_{threshold}"
    if interactions is not None and old_interactions < 500 <= interactions:
        return "interactions_500"
    if interactions is not None and old_interactions > 0:
        increase = interactions - old_interactions
        if interactions >= old_interactions * 3 and increase >= 100:
            return "interaction_growth_3x"
    return None
