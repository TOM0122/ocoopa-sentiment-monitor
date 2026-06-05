from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, Optional
from urllib.request import Request, urlopen


class DeliveryClient:
    def __init__(self, webhook_url: str = "", timeout_seconds: int = 10):
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds

    def send_alert(self, payload: Dict[str, object]) -> Optional[str]:
        if not self.webhook_url:
            return None
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            response.read()
        return self.webhook_url

    def alert_payload(
        self,
        title: str,
        url: str,
        risk_level: str,
        reason: str,
        confidence: float,
        evidence_check_passed: bool,
        needs_human_review: bool,
        sent_at: datetime,
    ) -> Dict[str, object]:
        prefix = "【红色舆情警报】" if risk_level == "red" else "【舆情提醒】"
        review_note = "需人工核实；" if needs_human_review else ""
        evidence_note = "证据已校验" if evidence_check_passed else "证据未完全校验"
        return {
            "text": (
                f"{prefix}{review_note}{title}\n"
                f"风险原因：{reason}\n"
                f"置信度：{confidence:.2f}；{evidence_note}\n"
                f"来源：{url}"
            ),
            "risk_level": risk_level,
            "title": title,
            "url": url,
            "reason": reason,
            "confidence": confidence,
            "evidence_check_passed": evidence_check_passed,
            "needs_human_review": needs_human_review,
            "sent_at": sent_at.isoformat(),
        }

