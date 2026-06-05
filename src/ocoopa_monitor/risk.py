from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List


RED_PATTERNS = {
    "death_or_serious_injury": [
        r"\bdeath\b",
        r"\bfatal\b",
        r"\bdied\b",
        r"\bwrongful death\b",
        r"\bserious injury\b",
        r"\b重伤\b",
        r"\b致死\b",
        r"\b死亡\b",
    ],
    "regulatory": [r"\bCPSC\b", r"\brecall\b", r"\b召回\b"],
    "lawsuit": [r"\blawsuit\b", r"\bclass action\b", r"\bproduct liability\b", r"\bsettlement\b", r"\b诉讼\b"],
    "safety": [r"\bfire\b", r"\bburn\b", r"\boverheat\b", r"\bexplode\b", r"\b起火\b", r"\b爆炸\b"],
    "channel": [r"\bAmazon\b.*\b(Ocoopa|removed|delisted)\b", r"\bdelisted\b", r"\bremoved\b", r"\b下架\b"],
    "fraud": [r"\brefund scam\b", r"\bscam\b", r"\b诈骗\b"],
}

YELLOW_PATTERNS = {
    "complaint": [r"\bcomplaint\b", r"\bunsafe\b", r"\bdanger\b", r"\bproblem\b", r"\b投诉\b", r"\b危险\b"],
    "review": [r"\breview\b", r"\bYouTube\b", r"\bTikTok\b", r"\b测评\b"],
}

NEGATION_HINTS = [
    "no recall",
    "not recalled",
    "no lawsuit",
    "not a lawsuit",
    "did not catch fire",
    "没有召回",
    "未召回",
    "没有起火",
]


@dataclass
class RuleDecision:
    risk_level: str
    sentiment: str
    category: str
    requires_escalation: bool
    reasons: List[str]
    confidence: float


class RiskRuleEngine:
    def evaluate(self, title: str, raw_text: str, matched_keywords: Iterable[str]) -> RuleDecision:
        text = f"{title}\n{raw_text}"
        lowered = text.lower()
        reasons: List[str] = []
        red_hits = []
        yellow_hits = []

        for reason, patterns in RED_PATTERNS.items():
            if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
                red_hits.append(reason)
        for reason, patterns in YELLOW_PATTERNS.items():
            if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
                yellow_hits.append(reason)

        negated = any(hint in lowered for hint in NEGATION_HINTS)

        if red_hits and negated and "death_or_serious_injury" not in red_hits:
            reasons.extend([f"negated:{hit}" for hit in red_hits])
            return RuleDecision("yellow", "neutral", self._category(red_hits), False, reasons, 0.62)
        if red_hits:
            reasons.extend(red_hits)
            return RuleDecision("red", "negative", self._category(red_hits), True, reasons, 0.86)
        if yellow_hits:
            reasons.extend(yellow_hits)
            return RuleDecision("yellow", "negative", self._category(yellow_hits), False, reasons, 0.68)
        if matched_keywords:
            return RuleDecision("green", "neutral", "other", False, ["keyword_match_only"], 0.55)
        return RuleDecision("green", "neutral", "other", False, ["no_risk_signal"], 0.45)

    @staticmethod
    def _category(reasons: Iterable[str]) -> str:
        reason_set = set(reasons)
        if {"lawsuit", "death_or_serious_injury"} & reason_set:
            return "lawsuit"
        if "regulatory" in reason_set:
            return "recall"
        if "safety" in reason_set:
            return "user_complaint"
        if "review" in reason_set:
            return "kol_review"
        if "fraud" in reason_set:
            return "other"
        return "other"
