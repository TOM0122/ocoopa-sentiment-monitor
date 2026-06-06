from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class DoctorReport:
    ok: bool
    errors: List[str]
    warnings: List[str]
    settings_summary: Dict[str, object]


def run_doctor(settings, production: bool = False) -> DoctorReport:
    errors: List[str] = []
    warnings: List[str] = []

    if settings.backfill_days != 180:
        warnings.append("OCOOPA_BACKFILL_DAYS should remain 180 unless business/legal approves a change.")

    if settings.llm_provider == "deepseek":
        if not settings.llm_api_key:
            errors.append("OCOOPA_LLM_API_KEY is required when OCOOPA_LLM_PROVIDER=deepseek.")
        if not settings.llm_base_url.startswith("https://"):
            warnings.append("OCOOPA_LLM_BASE_URL should use HTTPS in production.")
        if not settings.llm_model:
            errors.append("OCOOPA_LLM_MODEL is required when OCOOPA_LLM_PROVIDER=deepseek.")
    elif production:
        errors.append("Production must use OCOOPA_LLM_PROVIDER=deepseek per legal decision.")

    if settings.alert_channel == "dingtalk":
        if not settings.alert_webhook_url:
            errors.append("OCOOPA_ALERT_WEBHOOK_URL is required when OCOOPA_ALERT_CHANNEL=dingtalk.")
        if not settings.alert_webhook_secret:
            warnings.append("OCOOPA_ALERT_WEBHOOK_SECRET is recommended for signed DingTalk robots.")
        if settings.alert_rate_limit_per_minute > 20:
            warnings.append("DingTalk custom robot rate limit should not exceed 20 messages per minute.")
    elif production:
        errors.append("Production must use OCOOPA_ALERT_CHANNEL=dingtalk per business decision.")

    if not settings.serpapi_api_key and not settings.brave_search_api_key:
        warnings.append(
            "No commercial search API key configured. Set OCOOPA_BRAVE_SEARCH_API_KEY "
            "or OCOOPA_SERPAPI_API_KEY."
        )
    if not settings.gnews_api_key:
        warnings.append("OCOOPA_GNEWS_API_KEY is missing; commercial news recall will be reduced.")

    summary = {
        "db_path": settings.db_path,
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
        "high_lane_interval_minutes": settings.high_lane_interval_minutes,
        "regular_lane_interval_minutes": settings.regular_lane_interval_minutes,
        "backfill_days": settings.backfill_days,
    }
    return DoctorReport(ok=not errors, errors=errors, warnings=warnings, settings_summary=summary)
