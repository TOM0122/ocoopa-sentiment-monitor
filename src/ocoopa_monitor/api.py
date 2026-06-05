from __future__ import annotations

try:
    from fastapi import FastAPI
except ImportError:  # pragma: no cover - optional runtime dependency
    FastAPI = None  # type: ignore

from .config import load_settings
from .db import Database
from .keywords import DEFAULT_KEYWORDS
from .pipeline import MonitorPipeline
from .reports import DailyReportService
from .source_health import SourceHealthMonitor
from .sources import DEFAULT_SOURCES


if FastAPI is not None:
    app = FastAPI(title="Ocoopa Public Opinion Monitor")
    settings = load_settings()
    db = Database(settings.db_path)

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
else:
    app = None
