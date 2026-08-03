from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
from math import ceil
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from .syndication import ROLE_LABELS, with_syndication_fields
from .topics import TOPIC_LABELS, enrich_topic_fields, is_public_social_parent_post


DETAIL_METRICS = {
    "links": {
        "label": "有效传播链接",
        "short_label": "传播链接",
        "description": "符合监测口径的公开网页、帖子或评论记录；同一新闻在不同站点发布会分别保留。",
    },
    "clusters": {
        "label": "独立传播簇（自动归并）",
        "short_label": "独立传播簇",
        "description": "系统将同一新闻或事件的多个传播链接自动归并；这是分析口径，不等同于人工确认的事件数量。",
    },
    "syndicated": {
        "label": "转载扩散链接",
        "short_label": "转载扩散",
        "description": "已知信息被新闻网站或社媒账号再次传播的链接，是传播链接的子集，不代表出现新事故。",
    },
    "substantive": {
        "label": "新增实质信号（待复核）",
        "short_label": "新增实质信号",
        "description": "可能包含第一人称事故陈述、新监管或法律动作的信息簇；必须查看原文并完成人工核实。",
    },
    "parent_posts": {
        "label": "公开社媒母帖（人工查看评论）",
        "short_label": "社媒母帖",
        "description": "系统仅从公开索引发现母帖并保留原帖链接；不接入平台管理权限、不抓取评论。请人工打开原帖查看评论，再按需要在复核页登记处置。",
    },
}


def normalize_detail_metric(metric: str) -> str:
    return metric if metric in DETAIL_METRICS else "links"


def build_detail_view(
    rows: Iterable[Any],
    metric: str = "links",
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Any]:
    metric = normalize_detail_metric(metric)
    data = [enrich_topic_fields(with_syndication_fields(dict(row))) for row in rows]
    data.sort(key=_row_sort_key, reverse=True)
    clusters = _build_clusters(data)
    substantive_clusters = [cluster for cluster in clusters if cluster["has_substantive_update"]]
    counts = {
        "links": len(data),
        "clusters": len(clusters),
        "syndicated": sum(bool(row.get("is_syndicated")) for row in data),
        "substantive": len(substantive_clusters),
        "parent_posts": sum(is_public_social_parent_post(row) for row in data),
    }
    if metric == "clusters":
        selected = clusters
        item_kind = "cluster"
    elif metric == "substantive":
        selected = substantive_clusters
        item_kind = "cluster"
    elif metric == "syndicated":
        selected = [row for row in data if row.get("is_syndicated")]
        item_kind = "mention"
    elif metric == "parent_posts":
        selected = [row for row in data if is_public_social_parent_post(row)]
        item_kind = "mention"
    else:
        selected = data
        item_kind = "mention"

    page_size = min(max(int(page_size), 20), 200)
    total_items = len(selected)
    total_pages = max(1, ceil(total_items / page_size))
    page = min(max(int(page), 1), total_pages)
    offset = (page - 1) * page_size
    items = selected[offset : offset + page_size]
    return {
        "metric": metric,
        "metric_config": DETAIL_METRICS[metric],
        "counts": counts,
        "item_kind": item_kind,
        "items": items,
        "total_items": total_items,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "range_start": offset + 1 if total_items else 0,
        "range_end": min(offset + page_size, total_items),
    }


