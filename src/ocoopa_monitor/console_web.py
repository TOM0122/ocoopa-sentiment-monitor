from __future__ import annotations

import csv
import io
from collections import Counter
from html import escape
from typing import Any, Dict, Iterable, List
from urllib.parse import urlencode

# Built on existing db.fetch_mentions_between() rows (mentions + analysis fields)
# and db.list_recent_alerts() — no new per-backend SQL. All aggregation/filtering
# happens here in Python so it works identically on SQLite and Postgres.

CSV_COLUMNS = [
    "published_at",
    "fetched_at",
    "source_name",
    "source_type",
    "risk_level",
    "sentiment",
    "category",
    "title",
    "source_url",
    "summary_zh",
    "needs_human_review",
    "evidence_check_passed",
    "matched_keywords",
]


def _rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def compute_dashboard(rows: Iterable[Any], alerts: Iterable[Any]) -> Dict[str, Any]:
    data = _rows(rows)
    top = [r for r in data if r.get("risk_level") in {"red", "yellow"}][:20]
    return {
        "total": len(data),
        "alerts": len(list(alerts)),
        "risk": dict(Counter(str(r.get("risk_level") or "unknown") for r in data)),
        "sentiment": dict(Counter(str(r.get("sentiment") or "unknown") for r in data)),
        "source": dict(Counter(str(r.get("source_name") or "unknown") for r in data)),
        "top": top,
    }


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
        writer.writerow({c: r.get(c) for c in CSV_COLUMNS})
    return buf.getvalue()


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


def render_dashboard(stats: Dict[str, Any], window_days: int, token: str = "") -> str:
    nav = (
        f'<a href="/console/search{_q(token)}">检索</a> · '
        f'<a href="/console/export.csv{_q(token, days=window_days)}">导出 CSV</a>'
    )
    dist = (
        f"<p>近 {window_days} 天提及：<b>{stats['total']}</b> · 实时告警：<b>{stats['alerts']}</b></p>"
        f"<p>风险分布：{escape(str(stats['risk']))}<br>"
        f"情感分布：{escape(str(stats['sentiment']))}<br>"
        f"来源分布：{escape(str(stats['source']))}</p>"
    )
    items = "".join(
        "<li>"
        f'<span class="tag">{escape(str(r.get("risk_level")))}</span> '
        f'{escape(str(r.get("title") or ""))}'
        f'{" · 需人工核实" if r.get("needs_human_review") else ""}<br>'
        f'<a href="{escape(str(r.get("source_url") or ""))}" target="_blank">'
        f'{escape(str(r.get("source_url") or ""))}</a>'
        "</li>"
        for r in stats["top"]
    )
    top = f"<h3>近期红/黄风险</h3><ul>{items or '<li>暂无</li>'}</ul>"
    return _page("Ocoopa 舆情看板", f"<h2>Ocoopa 舆情看板</h2><p>{nav}</p>{dist}{top}")


def render_search(rows: List[Dict[str, Any]], q: str, risk: str, window_days: int, token: str = "") -> str:
    form = (
        f'<form method="get" action="/console/search">'
        f'<input type="hidden" name="token" value="{escape(token)}">'
        f'<input name="q" placeholder="关键词/标题/来源" value="{escape(q)}">'
        f'<select name="risk"><option value="">全部风险</option>'
        + "".join(
            f'<option value="{lvl}"{" selected" if risk == lvl else ""}>{lvl}</option>'
            for lvl in ("red", "yellow", "green")
        )
        + '</select> <button type="submit">检索</button></form>'
    )
    body_rows = "".join(
        "<tr>"
        f'<td>{escape(str(r.get("risk_level")))}</td>'
        f'<td>{escape(str(r.get("source_name") or ""))}</td>'
        f'<td>{escape(str(r.get("title") or ""))}<br>'
        f'<a href="{escape(str(r.get("source_url") or ""))}" target="_blank">链接</a></td>'
        f'<td>{escape(str(r.get("summary_zh") or ""))}</td>'
        "</tr>"
        for r in rows
    )
    table = (
        "<table><tr><th>风险</th><th>来源</th><th>标题</th><th>摘要</th></tr>"
        f"{body_rows or '<tr><td colspan=4>无匹配结果</td></tr>'}</table>"
    )
    back = f'<p><a href="/dashboard{_q(token)}">← 看板</a></p>'
    return _page("Ocoopa 舆情检索", f"<h2>Ocoopa 舆情检索</h2>{back}{form}<p>共 {len(rows)} 条</p>{table}")
