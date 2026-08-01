from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import Request, urlopen


def short_markdown_link(value: object) -> str:
    """Render a safe, compact visible label for an external HTTP(S) URL."""
    url = str(value or "").strip()
    if not url.startswith(("https://", "http://")):
        return "无公开链接"
    encoded = quote(url, safe=":/?#[]@!$&'+,;=%")
    return f"[链接]({encoded})"


class DeliveryChannel(ABC):
    @abstractmethod
    def send_alert(self, payload: Dict[str, object]) -> Optional[str]:
        raise NotImplementedError


class GenericWebhookChannel(DeliveryChannel):
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


class DingTalkRobotChannel(DeliveryChannel):
    def __init__(
        self,
        webhook_url: str,
        secret: str = "",
        at_mobiles: Optional[List[str]] = None,
        rate_limit_per_minute: int = 20,
        timeout_seconds: int = 10,
    ):
        self.webhook_url = webhook_url
        self.secret = secret
        self.at_mobiles = at_mobiles or []
        self.rate_limit_per_minute = rate_limit_per_minute
        self.timeout_seconds = timeout_seconds
        self._sent_timestamps: List[float] = []

    def send_alert(self, payload: Dict[str, object]) -> Optional[str]:
        if not self.webhook_url:
            return None
        self._respect_rate_limit()
        text = str(payload.get("text", ""))
        title = str(payload.get("title", "Ocoopa 舆情警报"))
        body = {
            "msgtype": "markdown",
            "markdown": {
                "title": title[:60],
                "text": text,
            },
            "at": {
                "atMobiles": []
                if (payload.get("needs_human_review") or payload.get("suppress_at"))
                else self.at_mobiles,
                "isAtAll": False,
            },
        }
        request = Request(
            self._signed_url(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            raw_response = response.read()
        try:
            result = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("DingTalk returned a non-JSON response") from exc
        if int(result.get("errcode", -1)) != 0:
            raise RuntimeError(
                f"DingTalk rejected message: errcode={result.get('errcode')} "
                f"errmsg={result.get('errmsg', '')}"
            )
        self._sent_timestamps.append(time.time())
        return self.webhook_url

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook_url
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
        separator = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{separator}{urlencode({'timestamp': timestamp})}&sign={sign}"

    def _respect_rate_limit(self) -> None:
        now = time.time()
        self._sent_timestamps = [sent for sent in self._sent_timestamps if now - sent < 60]
        if len(self._sent_timestamps) < self.rate_limit_per_minute:
            return
        sleep_for = 60 - (now - self._sent_timestamps[0])
        if sleep_for > 0:
            time.sleep(sleep_for)


class DeliveryClient:
    def __init__(self, channel: DeliveryChannel):
        self.channel = channel

    @classmethod
    def from_settings(cls, settings) -> "DeliveryClient":
        at_mobiles = [item.strip() for item in settings.alert_at_mobiles.split(",") if item.strip()]
        if settings.alert_channel == "dingtalk":
            return cls(
                DingTalkRobotChannel(
                    webhook_url=settings.alert_webhook_url,
                    secret=settings.alert_webhook_secret,
                    at_mobiles=at_mobiles,
                    rate_limit_per_minute=settings.alert_rate_limit_per_minute,
                    timeout_seconds=settings.request_timeout_seconds,
                )
            )
        return cls(GenericWebhookChannel(settings.alert_webhook_url, settings.request_timeout_seconds))

    def send_alert(self, payload: Dict[str, object]) -> Optional[str]:
        return self.channel.send_alert(payload)

    def send_text(self, title: str, text: str, suppress_at: bool = True) -> Optional[str]:
        """Send a non-alert message (daily report, source-health notice).

        suppress_at=True (default) avoids @-ing the on-call for routine pushes
        like the daily report; set False for urgent notices (e.g. P0 source down).
        """
        return self.channel.send_alert({"title": title, "text": text, "suppress_at": suppress_at})

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
        delivery_latency_seconds: Optional[int] = None,
        notification_priority: str = "urgent",
    ) -> Dict[str, object]:
        prefix = "【红色舆情警报】" if risk_level == "red" else "【舆情提醒】"
        review_note = "\n\n**需人工核实：证据校验未完全通过，勿作为确证事实外传。**" if needs_human_review else ""
        evidence_note = "证据已校验" if evidence_check_passed else "证据未完全校验"
        text = (
            f"### {prefix}\n\n"
            f"- 标题：{title}\n"
            f"- 原文：{short_markdown_link(url)}\n"
            f"- 风险原因：{reason}\n"
            f"- 证据状态：{evidence_note}\n"
            f"- 置信度：{confidence:.2f}\n"
            f"- 端到端延迟：{delivery_latency_seconds if delivery_latency_seconds is not None else '未知'} 秒\n"
            f"- 发送时间：{sent_at.isoformat()}"
            f"{review_note}"
        )
        return {
            "text": text,
            "risk_level": risk_level,
            "title": title,
            "url": url,
            "reason": reason,
            "confidence": confidence,
            "evidence_check_passed": evidence_check_passed,
            "needs_human_review": needs_human_review,
            "delivery_latency_seconds": delivery_latency_seconds,
            "sent_at": sent_at.isoformat(),
            "notification_priority": notification_priority,
            "suppress_at": notification_priority != "urgent",
        }

    def mention_batch_payload(self, rows: List[Dict[str, object]], priority: str) -> Dict[str, object]:
        urgent = priority == "urgent"
        recall_batch = bool(rows) and all(row.get("campaign") == "recall_26_659" for row in rows)
        cluster_count = len({str(row.get("story_cluster_key")) for row in rows if row.get("story_cluster_key")})
        syndicated_count = sum(bool(row.get("is_syndicated")) for row in rows)
        substantive_count = len({
            str(row.get("story_cluster_key") or row.get("mention_id") or row.get("source_url"))
            for row in rows if row.get("has_substantive_update")
        })
        news_reposts = sum(row.get("story_role") == "news_repost" for row in rows)
        social_amplifications = sum(row.get("story_role") == "social_amplification" for row in rows)
        contains_review = any(bool(row.get("needs_human_review")) for row in rows)
        urgent_rows = [row for row in rows if row.get("notification_priority") == "urgent"]
        # A mixed batch may contain an unverified routine row alongside a
        # verified emergency. Only suppress @ when every urgent row itself
        # still needs human verification.
        urgent_needs_review = bool(urgent_rows) and all(
            bool(row.get("needs_human_review")) for row in urgent_rows
        )
        lines = [
            (
                "### 【OCOOPA 召回传播更新】"
                if recall_batch else "### 【OCOOPA 公开舆情首次发现】"
            ) + ("（紧急）" if urgent else ""),
            "",
            "#### 总览",
            f"- 本轮新增传播链接：{len(rows)} 条",
            f"- 独立传播簇：{cluster_count or len(rows)} 个",
            f"- 转载扩散：{syndicated_count} 条（新闻 {news_reposts}｜社媒 {social_amplifications}）",
            (
                f"- 新增实质信号：{substantive_count} 条，请优先核实。"
                if substantive_count
                else "- 新增实质信号：自动规则未识别到；不等同于人工核实结论。"
            ),
            f"- 处置优先级：{'紧急复核' if urgent else '常规复核'}",
            "- 全部链接均已登记；数量分为传播链接与独立事实口径。",
            "",
            "#### 重点信息",
        ]
        for index, row in enumerate(rows, 1):
            role_label = str(row.get("story_role_label") or "独立讨论")
            metrics = "｜".join(
                f"{label}{row.get(key)}"
                for key, label in (
                    ("view_count", "浏览 "), ("like_count", "赞 "),
                    ("comment_count", "评 "), ("share_count", "转 "),
                )
                if row.get(key) is not None
            )
            lines.extend(
                [
                    f"{index}. **{str(row.get('platform') or 'web').upper()}｜{role_label}**",
                    f"   - 摘要：{str(row.get('summary_zh') or row.get('title') or '')[:220]}"
                    + ("（需人工核实）" if row.get("needs_human_review") else ""),
                    f"   - 主题：{row.get('campaign') or 'brand_major_risk'}｜建议：{row.get('recommended_action') or 'monitor'}",
                    f"   - {metrics + '｜' if metrics else ''}{short_markdown_link(row.get('source_url'))}",
                ]
            )
        lines.extend(
            [
                "",
                "#### 下一步",
                (
                    "- 先核实新增实质信号，再决定是否升级 PR/法务或进行评论引导。"
                    if substantive_count
                    else "- 当前以转载扩散为主；打开复核页抽查原文，持续观察是否出现新事实或高传播帖子。"
                ),
            ]
        )
        return {
            "title": (
                f"OCOOPA 召回传播总览（{len(rows)} 个新链接{'·紧急' if urgent else ''}）"
                if recall_batch
                else f"OCOOPA 公开舆情（{len(rows)} 条{'·紧急' if urgent else ''}）"
            ),
            "text": "\n".join(lines),
            "suppress_at": not urgent or urgent_needs_review,
            "needs_human_review": urgent_needs_review,
            "contains_human_review": contains_review,
            "notification_priority": priority,
        }
