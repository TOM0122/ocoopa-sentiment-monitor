"""Shared reporting-scope rules for reviewed monitoring records.

An item marked as a false positive remains in the evidence store for audit,
but must never inflate an operational metric, trend, digest, or queue.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List


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
