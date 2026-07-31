from __future__ import annotations

try:
    from fastapi import FastAPI, Header, Response
    from fastapi.responses import HTMLResponse
except ImportError:  # pragma: no cover - optional runtime dependency
    FastAPI = None  # type: ignore

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import load_settings
from .console_web import compute_dashboard, filter_rows, render_dashboard, render_search, rows_to_csv
from .db import create_database
from .keywords import DEFAULT_KEYWORDS
from .models import utcnow
from .pipeline import MonitorPipeline
from .reports import DailyReportService
from .outbox import DeliveryOutboxWorker
from .delivery import DeliveryClient
from .review_web import apply_mark, render_result, render_review_page, token_ok
from .source_health import SourceHealthMonitor
from .sources import DEFAULT_SOURCES


if FastAPI is not None:
    app = FastAPI(title="Ocoopa Public Opinion Monitor")
    settings = load_settings()
    db = create_database(settings)

    @app.on_event("startup")
    def startup() -> None:
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(DEFAULT_SOURCES)

    def _authorized(token: str, authorization: str) -> bool:
        provided = token
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        return token_ok(settings.review_token, provided)

    def _unauthorized(response_class=Response):
        if response_class is HTMLResponse:
            return HTMLResponse("<p>未授权（缺少或错误的 token）。</p>", status_code=401)
        return Response("unauthorized", status_code=401)

    @app.post("/run/high")
    def run_high(token: str = "", authorization: str = Header(default="")):
        if not _authorized(token, authorization):
            return _unauthorized()
        return MonitorPipeline(db, settings).run_lane("high")

    @app.post("/run/regular")
    def run_regular(token: str = "", authorization: str = Header(default="")):
        if not _authorized(token, authorization):
            return _unauthorized()
        return MonitorPipeline(db, settings).run_lane("regular")

    @app.post("/backfill")
    def run_backfill(days: int = 60, token: str = "", authorization: str = Header(default="")):
        if not _authorized(token, authorization):
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
        if not _authorized(token, authorization):
            return _unauthorized()
        return DailyReportService(db).generate(timezone_name)

    @app.get("/health/sources")
    def source_health(token: str = "", authorization: str = Header(default="")):
        if not _authorized(token, authorization):
            return _unauthorized()
        return {"unhealthy_sources": SourceHealthMonitor(db).check()}

    @app.get("/review", response_class=HTMLResponse)
    def review_page(
        token: str = "",
        limit: int = 50,
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        if not _authorized(token, authorization):
            return _unauthorized(HTMLResponse)
        return HTMLResponse(render_review_page(db.list_recent_incidents(min(max(limit, 1), 200)), token))

    @app.post("/review/mark", response_class=HTMLResponse)
    def review_mark(
        incident_id: int,
        status: str,
        days: Optional[int] = None,
        token: str = "",
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        if not _authorized(token, authorization):
            return _unauthorized(HTMLResponse)
        ok, message = apply_mark(db, incident_id, status, days)
        return HTMLResponse(render_result(ok, message, token), status_code=200 if ok else 400)

    def _window(days: int, timezone_name: str = "Asia/Shanghai"):
        end = utcnow()
        tz = ZoneInfo(timezone_name)
        local_start_date = end.astimezone(tz).date() - timedelta(days=max(1, days) - 1)
        local_start = datetime.combine(local_start_date, time.min, tzinfo=tz)
        return local_start.astimezone(timezone.utc), end

    @app.get("/review/analysis", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(
        token: str = "",
        days: int = 30,
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        if not _authorized(token, authorization):
            return _unauthorized(HTMLResponse)
        days = min(max(days, 1), 366)
        start, end = _window(days)
        stats = compute_dashboard(
            db.fetch_mentions_between(start, end),
            db.fetch_alerts_between(start, end),
            window_days=days,
            timezone_name="Asia/Shanghai",
            now=end,
        )
        return HTMLResponse(render_dashboard(stats, days, token))

    @app.get("/console/search", response_class=HTMLResponse)
    def console_search(
        token: str = "",
        q: str = "",
        risk: str = "",
        days: int = 30,
        authorization: str = Header(default=""),
    ) -> HTMLResponse:
        if not _authorized(token, authorization):
            return _unauthorized(HTMLResponse)
        days = min(max(days, 1), 366)
        start, end = _window(days)
        rows = filter_rows(db.fetch_mentions_between(start, end), q, risk)
        return HTMLResponse(render_search(rows, q, risk, days, token))

    @app.get("/console/export.csv")
    def console_export(
        token: str = "",
        days: int = 30,
        authorization: str = Header(default=""),
    ):
        if not _authorized(token, authorization):
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
        if not _authorized(token, authorization):
            return _unauthorized()
        return {"records": db.list_recall_mentions(min(max(limit, 1), 5000))}

    @app.post("/recall/sync")
    def recall_sync(
        token: str = "",
        limit: int = 50,
        authorization: str = Header(default=""),
    ):
        if not _authorized(token, authorization):
            return _unauthorized()
        worker = DeliveryOutboxWorker(db, DeliveryClient.from_settings(settings))
        return worker.drain(min(max(limit, 1), 200))
else:
    app = None
