from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import hmac
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from .db import MENTION_ACTION_STATUSES, REVIEW_STATUSES, str_to_dt
from .interventions import INTERVENTION_STATUSES, intervention_status
from .models import utcnow
from .syndication import with_syndication_fields

# Review operates on INCIDENTS (event_fingerprint), not on alerts: every
# red/yellow event is reviewable here, including ones absorbed silently during
# cold-start backfill that never produced a real-time alert.

_ACTIONS = [
    ("confirmed", "确认并跟进", None, "确认后保留后续同事件告警，并结束当前待处理升级。"),
    ("false_positive", "标记误报", None, "标记为误报后会永久抑制该事件的后续告警。"),
    ("muted", "静音 7 天", 7, "静音后会在未来 7 天内临时抑制该事件告警。"),
]

_STATUS_META = {
    "active": ("待处理", "pending"),
    "monitoring": ("已确认跟进", "monitoring"),
    "resolved": ("已标记误报", "resolved"),
    "muted": ("已静音", "muted"),
}


def token_ok(configured: str, provided: str) -> bool:
    """If no token is configured the page is open (dev); otherwise it must match."""
    if not configured:
        return True
    return bool(provided) and hmac.compare_digest(provided, configured)


def _safe_source_url(value: Any) -> str:
    """Only render external source links for normal web URLs."""
    url = str(value or "").strip()
    return url if urlsplit(url).scheme in {"http", "https"} else ""


def _status_meta(value: Any) -> Tuple[str, str]:
    return _STATUS_META.get(str(value or ""), ("状态待核", "unknown"))


def _mute_is_active(incident: Dict[str, Any]) -> bool:
    """Treat expired or malformed time-bound mutes as work that needs review again."""
    if str(incident.get("status") or "") != "muted":
        return False
    muted_until = incident.get("muted_until")
    if not muted_until:
        return True
    try:
        parsed = str_to_dt(str(muted_until))
        if parsed and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return bool(parsed and parsed > utcnow())
    except (TypeError, ValueError):
        return False


def _incident_priority_key(incident: Dict[str, Any]) -> Tuple[int, int, int]:
    status = str(incident.get("status") or "active")
    handled = status in {"monitoring", "resolved"} or (status == "muted" and _mute_is_active(incident))
    return (
        1 if handled else 0,
        0 if incident.get("needs_human_review") else 1,
        0 if incident.get("risk_level_max") == "red" else 1,
    )


def _action_forms(incident_id: int, token: str, compact: bool, csrf_token: str = "") -> str:
    forms: List[str] = []
    for status, label, days, warning in _ACTIONS:
        confirmation = escape(f"确定要{label}吗？{warning}", quote=True)
        button_class = "review-action review-action--primary" if status == "confirmed" else "review-action"
        hidden_days = f'<input type="hidden" name="days" value="{days}">' if days is not None else ""
        forms.append(
            '<form method="post" action="/review/mark" class="review-action-form" '
            f'onsubmit="return confirm(\'{confirmation}\')">'
            f'<input type="hidden" name="incident_id" value="{incident_id}">'
            f'<input type="hidden" name="status" value="{escape(status, quote=True)}">'
            f'{hidden_days}<input type="hidden" name="csrf" value="{escape(csrf_token, quote=True)}">'
            f'<button class="{button_class}" type="submit">{escape(label)}</button></form>'
        )
    actions = "".join(forms)
    if compact:
        return (
            '<details class="review-correction"><summary>更正复核结论</summary>'
            f'<div class="review-actions">{actions}</div></details>'
        )
    return f'<div class="review-actions" aria-label="复核操作">{actions}</div>'


