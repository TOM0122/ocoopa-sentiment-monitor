from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

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


def recall_update_payload(mention, analysis) -> Dict[str, object]:
    evidence = "已校验" if analysis.evidence_check_passed else "待人工核实"
    text = (
        "### 【OCOOPA 召回舆情新增】\n\n"
        f"- 平台/来源：{mention.source_type} / {mention.source_name}\n"
        f"- 标题：{mention.title}\n"
        f"- 风险：{analysis.risk_level}（{evidence}）\n"
        f"- 摘要：{analysis.summary_zh}\n"
        f"- 原文：{mention.source_url}\n"
        f"- 发现时间：{mention.fetched_at.isoformat()}"
    )
    return {
        "title": "OCOOPA 召回舆情新增",
        "text": text,
        "suppress_at": True,
        "needs_human_review": analysis.needs_human_review,
    }


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
        queued = 0
        for row in self.db.list_recall_mentions(limit=5000):
            if row.get("sync_status") != "pending":
                continue
            record_id = int(row["recall_record_id"])
            payload = recall_update_payload_from_row(row)
            queued += int(
                self.db.enqueue_delivery(
                    kind="recall_update",
                    dedupe_key=f"recall:{row['mention_id']}",
                    entity_type="recall_mention",
                    entity_id=record_id,
                    payload=payload,
                )
            )
            if queued >= limit:
                break
        return queued

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


def recall_update_payload_from_row(row: Dict[str, object]) -> Dict[str, object]:
    evidence = "已校验" if row.get("evidence_check_passed") else "待人工核实"
    text = (
        "### 【OCOOPA 召回舆情补充登记】\n\n"
        f"- 平台/来源：{row.get('source_type')} / {row.get('source_name')}\n"
        f"- 标题：{row.get('title')}\n"
        f"- 风险：{row.get('risk_level') or '未分级'}（{evidence}）\n"
        f"- 摘要：{row.get('summary_zh') or row.get('text_excerpt') or '无'}\n"
        f"- 原文：{row.get('source_url')}\n"
        f"- 首次发现：{row.get('first_discovered_at')}"
    )
    return {
        "title": "OCOOPA 召回舆情补充登记",
        "text": text,
        "suppress_at": True,
        "needs_human_review": bool(row.get("needs_human_review")),
    }
