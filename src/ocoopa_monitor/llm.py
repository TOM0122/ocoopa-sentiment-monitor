from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List

from .models import Mention
from .risk import RuleDecision


class LLMProvider(ABC):
    provider_name: str
    model_name: str

    @abstractmethod
    def analyze(self, mention: Mention, rule_decision: RuleDecision) -> Dict[str, object]:
        raise NotImplementedError


class RuleOnlyProvider(LLMProvider):
    provider_name = "local-rule"
    model_name = "risk-rule-engine-v1"

    def analyze(self, mention: Mention, rule_decision: RuleDecision) -> Dict[str, object]:
        quotes = self._quotes(mention.raw_text, rule_decision.reasons)
        reason = "; ".join(rule_decision.reasons)
        if rule_decision.risk_level == "red":
            summary = f"发现与 {mention.title[:80]} 相关的高危公开信号，命中原因：{reason}。"
        elif rule_decision.risk_level == "yellow":
            summary = f"发现与 {mention.title[:80]} 相关的需关注公开信号，命中原因：{reason}。"
        else:
            summary = f"发现与 {mention.title[:80]} 相关的公开提及，当前未见明确高危信号。"
        return {
            "sentiment": rule_decision.sentiment,
            "risk_level": rule_decision.risk_level,
            "category": rule_decision.category,
            "summary_zh": summary,
            "key_quotes": quotes,
            "requires_escalation": rule_decision.requires_escalation,
            "escalation_reason": reason,
            "confidence": rule_decision.confidence,
        }

    @staticmethod
    def _quotes(raw_text: str, reasons: List[str]) -> List[str]:
        text = raw_text.strip()
        if not text:
            return []
        lower = text.lower()
        triggers = [
            "wrongful death",
            "class action",
            "lawsuit",
            "recall",
            "cpsc",
            "fire",
            "death",
            "fatal",
            "burn",
            "explode",
        ]
        for trigger in triggers:
            idx = lower.find(trigger)
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(text), idx + len(trigger) + 120)
                return [text[start:end].strip()]
        return [text[:240].strip()]


class SchemaValidator:
    REQUIRED = {
        "sentiment",
        "risk_level",
        "category",
        "summary_zh",
        "key_quotes",
        "requires_escalation",
        "escalation_reason",
        "confidence",
    }

    def validate(self, payload: Dict[str, object]) -> Dict[str, object]:
        missing = self.REQUIRED - payload.keys()
        if missing:
            raise ValueError(f"LLM payload missing required fields: {sorted(missing)}")
        if payload["sentiment"] not in {"positive", "neutral", "negative"}:
            raise ValueError("invalid sentiment")
        if payload["risk_level"] not in {"red", "yellow", "green"}:
            raise ValueError("invalid risk_level")
        if payload["category"] not in {
            "lawsuit",
            "recall",
            "media_report",
            "user_complaint",
            "promotion",
            "kol_review",
            "other",
        }:
            raise ValueError("invalid category")
        if not isinstance(payload["key_quotes"], list):
            raise ValueError("key_quotes must be a list")
        confidence = float(payload["confidence"])
        if confidence < 0 or confidence > 1:
            raise ValueError("confidence must be between 0 and 1")
        payload["confidence"] = confidence
        return payload