def _render_mention(row: Dict[str, Any], csrf_token: str) -> str:
    row = with_syndication_fields(row)
    source_url = _safe_source_url(row.get("source_url"))
    parent_url = _safe_source_url(row.get("parent_url"))
    link = f'<a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">链接 ↗</a>' if source_url else "无公开链接"
    parent = f'<a href="{escape(parent_url, quote=True)}" target="_blank" rel="noopener noreferrer">父帖链接 ↗</a>' if parent_url else "无独立父帖"
    intervention = intervention_status(row)
    response_status = str(row.get("response_status") or "待判断")
    response_label = "未登记对外回应" if response_status == "待判断" else response_status
    intervention_options = "".join(
        f'<option value="{escape(value, quote=True)}{" selected" if value == intervention else ""}>{escape(value)}</option>'
        for value in INTERVENTION_STATUSES
    )
    response_options = "".join(
        f'<option value="{escape(value, quote=True)}{" selected" if value == response_status else ""}>{escape(value)}</option>'
        for value in MENTION_ACTION_STATUSES
    )
    metrics = " · ".join(
        f"{label} {row.get(key)}"
        for key, label in (("view_count", "浏览"), ("like_count", "点赞"), ("comment_count", "评论"), ("share_count", "分享"))
        if row.get(key) is not None
    ) or "平台未提供互动数据"
    return (
        f'<article class="mention-card" id="mention-{int(row.get("id") or 0)}">'
        f'<header><strong>{escape(str(row.get("platform") or "web").upper())}</strong>'
        f'<span>{escape(str(row.get("content_type") or "article"))}</span>'
        f'<span class="story-role story-role--{escape(str(row.get("story_role") or "independent_mention"), quote=True)}">{escape(str(row.get("story_role_label") or "独立讨论"))}</span>'
        f'<span class="response-state">{escape(intervention)}</span></header>'
        f'<h3>{escape(str(row.get("title") or "未命名内容"))}</h3>'
        f'<p>{escape(str(row.get("summary_zh") or row.get("text_excerpt") or "暂无摘要"))}</p>'
        '<dl class="mention-meta">'
        f'<div><dt>公开作者</dt><dd>{escape(str(row.get("author_or_publisher") or "未知"))}</dd></div>'
        f'<div><dt>发布时间</dt><dd>{escape(str(row.get("published_at") or "未知"))}</dd></div>'
        f'<div><dt>首次发现 / 延迟</dt><dd>{escape(str(row.get("first_seen_at") or row.get("fetched_at") or "未知"))} / {escape(str(row.get("discovery_latency_seconds") if row.get("discovery_latency_seconds") is not None else "未知"))} 秒</dd></div>'
        f'<div><dt>最近抓取</dt><dd>{escape(str(row.get("fetched_at") or "未知"))}</dd></div>'
        f'<div><dt>覆盖</dt><dd>{escape(str(row.get("coverage_tier") or "public_index"))} · {escape(str(row.get("discovery_method") or ""))}</dd></div>'
        f'<div><dt>互动</dt><dd>{escape(metrics)}</dd></div>'
        f'<div><dt>传播簇</dt><dd>{escape(str(row.get("story_cluster_label") or "未归类"))}</dd></div>'
        f'<div><dt>证据</dt><dd>{link} · {parent}</dd></div></dl>'
        '<details class="guidance"><summary>查看介入建议与评论草稿</summary>'
        f'<p><b>是否建议介入：</b>{escape(str(row.get("recommended_action") or "monitor"))}；{escape(str(row.get("intervention_reason") or "请人工判断"))}</p>'
        f'<p><b>原语言建议：</b>{escape(str(row.get("draft_original") or "暂无"))}</p>'
        f'<p><b>中文参考：</b>{escape(str(row.get("draft_zh") or "暂无"))}</p>'
        f'<p class="risk-note"><b>事实/法律提示：</b>{escape(str(row.get("legal_risk_note") or "发布前必须复核事实"))}</p></details>'
        '<form method="post" action="/review/mention-action" class="mention-action-form">'
        f'<input type="hidden" name="csrf" value="{escape(csrf_token, quote=True)}">'
        f'<input type="hidden" name="mention_id" value="{int(row.get("id") or 0)}">'
        f'<label>人工分流<select name="intervention_status">{intervention_options}</select></label>'
        f'<label>对外回应登记<select name="response_status">{response_options}</select><small>当前：{escape(response_label)}</small></label>'
        f'<label>回应链接<input name="response_url" type="url" value="{escape(str(row.get("response_url") or ""), quote=True)}" placeholder="https://"></label>'
        f'<label>操作人<input name="operator" value="{escape(str(row.get("operator") or ""), quote=True)}"></label>'
        f'<label class="wide">内部备注<textarea name="internal_note" rows="2">{escape(str(row.get("internal_note") or ""))}</textarea></label>'
        '<button type="submit">登记处置</button></form></article>'
    )


