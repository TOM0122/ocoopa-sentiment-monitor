from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List

from .normalize import normalize_text


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

        reason_words = self._important_ascii_words(escalation_reason)
        summary_words = self._important_ascii_words(summary_zh)
        reason_grounded = not reason_words or any(word in raw.lower() for word in reason_words)
        summary_grounded = not summary_words or any(word in raw.lower() for word in summary_words)
        has_source = bool(source_url)
        low_confidence = confidence < 0.7

        passed = dropped == 0 and reason_grounded and summary_grounded and has_source and not low_confidence
        notes = []
        if dropped:
            notes.append(f"dropped_ungrounded_quotes={dropped}")
        if not reason_grounded:
            notes.append("escalation_reason_not_grounded")
        if not summary_grounded:
            notes.append("summary_not_grounded")
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

    @staticmethod
    def _important_ascii_words(text: str) -> List[str]:
        words = []
        for raw_word in normalize_text(text).lower().split():
            word = "".join(ch for ch in raw_word if ch.isalnum())
            if len(word) >= 5 and word not in {"requires", "because", "about", "source"}:
                words.append(word)
        return words[:8]
