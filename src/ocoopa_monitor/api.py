from __future__ import annotations

try:
    from fastapi import FastAPI, Header, Request, Response
    from fastapi.responses import HTMLResponse, RedirectResponse
except ImportError:  # pragma: no cover - optional runtime dependency
    FastAPI = None  # type: ignore

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

from .config import load_settings
from .console_web import compute_dashboard, filter_rows, render_dashboard, render_search, rows_to_csv
from .analysis_details import build_detail_view, normalize_detail_metric, render_analysis_details
from .db import create_database
from .keywords import DEFAULT_KEYWORDS
from .models import utcnow
from .pipeline import MonitorPipeline
from .reports import DailyReportService
from .outbox import DeliveryOutboxWorker
from .delivery import DeliveryClient
from .review_web import apply_mark, render_result, render_review_page, token_ok
from .session_auth import COOKIE_NAME, CSRF_COOKIE_NAME, issue_session, new_csrf_token, verify_csrf, verify_session
from .source_health import SourceHealthMonitor
from .sources import sources_for_settings
from .topics import enrich_topic_fields
from .interventions import intervention_status, requires_human_intervention
from .operational import is_excluded_false_positive


if FastAPI is not None:
    app = FastAPI(title="Ocoopa Public Opinion Monitor")
    settings = load_settings()
    db = create_database(settings)

    @app.on_event("startup")
    def startup() -> None:
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(sources_for_settings(settings))

    def _api_authorized(token: str, authorization: str) -> bool:
        # Query-string credentials are accepted only by the two page routes
        # during migration. Automation APIs require an Authorization header.
        provided = ""
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        return token_ok(settings.review_token, provided)

    def _page_authorized(request: Request) -> bool:
        if not settings.review_token:
            return True
        return verify_session(request.cookies.get(COOKIE_NAME, ""), settings.session_secret)

    def _csrf_authorized(request: Request, provided: str) -> bool:
        if not settings.review_token:
            return True
        return verify_csrf(request.cookies.get(CSRF_COOKIE_NAME, ""), provided)

    def _session_redirect(target: str = "/review"):
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            issue_session(settings.session_secret, settings.session_ttl_hours),
            max_age=settings.session_ttl_hours * 3600,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE_NAME,
            new_csrf_token(),
            max_age=settings.session_ttl_hours * 3600,
            httponly=False,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    def _unauthorized(response_class=Response):
        if response_class is HTMLResponse:
            return HTMLResponse('<p>会话已失效，请<a href="/auth/login">重新登录</a>。</p>', status_code=401)
        return Response("unauthorized", status_code=401)

    @app.get("/auth/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        if not settings.review_token:
            return HTMLResponse('<p>开发环境未配置访问口令。<a href="/review">进入工作台</a></p>')
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>OCOOPA 舆情工作台登录</title><style>body{margin:0;background:#f4f7fb;color:#14213a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.card{max-width:420px;margin:12vh auto;padding:28px;border:1px solid #d9e1eb;border-radius:14px;background:#fff}label,input,button{display:block;width:100%;box-sizing:border-box}label{font-weight:700}input{margin:8px 0 16px;padding:11px;border:1px solid #aab8c9;border-radius:8px}button{padding:11px;border:0;border-radius:8px;background:#075985;color:#fff;font-weight:700}</style></head>'
            '<body><main class="card"><h1>OCOOPA 舆情工作台</h1><p>请输入访问口令。登录后地址栏不再携带长期 token。</p>'
            '<form method="post" action="/auth/login"><label for="code">访问口令</label><input id="code" name="access_code" type="password" autocomplete="current-password" required><button type="submit">登录</button></form></main></body></html>'
        )

    @app.post("/auth/login")
    async def login(request: Request):
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        access_code = form.get("access_code", [""])[0]
        if not token_ok(settings.review_token, access_code):
            return HTMLResponse('<p>访问口令错误。<a href="/auth/login">返回</a></p>', status_code=401)
        if not settings.session_secret:
            return HTMLResponse("<p>服务端尚未配置 OCOOPA_SESSION_SECRET，已拒绝签发不安全会话。</p>", status_code=503)
        return _session_redirect("/review")

    @app.post("/auth/logout")
    async def logout(request: Request):
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        if not _page_authorized(request) or not _csrf_authorized(request, form.get("csrf", [""])[0]):
            return _unauthorized(HTMLResponse)
        response = RedirectResponse("/auth/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        response.delete_cookie(CSRF_COOKIE_NAME, path="/")
        return response

    @app.post("/run/high")
    def run_high(token: str = "", authorization: str = Header(default="")):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        return MonitorPipeline(db, settings).run_lane("high")

    @app.post("/run/regular")
    def run_regular(token: str = "", authorization: str = Header(default="")):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        return MonitorPipeline(db, settings).run_lane("regular")

    @app.post("/run/licensed")
    def run_licensed(token: str = "", authorization: str = Header(default="")):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        return MonitorPipeline(db, settings).run_lane("licensed")

    @app.post("/backfill")
    def run_backfill(days: int = 60, token: str = "", authorization: str = Header(default="")):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        return MonitorPipeline(db, settings).run_lane(
            "high", backfill=True, since_days=min(max(days, 1), 3650)
        )

    @app.post("/reports/daily")
    def daily_report(
        timezone_name: str = "Asia/Shanghai",
        token: str = "",
        authorization: str = Header(default=""),
    ):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        return DailyReportService(db).generate(timezone_name)

    @app.get("/health/sources")
    def source_health(token: str = "", authorization: str = Header(default="")):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        return {"unhealthy_sources": SourceHealthMonitor(db).check()}

    @app.get("/review", response_class=HTMLResponse)
    def review_page(
        request: Request,
        token: str = "",
        limit: int = 50,
        platform: str = "",
        campaign: str = "",
        risk: str = "",
        response_status: str = "",
        intervention: str = "",
        quality: str = "",
        view: str = "queue",
        page: int = 1,
        days: int = 30,
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        if token and settings.allow_query_token and token_ok(settings.review_token, token):
            return _session_redirect(f"/review?limit={min(max(limit, 1), 200)}&days={min(max(days, 1), 366)}")
        if not _page_authorized(request):
            return _unauthorized(HTMLResponse)
        view = view if view in {"queue", "library"} else "queue"
        default_quality = "all" if view == "library" else "operational"
        quality = quality if quality in {"operational", "excluded", "all"} else default_quality
        page = max(page, 1)
        incidents = db.list_recent_incidents_detailed(None, scope=view)
        start_at, _ = _window(min(max(days, 1), 366))
        incidents = [
            incident for incident in incidents
            if (not risk or incident.get("risk_level_max") == risk)
            and (
                not incident.get("last_seen_at")
                or (datetime.fromisoformat(str(incident["last_seen_at"]).replace("Z", "+00:00")).astimezone(timezone.utc) >= start_at)
            )
        ]
        if platform or campaign or response_status or intervention or quality != "all":
            for incident in incidents:
                incident["mentions"] = [
                    row for row in incident.get("mentions", [])
                    if (not platform or row.get("platform") == platform)
                    and (not campaign or row.get("campaign") == campaign)
                    and (not response_status or (row.get("response_status") or "待判断") == response_status)
                    and (quality == "all" or (quality == "excluded") == is_excluded_false_positive(row))
                    and (
                        not intervention
                        or (intervention == "human" and requires_human_intervention(row))
                        or intervention_status(row) == intervention
                    )
                ]
            incidents = [incident for incident in incidents if incident.get("mentions")]
        total_incidents = len(incidents)
        page_size = min(max(limit, 10), 100)
        start_index = (page - 1) * page_size
        incidents = incidents[start_index : start_index + page_size]
        return HTMLResponse(
            render_review_page(
                incidents,
                "",
                csrf_token=request.cookies.get(CSRF_COOKIE_NAME, ""),
                filters={
                    "platform": platform, "campaign": campaign, "risk": risk,
                    "response_status": response_status, "intervention": intervention,
                    "quality": quality, "view": view, "days": str(days),
                    "page": str(page), "page_size": str(page_size),
                    "total_incidents": str(total_incidents),
                },
            )
        )

    @app.post("/review/mark", response_class=HTMLResponse)
    async def review_mark(request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        if not _page_authorized(request) or not _csrf_authorized(request, form.get("csrf", [""])[0]):
            return _unauthorized(HTMLResponse)
        try:
            incident_id = int(form.get("incident_id", ["0"])[0])
            status = form.get("status", [""])[0]
            raw_days = form.get("days", [""])[0]
            days = int(raw_days) if raw_days else None
        except (TypeError, ValueError):
            return HTMLResponse(render_result(False, "复核参数无效"), status_code=400)
        ok, message = apply_mark(db, incident_id, status, days)
        return HTMLResponse(render_result(ok, message, ""), status_code=200 if ok else 400)

    @app.post("/review/mention-action", response_class=HTMLResponse)
    async def review_mention_action(request: Request) -> HTMLResponse:
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        if not _page_authorized(request) or not _csrf_authorized(request, form.get("csrf", [""])[0]):
            return _unauthorized(HTMLResponse)
        try:
            mention_id = int(form.get("mention_id", ["0"])[0])
            ok = db.update_mention_intervention(
                mention_id,
                form.get("intervention_status", ["待分流"])[0],
                form.get("response_status", ["待判断"])[0],
                form.get("response_url", [""])[0],
                form.get("internal_note", [""])[0],
                form.get("operator", [""])[0],
            )
        except (TypeError, ValueError) as exc:
            return HTMLResponse(render_result(False, str(exc)), status_code=400)
        return HTMLResponse(render_result(ok, "逐条处置记录已更新" if ok else "未找到该舆情记录"), status_code=200 if ok else 404)

    def _window(days: int, timezone_name: str = "Asia/Shanghai"):
        end = utcnow()
        tz = ZoneInfo(timezone_name)
        local_start_date = end.astimezone(tz).date() - timedelta(days=max(1, days) - 1)
        local_start = datetime.combine(local_start_date, time.min, tzinfo=tz)
        return local_start.astimezone(timezone.utc), end

    @app.get("/review/analysis", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        token: str = "",
        days: int = 30,
        platform: str = "",
        campaign: str = "",
        topic: str = "",
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        if token and settings.allow_query_token and token_ok(settings.review_token, token):
            return _session_redirect(f"/review/analysis?days={min(max(days, 1), 366)}")
        if not _page_authorized(request):
            return _unauthorized(HTMLResponse)
        days = min(max(days, 1), 366)
        start, end = _window(days)
        rows = [
            dict(row) for row in db.fetch_mentions_between(start, end)
            if (not platform or row.get("platform") == platform)
            and (not campaign or row.get("campaign") == campaign)
            and (not topic or enrich_topic_fields(dict(row)).get("topic_primary") == topic)
        ]
        stats = compute_dashboard(
            rows,
            db.fetch_alerts_between(start, end),
            window_days=days,
            timezone_name="Asia/Shanghai",
            now=end,
            source_health=db.list_source_health(),
        )
        stats["filters"] = {"platform": platform, "campaign": campaign, "topic": topic}
        return HTMLResponse(
            render_dashboard(stats, days, "", request.cookies.get(CSRF_COOKIE_NAME, ""))
        )

    @app.get("/console/search", response_class=HTMLResponse)
    def console_search(
        request: Request,
        token: str = "",
        q: str = "",
        risk: str = "",
        days: int = 30,
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        if not _page_authorized(request):
            return _unauthorized(HTMLResponse)
        days = min(max(days, 1), 366)
        start, end = _window(days)
        rows = filter_rows(db.fetch_mentions_between(start, end), q, risk)
        return HTMLResponse(render_search(rows, q, risk, days, ""))

    @app.get("/review/analysis/details", response_class=HTMLResponse)
    def analysis_details(
        request: Request,
        token: str = "",
        metric: str = "links",
        days: int = 30,
        platform: str = "",
        campaign: str = "",
        topic: str = "",
        page: int = 1,
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        metric = normalize_detail_metric(metric)
        days = min(max(days, 1), 366)
        page = max(page, 1)
        if token and settings.allow_query_token and token_ok(settings.review_token, token):
            target = "/review/analysis/details?" + urlencode(
                {
                    key: value for key, value in {
                        "metric": metric,
                        "days": days,
                        "platform": platform,
                        "campaign": campaign,
                        "topic": topic,
                        "page": page,
                    }.items() if value not in (None, "")
                }
            )
            return _session_redirect(target)
        if not _page_authorized(request):
            return _unauthorized(HTMLResponse)
        start, end = _window(days)
        rows = [
            dict(row) for row in db.fetch_mentions_between(start, end)
            if (not platform or row.get("platform") == platform)
            and (not campaign or row.get("campaign") == campaign)
            and (not topic or enrich_topic_fields(dict(row)).get("topic_primary") == topic)
            and not is_excluded_false_positive(dict(row))
        ]
        view = build_detail_view(rows, metric=metric, page=page)
        return HTMLResponse(
            render_analysis_details(
                view,
                days,
                filters={"platform": platform, "campaign": campaign, "topic": topic},
                csrf_token=request.cookies.get(CSRF_COOKIE_NAME, ""),
            )
        )

    @app.get("/console/export.csv")
    def console_export(
        request: Request,
        token: str = "",
        days: int = 30,
        authorization: str = Header(default=""),
    ):
        if not (_page_authorized(request) or _api_authorized(token, authorization)):
            return _unauthorized()
        days = min(max(days, 1), 366)
        start, end = _window(days)
        csv_text = rows_to_csv(db.fetch_mentions_between(start, end))
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=ocoopa_mentions.csv"},
        )

    @app.get("/recall/mentions")
    def recall_mentions(
        token: str = "",
        limit: int = 500,
        authorization: str = Header(default=""),
    ):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        return {"records": db.list_recall_mentions(min(max(limit, 1), 5000))}

    @app.post("/recall/sync")
    def recall_sync(
        token: str = "",
        limit: int = 50,
        authorization: str = Header(default=""),
    ):
        if not _api_authorized(token, authorization):
            return _unauthorized()
        worker = DeliveryOutboxWorker(db, DeliveryClient.from_settings(settings))
        return worker.drain(min(max(limit, 1), 200))
else:
    app = None