def _build_clusters(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in data:
        key = str(row.get("story_cluster_key") or row.get("event_fingerprint") or row.get("id") or "")
        grouped.setdefault(key, []).append(row)
    clusters: List[Dict[str, Any]] = []
    for key, members in grouped.items():
        members.sort(key=_row_sort_key, reverse=True)
        role_counts = Counter(str(row.get("story_role") or "independent_mention") for row in members)
        risks = Counter(str(row.get("risk_level") or "unknown") for row in members)
        published = [_as_datetime(row.get("published_at")) for row in members]
        discovered = [
            _as_datetime(row.get("first_seen_at") or row.get("fetched_at"))
            for row in members
        ]
        published = [value for value in published if value is not None]
        discovered = [value for value in discovered if value is not None]
        primary = members[0]
        clusters.append(
            {
                "cluster_key": key,
                "cluster_label": str(primary.get("story_cluster_label") or "独立舆情事件"),
                "representative_title": str(primary.get("title") or "未命名内容"),
                "representative_summary": str(primary.get("summary_zh") or primary.get("text_excerpt") or "暂无摘要"),
                "link_count": len(members),
                "platforms": sorted({str(row.get("platform") or "web") for row in members}),
                "sources": sorted({str(row.get("source_name") or "未知来源") for row in members}),
                "role_counts": dict(role_counts),
                "risk_counts": dict(risks),
                "risk_level": _highest_risk(risks),
                "has_substantive_update": any(bool(row.get("has_substantive_update")) for row in members),
                "first_published_at": min(published).isoformat() if published else "",
                "latest_discovered_at": max(discovered).isoformat() if discovered else "",
                "members": members,
            }
        )
    clusters.sort(key=_cluster_sort_key, reverse=True)
    return clusters


def render_analysis_details(
    view: Dict[str, Any],
    window_days: int,
    filters: Optional[Dict[str, str]] = None,
    token: str = "",
    csrf_token: str = "",
) -> str:
    filters = filters or {}
    days = min(max(int(window_days), 1), 366)
    metric = str(view["metric"])
    metric_config = view["metric_config"]
    tabs = "".join(
        _render_detail_tab(name, config, metric, view, days, filters, token)
        for name, config in DETAIL_METRICS.items()
    )
    if view["item_kind"] == "cluster":
        content = "".join(
            _render_cluster(cluster, days, filters, token) for cluster in view["items"]
        )
    else:
        content = '<div class="evidence-list">' + "".join(
            _render_mention(row, days, filters, token) for row in view["items"]
        ) + "</div>"
    if not view["items"]:
        content = '<section class="empty-state"><h2>当前筛选范围暂无记录</h2><p>这只表示系统在当前时间、平台和主题范围内未发现对应内容。</p></section>'
    pagination = _render_pagination(view, days, filters, token)
    topic_options = "".join(
        f'<option value="{escape(name, quote=True)}"'
        f'{" selected" if filters.get("topic") == name else ""}'
        f'>{escape(label)}</option>'
        for name, label in TOPIC_LABELS.items()
    )
    filter_form = (
        '<form class="detail-filters" method="get" action="/review/analysis/details">'
        f'<input type="hidden" name="metric" value="{escape(metric, quote=True)}">'
        f'<input type="hidden" name="days" value="{days}">'
        f'<label>平台<input name="platform" value="{escape(str(filters.get("platform") or ""), quote=True)}" placeholder="tiktok / reddit"></label>'
        '<label>监测主题<select name="campaign"><option value="">全部</option>'
        f'<option value="recall_26_659"{" selected" if filters.get("campaign") == "recall_26_659" else ""}>召回 26-659</option>'
        f'<option value="brand_major_risk"{" selected" if filters.get("campaign") == "brand_major_risk" else ""}>品牌重大风险</option></select></label>'
        '<label>议题<select name="topic"><option value="">全部</option>'
        + topic_options
        + '</select></label>'
        '<button type="submit">应用筛选</button></form>'
    )
    scope_parts = [f"近 {days} 个北京时间自然日"]
    if filters.get("platform"):
        scope_parts.append(f"平台：{filters['platform']}")
    if filters.get("campaign"):
        scope_parts.append(
            "主题：召回 26-659" if filters["campaign"] == "recall_26_659" else "主题：品牌重大风险"
        )
    if filters.get("topic"):
        scope_parts.append(f"议题：{TOPIC_LABELS.get(filters['topic'], filters['topic'])}")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><title>OCOOPA 传播数据明细</title><style>'
        ':root{color-scheme:light dark;--canvas:#f3f6fa;--surface:#fff;--ink:#14213a;--muted:#617086;--line:#d8e0ea;--accent:#075985;--accent-soft:#e8f3f9;--red:#b42318;--red-soft:#fff1f0;--amber:#936300;--amber-soft:#fff7df;--green:#1f6b49;--radius:12px;--shadow:0 12px 32px rgba(15,35,60,.07)}'
        '*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}.page{max-width:1180px;margin:0 auto;padding:24px 20px 60px}a{color:var(--accent)}'
        '.workspace-nav{display:flex;gap:6px;margin-bottom:24px;padding:5px;border:1px solid var(--line);border-radius:10px;background:var(--surface);width:max-content}.workspace-nav a,.logout-button{padding:8px 13px;border:0;border-radius:7px;background:transparent;color:var(--muted);font:inherit;font-weight:700;text-decoration:none;cursor:pointer}.workspace-nav a[aria-current=page]{background:var(--accent);color:#fff}.logout-form{margin:0}'
        '.breadcrumbs{margin-bottom:12px;color:var(--muted);font-size:.86rem}.breadcrumbs a{text-underline-offset:3px}.header-row{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:18px}.eyebrow{margin:0 0 5px;color:var(--accent);font-size:.78rem;font-weight:800;letter-spacing:.07em}h1,h2,h3,p{margin-top:0}h1{margin-bottom:7px;font-size:clamp(1.7rem,4vw,2.25rem);letter-spacing:-.03em}.subtitle{margin:0;color:var(--muted)}'
        '.scope{padding:9px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--muted);font-size:.82rem}.detail-filters{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:0 0 14px;padding:12px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}.detail-filters label{display:grid;gap:4px;color:var(--muted);font-size:.76rem;font-weight:700}.detail-filters input,.detail-filters select{min-width:180px;padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--surface);color:var(--ink)}.detail-filters button{min-height:36px;padding:8px 12px;border:0;border-radius:7px;background:var(--accent);color:#fff;font-weight:700}'
        '.detail-tabs{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}.detail-tab{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--ink);text-decoration:none;font-size:.86rem;font-weight:750}.detail-tab strong{font-size:1.05rem}.detail-tab--active{border-color:var(--accent);background:var(--accent-soft);color:var(--accent)}'
        '.definition{margin:0 0 14px;padding:13px 15px;border-left:4px solid var(--accent);border-radius:var(--radius);background:var(--accent-soft)}.definition strong{display:block;margin-bottom:3px}.definition p{margin:0;color:var(--muted);font-size:.87rem}.result-meta{display:flex;justify-content:space-between;gap:10px;align-items:center;margin:0 0 10px;color:var(--muted);font-size:.82rem}'
        '.cluster-card{margin-bottom:11px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:0 2px 8px rgba(15,35,60,.03);overflow:hidden}.cluster-card>summary{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;padding:16px;cursor:pointer;list-style:none}.cluster-card>summary::-webkit-details-marker{display:none}.cluster-card>summary:after{content:"展开证据";align-self:center;color:var(--accent);font-size:.78rem;font-weight:800}.cluster-card[open]>summary:after{content:"收起"}.cluster-title{margin:5px 0 4px;font-size:1rem}.cluster-description{margin:0;color:var(--muted);font-size:.84rem}.cluster-stats{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end;align-content:flex-start}.tag{display:inline-flex;padding:3px 7px;border-radius:999px;background:#eef2f6;color:#4a5a70;font-size:.74rem;font-weight:750}.tag--red,.tag--substantive{background:var(--red-soft);color:var(--red)}.tag--yellow{background:var(--amber-soft);color:var(--amber)}.cluster-body{padding:0 16px 16px;border-top:1px solid var(--line)}.cluster-facts{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0;color:var(--muted);font-size:.79rem}'
        '.evidence-list{display:grid;gap:10px}.evidence-row{padding:14px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}.cluster-body .evidence-row{margin-top:10px;background:var(--canvas)}.evidence-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;color:var(--muted);font-size:.76rem}.evidence-row h3{margin:8px 0 5px;font-size:.94rem}.evidence-row p{margin-bottom:9px;color:var(--muted);font-size:.84rem}.evidence-facts{display:flex;flex-wrap:wrap;gap:8px 14px;color:var(--muted);font-size:.76rem}.evidence-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:11px}.evidence-actions a{padding:6px 9px;border:1px solid var(--line);border-radius:7px;background:var(--surface);font-size:.79rem;font-weight:800;text-decoration:none}.evidence-actions a:first-child{border-color:var(--accent);background:var(--accent);color:#fff}'
        '.pagination{display:flex;justify-content:center;align-items:center;gap:10px;margin-top:18px}.pagination a,.pagination span{padding:7px 10px;border:1px solid var(--line);border-radius:7px;background:var(--surface);text-decoration:none;font-size:.82rem}.pagination span{color:var(--muted)}.empty-state{padding:38px 20px;border:1px dashed #aab8c9;border-radius:var(--radius);background:var(--surface);text-align:center}.empty-state p{margin:0;color:var(--muted)}.method-note{margin-top:18px;color:var(--muted);font-size:.78rem}'
        'a:focus-visible,summary:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #7dd3fc;outline-offset:2px}@media(max-width:760px){.page{padding:16px 12px 40px}.workspace-nav{width:100%}.workspace-nav a{flex:1;text-align:center}.header-row{align-items:flex-start;flex-direction:column}.detail-tabs{grid-template-columns:1fr 1fr}.cluster-card>summary{grid-template-columns:1fr}.cluster-stats{justify-content:flex-start}.detail-filters{display:grid}.detail-filters input,.detail-filters select{width:100%;min-width:0}}'
        '@media(max-width:430px){.detail-tab{padding:10px 12px}.result-meta{align-items:flex-start;flex-direction:column}}@media(prefers-color-scheme:dark){:root{--canvas:#111a29;--surface:#172235;--ink:#eff6ff;--muted:#b1c0d3;--line:#34455e;--accent:#7dd3fc;--accent-soft:#12324a;--red:#ffb4ac;--red-soft:#482523;--amber:#ffd68a;--amber-soft:#423313;--shadow:0 12px 32px rgba(0,0,0,.2)}.workspace-nav a[aria-current=page]{background:#7dd3fc;color:#082f49}.tag{background:#2a3950;color:#d7e2ef}.detail-tab--active{color:#bae6fd}.evidence-actions a:first-child{background:#7dd3fc;color:#082f49}}'
        '</style></head><body><main class="page">'
        '<nav class="workspace-nav" aria-label="舆情工作台"><a href="/review'
        + _q(token)
        + '">事件复核</a><a href="/review/analysis'
        + _q(token, days=days, platform=filters.get("platform"), campaign=filters.get("campaign"), topic=filters.get("topic"))
        + '" aria-current="page">分析看板</a><form class="logout-form" method="post" action="/auth/logout"><input type="hidden" name="csrf" value="'
        + escape(csrf_token, quote=True)
        + '"><button class="logout-button" type="submit">退出</button></form></nav>'
        '<div class="breadcrumbs"><a href="/review/analysis'
        + _q(token, days=days, platform=filters.get("platform"), campaign=filters.get("campaign"), topic=filters.get("topic"))
        + '">分析看板</a> / 传播数据明细</div>'
        '<header class="header-row"><div><p class="eyebrow">OCOOPA / 证据追溯</p><h1>传播数据明细</h1><p class="subtitle">从汇总数字进入传播簇和原文证据。</p></div><div class="scope">'
        + escape(" · ".join(scope_parts))
        + '</div></header>'
        + filter_form
        + '<nav class="detail-tabs" aria-label="传播数据分类">'
        + tabs
        + '</nav><section class="definition"><strong>'
        + escape(metric_config["label"])
        + '</strong><p>'
        + escape(metric_config["description"])
        + '</p></section><div class="result-meta"><span>共 '
        + str(view["total_items"])
        + ' 项</span><span>当前显示 '
        + str(view["range_start"])
        + '–'
        + str(view["range_end"])
        + '</span></div>'
        + content
        + pagination
        + '<p class="method-note">说明：传播簇与新增实质信号由规则自动归纳，页面只用于证据追溯与工作分流，不替代事实确认、责任判断或法律结论。</p>'
        '</main></body></html>'
    )


