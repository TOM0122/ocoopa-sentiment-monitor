from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from urllib.request import Request, urlopen

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


class DeepSeekProvider(LLMProvider):
    provider_name = "deepseek"

    def __init__(
        self,
        api_key: str,
        model_name: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: int = 30,
    ):
        if not api_key:
            raise ValueError("OCOOPA_LLM_API_KEY is required for DeepSeekProvider")
        self.api_key = api_key
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def analyze(self, mention: Mention, rule_decision: RuleDecision) -> Dict[str, object]:
        payload = {
            "model": self.model_name,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是企业内部舆情监控系统的结构化分析器。"
                        "只能基于用户提供的原文、标题、URL、来源和规则命中信息判断。"
                        "不得补充外部知识，不得创造来源，不得推断输入中没有的事实。"
                        "如果证据不足，必须降低置信度并标注需要人工核实。"
                        "只输出 JSON，不输出 Markdown。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "required_schema": {
                                "sentiment": "positive|neutral|negative",
                                "risk_level": "red|yellow|green",
                                "category": (
                                    "lawsuit|recall|media_report|user_complaint|"
                                    "promotion|kol_review|other"
                                ),
                                "summary_zh": "一句中文摘要，只可引用输入事实",
                                "key_quotes": ["必须是 raw_text 的逐字子串"],
                                "requires_escalation": "boolean",
                                "escalation_reason": "中文或英文，必须可由原文支撑",
                                "confidence": "0-1 number",
                            },
                            "red_criteria": [
                                "死亡/重伤",
                                "主流媒体报道",
                                "集体诉讼招募",
                                "CPSC 新执法或召回",
                                "政府官员/议员发声",
                                "高影响力博主负面专题",
                                "假冒退款诈骗",
                            ],
                            "rule_decision": {
                                "risk_level": rule_decision.risk_level,
                                "sentiment": rule_decision.sentiment,
                                "category": rule_decision.category,
                                "requires_escalation": rule_decision.requires_escalation,
                                "reasons": rule_decision.reasons,
                                "confidence": rule_decision.confidence,
                            },
                            "input": {
                                "title": mention.title,
                                "source_url": mention.source_url,
                                "source_name": mention.source_name,
                                "published_at": mention.published_at.isoformat()
                                if mention.published_at
                                else None,
                                "matched_keywords": mention.matched_keywords,
                                "raw_text": mention.raw_text,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return json.loads(content)
        if isinstance(content, dict):
            return content
        raise ValueError("DeepSeek response content is not JSON")


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


def provider_from_settings(settings) -> LLMProvider:
    if settings.llm_provider == "deepseek":
        return DeepSeekProvider(
            api_key=settings.llm_api_key,
            model_name=settings.llm_model,
            base_url=settings.llm_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
    return RuleOnlyProvider()