def _render_incident(incident: Dict[str, Any], token: str, csrf_token: str = "") -> str:
    risk = str(incident.get("risk_level_max") or "yellow")
    risk_label = "红色风险" if risk == "red" else "黄色风险"
    status_label, status_class = _status_meta(incident.get("status"))
    if str(incident.get("status") or "") == "muted" and not _mute_is_active(incident):
        status_label, status_class = "静音已到期", "pending"
    handled = status_class in {"monitoring", "resolved", "muted"}
    title = escape(str(incident.get("title") or incident.get("primary_topic") or "未命名事件"))
    summary = escape(str(incident.get("summary_zh") or "暂无自动摘要，请查看原始来源后完成核实。"))
    mention_count = int(incident.get("mention_count") or 1)
    source_count = int(incident.get("source_count") or 1)
    last_seen = escape(str(incident.get("last_seen_at") or "未知"))
    source_url = _safe_source_url(incident.get("source_url"))
    source = (
        f'<a class="source-link" href="{escape(source_url, quote=True)}" target="_blank" '
        'rel="noopener noreferrer">查看原始来源 <span aria-hidden="true">↗</span></a>'
        if source_url
        else '<span class="source-link source-link--unavailable">原始来源链接不可用</span>'
    )
    human_review = (
        '<span class="flag flag--review">需人工核实</span>' if incident.get("needs_human_review") else ""
    )
    evidence = (
        '<span class="flag flag--verified">证据已校验</span>'
        if incident.get("evidence_check_passed")
        else '<span class="flag flag--review">证据待复核</span>'
    )
    muted_note = ""
    if str(incident.get("status") or "") == "muted" and incident.get("muted_until"):
        mute_prefix = "静音至" if _mute_is_active(incident) else "静音已于"
        muted_note = f'<span class="muted-note">{mute_prefix} {escape(str(incident["muted_until"]))}</span>'
    actions = _action_forms(int(incident["incident_id"]), token, compact=handled, csrf_token=csrf_token)
    mentions = incident.get("mentions") or []
    mention_html = "".join(_render_mention(dict(row), csrf_token) for row in mentions)
    evidence_list = (
        f'<details class="mention-list"><summary>展开全部独立帖子 / 评论（{len(mentions)}）</summary>{mention_html}</details>'
        if mentions else ""
    )
    return (
        f'<article class="incident incident--{risk}" aria-label="{risk_label}：{title}">'
        '<div class="incident-header">'
        f'<label class="bulk-choice"><input class="bulk-select" form="bulk-action-form" type="checkbox" name="incident_id" value="{int(incident["incident_id"])}"><span class="sr-only">选择事件：{title}</span></label>'
        f'<span class="risk-badge risk-badge--{risk}">{risk_label}</span>'
        f'<span class="status-badge status-badge--{status_class}">{status_label}</span>'
        f'{human_review}{evidence}{muted_note}'
        '</div>'
        f'<h2>{title}</h2>'
        f'<p class="incident-summary">{summary}</p>'
        '<dl class="incident-meta">'
        f'<div><dt>传播</dt><dd>{mention_count} 条提及，{source_count} 个来源</dd></div>'
        f'<div><dt>最后出现</dt><dd><time>{last_seen}</time></dd></div>'
        f'<div><dt>证据</dt><dd>{source}</dd></div>'
        '</dl>'
        f'{actions}'
        f'{evidence_list}'
        '</article>'
    )


def _bulk_action_form(csrf_token: str, total: int) -> str:
    if not total:
        return ""
    return (
        '<form id="bulk-action-form" method="post" action="/review/mark-bulk" class="bulk-actions" '
        'onsubmit="return confirmBulkAction(this)">'
        f'<input type="hidden" name="csrf" value="{escape(csrf_token, quote=True)}">'
        '<label class="bulk-select-all"><input id="bulk-select-all" type="checkbox"> 全选本页</label>'
        '<span id="bulk-selection-count" aria-live="polite">已选 0 项</span>'
        '<span class="bulk-divider" aria-hidden="true"></span>'
        '<button type="submit" name="status" value="confirmed">批量确认并跟进</button>'
        '<button type="submit" name="status" value="false_positive" class="bulk-danger">批量标记误报</button>'
        '<input type="hidden" name="days" value="7">'
        '<button type="submit" name="status" value="muted">批量静音 7 天</button>'
        '</form>'
    )


