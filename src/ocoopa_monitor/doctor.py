from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class DoctorReport:
    ok: bool
    errors: List[str]
    warnings: List[str]
    settings_summary: Dict[str, object]


def run_doctor(settings, production: bool = False, role: str = "all") -> DoctorReport:
    if role not in {"all", "scheduler", "web"}:
        raise ValueError(f"unsupported doctor role: {role}")

    errors: List[str] = []
    warnings: List[str] = []
    check_scheduler = role in {"all", "scheduler"}
    check_web = role in {"all", "web"}

    if check_scheduler and settings.backfill_days != 180:
        warnings.append("OCOOPA_BACKFILL_DAYS should remain 180 unless business/legal approves a change.")

    if production and not settings.db_url:
        errors.append("Production must use Railway PostgreSQL via OCOOPA_DB_URL or DATABASE_URL.")
    if check_web and production and not settings.review_token:
        errors.append("Production web service requires OCOOPA_REVIEW_TOKEN.")
    if check_web and production and not settings.session_secret:
        errors.append("Production web service requires independent OCOOPA_SESSION_SECRET.")
    if check_web and production and settings.session_secret and settings.session_secret == settings.review_token:
        errors.append("OCOOPA_SESSION_SECRET must be independent from OCOOPA_REVIEW_TOKEN.")
    if check_web and production and settings.allow_query_token:
        warnings.append(
            "Legacy query-token migration is enabled; set OCOOPA_ALLOW_QUERY_TOKEN=false "
            "after existing browser sessions have migrated."
        )

    if check_scheduler and settings.llm_provider == "deepseek":
        if not settings.llm_api_key:
            errors.append("OCOOPA_LLM_API_KEY is required when OCOOPA_LLM_PROVIDER=deepseek.")
        if not settings.llm_base_url.startswith("https://"):
            warnings.append("OCOOPA_LLM_BASE_URL should use HTTPS in production.")
        if not settings.llm_model:
            errors.append("OCOOPA_LLM_MODEL is required when OCOOPA_LLM_PROVIDER=deepseek.")
    elif check_scheduler and production:
        errors.append("Production must use OCOOPA_LLM_PROVIDER=deepseek per legal decision.")

    if check_scheduler and settings.alert_channel == "dingtalk":
        if not settings.alert_webhook_url:
            errors.append("OCOOPA_ALERT_WEBHOOK_URL is required when OCOOPA_ALERT_CHANNEL=dingtalk.")
        if not settings.alert_webhook_secret:
            warnings.append("OCOOPA_ALERT_WEBHOOK_SECRET is recommended for signed DingTalk robots.")
        if settings.alert_rate_limit_per_minute > 20:
            warnings.append("DingTalk custom robot rate limit should not exceed 20 messages per minute.")
    elif check_scheduler and production:
        errors.append("Production must use OCOOPA_ALERT_CHANNEL=dingtalk per business decision.")

    if check_scheduler and not settings.serpapi_api_key and not settings.brave_search_api_key:
        warnings.append(
            "No commercial search API key configured. Set OCOOPA_BRAVE_SEARCH_API_KEY "
            "or OCOOPA_SERPAPI_API_KEY."
        )
    if check_scheduler and not settings.gnews_api_key:
        warnings.append("OCOOPA_GNEWS_API_KEY is missing; commercial news recall will be reduced.")
    brandwatch_fields = (
        settings.brandwatch_api_token,
        settings.brandwatch_project_id,
        settings.brandwatch_query_id,
    )
    if check_scheduler and any(brandwatch_fields) and not all(brandwatch_fields):
        errors.append(
            "Brandwatch requires OCOOPA_BRANDWATCH_TOKEN, OCOOPA_BRANDWATCH_PROJECT_ID, "
            "and OCOOPA_BRANDWATCH_QUERY_ID together."
        )
    elif check_scheduler and not all(brandwatch_fields):
        warnings.append("Brandwatch licensed social monitoring is disabled until pilot credentials are set.")

    summary = {
        "role": role,
        "db_backend": "postgres" if settings.db_url else "sqlite",
        "db_path": settings.db_path,
        "db_url_configured": bool(settings.db_url),
        "alert_channel": settings.alert_channel,
        "dingtalk_webhook_configured": bool(settings.alert_webhook_url),
        "dingtalk_secret_configured": bool(settings.alert_webhook_secret),
        "alert_at_mobiles_count": len([m for m in settings.alert_at_mobiles.split(",") if m.strip()]),
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "llm_api_key_configured": bool(settings.llm_api_key),
        "serpapi_configured": bool(settings.serpapi_api_key),
        "brave_search_configured": bool(settings.brave_search_api_key),
        "gnews_configured": bool(settings.gnews_api_key),
        "brandwatch_configured": all(brandwatch_fields),
        "session_secret_configured": bool(settings.session_secret),
        "query_token_migration_enabled": bool(settings.allow_query_token),
        "high_lane_interval_minutes": settings.high_lane_interval_minutes,
        "regular_lane_interval_minutes": settings.regular_lane_interval_minutes,
        "licensed_lane_interval_minutes": settings.licensed_lane_interval_minutes,
        "backfill_days": settings.backfill_days,
    }
    return DoctorReport(ok=not errors, errors=errors, warnings=warnings, settings_summary=summary)
