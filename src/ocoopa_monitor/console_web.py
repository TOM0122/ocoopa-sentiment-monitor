from __future__ import annotations

import csv
import io
from html import escape
from typing import Any, Dict, Iterable, List
from urllib.parse import urlencode

from .analysis_web import compute_dashboard, render_dashboard
from .syndication import with_syndication_fields

# Search and export are built on db.fetch_mentions_between() rows. Dashboard
# aggregation lives in analysis_web and receives time-bounded alert rows so the
# same reporting window is applied on SQLite and Postgres.

CSV_COLUMNS = [
    "platform",
    "content_type",
    "provider",
    "provider_item_id",
    "coverage_tier",
    "discovery_method",
    "published_at",
    "first_seen_at",
    "fetched_at",
    "provider_added_at",
    "discovery_latency_seconds",
    "source_name",
    "source_type",
    "risk_level",
    "sentiment",
    "category",
    "campaign",
    "relevance",
    "novelty_type",
    "story_cluster_key",
    "story_cluster_label",
    "story_role",
    "story_role_label",
    "is_syndicated",
    "has_substantive_update",
    "notification_priority",
    "recommended_action",
    "response_status",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
    "title",
    "source_url",
    "summary_zh",
    "needs_human_review",
    "evidence_check_passed",
    "matched_keywords",
]


def _rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def filter_rows(rows: Iterable[Any], q: str = "", risk: str = "") -> List[Dict[str, Any]]:
    data = _rows(rows)
    if risk:
        data = [r for r in data if str(r.get("risk_level")) == risk]
    if q:
        ql = q.lower()
        data = [
            r
            for r in data
            if ql in str(r.get("title") or "").lower()
            or ql in str(r.get("summary_zh") or "").lower()
            or ql in str(r.get("source_name") or "").lower()
        ]
    return data


def rows_to_csv(rows: Iterable[Any]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in _rows(rows):
        r = with_syndication_fields(r)
        writer.writerow({c: _csv_safe(r.get(c)) for c in CSV_COLUMNS})
    return buf.getvalue()


def _csv_safe(value: Any) -> Any:
    """Prevent untrusted titles/URLs from becoming spreadsheet formulas."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _q(token: str, **extra: Any) -> str:
    params = {k: v for k, v in extra.items() if v not in (None, "")}
    if token:
        params["token"] = token
    return ("?" + urlencode(params)) if params else ""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:sans-serif;max-width:900px;margin:1rem auto;padding:0 1rem}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:.35rem;text-align:left;font-size:.9rem}"
        "li{margin:.4rem 0}a{color:#06c}.tag{display:inline-block;padding:0 .4rem;border-radius:4px;background:#eee}"
        "</style></head><body>" + body + "</body></html>"
    )


def _render_search_row(row: Dict[str, Any]) -> str:
    enriched = with_syndication_fields(row)
    return (
        "<tr>"
        f'<td>{escape(str(enriched.get("risk_level")))}</td>'
        f'<td>{escape(str(enriched.get("source_name") or ""))}<br>'
        f'<span class="tag">{escape(str(enriched.get("story_role_label") or "独立讨论"))}</span></td>'
        f'<td>{escape(str(enriched.get("title") or ""))}<br>'
        f'<a href="{escape(str(enriched.get("source_url") or ""), quote=True)}" target="_blank" rel="noopener noreferrer">链接</a></td>'
        f'<td>{escape(str(enriched.get("summary_zh") or ""))}</td>'
        "</tr>"
    )


def render_search(rows: List[Dict[str, Any]], q: str, risk: str, window_days: int, token: str = "") -> str:
    form = (
        f'<form method="get" action="/console/search">'
        f'<input type="hidden" name="days" value="{int(window_days)}">'
        f'<input name="q" placeholder="关键词/标题/来源" value="{escape(q)}">'
        f'<select name="risk"><option value="">全部风险</option>'
        + "".join(
            f'<option value="{lvl}"{" selected" if risk == lvl else ""}>{lvl}</option>'
            for lvl in ("red", "yellow", "green")
        )
        + '</select> <button type="submit">检索</button></form>'
    )
    body_rows = "".join(_render_search_row(row) for row in rows)
    table = (
        "<table><tr><th>风险</th><th>来源</th><th>标题</th><th>摘要</th></tr>"
        f"{body_rows or '<tr><td colspan=4>无匹配结果</td></tr>'}</table>"
    )
    back = f'<p><a href="/review/analysis{_q(token, days=window_days)}">← 分析看板</a></p>'
    return _page("Ocoopa 舆情检索", f"<h2>Ocoopa 舆情检索</h2>{back}{form}<p>共 {len(rows)} 条</p>{table}")