def _render_cluster(
    cluster: Dict[str, Any], days: int, filters: Dict[str, str], token: str
) -> str:
    role_text = "、".join(
        f"{ROLE_LABELS.get(role, role)} {count}"
        for role, count in sorted(cluster["role_counts"].items(), key=lambda item: (-item[1], item[0]))
    )
    risk = str(cluster["risk_level"])
    tags = [f'<span class="tag tag--{escape(risk, quote=True)}">{escape(_risk_label(risk))}</span>']
    if cluster["has_substantive_update"]:
        tags.append('<span class="tag tag--substantive">待复核实质信号</span>')
    tags.extend(
        [
            f'<span class="tag">{int(cluster["link_count"])} 个链接</span>',
            f'<span class="tag">{len(cluster["platforms"])} 个平台</span>',
        ]
    )
    members = "".join(_render_mention(row, days, filters, token) for row in cluster["members"])
    return (
        '<details class="cluster-card"><summary><div><div class="evidence-meta">'
        f'<span>{escape(str(cluster["cluster_label"]))}</span></div>'
        f'<h2 class="cluster-title">{escape(str(cluster["representative_title"]))}</h2>'
        f'<p class="cluster-description">{escape(str(cluster["representative_summary"]))}</p></div>'
        f'<div class="cluster-stats">{"".join(tags)}</div></summary><div class="cluster-body">'
        '<div class="cluster-facts">'
        f'<span>平台：{escape("、".join(cluster["platforms"]))}</span>'
        f'<span>传播角色：{escape(role_text or "未分类")}</span>'
        f'<span>最早发布：{escape(_display_time(cluster["first_published_at"]))}</span>'
        f'<span>最近发现：{escape(_display_time(cluster["latest_discovered_at"]))}</span>'
        f'</div>{members}</div></details>'
    )