def render_review_page(
    incidents: List[Dict[str, Any]], token: str = "", csrf_token: str = "",
    filters: Optional[Dict[str, str]] = None,
) -> str:
    # Preserve newest-first order inside each operational priority band.
    ordered = sorted(incidents, key=lambda it: str(it.get("last_seen_at") or ""), reverse=True)
    ordered.sort(key=_incident_priority_key)
    view = str((filters or {}).get("view") or "queue")
    total = len(ordered)
    red_count = sum(it.get("risk_level_max") == "red" for it in ordered)
    human_count = sum(bool(it.get("needs_human_review")) for it in ordered)
    pending_count = sum(
        str(it.get("status") or "active") == "active"
        or (str(it.get("status") or "") == "muted" and not _mute_is_active(it))
        for it in ordered
    )
    review_href = "/review"
    analysis_params: Dict[str, Any] = {"days": 30}
    analysis_href = "/review/analysis?" + urlencode(analysis_params)
    rows = "".join(_render_incident(incident, token, csrf_token) for incident in ordered)
    filters = filters or {}
    intervention_options = "".join(
        f'<option value="{escape(value, quote=True)}"'
        f'{" selected" if filters.get("intervention") == value else ""}>{escape(value)}</option>'
        for value in INTERVENTION_STATUSES
    )
    view_params = {key: value for key, value in filters.items() if key not in {"view", "page", "page_size", "total_incidents"} and value}
    queue_href = "/review?" + urlencode({**view_params, "view": "queue", "quality": "operational"})
    library_href = "/review?" + urlencode({**view_params, "view": "library", "quality": "all"})
    queue_current = ' aria-current="page"' if view == "queue" else ""
    library_current = ' aria-current="page"' if view == "library" else ""
    filter_form = (
        '<form class="filters" method="get" action="/review">'
        f'<input type="hidden" name="view" value="{escape(view, quote=True)}">'
        f'<label>平台<input name="platform" value="{escape(filters.get("platform", ""), quote=True)}" placeholder="tiktok / reddit"></label>'
        f'<label>监测主题<select name="campaign"><option value="">全部</option><option value="recall_26_659"{" selected" if filters.get("campaign") == "recall_26_659" else ""}>召回 26-659</option><option value="brand_major_risk"{" selected" if filters.get("campaign") == "brand_major_risk" else ""}>品牌重大风险</option></select></label>'
        f'<label>风险<select name="risk"><option value="">全部</option><option value="red"{" selected" if filters.get("risk") == "red" else ""}>红色</option><option value="yellow"{" selected" if filters.get("risk") == "yellow" else ""}>黄色</option><option value="green"{" selected" if filters.get("risk") == "green" else ""}>绿色</option></select></label>'
        f'<label>人工分流<select name="intervention"><option value="">全部</option><option value="human"{" selected" if filters.get("intervention") == "human" else ""}>需人工介入</option>{intervention_options}</select></label>'
        f'<label>统计口径<select name="quality"><option value="operational"{" selected" if filters.get("quality", "all" if view == "library" else "operational") == "operational" else ""}>计入运营统计</option><option value="excluded"{" selected" if filters.get("quality") == "excluded" else ""}>已排除误报</option><option value="all"{" selected" if filters.get("quality", "all" if view == "library" else "operational") == "all" else ""}>全部证据</option></select></label>'
        f'<label>时间<select name="days"><option value="7"{" selected" if filters.get("days") == "7" else ""}>7 天</option><option value="30"{" selected" if filters.get("days", "30") == "30" else ""}>30 天</option><option value="90"{" selected" if filters.get("days") == "90" else ""}>90 天</option></select></label>'
        '<button type="submit">筛选</button></form>'
    )
    heading = "先处理需要人工判断的事件" if view == "queue" else "全量证据库"
    intro = (
        "仅显示当前需要人工核实、查看评论、建议回应或升级的红黄事件；新闻转载默认仅监测，不等同于待回应。"
        if view == "queue" else
        "保留所有独立记录、已归档项目和误报审计。已标记误报的内容不会计入分析看板、趋势或日报。"
    )
    empty = "当前筛选范围暂无待处置事件。" if view == "queue" else "当前筛选范围暂无证据记录。"
    try:
        page = max(int(filters.get("page") or 1), 1)
        page_size = max(int(filters.get("page_size") or 50), 1)
        total_incidents = max(int(filters.get("total_incidents") or total), total)
    except (TypeError, ValueError):
        page, page_size, total_incidents = 1, 50, total
    total_pages = max(1, (total_incidents + page_size - 1) // page_size)
    page = min(page, total_pages)
    page_params = {
        key: value for key, value in filters.items()
        if key not in {"page", "page_size", "total_incidents"} and value
    }
    pagination = (
        '<nav class="review-pagination" aria-label="复核分页">'
        + (f'<a href="/review?{escape(urlencode({**page_params, "page": page - 1}), quote=True)}">上一页</a>' if page > 1 else '<span>上一页</span>')
        + f'<span>第 {page} / {total_pages} 页 · 共 {total_incidents} 个事件</span>'
        + (f'<a href="/review?{escape(urlencode({**page_params, "page": page + 1}), quote=True)}">下一页</a>' if page < total_pages else '<span>下一页</span>')
        + '</nav>'
    ) if total_incidents else ""
    body = (
        f'<section class="incident-list" aria-label="复核事件列表">{rows}</section>'
        if rows
        else (
            f'<section class="empty-state"><h2>{escape(empty)}</h2><p>请调整筛选条件，或等待新的公开信息进入系统。</p></section>'
        )
    )
    bulk_actions = _bulk_action_form(csrf_token, total)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<title>OCOOPA 舆情复核</title>'
        '<style>'
        ':root{color-scheme:light dark;--canvas:#f4f7fb;--surface:#fff;--ink:#14213a;--muted:#5d6b82;'
        '--line:#d9e1eb;--accent:#075985;--accent-strong:#0c4a6e;--soft:#e8f2f8;--shadow:0 12px 32px rgba(15,35,60,.08);'
        '--red:#b42318;--red-soft:#fff1f0;--amber:#9a6700;--amber-soft:#fff8e8;--green:#1f6b49;--green-soft:#effaf4;'
        '--radius:12px}*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);'
        'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}.page{max-width:1120px;margin:0 auto;padding:32px 20px 56px}'
        '.workspace-nav{display:flex;gap:6px;width:max-content;margin-bottom:26px;padding:5px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}.workspace-nav a,.logout-button{padding:8px 13px;border:0;border-radius:7px;background:transparent;color:var(--muted);font:inherit;font-weight:700;text-decoration:none;cursor:pointer}.workspace-nav a[aria-current="page"]{background:var(--accent);color:#fff}.logout-form{margin:0}'
        '.review-tabs{display:flex;gap:8px;margin:0 0 18px}.review-tabs a{padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--muted);font-size:.88rem;font-weight:700;text-decoration:none}.review-tabs a[aria-current="page"]{border-color:var(--accent);background:var(--soft);color:var(--accent-strong)}'
        '.review-pagination{display:flex;justify-content:center;gap:9px;align-items:center;margin:18px 0;color:var(--muted);font-size:.84rem}.review-pagination a,.review-pagination span{padding:7px 10px;border:1px solid var(--line);border-radius:7px;background:var(--surface);text-decoration:none}.review-pagination span{color:var(--muted)}'
        '.bulk-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 18px;padding:12px 14px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:0 2px 8px rgba(15,35,60,.035)}.bulk-actions button{min-height:35px;padding:7px 10px;border:1px solid #b8c5d3;border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:.84rem;font-weight:700;cursor:pointer}.bulk-actions button:first-of-type{border-color:var(--accent);background:var(--accent);color:#fff}.bulk-actions .bulk-danger{border-color:#e6aaa4;color:var(--red)}.bulk-select-all,.bulk-choice{display:inline-flex;align-items:center;gap:6px;font-size:.84rem;font-weight:700;cursor:pointer}.bulk-choice{margin-right:1px}.bulk-choice input,.bulk-select-all input{width:17px;height:17px;accent-color:var(--accent);cursor:pointer}.bulk-divider{width:1px;height:24px;background:var(--line)}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}'
        '.page-header{padding:8px 0 24px}.eyebrow{margin:0 0 7px;color:var(--accent);font-size:.78rem;font-weight:700;letter-spacing:.08em}'
        'h1,h2,p{margin-top:0}h1{margin-bottom:8px;font-size:clamp(1.75rem,4vw,2.35rem);letter-spacing:-.03em;line-height:1.15}'
        '.intro{max-width:760px;margin:0;color:var(--muted)}.overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:20px}'
        '.metric{min-height:94px;padding:16px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:0 2px 8px rgba(15,35,60,.035)}'
        '.metric b{display:block;font-size:1.75rem;line-height:1.05}.metric span{display:block;margin-top:7px;color:var(--muted);font-size:.86rem}.metric--attention{border-color:#f0c76a;background:var(--amber-soft)}'
        '.metric--risk{border-color:#efb4ae;background:var(--red-soft)}.review-guidance{margin:0 0 22px;padding:14px 16px;border-left:4px solid var(--accent);background:var(--soft);color:#1c4863;font-size:.92rem}'
        '.incident-list{display:grid;gap:14px}.incident{padding:20px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:var(--shadow)}'
        '.incident--red{border-left:5px solid var(--red)}.incident--yellow{border-left:5px solid var(--amber)}.incident-header{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:11px}'
        '.risk-badge,.status-badge,.flag,.muted-note{display:inline-flex;align-items:center;min-height:24px;padding:3px 8px;border-radius:999px;font-size:.78rem;font-weight:700}.risk-badge--red{color:#8a1c14;background:var(--red-soft)}'
        '.risk-badge--yellow{color:#785300;background:var(--amber-soft)}.status-badge--pending,.flag--review{color:#875b00;background:var(--amber-soft)}.status-badge--monitoring,.flag--verified{color:#15533a;background:var(--green-soft)}'
        '.status-badge--resolved{color:#4b5563;background:#edf0f4}.status-badge--muted{color:#5d4a7a;background:#f4f0fb}.status-badge--unknown{color:#475569;background:#eef2f6}.muted-note{color:#5d4a7a;background:#f4f0fb}'
        '.incident h2{margin-bottom:8px;font-size:1.1rem;line-height:1.35}.incident-summary{margin-bottom:15px;color:#2f4058}.incident-meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:0 0 17px;padding:0}'
        '.incident-meta div{min-width:0}.incident-meta dt{margin-bottom:3px;color:var(--muted);font-size:.75rem;font-weight:700}.incident-meta dd{margin:0;font-size:.9rem;overflow-wrap:anywhere}.source-link{color:var(--accent-strong);font-weight:700;text-underline-offset:3px}.source-link--unavailable{color:var(--muted);font-weight:400}'
        '.review-actions{display:flex;flex-wrap:wrap;gap:8px}.review-action-form{margin:0}.review-action{min-height:36px;padding:7px 11px;border:1px solid #b8c5d3;border-radius:8px;background:var(--surface);color:var(--ink);font:inherit;font-size:.88rem;font-weight:700;cursor:pointer}'
        '.review-action--primary{border-color:var(--accent);background:var(--accent);color:#fff}.review-action:hover{border-color:var(--accent)}.review-action--primary:hover{background:var(--accent-strong)}.review-action:active{transform:translateY(1px)}'
        '.review-action:focus-visible,.source-link:focus-visible,summary:focus-visible{outline:3px solid #7dd3fc;outline-offset:2px}.review-correction{margin-top:3px}.review-correction summary{color:var(--muted);font-size:.88rem;cursor:pointer}.review-correction .review-actions{margin-top:11px}'
        '.empty-state{padding:38px 24px;border:1px dashed #aab8c9;border-radius:var(--radius);background:var(--surface);text-align:center}.empty-state h2{font-size:1.2rem}.empty-state p{margin-bottom:0;color:var(--muted)}'
        '.filters{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:end;margin:0 0 18px;padding:14px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}.filters label,.mention-action-form label{display:grid;gap:5px;color:var(--muted);font-size:.78rem;font-weight:700}.filters input,.filters select,.mention-action-form input,.mention-action-form select,.mention-action-form textarea{width:100%;padding:8px;border:1px solid #b8c5d3;border-radius:7px;background:var(--surface);color:var(--ink);font:inherit}.filters button,.mention-action-form button{min-height:38px;padding:8px 12px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-weight:700}.mention-list{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}.mention-list>summary,.guidance>summary{cursor:pointer;font-weight:700}.mention-card{margin-top:12px;padding:15px;border:1px solid var(--line);border-radius:10px;background:var(--canvas)}.mention-card:target{border-color:var(--accent);box-shadow:0 0 0 3px var(--soft)}.mention-card header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;color:var(--muted);font-size:.78rem}.mention-card h3{margin:9px 0 6px;font-size:1rem}.story-role{padding:2px 7px;border-radius:999px;background:var(--soft);color:var(--accent-strong);font-weight:700}.story-role--substantive_update{background:var(--red-soft);color:var(--red)}.response-state{margin-left:auto}.mention-meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.mention-meta dt{color:var(--muted);font-size:.72rem;font-weight:700}.mention-meta dd{margin:2px 0;font-size:.82rem;overflow-wrap:anywhere}.guidance{margin:10px 0;padding:10px;border-left:3px solid var(--accent);background:var(--surface)}.guidance p{margin:8px 0;font-size:.86rem}.risk-note{color:var(--red)}.mention-action-form{display:grid;grid-template-columns:1fr 1fr 1fr;gap:9px;align-items:end}.mention-action-form .wide{grid-column:1/-1}.mention-action-form button{justify-self:start}'
        '@media (max-width:720px){.page{padding:24px 14px 40px}.workspace-nav{width:100%}.workspace-nav a{flex:1;text-align:center}.overview{grid-template-columns:repeat(2,minmax(0,1fr))}.incident{padding:16px}.incident-meta,.mention-meta,.filters,.mention-action-form{grid-template-columns:1fr;gap:9px}.mention-action-form .wide{grid-column:auto}.review-action{width:100%}.review-action-form{flex:1 1 100%}.bulk-actions{align-items:stretch}.bulk-actions button{flex:1 1 100%}.bulk-divider{display:none}}'
        '@media (prefers-color-scheme:dark){:root{--canvas:#111a29;--surface:#172235;--ink:#eff6ff;--muted:#b1c0d3;--line:#34455e;--accent:#7dd3fc;--accent-strong:#bae6fd;--soft:#12324a;--shadow:0 12px 32px rgba(0,0,0,.2);--red:#ffb4ac;--red-soft:#482523;--amber:#ffd68a;--amber-soft:#423313;--green:#a4e2c0;--green-soft:#173a2b}.workspace-nav a[aria-current="page"]{background:#7dd3fc;color:#082f49}.intro,.incident-summary{color:var(--muted)}.review-guidance{color:#c8eafa}.source-link{color:var(--accent)}.review-action{background:#172235;color:var(--ink);border-color:#52657c}.review-action--primary{background:#7dd3fc;border-color:#7dd3fc;color:#082f49}.review-action--primary:hover{background:#bae6fd}}'
        '</style></head><body><main class="page"><nav class="workspace-nav" aria-label="舆情工作台">'
        f'<a href="{escape(review_href, quote=True)}" aria-current="page">事件复核</a>'
        f'<a href="{escape(analysis_href, quote=True)}">分析看板</a>'
        f'<form class="logout-form" method="post" action="/auth/logout"><input type="hidden" name="csrf" value="{escape(csrf_token, quote=True)}"><button class="logout-button" type="submit">退出</button></form></nav>'
        '<header class="page-header"><p class="eyebrow">OCOOPA / 召回复核工作台</p>'
        f'<h1>{escape(heading)}</h1><p class="intro">{escape(intro)}</p></header>'
        f'<nav class="review-tabs" aria-label="复核视图"><a href="{escape(queue_href, quote=True)}"{queue_current}>待处置队列</a><a href="{escape(library_href, quote=True)}"{library_current}>全量证据库</a></nav>'
        '<section class="overview" aria-label="本页风险概览">'
        f'<div class="metric"><b>{total}</b><span>{"本页待处置事件" if view == "queue" else "本页证据事件"}</span></div>'
        f'<div class="metric metric--risk"><b>{red_count}</b><span>红色风险</span></div>'
        f'<div class="metric metric--attention"><b>{human_count}</b><span>需人工核实</span></div>'
        f'<div class="metric"><b>{pending_count}</b><span>待处理</span></div></section>'
        f'{filter_form}'
        '<p class="review-guidance"><strong>操作影响：</strong>确认并跟进会结束当前待处理升级，但保留后续同事件告警；标记误报会永久抑制，并立即从看板、趋势与日报中排除，但仍保留在证据库；静音 7 天为临时抑制。系统不发布或回复任何平台内容。</p>'
        f'{bulk_actions}{body}{pagination}</main><script>(()=>{{const selected=()=>[...document.querySelectorAll(".bulk-select:checked")];const count=document.getElementById("bulk-selection-count");const all=document.getElementById("bulk-select-all");const sync=()=>{{const items=document.querySelectorAll(".bulk-select");if(count)count.textContent=`已选 ${{selected().length}} 项`;if(all){{all.checked=items.length>0&&selected().length===items.length;all.indeterminate=selected().length>0&&selected().length<items.length}}}};document.querySelectorAll(".bulk-select").forEach(item=>item.addEventListener("change",sync));if(all)all.addEventListener("change",()=>{{document.querySelectorAll(".bulk-select").forEach(item=>item.checked=all.checked);sync()}});window.confirmBulkAction=form=>{{const chosen=selected().length;if(!chosen){{alert("请先选择至少一个事件。");return false}}const button=form.querySelector("button[type=submit]:focus");const action=button?button.textContent:"批量操作";return confirm(`确定要对 ${{chosen}} 个事件执行“${{action}}”吗？此操作会分别记录到每个事件。`)}};sync();const openTarget=()=>{{if(!location.hash.startsWith("#mention-"))return;const target=document.getElementById(decodeURIComponent(location.hash.slice(1)));if(!target)return;let parent=target.parentElement;while(parent){{if(parent.tagName==="DETAILS")parent.open=true;parent=parent.parentElement}}target.scrollIntoView({{block:"center"}})}};window.addEventListener("hashchange",openTarget);openTarget()}})();</script></body></html>'
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


def apply_bulk_mark(
    db,
    incident_ids: List[int],
    status: str,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Apply one reviewed event action to a bounded, explicit selection.

    All IDs are resolved before any record changes, preventing a malformed
    selection from producing a partly-applied bulk decision.
    """
    if status not in REVIEW_STATUSES:
        return False, f"无效状态：{status}"
    unique_ids = list(dict.fromkeys(incident_ids))
    if not unique_ids:
        return False, "请至少选择一个事件"
    if len(unique_ids) > 100:
        return False, "单次最多可处理 100 个事件"
    missing = [incident_id for incident_id in unique_ids if not db.get_fingerprint_by_incident(incident_id)]
    if missing:
        return False, f"未找到事件：{', '.join(str(value) for value in missing[:5])}"
    for incident_id in unique_ids:
        ok, _ = apply_mark(db, incident_id, status, days, now)
        if not ok:  # Defensive: validation above normally makes this unreachable.
            return False, f"事件 #{incident_id} 未能完成批量处置"
    return True, f"已对 {len(unique_ids)} 个事件执行 {status}"


def render_result(ok: bool, message: str, token: str = "") -> str:
    back = "/review" + (f"?{urlencode({'token': token})}" if token else "")
    result_class = "result--success" if ok else "result--error"
    title = "复核结论已记录" if ok else "未能记录复核结论"
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>OCOOPA 舆情复核</title>'
        '<style>body{margin:0;padding:24px;background:#f4f7fb;color:#14213a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.result{max-width:620px;margin:8vh auto;padding:28px;border:1px solid #d9e1eb;border-radius:12px;background:#fff;box-shadow:0 12px 32px rgba(15,35,60,.08)}.result--success{border-left:5px solid #1f6b49}.result--error{border-left:5px solid #b42318}h1{margin-top:0;font-size:1.45rem}p{line-height:1.6}a{color:#075985;font-weight:700;text-underline-offset:3px}@media (prefers-color-scheme:dark){body{background:#111a29;color:#eff6ff}.result{border-color:#34455e;background:#172235}a{color:#7dd3fc}}</style>'
        '</head><body><main class="result ' + result_class + '"><h1>' + title + '</h1>'
        f'<p aria-live="polite">{escape(message)}</p><p><a href="{escape(back, quote=True)}">返回复核列表</a></p>'
        '</main></body></html>'
    )
