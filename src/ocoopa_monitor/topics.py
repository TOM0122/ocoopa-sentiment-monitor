from __future__ import annotations

import re
from typing import Any, Dict


TOPIC_LABELS = {
    "recall_regulatory": "召回 / 监管传播",
    "legal_action": "法律行动",
    "incident_claim": "事故 / 伤害陈述",
    "customer_process": "召回流程与退款",
    "misinformation_scam": "错误信息 / 诈骗",
    "product_safety": "产品安全讨论",
    "other": "其他讨论",
}

_LEGAL_ACTION = re.compile(
    r"\blawsuit\b|\bclass action\b|\bwrongful death\b|\bproduct liability\b|"
    r"\bsettlement\b|\bsue(?:d|s|ing)?\b|\blegal claim\b|"
    r"诉讼|起诉|集体诉讼|索赔|和解|过失致死",
    re.IGNORECASE,
)
_RECALL_OR_REGULATOR = re.compile(
    r"\brecall(?:ed)?\b|\bCPSC\b|\bconsumer product safety commission\b|"
    r"\bregulator(?:y)?\b|\b26-659\b|\bUT3053\b|\bUT3056\b|\bZLS-118\b|\bH01\b|"
    r"召回|监管|消费品安全委员会",
    re.IGNORECASE,
)
_INCIDENT = re.compile(
    r"caught fire|started a fire|\bfire\b|\bburn(?:ed|t)?\b|\boverheat(?:ed|ing)?\b|"
    r"\bexplod(?:e|ed|ing)?\b|\bsmoke\b|\binjur(?:y|ed)\b|\bhospital\b|"
    r"\bdeath\b|\bdied\b|\bfatal\b|起火|着火|烧伤|过热|爆炸|冒烟|受伤|住院|死亡|致死",
    re.IGNORECASE,
)
_FIRST_PERSON = re.compile(
    r"\b(?:i|i'm|i've|me|my|mine|we|our|us)\b|(?:^|[\s，。！；])我(?:的|们)?",
    re.IGNORECASE,
)
_PROCESS = re.compile(
    r"\brefund\b|\breturn\b|\breimbursement\b|\bgift card\b|\bserial number\b|"
    r"\bclaim form\b|\brecall form\b|\bdisposal\b|退款|退货|礼品卡|序列号|表单|处理流程|回收处理",
    re.IGNORECASE,
)
_MISINFORMATION = re.compile(
    r"\bscam\b|\bfake recall\b|\bmisinformation\b|\bfalse(?:ly)? reported\b|"
    r"诈骗|假召回|错误信息|谣言|虚假",
    re.IGNORECASE,
)


def enrich_topic_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Add an explainable, display-only topic axis without rewriting evidence.

    ``analysis_results.category`` remains the historical model/rule label.  This
    layer is deliberately deterministic so the dashboard can distinguish legal
    action from an injury mentioned in a recall repost, even for older rows.
    """
    enriched = dict(row)
    text = "\n".join(
        str(enriched.get(field) or "")
        for field in ("title", "raw_text", "text_excerpt", "summary_zh")
    )
    legal = bool(_LEGAL_ACTION.search(text))
    recall = bool(_RECALL_OR_REGULATOR.search(text))
    incident = bool(_INCIDENT.search(text))
    first_person = bool(_FIRST_PERSON.search(text))
    process = bool(_PROCESS.search(text))
    misinformation = bool(_MISINFORMATION.search(text))

    # Legal action needs an explicit legal marker.  A recall release reporting
    # historic injuries/death remains recall/regulatory propagation, while a
    # first-person incident becomes an incident claim for human review.
    if legal:
        primary = "legal_action"
    elif first_person and incident:
        primary = "incident_claim"
    elif recall:
        primary = "recall_regulatory"
    elif process:
        primary = "customer_process"
    elif misinformation:
        primary = "misinformation_scam"
    elif incident:
        primary = "product_safety"
    else:
        primary = "other"

    labels = []
    if recall:
        labels.append("recall_regulatory")
    if legal:
        labels.append("legal_action")
    if incident:
        labels.append("incident_claim")
    if process:
        labels.append("customer_process")
    if misinformation:
        labels.append("misinformation_scam")
    enriched.update(
        {
            "topic_primary": primary,
            "topic_primary_label": TOPIC_LABELS[primary],
            "topic_flags": labels,
            "has_first_person_incident": first_person and incident,
        }
    )
    return enriched


def is_public_social_parent_post(row: Dict[str, Any]) -> bool:
    """Whether a discovered public post can be opened for human comment review.

    This is intentionally a worklist eligibility check, not a claim that the
    system collected comments, account data, or engagement from the platform.
    """
    platform = str(row.get("platform") or "").casefold()
    content_type = str(row.get("content_type") or "").casefold()
    return platform in {"facebook", "instagram", "tiktok", "youtube", "x"} and content_type in {
        "post",
        "video",
    }
