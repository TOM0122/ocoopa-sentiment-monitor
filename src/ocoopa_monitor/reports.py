from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .db import Database


class DailyReportService:
    def __init__(self, db: Database):
        self.db = db

    def generate(self, timezone_name: str = "Asia/Shanghai", date: Optional[str] = None) -> Dict[str, object]:
        tz = ZoneInfo(timezone_name)
        report_date = date or datetime.now(tz).date().isoformat()
        local_start = datetime.combine(datetime.fromisoformat(report_date).date(), time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        rows = self.db.fetch_mentions_between(
            local_start.astimezone(timezone.utc),
            local_end.astimezone(timezone.utc),
        )

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
        ][:10]
        recommended_actions = self._recommended_actions(top_risks)
        text = self._render(
            report_date=report_date,
            total=len(rows),
            new_mentions=sum(1 for row in rows if row["is_new"]),
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
            "new_mentions": sum(1 for row in rows if row["is_new"]),
            "backfill_mentions": sum(1 for row in rows if row["backfill"]),
            "sentiment_distribution": dict(sentiment),
            "source_distribution": dict(sources),
            "risk_distribution": dict(risks),
            "top_risks": top_risks,
            "trend_vs_yesterday": {},
            "recommended_actions": recommended_actions,
            "generated_text_zh": text,
            "delivery_status": "stored",
        }
        self.db.insert_daily_report(report)
        return report

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
        lines = [
            f"# Ocoopa 舆情日报 {report_date}",
            "",
            f"- 今日提及总数：{total}",
            f"- 新增提及：{new_mentions}",
            f"- 历史回溯提及：{backfill_mentions}",
            f"- 情感分布：{sentiment}",
            f"- 风险分布：{risks}",
            f"- 渠道分布：{sources}",
            "",
            "## Top 风险项",
        ]
        if not top_risks:
            lines.append("- 暂无红/黄级风险项。")
        for item in top_risks:
            review = "，需人工核实" if item.get("needs_human_review") else ""
            evidence = "证据已校验" if item.get("evidence_check_passed") else "证据未完全校验"
            lines.append(f"- [{item['risk_level']}] {item['title']}（{evidence}{review}）")
            lines.append(f"  来源：{item['url']}")
            lines.append(f"  摘要：{item.get('summary_zh') or '无'}")
        lines.extend(["", "## 建议动作"])
        lines.extend(f"- {action}" for action in recommended_actions)
        return "\n".join(lines)