def _render_detail_tab(
    name: str,
    config: Dict[str, str],
    active_metric: str,
    view: Dict[str, Any],
    days: int,
    filters: Dict[str, str],
    token: str,
) -> str:
    active = name == active_metric
    current = ' aria-current="page"' if active else ""
    href = "/review/analysis/details" + _q(
        token,
        metric=name,
        days=days,
        platform=filters.get("platform"),
        campaign=filters.get("campaign"),
        topic=filters.get("topic"),
    )
    return (
        f'<a class="detail-tab{(" detail-tab--active" if active else "")}" '
        f'href="{escape(href, quote=True)}"{current}>'
        f'<span>{escape(config["short_label"])}</span>'
        f'<strong>{int(view["counts"].get(name, 0))}</strong></a>'
    )


def _render_mention(
    row: Dict[str, Any], days: int, filters: Dict[str, str], token: str
) -> str:
    risk = str(row.get("risk_level") or "unknown")
    source_url = _safe_url(row.get("source_url"))
    row_platform = str(row.get("platform") or "web")
    row_campaign = str(row.get("campaign") or filters.get("campaign") or "")
    review_href = "/review" + _q(
        token,
        limit=200,
        days=days,
        platform=row_platform,
        campaign=row_campaign,
    ) + (f'#mention-{int(row["id"])}' if row.get("id") else "")
    original = (
        f'<a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">查看原文证据 ↗</a>'
        if source_url else '<span class="tag">原文链接不可用</span>'
    )
    manual_comment_review = (
        f'<a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">人工查看评论 ↗</a>'
        if is_public_social_parent_post(row) and source_url else ""
    )
    metrics = " · ".join(
        f"{label} {int(row[key])}"
        for key, label in (("view_count", "浏览"), ("like_count", "点赞"), ("comment_count", "评论"), ("share_count", "分享"))
        if row.get(key) is not None
    )
    return (
        f'<article class="evidence-row" id="detail-mention-{int(row.get("id") or 0)}"><div class="evidence-meta">'
        f'<span class="tag tag--{escape(risk, quote=True)}">{escape(_risk_label(risk))}</span>'
        f'<span class="tag">{escape(str(row.get("story_role_label") or "独立讨论"))}</span>'
        f'<span class="tag">{escape(str(row.get("topic_primary_label") or "其他讨论"))}</span>'
        f'<span>{escape(row_platform.upper())}</span><span>{escape(str(row.get("source_name") or "未知来源"))}</span></div>'
        f'<h3>{escape(str(row.get("title") or "未命名内容"))}</h3>'
        f'<p>{escape(str(row.get("summary_zh") or row.get("text_excerpt") or "暂无摘要，请查看原文。"))}</p>'
        '<div class="evidence-facts">'
        f'<span>发布：{escape(_display_time(row.get("published_at")))}</span>'
        f'<span>首次发现：{escape(_display_time(row.get("first_seen_at") or row.get("fetched_at")))}</span>'
        f'<span>最近抓取：{escape(_display_time(row.get("fetched_at")))}</span>'
        f'<span>处置：{escape(str(row.get("response_status") or "待判断"))}</span>'
        + (f'<span>{escape(metrics)}</span>' if metrics else '<span>平台未提供互动数据</span>')
        + '</div><div class="evidence-actions">'
        + original
        + manual_comment_review
        + f'<a href="{escape(review_href, quote=True)}">进入复核页</a></div></article>'
    )


