"""Human-only intervention routing.

The monitor may recommend a next review step, but it never publishes, replies,
or uses a platform account.  News reposts default to monitoring rather than a
response task.
"""
from __future__ import annotations

from typing import Any, Dict

from .operational import is_excluded_false_positive
from .topics import is_public_social_parent_post


INTERVENTION_STATUSES = (
    "待分流",
    "仅监测",
    "人工查看评论",
    "建议回应",
    "升级 PR/法务",
    "已归档",
)

HUMAN_INTERVENTION_STATUSES = {"待分流", "人工查看评论", "建议回应", "升级 PR/法务"}


def inferred_intervention_status(row: Dict[str, Any]) -> str:
    """Conservative default when an operator has not chosen a route yet."""
    if is_excluded_false_positive(row):
        return "已归档"
    recommended = str(row.get("recommended_action") or "monitor")
    if recommended == "escalate_pr_legal":
        return "升级 PR/法务"
    if is_public_social_parent_post(row):
        return "人工查看评论"
    if recommended in {"suggest_response", "review_for_response"}:
        return "建议回应"
    if bool(row.get("needs_human_review")) and not bool(row.get("evidence_check_passed")):
        return "待分流"
    # Press-release reporting, ordinary news syndication, and routine public
    # links are evidence to watch; they are not an instruction to reply.
    return "仅监测"


def intervention_status(row: Dict[str, Any]) -> str:
    saved = str(row.get("intervention_status") or "")
    return saved if saved in INTERVENTION_STATUSES else inferred_intervention_status(row)


def requires_human_intervention(row: Dict[str, Any]) -> bool:
    return intervention_status(row) in HUMAN_INTERVENTION_STATUSES
