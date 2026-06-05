from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: str
    alert_webhook_url: str
    high_lane_interval_minutes: int
    regular_lane_interval_minutes: int
    p0_health_threshold_minutes: int
    backfill_days: int
    request_timeout_seconds: int


def load_settings() -> Settings:
    return Settings(
        db_path=os.getenv("OCOOPA_DB_PATH", str(Path.cwd() / "ocoopa_monitor.db")),
        alert_webhook_url=os.getenv("OCOOPA_ALERT_WEBHOOK_URL", ""),
        high_lane_interval_minutes=int(os.getenv("OCOOPA_HIGH_LANE_INTERVAL_MINUTES", "15")),
        regular_lane_interval_minutes=int(os.getenv("OCOOPA_REGULAR_LANE_INTERVAL_MINUTES", "60")),
        p0_health_threshold_minutes=int(os.getenv("OCOOPA_P0_HEALTH_THRESHOLD_MINUTES", "120")),
        backfill_days=int(os.getenv("OCOOPA_BACKFILL_DAYS", "60")),
        request_timeout_seconds=int(os.getenv("OCOOPA_REQUEST_TIMEOUT_SECONDS", "20")),
    )

