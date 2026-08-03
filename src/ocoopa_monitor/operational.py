"""Shared reporting-scope rules for reviewed monitoring records.

An item marked as a false positive remains in the evidence store for audit,
but must never inflate an operational metric, trend, digest, or queue.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


def is_excluded_false_positive(row: Dict[str, Any]) -> bool:
    """Return true only for an explicit, human-reviewed false positive."""
    return (
        str(row.get("review_status") or "") == "false_positive"
        or str(row.get("incident_status") or "") == "resolved"
    )


def operational_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep records that count toward current operational reporting."""
    data = [dict(row) for row in rows]
    return [row for row in data if not is_excluded_false_positive(row)]


def has_pending_evidence_review(row: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether an algorithmic evidence flag is still an active human task."""
    if not bool(row.get("needs_human_review")):
        return False
    status = str(row.get("incident_status") or "active")
    if status in {"monitoring", "resolved"}:
        return False
    if status != "muted":
        return True
    raw_until = row.get("incident_muted_until") or row.get("muted_until")
    if not raw_until:
        return False
    try:
        muted_until = raw_until if isinstance(raw_until, datetime) else datetime.fromisoformat(str(raw_until).replace("Z", "+00:00"))
        if muted_until.tzinfo is None:
            muted_until = muted_until.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return muted_until <= current
    except (TypeError, ValueError):
        return True
