from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo


_CATEGORY_LABELS = {
    "lawsuit": "诉讼与索赔",
    "recall": "召回与监管",
    "media_report": "媒体报道",
    "user_complaint": "用户投诉",
    "promotion": "营销活动",
    "kol_review": "KOL / 测评",
    "other": "其他讨论",
    "unknown": "未分类",
}

_SENTIMENT_LABELS = {
    "negative": "负面",
    "neutral": "中性",
    "positive": "正面",
    "unknown": "未判定",
}

_IGNORED_KEYWORDS = {"search_api_query_hit"}


def _rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


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


def _matched_keywords(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = [value]
        values = decoded if isinstance(decoded, list) else [decoded]
    else:
        values = []
    result: List[str] = []
    for item in values:
        keyword = " ".join(str(item or "").split()).strip()
        if keyword and keyword.lower() not in _IGNORED_KEYWORDS:
            result.append(keyword)
    return result


def _trend_summary(daily: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if len(daily) < 2:
        return {
            "direction": "flat",
            "label": "数据窗口不足，暂不判断趋势",
            "delta_percent": None,
            "current_average": float(daily[-1]["total"] if daily else 0),
            "previous_average": 0.0,
            "comparison_days": 0,
        }
    comparison_days = 7 if len(daily) >= 14 else min(3, len(daily) // 2)
    current = daily[-comparison_days:]
    previous = daily[-comparison_days * 2 : -comparison_days]
    current_average = sum(item["total"] for item in current) / comparison_days
    previous_average = sum(item["total"] for item in previous) / comparison_days
    if previous_average == 0:
        if current_average == 0:
            direction, label, delta = "flat", "近期日均提及量保持为 0", 0.0
        else:
            direction, label, delta = (
                "up",
                f"最近 {comparison_days} 天日均 {current_average:.1f} 条，上一周期为 0",
                None,
            )
    else:
        delta = (current_average - previous_average) / previous_average * 100
        if delta >= 10:
            direction = "up"
            verb = "上升"
        elif delta <= -10:
            direction = "down"
            verb = "下降"
        else:
            direction = "flat"
            verb = "基本持平"
        label = (
            f"最近 {comparison_days} 天日均 {current_average:.1f} 条，"
            f"较上一周期{verb} {abs(delta):.1f}%"
        )
    return {
        "direction": direction,
        "label": label,
        "delta_percent": delta,
        "current_average": current_average,
        "previous_average": previous_average,
        "comparison_days": comparison_days,
    }


def _build_analysis(stats: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    total = stats["total"]
    risk = stats["risk"]
    sentiment = stats["sentiment"]
    review_needed = stats["review_needed"]
    trend = stats["trend"]
    top_categories = stats["category_ranked"][:3]
    top_keywords = stats["keywords"][:5]

    if total:
        summaries = [
            (
                f"近 {stats['window_days']} 天共收录 {total} 条提及，涉及 "
                f"{stats['unique_events']} 个事件指纹；{trend['label']}。"
            )
        ]
        negative = sentiment.get("negative", 0)
        summaries.append(
            f"红色 {risk.get('red', 0)} 条、黄色 {risk.get('yellow', 0)} 条；"
            f"负面 {negative} 条，占比 {negative / total:.1%}；"
            f"其中 {review_needed} 条需要人工核实。"
        )
        category_text = "、".join(f"{label} {count}" for _, label, count in top_categories) or "暂无明确分类"
        keyword_text = "、".join(f"{item['keyword']} {item['count']}" for item in top_keywords) or "暂无可归纳关键词"
        summaries.append(f"主要议题集中在：{category_text}。高频监测词：{keyword_text}。")
        if stats["backfill_mentions"]:
            summaries.append(
                f"其中 {stats['backfill_mentions']} 条为历史回溯，已计入总量与内容归纳，"
                "但不计入每日新增走线和趋势涨跌。"
            )
    else:
        summaries = [
            f"近 {stats['window_days']} 天未收录相关提及，不能据此判断舆情已消失，需要同时检查数据源健康状态。"
        ]

    recommendations: List[str] = []
    if not total:
        recommendations.append("检查高敏、常规和 CPSC 数据源的最近成功抓取时间，排除采集异常。")
    if risk.get("red", 0):
        recommendations.append("优先完成红色风险的原文、证据和事件归并复核，再统一对外口径。")
    if review_needed:
        recommendations.append(f"先处理 {review_needed} 条需人工核实内容，核验前不得作为确证事实外传。")
    if trend["direction"] == "up":
        recommendations.append("提及量进入上升周期，建议提高重点平台巡检频率并观察是否出现跨来源扩散。")
    if top_categories and top_categories[0][0] in {"lawsuit", "recall"}:
        recommendations.append(
            f"当前首要议题为“{top_categories[0][1]}”，建议同步 PR、法务与客服准备事实清单和标准答复。"
        )
    if stats["source_concentration"] >= 0.6 and total >= 5:
        recommendations.append("当前信息较集中于少数来源，建议用第二来源交叉验证，避免把单点重复传播当作独立事件。")
    if not recommendations:
        recommendations.append("维持现有监测节奏，持续观察高频关键词是否出现新的风险语境或跨平台传播。")
    return summaries, recommendations[:4]


def compute_dashboard(
    rows: Iterable[Any],
    alerts: Iterable[Any],
    window_days: int = 30,
    timezone_name: str = "Asia/Shanghai",
    now: Optional[datetime] = None,
    source_health: Iterable[Any] = (),
) -> Dict[str, Any]:
    data = _rows(rows)
    alert_rows = _rows(alerts)
    health_rows = _rows(source_health)
    days = min(max(int(window_days), 1), 366)
    tz = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_today = current.astimezone(tz).date()
    dates = [local_today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    daily_by_date: Dict[date, Dict[str, Any]] = {
        day: {"date": day.isoformat(), "total": 0, "published_total": 0, "red": 0, "yellow": 0, "negative": 0}
        for day in dates
    }
    risk = Counter(str(row.get("risk_level") or "unknown") for row in data)
    sentiment = Counter(str(row.get("sentiment") or "unknown") for row in data)
    sources = Counter(str(row.get("source_name") or "未知来源") for row in data)
    categories = Counter(str(row.get("category") or "unknown") for row in data)
    platforms = Counter(str(row.get("platform") or "web") for row in data)
    content_types = Counter(str(row.get("content_type") or "article") for row in data)
    campaigns = Counter(str(row.get("campaign") or "brand_major_risk") for row in data)
    response_statuses = Counter(str(row.get("response_status") or "待判断") for row in data)
    keyword_counter: Counter[str] = Counter()
    keyword_labels: Dict[str, str] = {}
    platform_by_day: Dict[str, Counter[date]] = {}
    event_platforms: Dict[str, set] = {}
    event_titles: Dict[str, str] = {}
    for row in data:
        timestamp = _as_datetime(row.get("fetched_at"))
        if timestamp:
            day = timestamp.astimezone(tz).date()
            if day in daily_by_date and not bool(row.get("backfill")):
                bucket = daily_by_date[day]
                bucket["total"] += 1
                risk_level = str(row.get("risk_level") or "unknown")
                if risk_level in {"red", "yellow"}:
                    bucket[risk_level] += 1
                if str(row.get("sentiment") or "unknown") == "negative":
                    bucket["negative"] += 1
                platform_name = str(row.get("platform") or "web")
                platform_by_day.setdefault(platform_name, Counter())[day] += 1
        event_key = str(row.get("event_fingerprint") or "")
        if event_key:
            event_platforms.setdefault(event_key, set()).add(str(row.get("platform") or "web"))
            event_titles.setdefault(event_key, str(row.get("title") or "未命名事件"))
        published = _as_datetime(row.get("published_at"))
        if published:
            published_day = published.astimezone(tz).date()
            if published_day in daily_by_date:
                daily_by_date[published_day]["published_total"] += 1
        for keyword in _matched_keywords(row.get("matched_keywords")):
            normalized = keyword.casefold()
            keyword_counter[normalized] += 1
            keyword_labels.setdefault(normalized, keyword)

    daily = list(daily_by_date.values())
    trend_source = daily[:-1] if len(daily) > 1 else daily
    trend = _trend_summary(trend_source)
    if trend["comparison_days"]:
        prefix = f"最近 {trend['comparison_days']} 天日均"
        trend["label"] = trend["label"].replace(
            prefix, f"最近 {trend['comparison_days']} 个完整日日均", 1
        )
    top: List[Dict[str, Any]] = []
    seen_events = set()
    for row in data:
        if row.get("risk_level") not in {"red", "yellow"}:
            continue
        identity = str(
            row.get("event_fingerprint")
            or row.get("canonical_url")
            or row.get("source_url")
            or row.get("title")
            or ""
        )
        if identity and identity in seen_events:
            continue
        if identity:
            seen_events.add(identity)
        top.append(row)
        if len(top) >= 20:
            break
    total = len(data)
    social_platforms = {"tiktok", "instagram", "facebook", "youtube", "x", "reddit", "social", "review"}
    surge_count = sum(
        int(row.get("view_count") or 0) >= 10000
        or sum(int(row.get(key) or 0) for key in ("like_count", "comment_count", "share_count")) >= 500
        for row in data
    )
    top_spread = sorted(
        data,
        key=lambda row: (
            int(row.get("view_count") or 0),
            sum(int(row.get(key) or 0) for key in ("like_count", "comment_count", "share_count")),
        ),
        reverse=True,
    )[:10]
    coverage = []
    for platform, count in sorted(platforms.items(), key=lambda item: (-item[1], item[0])):
        platform_rows = [row for row in data if str(row.get("platform") or "web") == platform]
        delays = sorted(
            int(row["discovery_latency_seconds"])
            for row in platform_rows if row.get("discovery_latency_seconds") is not None
        )
        coverage.append(
            {
                "platform": platform,
                "count": count,
                "tiers": sorted({str(row.get("coverage_tier") or "public_index") for row in platform_rows}),
                "last_success": max((str(row.get("fetched_at") or "") for row in platform_rows), default=""),
                "p95_latency": delays[min(len(delays) - 1, int(len(delays) * 0.95))] if delays else None,
            }
        )
    delivery_latencies = []
    for row in data:
        queued_at = _as_datetime(row.get("notification_created_at"))
        delivered_at = _as_datetime(row.get("notification_delivered_at"))
        if queued_at and delivered_at and delivered_at >= queued_at:
            delivery_latencies.append(int((delivered_at - queued_at).total_seconds()))
    delivery_latencies.sort()
    delivery_p95 = (
        delivery_latencies[min(len(delivery_latencies) - 1, int(len(delivery_latencies) * 0.95))]
        if delivery_latencies else None
    )
    platform_trends = [
        {
            "platform": platform,
            "values": [platform_by_day.get(platform, {}).get(day, 0) for day in dates],
        }
        for platform, _ in sorted(platforms.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]
    cross_platform_events = sorted(
        (
            {
                "title": event_titles[event_key],
                "platforms": sorted(values),
                "platform_count": len(values),
            }
            for event_key, values in event_platforms.items() if len(values) > 1
        ),
        key=lambda item: (-item["platform_count"], item["title"]),
    )[:10]
    source_ranked = sorted(sources.items(), key=lambda item: (-item[1], item[0]))
    category_ranked = [
        (name, _CATEGORY_LABELS.get(name, name), count)
        for name, count in sorted(categories.items(), key=lambda item: (-item[1], item[0]))
    ]
    stats: Dict[str, Any] = {
        "window_days": days,
        "timezone": timezone_name,
        "generated_at": current.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "valid_mentions": total,
        "social_mentions": sum(str(row.get("platform") or "web") in social_platforms for row in data),
        "urgent_mentions": sum(str(row.get("notification_priority") or "standard") == "urgent" for row in data),
        "pending_responses": sum(str(row.get("response_status") or "待判断") in {"待判断", "建议回应"} for row in data),
        "surge_mentions": surge_count,
        "alerts": len(alert_rows),
        "unique_events": len({str(row.get("event_fingerprint")) for row in data if row.get("event_fingerprint")}),
        "new_mentions": sum(bool(row.get("is_new")) for row in data),
        "monitored_mentions": sum(not bool(row.get("backfill")) for row in data),
        "backfill_mentions": sum(bool(row.get("backfill")) for row in data),
        "review_needed": sum(bool(row.get("needs_human_review")) for row in data),
        "risk": dict(risk),
        "sentiment": dict(sentiment),
        "source": dict(sources),
        "source_ranked": source_ranked,
        "source_concentration": (source_ranked[0][1] / total) if source_ranked and total else 0.0,
        "category": dict(categories),
        "platform": dict(platforms),
        "content_type": dict(content_types),
        "campaign": dict(campaigns),
        "response_status": dict(response_statuses),
        "coverage": coverage,
        "source_health": health_rows,
        "delivery_p95_seconds": delivery_p95,
        "top_spread": top_spread,
        "platform_trends": platform_trends,
        "cross_platform_events": cross_platform_events,
        "category_ranked": category_ranked,
        "keywords": [
            {"keyword": keyword_labels[keyword], "count": count}
            for keyword, count in sorted(keyword_counter.items(), key=lambda item: (-item[1], item[0]))[:12]
        ],
        "daily": daily,
        "trend": trend,
        "top": top,
    }
    summaries, recommendations = _build_analysis(stats)
    stats["summaries"] = summaries
    stats["recommendations"] = recommendations
    return stats


def _q(token: str, **extra: Any) -> str:
    params = {key: value for key, value in extra.items() if value not in (None, "")}
    if token:
        params["token"] = token
    return ("?" + urlencode(params)) if params else ""


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if urlsplit(url).scheme in {"http", "https"} else ""


def _polyline(values: Sequence[int], width: int, height: int, left: int, top: int, maximum: int) -> str:
    if not values:
        return ""
    inner_width = width - left - 24
    inner_height = height - top - 48
    denominator = max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = left + (inner_width * index / denominator)
        y = top + inner_height - (inner_height * value / maximum)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _trend_chart(daily: Sequence[Dict[str, Any]]) -> str:
    width, height, left, top = 940, 300, 48, 22
    maximum = max([max(item["total"], item.get("published_total", 0)) for item in daily] + [1])
    total_points = _polyline([item["total"] for item in daily], width, height, left, top, maximum)
    published_points = _polyline([item.get("published_total", 0) for item in daily], width, height, left, top, maximum)
    red_points = _polyline([item["red"] for item in daily], width, height, left, top, maximum)
    yellow_points = _polyline([item["yellow"] for item in daily], width, height, left, top, maximum)
    inner_height = height - top - 48
    grid_parts: List[str] = []
    tick_values = sorted({0, maximum, max(1, round(maximum / 2))})
    for value in tick_values:
        y = top + inner_height - inner_height * value / maximum
        grid_parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - 24}" y2="{y:.1f}" class="chart-grid"/>'
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" class="chart-label">{value}</text>'
        )
    label_parts: List[str] = []
    if daily:
        step = max(1, (len(daily) - 1) // 5)
        label_indexes = sorted(set(list(range(0, len(daily), step)) + [len(daily) - 1]))
        denominator = max(len(daily) - 1, 1)
        for index in label_indexes:
            x = left + (width - left - 24) * index / denominator
            label_parts.append(
                f'<text x="{x:.1f}" y="{height - 16}" text-anchor="middle" class="chart-label">'
                f'{escape(daily[index]["date"][5:])}</text>'
            )
    return (
        '<svg class="trend-chart" viewBox="0 0 940 300" role="img" '
        'aria-label="每日新增舆情提及量、红色风险和黄色风险趋势折线图">'
        + "".join(grid_parts)
        + "".join(label_parts)
        + f'<polyline points="{total_points}" class="chart-line chart-line--total"/>'
        + f'<polyline points="{published_points}" class="chart-line chart-line--published"/>'
        + f'<polyline points="{red_points}" class="chart-line chart-line--red"/>'
        + f'<polyline points="{yellow_points}" class="chart-line chart-line--yellow"/>'
        + "</svg>"
    )


def _coverage_rows(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="5">暂无平台覆盖数据</td></tr>'
    return "".join(
        f'<tr><td>{escape(str(row.get("platform") or "web"))}</td><td>{row.get("count", 0)}</td>'
        f'<td>{escape(" / ".join(row.get("tiers") or []))}</td>'
        f'<td>{escape(str(row.get("last_success") or "未知"))}</td>'
        f'<td>{escape(str(row.get("p95_latency") if row.get("p95_latency") is not None else "无供应商延迟数据"))}</td></tr>'
        for row in rows
    )


def _source_health_rows(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="6">暂无采集源健康数据</td></tr>'
    return "".join(
        f'<tr><td>{escape(str(row.get("source_name") or "未知"))}</td>'
        f'<td>{escape(str(row.get("lane") or "未知"))}</td>'
        f'<td>{escape(str(row.get("health_status") or "unknown"))}</td>'
        f'<td>{escape(str(row.get("last_success_at") or "尚未成功"))}</td>'
        f'<td>{escape(str(row.get("last_attempt_at") or "尚未运行"))}</td>'
        f'<td>{int(row.get("consecutive_failures") or 0)}</td></tr>'
        for row in rows
    )


def _spread_rows(rows: Sequence[Dict[str, Any]]) -> str:
    measured = [row for row in rows if any(row.get(key) is not None for key in ("view_count", "like_count", "comment_count", "share_count"))]
    if not measured:
        return '<tr><td colspan="6">当前来源未提供互动数据，不推断传播等级。</td></tr>'
    return "".join(
        f'<tr><td>{escape(str(row.get("platform") or "web"))}</td><td>{escape(str(row.get("title") or "未命名内容"))}</td>'
        f'<td>{row.get("view_count") if row.get("view_count") is not None else "—"}</td>'
        f'<td>{row.get("like_count") if row.get("like_count") is not None else "—"}</td>'
        f'<td>{row.get("comment_count") if row.get("comment_count") is not None else "—"}</td>'
        f'<td>{row.get("share_count") if row.get("share_count") is not None else "—"}</td></tr>'
        for row in measured[:10]
    )


def _platform_trend_rows(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="3">暂无平台趋势数据</td></tr>'
    return "".join(
        f'<tr><td>{escape(str(row.get("platform") or "web"))}</td>'
        f'<td>{sum(row.get("values") or [])}</td><td>{escape(" · ".join(str(value) for value in (row.get("values") or [])[-7:]))}</td></tr>'
        for row in rows
    )


def _cross_platform_rows(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="3">当前窗口内尚未识别到跨平台扩散事件。</td></tr>'
    return "".join(
        f'<tr><td>{escape(str(row.get("title") or "未命名事件"))}</td><td>{row.get("platform_count", 0)}</td>'
        f'<td>{escape(" / ".join(row.get("platforms") or []))}</td></tr>'
        for row in rows
    )


def _distribution_rows(values: Dict[str, int], labels: Dict[str, str], total: int, limit: int = 7) -> str:
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))[:limit]
    if not ordered:
        return '<p class="empty-inline">暂无数据</p>'
    maximum = max(count for _, count in ordered) or 1
    return "".join(
        '<div class="distribution-row">'
        f'<span>{escape(labels.get(name, name))}</span>'
        f'<div class="distribution-bar"><i style="width:{count / maximum * 100:.1f}%"></i></div>'
        f'<strong>{count}</strong><small>{count / total:.1%}</small></div>'
        for name, count in ordered
    )


def _render_top_risks(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty-inline">当前时间窗内暂无红色或黄色风险。</div>'
    cards: List[str] = []
    for row in rows[:6]:
        risk = str(row.get("risk_level") or "yellow")
        title = escape(str(row.get("title") or "未命名事件"))
        summary = escape(str(row.get("summary_zh") or "暂无摘要，请查看原始来源。"))
        source_name = escape(str(row.get("source_name") or "未知来源"))
        source_url = _safe_url(row.get("source_url"))
        link = (
            f'<a href="{escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">查看原文</a>'
            if source_url
            else '<span>原文链接不可用</span>'
        )
        review = '<span class="mini-tag">需人工核实</span>' if row.get("needs_human_review") else ""
        cards.append(
            f'<article class="risk-item risk-item--{risk}"><div class="risk-item__meta">'
            f'<span class="risk-label risk-label--{risk}">{"红色" if risk == "red" else "黄色"}</span>{review}'
            f'<span>{source_name}</span></div><h3>{title}</h3><p>{summary}</p><footer>{link}</footer></article>'
        )
    return "".join(cards)


def render_dashboard(stats: Dict[str, Any], window_days: int, token: str = "", csrf_token: str = "") -> str:
    days = int(stats.get("window_days") or window_days)
    filters = stats.get("filters") or {}
    trend_class = str(stats.get("trend", {}).get("direction") or "flat")
    date_links = "".join(
        f'<a href="/review/analysis{_q(token, days=value, platform=filters.get("platform"), campaign=filters.get("campaign"))}" '
        f'class="period-link{(" period-link--active" if value == days else "")}">{value} 天</a>'
        for value in (7, 14, 30, 90)
    )
    keyword_html = "".join(
        f'<span class="keyword-chip">{escape(item["keyword"])} <b>{item["count"]}</b></span>'
        for item in stats.get("keywords", [])
    ) or '<span class="empty-inline">暂无可归纳关键词</span>'
    summary_html = "".join(f"<li>{escape(item)}</li>" for item in stats.get("summaries", []))
    recommendation_html = "".join(
        f"<li>{escape(item)}</li>" for item in stats.get("recommendations", [])
    )
    category_labels = {name: label for name, label, _ in stats.get("category_ranked", [])}
    source_rows = _distribution_rows(stats.get("source", {}), {}, max(stats.get("total", 0), 1), 6)
    category_rows = _distribution_rows(
        stats.get("category", {}), category_labels, max(stats.get("total", 0), 1), 6
    )
    sentiment_rows = _distribution_rows(
        stats.get("sentiment", {}), _SENTIMENT_LABELS, max(stats.get("total", 0), 1), 4
    )
    platform_rows = _distribution_rows(stats.get("platform", {}), {}, max(stats.get("total", 0), 1), 8)
    campaign_rows = _distribution_rows(
        stats.get("campaign", {}),
        {"recall_26_659": "召回 26-659", "brand_major_risk": "品牌重大风险"},
        max(stats.get("total", 0), 1),
        4,
    )
    response_rows = _distribution_rows(stats.get("response_status", {}), {}, max(stats.get("total", 0), 1), 6)
    daily_rows = "".join(
        f'<tr><td>{escape(item["date"])}</td><td>{item.get("published_total", 0)}</td><td>{item["total"]}</td><td>{item["red"]}</td>'
        f'<td>{item["yellow"]}</td><td>{item["negative"]}</td></tr>'
        for item in reversed(stats.get("daily", []))
    )
    filter_form = (
        '<form class="dashboard-filters" method="get" action="/review/analysis">'
        f'<input type="hidden" name="days" value="{days}">'
        f'<label>平台<input name="platform" value="{escape(str(filters.get("platform") or ""), quote=True)}" placeholder="tiktok / reddit"></label>'
        '<label>监测主题<select name="campaign"><option value="">全部</option>'
        f'<option value="recall_26_659"{" selected" if filters.get("campaign") == "recall_26_659" else ""}>召回 26-659</option>'
        f'<option value="brand_major_risk"{" selected" if filters.get("campaign") == "brand_major_risk" else ""}>品牌重大风险</option></select></label>'
        '<button type="submit">应用筛选</button></form>'
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><title>OCOOPA 舆情分析看板</title>'
        '<style>'
        ':root{color-scheme:light dark;--canvas:#f3f6fa;--surface:#fff;--ink:#14213a;--muted:#617086;--line:#d8e0ea;'
        '--accent:#075985;--accent-soft:#e8f3f9;--red:#b42318;--red-soft:#fff1f0;--amber:#936300;--amber-soft:#fff7df;'
        '--green:#1f6b49;--shadow:0 12px 32px rgba(15,35,60,.07);--radius:12px}*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}'
        '.page{max-width:1240px;margin:0 auto;padding:24px 20px 60px}.workspace-nav{display:flex;gap:6px;margin-bottom:26px;padding:5px;border:1px solid var(--line);border-radius:10px;background:var(--surface);width:max-content}'
        '.workspace-nav a,.logout-button{padding:8px 13px;border:0;border-radius:7px;background:transparent;color:var(--muted);font:inherit;font-weight:700;text-decoration:none;cursor:pointer}.workspace-nav a[aria-current="page"]{background:var(--accent);color:#fff}.logout-form{margin:0}'
        '.header-row{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;margin-bottom:22px}.eyebrow{margin:0 0 6px;color:var(--accent);font-size:.78rem;font-weight:800;letter-spacing:.07em}'
        'h1,h2,h3,p{margin-top:0}h1{margin-bottom:8px;font-size:clamp(1.8rem,4vw,2.4rem);letter-spacing:-.03em}.subtitle{margin:0;color:var(--muted)}.header-actions{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}'
        '.period-link,.secondary-link{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink);font-size:.86rem;font-weight:700;text-decoration:none;white-space:nowrap}.period-link--active{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}'
        '.dashboard-filters{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:0 0 16px;padding:12px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface)}.dashboard-filters label{display:grid;gap:4px;color:var(--muted);font-size:.76rem;font-weight:700}.dashboard-filters input,.dashboard-filters select{min-width:170px;padding:8px;border:1px solid var(--line);border-radius:7px;background:var(--surface);color:var(--ink)}.dashboard-filters button{min-height:36px;padding:8px 12px;border:0;border-radius:7px;background:var(--accent);color:#fff;font-weight:700}'
        '.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-bottom:16px}.metric{padding:15px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:0 2px 8px rgba(15,35,60,.03)}'
        '.metric span{display:block;color:var(--muted);font-size:.82rem}.metric strong{display:block;margin:4px 0;font-size:1.75rem;line-height:1.1}.metric small{color:var(--muted)}.metric--risk{border-color:#efb4ae;background:var(--red-soft)}'
        '.trend-note{display:inline-flex;margin-top:4px;padding:3px 8px;border-radius:999px;font-size:.78rem;font-weight:800}.trend-note--up{color:#8a1c14;background:var(--red-soft)}.trend-note--down{color:#15533a;background:#effaf4}.trend-note--flat{color:#4a5a70;background:#eef2f6}'
        '.grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,.8fr);gap:16px;margin-bottom:16px}.panel{min-width:0;padding:20px;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:var(--shadow)}'
        '.panel-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:14px}.panel h2{margin-bottom:4px;font-size:1.08rem}.panel-description{margin:0;color:var(--muted);font-size:.86rem}.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:.78rem}.legend i{display:inline-block;width:15px;height:3px;margin-right:5px;vertical-align:middle}.legend-published{background:#6d4db3}.legend-total{background:#075985}.legend-red{background:#b42318}.legend-yellow{background:#d69e2e}'
        '.trend-chart{display:block;width:100%;height:auto;min-height:240px}.chart-grid{stroke:var(--line);stroke-width:1}.chart-label{fill:var(--muted);font-size:11px}.chart-line{fill:none;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}.chart-line--total{stroke:#075985}.chart-line--published{stroke:#6d4db3;stroke-dasharray:7 5}.chart-line--red{stroke:#b42318}.chart-line--yellow{stroke:#d69e2e}'
        '.analysis-list,.recommendation-list{margin:0;padding-left:1.25rem}.analysis-list li,.recommendation-list li{margin:.65rem 0}.recommendation-list li::marker{color:var(--accent);font-weight:800}.recommendation-head{margin-top:22px}.keyword-panel{margin-bottom:16px}'
        '.distribution-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-bottom:16px}.distribution-row{display:grid;grid-template-columns:minmax(72px,1fr) 1.5fr 32px 44px;gap:8px;align-items:center;margin:10px 0;font-size:.82rem}.distribution-row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.distribution-row strong{text-align:right}.distribution-row small{color:var(--muted);text-align:right}.distribution-bar{height:7px;border-radius:999px;background:#e9eef4;overflow:hidden}.distribution-bar i{display:block;height:100%;border-radius:inherit;background:var(--accent)}'
        '.keyword-wrap{display:flex;flex-wrap:wrap;gap:8px}.keyword-chip{padding:7px 9px;border:1px solid #bfd2df;border-radius:8px;background:var(--accent-soft);color:#19475f;font-size:.84rem}.keyword-chip b{margin-left:4px}.risk-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.risk-item{padding:14px;border:1px solid var(--line);border-left:4px solid var(--amber);border-radius:10px;background:var(--surface)}.risk-item--red{border-left-color:var(--red)}'
        '.risk-item__meta{display:flex;flex-wrap:wrap;gap:7px;align-items:center;color:var(--muted);font-size:.76rem}.risk-label,.mini-tag{padding:2px 6px;border-radius:999px;font-weight:800}.risk-label--red{color:#8a1c14;background:var(--red-soft)}.risk-label--yellow,.mini-tag{color:#775000;background:var(--amber-soft)}.risk-item h3{margin:9px 0 6px;font-size:.94rem}.risk-item p{margin-bottom:8px;color:var(--muted);font-size:.84rem}.risk-item footer a{color:var(--accent);font-size:.82rem;font-weight:800;text-underline-offset:3px}.risk-item footer span{color:var(--muted);font-size:.82rem}'
        '.daily-details{margin-top:12px}.daily-details summary{cursor:pointer;color:var(--accent);font-weight:800}.daily-table-wrap{overflow:auto;margin-top:12px}.daily-table{width:100%;border-collapse:collapse;font-size:.82rem}.daily-table th,.daily-table td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}.daily-table th:first-child,.daily-table td:first-child{text-align:left}.empty-inline{color:var(--muted)}'
        '.footer-note{margin-top:18px;color:var(--muted);font-size:.78rem}.secondary-link:focus-visible,.period-link:focus-visible,.workspace-nav a:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid #7dd3fc;outline-offset:2px}'
        '@media(max-width:1100px){.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:900px){.header-row{align-items:flex-start;flex-direction:column}.header-actions{justify-content:flex-start}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.grid,.distribution-grid{grid-template-columns:1fr}.risk-grid{grid-template-columns:1fr}}'
        '@media(max-width:560px){.page{padding:16px 12px 40px}.workspace-nav{width:100%}.workspace-nav a{flex:1;text-align:center}.metrics{grid-template-columns:1fr 1fr}.metric{padding:13px}.metric strong{font-size:1.45rem}.trend-note{display:block;padding:0;background:transparent;font-size:.73rem;line-height:1.35}.header-actions{gap:6px}.panel{padding:15px}.trend-chart{min-height:190px}.distribution-row{grid-template-columns:minmax(68px,1fr) 1fr 28px 42px}}'
        '@media(prefers-color-scheme:dark){:root{--canvas:#111a29;--surface:#172235;--ink:#eff6ff;--muted:#b1c0d3;--line:#34455e;--accent:#7dd3fc;--accent-soft:#12324a;--red:#ffb4ac;--red-soft:#482523;--amber:#ffd68a;--amber-soft:#423313;--shadow:0 12px 32px rgba(0,0,0,.2)}.workspace-nav a[aria-current="page"]{background:#7dd3fc;color:#082f49}.period-link--active{color:#bae6fd}.keyword-chip{border-color:#315d75;color:#cbefff}.distribution-bar{background:#2a3950}.chart-line--total{stroke:#7dd3fc}.chart-line--red{stroke:#ffb4ac}.chart-line--yellow{stroke:#ffd68a}.legend-total{background:#7dd3fc}.legend-red{background:#ffb4ac}.legend-yellow{background:#ffd68a}.trend-note--down{color:#a4e2c0;background:#173a2b}}'
        '</style></head><body><main class="page">'
        '<nav class="workspace-nav" aria-label="舆情工作台"><a href="/review'
        + _q(token)
        + '">事件复核</a><a href="/review/analysis'
        + _q(token, days=days)
        + '" aria-current="page">分析看板</a><form class="logout-form" method="post" action="/auth/logout"><input type="hidden" name="csrf" value="'
        + escape(csrf_token, quote=True)
        + '"><button class="logout-button" type="submit">退出</button></form></nav>'
        '<header class="header-row"><div><p class="eyebrow">OCOOPA / 舆情数据分析</p><h1>从数据判断趋势和下一步</h1>'
        f'<p class="subtitle">近 {days} 个北京时间自然日，更新于 {escape(str(stats.get("generated_at") or "未知"))}</p></div>'
        f'<div class="header-actions">{date_links}<a class="secondary-link" href="/console/search{_q(token, days=days)}">检索</a>'
        f'<a class="secondary-link" href="/console/export.csv{_q(token, days=days)}">导出 CSV</a></div></header>'
        + filter_form
        + '<section class="metrics" aria-label="舆情核心指标">'
        f'<article class="metric"><span>有效提及</span><strong>{stats.get("valid_mentions", 0)}</strong><small>回溯 {stats.get("backfill_mentions", 0)} · 投递 P95 {stats.get("delivery_p95_seconds") if stats.get("delivery_p95_seconds") is not None else "暂无"}秒</small></article>'
        f'<article class="metric"><span>社媒提及</span><strong>{stats.get("social_mentions", 0)}</strong><small>公开平台内容</small></article>'
        f'<article class="metric"><span>独立传播事件</span><strong>{stats.get("unique_events", 0)}</strong><small class="trend-note trend-note--{trend_class}">{escape(str(stats.get("trend", {}).get("label") or "暂无趋势"))}</small></article>'
        f'<article class="metric metric--risk"><span>紧急内容</span><strong>{stats.get("urgent_mentions", 0)}</strong><small>需要负责人优先复核</small></article>'
        f'<article class="metric"><span>待回应</span><strong>{stats.get("pending_responses", 0)}</strong><small>待判断或建议回应</small></article>'
        f'<article class="metric"><span>传播突增</span><strong>{stats.get("surge_mentions", 0)}</strong><small>达到互动阈值</small></article>'
        '</section>'
        '<section class="grid"><article class="panel"><div class="panel-head"><div><h2>每日新增与风险走线</h2><p class="panel-description">按系统收录时间统计；排除历史回溯，今日为截至当前的部分数据。</p></div>'
        '<div class="legend"><span><i class="legend-published"></i>内容发布</span><span><i class="legend-total"></i>系统发现</span><span><i class="legend-red"></i>红色</span><span><i class="legend-yellow"></i>黄色</span></div></div>'
        + _trend_chart(stats.get("daily", []))
        + '<details class="daily-details"><summary>查看每日明细</summary><div class="daily-table-wrap"><table class="daily-table"><thead><tr><th>日期</th><th>发布量</th><th>发现量</th><th>红色</th><th>黄色</th><th>负面</th></tr></thead><tbody>'
        + daily_rows
        + '</tbody></table></div></details></article>'
        '<aside class="panel"><div class="panel-head"><div><h2>自动归纳</h2><p class="panel-description">仅依据当前收录、分类与证据状态生成。</p></div></div><ul class="analysis-list">'
        + summary_html
        + '</ul><div class="panel-head recommendation-head"><div><h2>下一步建议</h2><p class="panel-description">建议仍需由 PR、法务与客服负责人确认。</p></div></div><ol class="recommendation-list">'
        + recommendation_html
        + '</ol></aside></section>'
        '<section class="distribution-grid"><article class="panel"><h2>情感结构</h2><p class="panel-description">负面比例用于识别压力，不代表事件真实性。</p>'
        + sentiment_rows
        + '</article><article class="panel"><h2>议题归纳</h2><p class="panel-description">来自分析分类。</p>'
        + category_rows
        + '</article><article class="panel"><h2>来源结构</h2><p class="panel-description">用于识别单一来源集中度。</p>'
        + source_rows
        + '</article></section>'
        '<section class="distribution-grid"><article class="panel"><h2>平台分布</h2><p class="panel-description">按公开内容所在平台归类。</p>'
        + platform_rows
        + '</article><article class="panel"><h2>监测主题</h2><p class="panel-description">召回专项与品牌重大风险独立统计。</p>'
        + campaign_rows
        + '</article><article class="panel"><h2>回应状态</h2><p class="panel-description">逐条处置闭环。</p>'
        + response_rows
        + '</article></section>'
        '<section class="panel keyword-panel"><div class="panel-head"><div><h2>高频关键词</h2><p class="panel-description">来自实际命中的监测词，已排除内部检索占位标记。</p></div></div><div class="keyword-wrap">'
        + keyword_html
        + '</div></section>'
        '<section class="panel"><div class="panel-head"><div><h2>近期重点风险</h2><p class="panel-description">最多显示 6 条，完整记录可通过检索或 CSV 查看。</p></div></div><div class="risk-grid">'
        + _render_top_risks(stats.get("top", []))
        + '</div></section>'
        '<section class="grid"><article class="panel"><div class="panel-head"><div><h2>高传播帖子榜</h2><p class="panel-description">仅展示平台实际返回的互动数据；缺失时不估算。</p></div></div><div class="daily-table-wrap"><table class="daily-table"><thead><tr><th>平台</th><th>内容</th><th>浏览</th><th>赞</th><th>评论</th><th>分享</th></tr></thead><tbody>'
        + _spread_rows(stats.get("top_spread", []))
        + '</tbody></table></div></article><aside class="panel"><div class="panel-head"><div><h2>各平台发现走线</h2><p class="panel-description">右列为最近 7 个自然日的发现量序列。</p></div></div><div class="daily-table-wrap"><table class="daily-table"><thead><tr><th>平台</th><th>窗口总量</th><th>近 7 日</th></tr></thead><tbody>'
        + _platform_trend_rows(stats.get("platform_trends", []))
        + '</tbody></table></div></aside></section>'
        '<section class="panel"><div class="panel-head"><div><h2>跨平台扩散</h2><p class="panel-description">按事件指纹归并，显示同一传播事件出现的平台数量。</p></div></div><div class="daily-table-wrap"><table class="daily-table"><thead><tr><th>事件</th><th>平台数</th><th>平台</th></tr></thead><tbody>'
        + _cross_platform_rows(stats.get("cross_platform_events", []))
        + '</tbody></table></div></section>'
        '<section class="panel"><div class="panel-head"><div><h2>平台覆盖矩阵</h2><p class="panel-description">零记录仅表示当前来源未发现，不代表平台没有讨论；持牌源、公开索引与历史回溯必须分开解释。</p></div></div><div class="daily-table-wrap"><table class="daily-table"><thead><tr><th>平台</th><th>提及</th><th>覆盖等级</th><th>最近发现</th><th>P95 发现延迟（秒）</th></tr></thead><tbody>'
        + _coverage_rows(stats.get("coverage", []))
        + '</tbody></table></div></section>'
        '<section class="panel"><div class="panel-head"><div><h2>采集源健康</h2><p class="panel-description">直接反映调度器的最近成功与连续失败；与平台零记录分开判断。</p></div></div><div class="daily-table-wrap"><table class="daily-table"><thead><tr><th>采集源</th><th>通道</th><th>状态</th><th>最近成功</th><th>最近尝试</th><th>连续失败</th></tr></thead><tbody>'
        + _source_health_rows(stats.get("source_health", []))
        + '</tbody></table></div></section>'
        f'<p class="footer-note">口径：北京时间；按系统收录时间聚合；历史回溯计入总量并单独保留字段；事件指纹用于去重观察，不等同于法律事实认定。新收录 {stats.get("new_mentions", 0)} 条，历史回溯 {stats.get("backfill_mentions", 0)} 条。</p>'
        '</main></body></html>'
    )
