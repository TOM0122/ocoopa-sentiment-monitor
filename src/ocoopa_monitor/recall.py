from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .delivery import short_markdown_link
from .models import RawItem, utcnow

CAMPAIGN_KEY = "ocoopa-cpsc-26-659"
AFFECTED_MODELS = ("UT3053", "UT3056", "ZLS-118", "ZLS-118S", "ZLS-118D", "H01", "H01(PD)")


def is_current_recall(title: str, raw_text: str) -> bool:
    text = f"{title}\n{raw_text}".lower()
    brand = any(term in text for term in ("ocoopa", "shenzhen street cat technology"))
    hand_warmer = any(term in text for term in ("hand warmer", "handwarmer", "暖手宝"))
    distinctive_counts = bool(
        re.search(r"\b1[.,]?5\s*(?:m|million)\b", text)
        or re.search(r"\b1[,\s]?480\b", text)
        or re.search(r"\b350\b.{0,30}\bburn", text)
    )
    identity = (
        "26-659" in text
        or any(model.lower() in text for model in AFFECTED_MODELS)
        or (distinctive_counts and hand_warmer)
    )
    recall_context = any(
        term in text
        for term in ("recall", "recalled", "product recall", "cpsc", "召回", "fire hazard", "burn hazard")
    )
    return recall_context and (
        (brand and (identity or hand_warmer))
        or (identity and hand_warmer)
    )


