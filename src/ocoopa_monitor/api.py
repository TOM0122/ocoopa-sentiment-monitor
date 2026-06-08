from __future__ import annotations

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
except ImportError:  # pragma: no cover - optional runtime dependency
    FastAPI = None  # type: ignore

from typing import Optional

from .config import load_settings
from .db import create_database
from .keywords import DEFAULT_KEYWORDS
from .pipeline import MonitorPipeline
from .reports import DailyReportService
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

    @app.post("/run/high")
    def run_high() -> dict:
        return MonitorPipeline(db, settings).run_lane("high")

    @app.post("/run/regular")
    def run_regular() -> dict:
        return MonitorPipeline(db, settings).run_lane("regular")

    @app.post("/backfill")
    def run_backfill(days: int = 60) -> dict:
        return MonitorPipeline(db, settings).run_lane("high", backfill=True, since_days=days)

    @app.post("/reports/daily")
    def daily_report(timezone_name: str = "Asia/Shanghai") -> dict:
        return DailyReportService(db).generate(timezone_name)

    @app.get("/health/sources")
    def source_health() -> dict:
        return {"unhealthy_sources": SourceHealthMonitor(db).check()}

    @app.get("/review", response_class=HTMLResponse)
    def review_page(token: str = "", limit: int = 50) -> HTMLResponse:
        if not token_ok(settings.review_token, token):
            return HTMLResponse("<p>未授权（缺少或错误的 token）。</p>", status_code=401)
        return HTMLResponse(render_review_page(db.list_recent_alerts(limit), token))

    @app.post("/review/mark", response_class=HTMLResponse)
    def review_mark(alert_id: int, status: str, days: Optional[int] = None, token: str = "") -> HTMLResponse:
        if not token_ok(settings.review_token, token):
            return HTMLResponse("<p>未授权（缺少或错误的 token）。</p>", status_code=401)
        ok, message = apply_mark(db, alert_id, status, days)
        return HTMLResponse(render_result(ok, message, token), status_code=200 if ok else 400)
else:
    app = None
