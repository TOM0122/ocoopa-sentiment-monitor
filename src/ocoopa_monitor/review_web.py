from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
import hmac
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .db import REVIEW_STATUSES
from .models import utcnow

# Review operates on INCIDENTS (event_fingerprint), not on alerts: every
# red/yellow event is reviewable here, including ones absorbed silently during
# cold-start backfill that never produced a real-time alert.

# Buttons offered per incident row: (review status, label, mute-days).
_ACTIONS = [
    ("confirmed", "确认", None),
    ("false_positive", "误报", None),
    ("muted", "静音7天", 7),
]


def token_ok(configured: str, provided: str) -> bool:
    """If no token is configured the page is open (dev); otherwise it must match."""
    if not configured:
        return True
    return bool(provided) and hmac.compare_digest(provided, configured)


def _mark_url(incident_id: int, status: str, days: Optional[int], token: str) -> str:
    params: Dict[str, Any] = {"incident_id": incident_id, "status": status}
    if days is not None:
        params["days"] = days
    if token:
        params["token"] = token
    return "/review/mark?" + urlencode(params)


def render_review_page(incidents: List[Dict[str, Any]], token: str = "") -> str:
    rows: List[str] = []
    for it in incidents:
        handled = it.get("status") in {"resolved", "muted"}
        review = " · <b>需人工核实</b>" if it.get("needs_human_review") else ""
        status_note = f" · 已处理（{escape(str(it.get('status')))}）" if handled else ""
        spread = f"{it.get('mention_count') or 1} 条 / {it.get('source_count') or 1} 源"
        buttons = " ".join(
            f'<form method="post" action="{_mark_url(it["incident_id"], status, days, token)}" '
            f'style="display:inline">'
            f'<button type="submit">{escape(label)}</button></form>'
            for status, label, days in _ACTIONS
        )
        rows.append(
            "<li>"
            f'<b>[{escape(str(it.get("risk_level_max")))}]</b> '
            f'{escape(str(it.get("title") or it.get("primary_topic") or ""))}'
            f'{review}{status_note} <small>({spread})</small><br>'
            f'{escape(str(it.get("summary_zh") or ""))}<br>'
            f'<a href="{escape(str(it.get("source_url") or ""))}" target="_blank">'
            f'{escape(str(it.get("source_url") or ""))}</a><br>'
            f"{buttons}"
            "</li>"
        )
    body = "<ul>" + "".join(rows) + "</ul>" if rows else "<p>暂无红/黄事件。</p>"
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Ocoopa 舆情复核</title>"
        "<style>body{font-family:sans-serif;max-width:760px;margin:1rem auto;padding:0 1rem}"
        "li{margin:0 0 1rem;padding:.6rem;border:1px solid #ddd;border-radius:6px;list-style:none}"
        "button{margin-right:.4rem;padding:.3rem .7rem}ul{padding:0}small{color:#888}</style></head>"
        "<body><h2>Ocoopa 舆情复核</h2>"
        "<p>确认 / 误报 / 静音 —— 标记后该事件的后续实时告警会相应抑制。涵盖所有红/黄事件（含未触发实时告警的）。</p>"
        f"{body}</body></html>"
    )


def apply_mark(
    db,
    incident_id: int,
    status: str,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Mark an incident confirmed / false_positive / muted. Pure logic, testable."""
    if status not in REVIEW_STATUSES:
        return False, f"无效状态：{status}"
    fingerprint = db.get_fingerprint_by_incident(incident_id)
    if not fingerprint:
        return False, f"未找到事件 #{incident_id}"
    muted_until = None
    if status == "muted" and days:
        muted_until = (now or utcnow()) + timedelta(days=days)
    if not db.review_incident(fingerprint, status, muted_until):
        return False, "未找到对应事件"
    db.ack_alerts_by_fingerprint(fingerprint)  # also stops escalation for any alert on this event
    return True, f"事件 #{incident_id} 已标记为 {status}"


def render_result(ok: bool, message: str, token: str = "") -> str:
    back = "/review" + (f"?{urlencode({'token': token})}" if token else "")
    color = "#0a0" if ok else "#a00"
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        "<title>已处理</title></head><body style=\"font-family:sans-serif;max-width:600px;margin:2rem auto\">"
        f'<p style="color:{color}">{escape(message)}</p>'
        f'<p><a href="{escape(back)}">← 返回复核列表</a></p></body></html>'
    )
