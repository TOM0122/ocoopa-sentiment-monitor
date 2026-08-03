from __future__ import annotations

from dataclasses import replace
from typing import List
from urllib.parse import quote_plus

from .models import SourceConfig


# Quota strategy (R5):
# High-sensitivity lane runs every 15 min -> ~96 calls/day per source.
# Commercial search/news APIs (Brave 30/day, GNews 100/day free tiers) cannot
# sustain that cadence, so the HIGH lane uses only free, unmetered sources
# (Google News RSS + CPSC). Commercial APIs run on the REGULAR lane (hourly,
# ~24 calls/day) to stay inside free quotas while still adding breadth.
# SerpAPI's free tier is ~100/month (unusable for monitoring) and is omitted
# until a paid key is provisioned.
DEFAULT_SOURCES: List[SourceConfig] = [
    # --- HIGH lane (15 min): free, unmetered only -> carries timeliness ---
    SourceConfig(
        source_name="google_news_high_rss",
        source_type="news",
        priority="P0",
        lane="high",
        method="rss",
        url="google-news://high",
        alert_threshold_minutes=120,
    ),
    SourceConfig(
        source_name="cpsc_recalls",
        source_type="cpsc",
        # The legacy retrieval API can lag newly published CPSC.gov pages.
        # Keep it as supplementary P1; the exact-query Google News P0 source
        # independently captures the official CPSC publication in real time.
        priority="P1",
        lane="high",
        method="api",
        url="https://www.saferproducts.gov/RestWebServices/Recall?format=json",
        alert_threshold_minutes=120,
    ),
    # Legal lead-gen / class-action aggregator (public RSS, robots-allowed,
    # Crawl-delay 10s). Earliest signal of class-action recruitment. Keyword
    # filtering keeps only Ocoopa / hand-warmer items, so volume stays low.
    SourceConfig(
        source_name="aboutlawsuits_rss",
        source_type="legal",
        priority="P0",
        lane="high",
        method="generic_rss",
        url="https://www.aboutlawsuits.com/feed/",
        alert_threshold_minutes=180,
    ),
    SourceConfig(
        source_name="reddit_recall_atom",
        source_type="social",
        priority="P1",
        lane="high",
        method="generic_rss",
        url=(
            "https://www.reddit.com/search.rss?q="
            + quote_plus('"OCOOPA" (recall OR fire OR burn OR overheat OR UT3053 OR UT3056 OR ZLS-118 OR H01)')
            + "&sort=new&t=month"
        ),
        alert_threshold_minutes=240,
    ),
    # --- REGULAR lane (hourly): commercial APIs throttled into free quotas ---
    SourceConfig(
        source_name="brave_regular_search",
        source_type="search",
        priority="P1",
        lane="regular",
        method="brave_search",
        url="https://api.search.brave.com/res/v1/web/search",
        alert_threshold_minutes=360,
    ),
    # A precise complementary query for the public Facebook accounts of news
    # outlets that syndicate the recall.  It is throttled by the pipeline to
    # once per four hours, keeping the combined Brave use at ~30 calls/day.
    SourceConfig(
        source_name="brave_media_outlet_social",
        source_type="social",
        priority="P1",
        lane="regular",
        method="brave_search",
        url="https://api.search.brave.com/res/v1/web/search",
        alert_threshold_minutes=360,
    ),
    SourceConfig(
        source_name="gnews_regular_news",
        source_type="news",
        priority="P1",
        lane="regular",
        method="gnews",
        url="https://gnews.io/api/v4/search",
        alert_threshold_minutes=360,
    ),
    SourceConfig(
        source_name="google_news_regular_rss",
        source_type="news",
        priority="P1",
        lane="regular",
        method="rss",
        url="google-news://regular",
        alert_threshold_minutes=360,
    ),
    SourceConfig(
        source_name="brandwatch_mentions",
        source_type="social",
        priority="P1",
        lane="licensed",
        method="brandwatch",
        url="brandwatch://mentions",
        active=False,
        alert_threshold_minutes=30,
    ),
]


def sources_for_settings(settings) -> List[SourceConfig]:
    """Enable the licensed connector only when every read-only credential exists."""
    configured = bool(
        getattr(settings, "brandwatch_api_token", "")
        and getattr(settings, "brandwatch_project_id", "")
        and getattr(settings, "brandwatch_query_id", "")
    )
    return [
        replace(source, active=configured) if source.method == "brandwatch" else source
        for source in DEFAULT_SOURCES
    ]
