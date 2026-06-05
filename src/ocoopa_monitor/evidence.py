from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Set

from .normalize import normalize_text

RISK_TERMS = {
    "死亡": ["死亡", "致死", "death", "fatal", "died", "dead", "wrongful death"],
    "致死": ["死亡", "致死", "death", "fatal", "wrongful death"],
    "重伤": ["重伤", "serious injury"],
    "起火": ["起火", "fire", "sparked fire", "caught fire"],
    "爆炸": ["爆炸", "explode", "explosion"],
    "召回": ["召回", "recall", "recalled"],
    "诉讼": ["诉讼", "lawsuit", "sued", "legal action"],
    "集体诉讼": ["集体诉讼", "class action"],
    "过失致死": ["过失致死", "wrongful death"],
    "产品责任": ["产品责任", "product liability"],
    "下架": ["下架", "delisted", "removed"],
    "火灾": ["火灾", "fire"],
    "烧伤": ["烧伤", "burn"],
}

STOP_TERMS = {
    "发现",
    "相关",
    "公开",
    "信号",
    "命中",
    "原因",
    "需要",
    "人工",
    "核实",
    "风险",
    "摘要",
    "内容",
    "来源",
    "当前",
    "提及",
    "高危",
    "舆情",
    "监控",
}


@dataclass
class EvidenceResult:
    passed: bool
    valid_quotes: List[str]
    offsets: List[Dict[str, int]]
    notes: str
    needs_human_review: bool


class EvidenceChecker:
    def check(
        self,
        raw_text: str,
        source_url: str,
        summary_zh: str,
        escalation_reason: str,
        key_quotes: Iterable[str],
        confidence: float,
        risk_level: str,
    ) -> EvidenceResult:
        raw = normalize_text(raw_text)
        valid_quotes: List[str] = []
        offsets: List[Dict[str, int]] = []
        dropped = 0

        for quote in key_quotes:
            quote_clean = normalize_text(quote)
            if not quote_clean:
                continue
            start = raw.find(quote_clean)
            if start >= 0:
                valid_quotes.append(quote_clean)
                offsets.append({"start": start, "end": start + len(quote_clean)})
            else:
                dropped += 1

        reason_terms = self._grounding_terms(escalation_reason)
        summary_terms = self._grounding_terms(summary_zh)
        reason_missing = sorted(term for term in reason_terms if not self._term_grounded(term, raw))
        summary_missing = sorted(term for term in summary_terms if not self._term_grounded(term, raw))
        reason_grounded = not reason_missing
        summary_grounded = not summary_missing
        has_source = bool(source_url)
        low_confidence = confidence < 0.7

        passed = dropped == 0 and reason_grounded and summary_grounded and has_source and not low_confidence
        notes = []
        if dropped:
            notes.append(f"dropped_ungrounded_quotes={dropped}")
        if not reason_grounded:
            notes.append(f"escalation_reason_not_grounded={','.join(reason_missing[:8])}")
        if not summary_grounded:
            notes.append(f"summary_not_grounded={','.join(summary_missing[:8])}")
        if not has_source:
            notes.append("missing_source_url")
        if low_confidence:
            notes.append("low_confidence")
        if not notes:
            notes.append("passed")

        return EvidenceResult(
            passed=passed,
            valid_quotes=valid_quotes,
            offsets=offsets,
            notes="; ".join(notes),
            needs_human_review=(not passed and risk_level == "red") or low_confidence,
        )

    def _grounding_terms(self, text: str) -> Set[str]:
        clean = normalize_text(text)
        terms: Set[str] = set()
        terms.update(self._numbers(clean))
        terms.update(self._ascii_entities(clean))
        terms.update(self._chinese_terms(clean))
        for canonical, aliases in RISK_TERMS.items():
            if canonical in clean or any(alias.lower() in clean.lower() for alias in aliases):
                terms.add(canonical)
        return {term for term in terms if term and term not in STOP_TERMS}

    @staticmethod
    def _numbers(text: str) -> Set[str]:
        return set(re.findall(r"\b\d+(?:[,.]\d+)*(?:\.\d+)?%?\b", text))

    @staticmethod
    def _ascii_entities(text: str) -> Set[str]:
        terms: Set[str] = set()
        for match in re.findall(r"\b[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*)*\b", text):
            if len(match) >= 3:
                terms.add(match.strip())
        for match in re.findall(r"\b(?:CPSC|Ocoopa|Ocopa|Amazon|DeepSeek|SerpAPI|GNews)\b", text, flags=re.I):
            terms.add(match.strip())
        return terms

    @staticmethod
    def _chinese_terms(text: str) -> Set[str]:
        terms: Set[str] = set()
        entity_patterns = [
            r"[\u4e00-\u9fffA-Za-z0-9-]{2,}(?:公司|委员会|法院|律所|律师|集团|部门|机构)",
            r"(?:亚马逊|美国消费品安全委员会|消费品安全委员会|加州|CPSC|Ocoopa)[\u4e00-\u9fffA-Za-z0-9-]{0,8}",
            r"[\u4e00-\u9fffA-Za-z0-9-]{0,8}(?:暖手宝|充电宝|移动电源|型号|案号)",
        ]
        for pattern in entity_patterns:
            for term in re.findall(pattern, text):
                if 2 <= len(term) <= 18 and term not in STOP_TERMS:
                    terms.add(term)
        return terms

    def _term_grounded(self, term: str, raw: str) -> bool:
        if term in raw:
            return True
        lowered_raw = raw.lower()
        if term.lower() in lowered_raw:
            return True
        aliases = RISK_TERMS.get(term, [])
        return any(alias.lower() in lowered_raw for alias in aliases)
