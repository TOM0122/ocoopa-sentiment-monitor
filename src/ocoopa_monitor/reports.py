from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .db import Database
from .delivery import short_markdown_link


class DailyReportService:
    def __init__(self, db: Database, delivery_client=None):
        self.db = db
        self.delivery_client = delivery_client

    def generate(self, timezone_name: str = "Asia/Shanghai", date: Optional[str] = None) -> Dict[str, object]:
        tz = ZoneInfo(timezone_name)
        report_date = date or datetime.now(tz).date().isoformat()
        local_start = datetime.combine(datetime.fromisoformat(report_date).date(), time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        rows = self.db.fetch_mentions_between(
            local_start.astimezone(timezone.utc),
            local_end.astimezone(timezone.utc),
        )
        # The query is a first-discovery cohort. is_new is mutable operational
        # state and becomes false when the same URL is polled again, so it
        # cannot be used to reconstruct a historical daily-new count.
        new_mentions = sum(1 for row in rows if not row["backfill"])

        sentiment = Counter(str(row["sentiment"] or "unknown") for row in rows)
        sources = Counter(str(row["source_name"]) for row in rows)
        risks = Counter(str(row["risk_level"] or "unknown") for row in rows)
        top_risks = [
            {
                "title": row["title"],
                "source": row["source_name"],
                "url": row["source_url"],
                "risk_level": row["risk_level"],
                "summary_zh": row["summary_zh"],
                "evidence_check_passed": bool(row["evidence_check_passed"]),
                "needs_human_review": bool(row["needs_human_review"]),
            }
            for row in rows
            if row["risk_level"] in {"red", "yellow"}
        ][:5]
        recommended_actions = self._recommended_actions(top_risks)
        text = self._render(
            report_date=report_date,
            total=len(rows),
            new_mentions=new_mentions,
            backfill_mentions=sum(1 for row in rows if row["backfill"]),
            sentiment=dict(sentiment),
            sources=dict(sources),
            risks=dict(risks),
            top_risks=top_risks,
            recommended_actions=recommended_actions,
        )
        report = {
            "report_date": report_date,
            "timezone": timezone_name,
            "total_mentions": len(rows),
            "new_mentions": new_mentions,
            "backfill_mentions": sum(1 for row in rows if row["backfill"]),
            "sentiment_distribution": dict(sentiment),
            "source_distribution": dict(sources),
            "risk_distribution": dict(risks),
            "top_risks": top_risks,
            "trend_vs_yesterday": {},
            "recommended_actions": recommended_actions,
            "generated_text_zh": text,
            "delivery_status": self._deliver(report_date, text),
        }
        self.db.insert_daily_report(report)
        return report

    def _deliver(self, report_date: str, text: str) -> str:
        if not self.delivery_client:
            return "stored"
        try:
            sent_to = self.delivery_client.send_text(
                title=f"Ocoopa 舆情日报 {report_date}", text=text, suppress_at=True
            )
        except Exception:
            return "failed"
        return "delivered" if sent_to else "stored"

    @staticmethod
    def _recommended_actions(top_risks: List[Dict[str, object]]) -> List[str]:
        if not top_risks:
            return ["今日未发现红/黄级新增风险，继续监控高敏车道与 CPSC 源健康。"]
        actions = []
        if any(item["risk_level"] == "red" for item in top_risks):
            actions.append("请 PR/法务优先复核红色风险原文链接与证据校验状态。")
        if any(item.get("needs_human_review") for item in top_risks):
            actions.append("存在需人工核实条目，未通过证据校验内容不得作为确证事实外传。")
        actions.append("检查是否出现主流媒体、CPSC 或集体诉讼招募的新源头。")
        return actions

    @staticmethod
    def _render(
        report_date: str,
        total: int,
        new_mentions: int,
        backfill_mentions: int,
        sentiment: Dict[str, int],
        sources: Dict[str, int],
        risks: Dict[str, int],
        top_risks: List[Dict[str, object]],
        recommended_actions: List[str],
    ) -> str:
        conclusions = DailyReportService._conclusions(
            total, new_mentions, backfill_mentions, risks, top_risks
        )
        lines = [
            f"### 【OCOOPA 舆情日报｜{report_date}】",
            "",
            "#### 总览",
            f"- 今日收录：{total} 条｜新增 {new_mentions} 条｜历史回溯 {backfill_mentions} 条",
            (
                f"- 风险分布：红 {risks.get('red', 0)}｜黄 {risks.get('yellow', 0)}"
                f"｜绿 {risks.get('green', 0)}｜未分级 {risks.get('unknown', 0)}"
            ),
            (
                "- 情感分布："
                f"{DailyReportService._format_distribution(sentiment, 4, translate_sentiment=True)}"
            ),
            f"- 主要渠道：{DailyReportService._format_distribution(sources, 4)}",
            "",
            "#### 核心结论",
        ]
        lines.extend(f"- {item}" for item in conclusions)
        lines.extend(["", "#### 重点风险"])
        if not top_risks:
            lines.append("- 暂无红/黄级风险项。")
        for index, item in enumerate(top_risks, 1):
            evidence = (
                "待核实"
                if item.get("needs_human_review") or not item.get("evidence_check_passed")
                else "已校验"
            )
            lines.append(
                f"{index}. **[{DailyReportService._risk_label(item['risk_level'])}] "
                f"{DailyReportService._compact(item['title'], 110)}**"
            )
            lines.append(
                f"   - 摘要：{DailyReportService._compact(item.get('summary_zh') or '暂无摘要', 180)}"
            )
            lines.append(
                f"   - 来源：{item.get('source') or '未知'}"
                f"｜证据：{evidence}｜{short_markdown_link(item.get('url'))}"
            )
        lines.extend(["", "#### 建议动作"])
        lines.extend(f"- {action}" for action in recommended_actions)
        return "\n".join(lines)

    @staticmethod
    def _conclusions(
        total: int,
        new_mentions: int,
        backfill_mentions: int,
        risks: Dict[str, int],
        top_risks: List[Dict[str, object]],
    ) -> List[str]:
        if risks.get("red", 0):
            lead = f"今日出现 {risks['red']} 条红色风险，需优先复核重点风险及原始证据。"
        elif risks.get("yellow", 0):
            lead = f"今日未出现红色风险，{risks['yellow']} 条黄色风险需持续跟踪。"
        elif total:
            lead = "今日未出现红/黄级风险，整体舆情暂未发现新增高危信号。"
        else:
            lead = "今日未收录相关提及，请同时确认重点数据源运行状态。"
        conclusions = [lead]
        if backfill_mentions:
            conclusions.append(
                f"其中 {backfill_mentions} 条为历史回溯，不应解读为今日新发舆情。"
            )
        review_count = sum(bool(item.get("needs_human_review")) for item in top_risks)
        if review_count:
            conclusions.append(
                f"重点风险中有 {review_count} 条待人工核实，核验前不得作为确证事实外传。"
            )
        elif new_mentions:
            conclusions.append("新增内容已统一登记，继续观察是否形成跨平台扩散。")
        return conclusions

    @staticmethod
    def _format_distribution(
        values: Dict[str, int], limit: int, translate_sentiment: bool = False
    ) -> str:
        if not values:
            return "暂无"
        ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
        labels = {
            "negative": "负面",
            "neutral": "中性",
            "positive": "正面",
            "unknown": "未判定",
        }
        visible = [
            (
                f"{labels.get(name, name) if translate_sentiment else DailyReportService._compact(name, 28)}"
                f" {count}"
            )
            for name, count in ordered[:limit]
        ]
        remainder = sum(count for _, count in ordered[limit:])
        if remainder:
            visible.append(f"其他 {remainder}")
        return "｜".join(visible)

    @staticmethod
    def _compact(value: object, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"

    @staticmethod
    def _risk_label(value: object) -> str:
        return {
            "red": "红色",
            "yellow": "黄色",
            "green": "绿色",
        }.get(str(value or "").lower(), "未分级")
