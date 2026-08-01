from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: str
    db_url: str
    alert_channel: str
    alert_webhook_url: str
    alert_webhook_secret: str
    alert_at_mobiles: str
    alert_rate_limit_per_minute: int
    alert_cooldown_hours: int
    alert_ack_timeout_minutes: int
    review_token: str
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_base_url: str
    serpapi_api_key: str
    brave_search_api_key: str
    gnews_api_key: str
    high_lane_interval_minutes: int
    regular_lane_interval_minutes: int
    p0_health_threshold_minutes: int
    backfill_days: int
    request_timeout_seconds: int
    licensed_lane_interval_minutes: int = 5
    brandwatch_api_token: str = ""
    brandwatch_project_id: str = ""
    brandwatch_query_id: str = ""
    brandwatch_base_url: str = "https://api.brandwatch.com"
    session_secret: str = ""
    session_ttl_hours: int = 12
    allow_query_token: bool = True


def load_settings() -> Settings:
    return Settings(
        db_path=os.getenv("OCOOPA_DB_PATH", str(Path.cwd() / "ocoopa_monitor.db")),
        db_url=os.getenv("OCOOPA_DB_URL", os.getenv("DATABASE_URL", "")),
        alert_channel=os.getenv("OCOOPA_ALERT_CHANNEL", "generic"),
        alert_webhook_url=os.getenv("OCOOPA_ALERT_WEBHOOK_URL", ""),
        alert_webhook_secret=os.getenv("OCOOPA_ALERT_WEBHOOK_SECRET", ""),
        alert_at_mobiles=os.getenv("OCOOPA_ALERT_AT_MOBILES", ""),
        alert_rate_limit_per_minute=int(os.getenv("OCOOPA_ALERT_RATE_LIMIT_PER_MINUTE", "20")),
        alert_cooldown_hours=int(os.getenv("OCOOPA_ALERT_COOLDOWN_HOURS", "6")),
        alert_ack_timeout_minutes=int(os.getenv("OCOOPA_ALERT_ACK_TIMEOUT_MINUTES", "30")),
        review_token=os.getenv("OCOOPA_REVIEW_TOKEN", ""),
        llm_provider=os.getenv("OCOOPA_LLM_PROVIDER", "rule").lower(),
        llm_model=os.getenv("OCOOPA_LLM_MODEL", "deepseek-v4-flash"),
        llm_api_key=os.getenv("OCOOPA_LLM_API_KEY", ""),
        llm_base_url=os.getenv("OCOOPA_LLM_BASE_URL", "https://api.deepseek.com"),
        serpapi_api_key=os.getenv("OCOOPA_SERPAPI_API_KEY", ""),
        brave_search_api_key=os.getenv("OCOOPA_BRAVE_SEARCH_API_KEY", ""),
        gnews_api_key=os.getenv("OCOOPA_GNEWS_API_KEY", ""),
        high_lane_interval_minutes=int(os.getenv("OCOOPA_HIGH_LANE_INTERVAL_MINUTES", "15")),
        regular_lane_interval_minutes=int(os.getenv("OCOOPA_REGULAR_LANE_INTERVAL_MINUTES", "60")),
        p0_health_threshold_minutes=int(os.getenv("OCOOPA_P0_HEALTH_THRESHOLD_MINUTES", "120")),
        backfill_days=int(os.getenv("OCOOPA_BACKFILL_DAYS", "180")),
        request_timeout_seconds=int(os.getenv("OCOOPA_REQUEST_TIMEOUT_SECONDS", "20")),
        licensed_lane_interval_minutes=int(os.getenv("OCOOPA_LICENSED_LANE_INTERVAL_MINUTES", "5")),
        brandwatch_api_token=os.getenv("OCOOPA_BRANDWATCH_TOKEN", ""),
        brandwatch_project_id=os.getenv("OCOOPA_BRANDWATCH_PROJECT_ID", ""),
        brandwatch_query_id=os.getenv("OCOOPA_BRANDWATCH_QUERY_ID", ""),
        brandwatch_base_url=os.getenv("OCOOPA_BRANDWATCH_BASE_URL", "https://api.brandwatch.com"),
        session_secret=os.getenv("OCOOPA_SESSION_SECRET", ""),
        session_ttl_hours=int(os.getenv("OCOOPA_SESSION_TTL_HOURS", "12")),
        allow_query_token=os.getenv("OCOOPA_ALLOW_QUERY_TOKEN", "true").lower() in {"1", "true", "yes"},
    )
