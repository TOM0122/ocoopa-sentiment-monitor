from __future__ import annotations

from typing import Optional

from .evidence import EvidenceChecker
from .llm import LLMProvider, RuleOnlyProvider, SchemaValidator
from .models import AnalysisResult, Mention, utcnow
from .risk import RiskRuleEngine


class AnalysisService:
    def __init__(
        self,
        rule_engine: Optional[RiskRuleEngine] = None,
        provider: Optional[LLMProvider] = None,
        fallback_provider: Optional[LLMProvider] = None,
        validator: Optional[SchemaValidator] = None,
        evidence_checker: Optional[EvidenceChecker] = None,
    ):
        self.rule_engine = rule_engine or RiskRuleEngine()
        self.provider = provider or RuleOnlyProvider()
        self.fallback_provider = fallback_provider or RuleOnlyProvider()
        self.validator = validator or SchemaValidator()
        self.evidence_checker = evidence_checker or EvidenceChecker()

    def analyze(self, mention: Mention) -> AnalysisResult:
        if mention.id is None:
            raise ValueError("mention.id is required before analysis")
        rule_decision = self.rule_engine.evaluate(mention.title, mention.raw_text, mention.matched_keywords)
        try:
            payload = self.validator.validate(self.provider.analyze(mention, rule_decision))
            provider = self.provider
            fallback_note = ""
        except Exception:
            payload = self.validator.validate(self.fallback_provider.analyze(mention, rule_decision))
            provider = self.fallback_provider
            fallback_note = "llm_provider_failed_fallback_rule_only; "
        evidence = self.evidence_checker.check(
            raw_text=mention.raw_text,
            source_url=mention.source_url,
            summary_zh=str(payload["summary_zh"]),
            escalation_reason=str(payload["escalation_reason"]),
            key_quotes=[str(item) for item in payload["key_quotes"]],
            confidence=float(payload["confidence"]),
            risk_level=str(payload["risk_level"]),
        )
        needs_human_review = evidence.needs_human_review
        if str(payload["risk_level"]) == "red" and not evidence.passed:
            needs_human_review = True
        return AnalysisResult(
            mention_id=mention.id,
            model_provider=provider.provider_name,
            model_name=provider.model_name,
            prompt_version="m1-rule-first-v1",
            sentiment=str(payload["sentiment"]),
            risk_level=str(payload["risk_level"]),
            category=str(payload["category"]),
            summary_zh=str(payload["summary_zh"]),
            key_quotes=evidence.valid_quotes,
            key_quote_offsets=evidence.offsets,
            requires_escalation=bool(payload["requires_escalation"]),
            escalation_reason=str(payload["escalation_reason"]),
            confidence=float(payload["confidence"]),
            evidence_check_passed=evidence.passed,
            evidence_check_notes=fallback_note + evidence.notes,
            needs_human_review=needs_human_review,
            analysis_created_at=utcnow(),
        )