def _render_pagination(
    view: Dict[str, Any], days: int, filters: Dict[str, str], token: str
) -> str:
    if int(view["total_pages"]) <= 1:
        return ""
    page = int(view["page"])
    common = {
        "metric": view["metric"],
        "days": days,
        "platform": filters.get("platform"),
        "campaign": filters.get("campaign"),
        "topic": filters.get("topic"),
    }
    previous = (
        f'<a href="/review/analysis/details{_q(token, page=page - 1, **common)}">← 上一页</a>'
        if page > 1 else '<span>← 上一页</span>'
    )
    following = (
        f'<a href="/review/analysis/details{_q(token, page=page + 1, **common)}">下一页 →</a>'
        if page < int(view["total_pages"]) else '<span>下一页 →</span>'
    )
    return f'<nav class="pagination" aria-label="分页">{previous}<span>第 {page} / {int(view["total_pages"])} 页</span>{following}</nav>'


def _q(token: str, **extra: Any) -> str:
    params = {key: value for key, value in extra.items() if value not in (None, "")}
    if token:
        params["token"] = token
    return "?" + urlencode(params) if params else ""


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if urlsplit(url).scheme in {"http", "https"} else ""


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _display_time(value: Any) -> str:
    parsed = _as_datetime(value)
    return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M") if parsed else "未知"


def _row_sort_key(row: Dict[str, Any]) -> tuple:
    timestamp = _as_datetime(
        row.get("first_seen_at") or row.get("fetched_at")
    ) or datetime.min.replace(tzinfo=timezone.utc)
    return (
        bool(row.get("has_substantive_update")),
        {"red": 3, "yellow": 2, "green": 1}.get(str(row.get("risk_level")), 0),
        str(row.get("notification_priority")) == "urgent",
        timestamp.timestamp(),
    )


def _cluster_sort_key(cluster: Dict[str, Any]) -> tuple:
    timestamp = _as_datetime(cluster.get("latest_discovered_at")) or datetime.min.replace(tzinfo=timezone.utc)
    return (
        bool(cluster.get("has_substantive_update")),
        {"red": 3, "yellow": 2, "green": 1}.get(str(cluster.get("risk_level")), 0),
        timestamp.timestamp(),
        int(cluster.get("link_count") or 0),
    )


def _highest_risk(risks: Counter) -> str:
    for risk in ("red", "yellow", "green"):
        if risks.get(risk):
            return risk
    return "unknown"


def _risk_label(risk: str) -> str:
    return {"red": "红色", "yellow": "黄色", "green": "绿色"}.get(risk, "未定级")