class RecallRegistryService:
    def __init__(self, db, pipeline):
        self.db = db
        self.pipeline = pipeline

    def import_group(self, input_path: str) -> Dict[str, int]:
        path = Path(input_path)
        rows = list(self._read_rows(path))
        stats = {"rows": len(rows), "imported": 0, "duplicates": 0, "irrelevant": 0}
        keyword_terms = [kw.term for kw in self.db.get_keywords()]
        for row in rows:
            title = (row.get("title") or "").strip()
            content = (row.get("content") or row.get("raw_text") or "").strip()
            if not is_current_recall(title, content):
                stats["irrelevant"] += 1
                continue
            source_url = (row.get("source_url") or row.get("url") or "").strip()
            if not source_url:
                digest = hashlib.sha256(
                    json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()[:24]
                source_url = f"group-import://{digest}"
            raw = RawItem(
                source_type=(row.get("platform") or "group").strip(),
                source_name=(row.get("source_name") or "dingtalk_group_import").strip(),
                source_url=source_url,
                title=title or source_url,
                raw_text=content or title,
                published_at=_parse_datetime(row.get("published_at")),
                author_or_publisher=(row.get("author_or_publisher") or row.get("author") or None),
                language=row.get("language") or None,
                country_or_market=row.get("country_or_market") or None,
                tos_method="manual_group_import",
            )
            mention = self.pipeline._build_mention(raw, keyword_terms, backfill=True)
            stored = self.db.upsert_mention(mention)
            if not stored.is_new and not stored.is_updated:
                stats["duplicates"] += 1
            else:
                analysis = self.pipeline.analysis_service.analyze(stored)
                self.db.insert_analysis(analysis)
                self.db.upsert_incident_group(stored, analysis)
                stats["imported"] += 1
            self.db.upsert_recall_mention(
                stored.id,
                origin="group_import",
                sync_status="synced",
                group_synced_at=_parse_datetime(row.get("group_synced_at")) or utcnow(),
            )
        return stats

    def export_csv(self, output_path: str, limit: int = 5000) -> int:
        rows = self.db.list_recall_mentions(limit)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "recall_record_id", "campaign_key", "origin", "sync_status",
            "source_type", "source_name", "title", "source_url",
            "author_or_publisher", "published_at", "first_discovered_at", "last_seen_at",
            "group_synced_at", "synced_at", "risk_level", "sentiment", "category",
            "summary_zh", "confidence", "evidence_check_passed", "needs_human_review",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _csv_safe(row.get(key)) for key in fields})
        return len(rows)

    def queue_pending(self, limit: int = 10) -> int:
        # Retire pending one-item jobs created by the previous release. Their
        # registry rows remain pending and are folded into this summary.
        self.db.supersede_pending_recall_updates()
        # Keep exactly one retryable digest in flight. Newly discovered rows
        # wait for the next scheduler tick instead of being duplicated across
        # overlapping summaries.
        if self.db.has_pending_delivery("recall_digest"):
            return 0
        rows = []
        for row in self.db.list_recall_mentions(limit=5000):
            if (
                row.get("sync_status") != "pending"
                or row.get("has_alert")
                or row.get("has_mention_notification")
                or row.get("backfill")
                or self.db.is_incident_suppressed(str(row.get("event_fingerprint") or ""))
            ):
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        if not rows:
            return 0
        record_ids = sorted(int(row["recall_record_id"]) for row in rows)
        digest = hashlib.sha256(",".join(map(str, record_ids)).encode("ascii")).hexdigest()[:20]
        queued = self.db.enqueue_delivery(
            kind="recall_digest",
            dedupe_key=f"recall-digest:{digest}",
            entity_type="recall_batch",
            entity_id=0,
            payload=recall_digest_payload(rows),
        )
        return len(rows) if queued else 0

    def backfill_existing(self, *, assume_group_reported: bool = True) -> Dict[str, int]:
        """Register recall mentions already present before this feature shipped.

        Existing production mentions appeared in the team's daily report, so
        the rollout migration can mark them synced and avoid replaying a large
        historical notification storm. New mentions remain pending and use the
        normal immediate delivery path.
        """
        rows = self.db.fetch_mentions_between(
            datetime(2018, 1, 1, tzinfo=timezone.utc),
            utcnow() + timedelta(days=1),
        )
        stats = {"scanned": len(rows), "registered": 0}
        for row in rows:
            if not is_current_recall(str(row["title"]), str(row["raw_text"])):
                continue
            self.db.upsert_recall_mention(
                int(row["id"]),
                origin="external",
                sync_status="synced" if assume_group_reported else "pending",
                group_synced_at=utcnow() if assume_group_reported else None,
            )
            stats["registered"] += 1
        return stats

    @staticmethod
    def _read_rows(path: Path) -> Iterable[Dict[str, str]]:
        if path.suffix.lower() in {".jsonl", ".ndjson"}:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)
            return
        with path.open(encoding="utf-8-sig", newline="") as handle:
            yield from csv.DictReader(handle)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _csv_safe(value):
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def recall_digest_payload(rows: List[Dict[str, object]]) -> Dict[str, object]:
    risks = Counter(str(row.get("risk_level") or "unrated") for row in rows)
    sources = Counter(str(row.get("source_type") or "unknown") for row in rows)
    needs_review = sum(bool(row.get("needs_human_review")) for row in rows)
    source_summary = "、".join(
        f"{_source_label(name)} {count} 条" for name, count in sources.most_common(4)
    ) or "暂无"
    if risks.get("red"):
        conclusion = f"本批包含 {risks['red']} 条红色风险，需优先查看重点信息。"
    elif risks.get("yellow"):
        conclusion = f"本批以跟进观察为主，包含 {risks['yellow']} 条黄色风险。"
    else:
        conclusion = "本批未出现新增红色风险，继续观察传播变化。"
    lines = [
        "### 【OCOOPA 召回舆情总览】",
        "",
        "#### 总览",
        f"- 本次新增：{len(rows)} 条",
        (
            f"- 风险分布：红 {risks.get('red', 0)}｜黄 {risks.get('yellow', 0)}"
            f"｜绿 {risks.get('green', 0)}｜未分级 {risks.get('unrated', 0)}"
        ),
        f"- 渠道分布：{source_summary}",
        f"- 待人工核实：{needs_review} 条",
        "",
        "#### 核心结论",
        f"- {conclusion}",
        "- 多渠道信息已统一归入本次总览，完整记录保留在召回统计表。",
        "",
        "#### 重点信息",
    ]
    for index, row in enumerate(rows[:8], 1):
        risk = _risk_label(row.get("risk_level"))
        title = _compact(row.get("title"), 110)
        summary = _compact(row.get("summary_zh") or row.get("text_excerpt") or "暂无摘要", 180)
        evidence = (
            "待核实"
            if row.get("needs_human_review") or not row.get("evidence_check_passed")
            else "已校验"
        )
        lines.extend(
            [
                f"{index}. **[{risk}] {title}**",
                f"   - 摘要：{summary}",
                (
                    f"   - 来源：{row.get('source_name') or '未知'}"
                    f"｜证据：{evidence}｜{short_markdown_link(row.get('source_url'))}"
                ),
            ]
        )
    if len(rows) > 8:
        lines.append(f"- 另有 {len(rows) - 8} 条已登记至统计表，本次不逐条展开。")
    lines.extend(
        [
            "",
            "#### 建议动作",
            "- 优先复核红色及“待人工核实”条目；未完成证据核验前不得作为确证事实外传。",
            "- 继续关注主流媒体、监管机构及社交平台是否出现新的独立信号。",
        ]
    )
    return {
        "title": f"OCOOPA 召回舆情总览（新增 {len(rows)} 条）",
        "text": "\n".join(lines),
        "suppress_at": True,
        "needs_human_review": needs_review > 0,
        "_recall_record_ids": [int(row["recall_record_id"]) for row in rows],
    }


def _compact(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _risk_label(value: object) -> str:
    return {
        "red": "红色",
        "yellow": "黄色",
        "green": "绿色",
        "unrated": "未分级",
    }.get(str(value or "unrated").lower(), "未分级")


def _source_label(value: object) -> str:
    return {
        "news": "新闻",
        "social": "社交平台",
        "cpsc": "监管机构",
        "legal": "法律信息",
        "search": "网页搜索",
        "group": "群内同步",
        "unknown": "其他",
    }.get(str(value or "unknown").lower(), _compact(value, 24) or "其他")
