from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .db import REVIEW_STATUSES
from .models import utcnow

# Buttons offered per alert row: (review status, label, mute-days).
_ACTIONS = [
    ("confirmed", "确认", None),
    ("false_positive", "误报", None),
    ("muted", "静音7天", 7),
]


def token_ok(configured: str, provided: str) -> bool:
    """If no token is configured the page is open (dev); otherwise it must match."""
    if not configured:
        return True
    return bool(provided) and provided == configured


def _mark_url(alert_id: int, status: str, days: Optional[int], token: str) -> str:
    params: Dict[str, Any] = {"alert_id": alert_id, "status": status}
    if days is not None:
        params["days"] = days
    if token:
        params["token"] = token
    return "/review/mark?" + urlencode(params)


def render_review_page(alerts: List[Dict[str, Any]], token: str = "") -> str:
    rows: List[str] = []
    for a in alerts:
        suppressed = a.get("incident_status") in {"resolved", "muted"}
        review = " · <b>需人工核实</b>" if a.get("needs_human_review") else ""
        status_note = f" · 已处理({escape(str(a.get('incident_status')))})" if suppressed else ""
        buttons = " ".join(
            f'<form method="post" action="{_mark_url(a["alert_id"], status, days, token)}" '
            f'style="display:inline">'
            f'<button type="submit">{escape(label)}</button></form>'
            for status, label, days in _ACTIONS
        )
        rows.append(
            "<li>"
            f'<b>#{a["alert_id"]}</b> [{escape(str(a.get("risk_level")))}]{review}{status_note}<br>'
            f'{escape(str(a.get("title") or ""))}<br>'
            f'<a href="{escape(str(a.get("source_url") or ""))}" target="_blank">'
            f'{escape(str(a.get("source_url") or ""))}</a><br>'
            f"{buttons}"
            "</li>"
        )
    body = "<ul>" + "".join(rows) + "</ul>" if rows else "<p>暂无告警。</p>"
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Ocoopa 舆情复核</title>"
        "<style>body{font-family:sans-serif;max-width:760px;margin:1rem auto;padding:0 1rem}"
        "li{margin:0 0 1rem;padding:.6rem;border:1px solid #ddd;border-radius:6px;list-style:none}"
        "button{margin-right:.4rem;padding:.3rem .7rem}ul{padding:0}</style></head>"
        f"<body><h2>Ocoopa 舆情复核</h2><p>确认 / 误报 / 静音 —— 标记后该事件的后续实时告警会相应抑制。</p>{body}</body></html>"
    )


def apply_mark(
    db,
    alert_id: int,
    status: str,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Returns (ok, message). Pure logic so it is testable without FastAPI."""
    if status not in REVIEW_STATUSES:
        return False, f"无效状态：{status}"
    fingerprint = db.get_fingerprint_by_alert(alert_id)
    if not fingerprint:
        return False, f"未找到告警 #{alert_id}"
    muted_until = None
    if status == "muted" and days:
        muted_until = (now or utcnow()) + timedelta(days=days)
    updated = db.review_incident(fingerprint, status, muted_until)
    if not updated:
        return False, "未找到对应事件"
    return True, f"#{alert_id} 已标记为 {status}"


def render_result(ok: bool, message: str, token: str = "") -> str:
    back = "/review" + (f"?{urlencode({'token': token})}" if token else "")
    color = "#0a0" if ok else "#a00"
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<title>已处理</title></head><body style=\"font-family:sans-serif;max-width:600px;margin:2rem auto\">"
        f'<p style="color:{color}">{escape(message)}</p>'
        f'<p><a href="{escape(back)}">← 返回复核列表</a></p></body></html>'
    )
