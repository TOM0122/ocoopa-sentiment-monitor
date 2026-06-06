from __future__ import annotations

from typing import List

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
        priority="P0",
        lane="high",
        method="api",
        url="http://www.saferproducts.gov/RestWebServices/Recall?format=json",
        alert_threshold_minutes=120,
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
        source_name="prnewswire_rss",
        source_type="news",
        priority="P1",
        lane="regular",
        method="generic_rss",
        url="https://www.prnewswire.com/rss/news-releases-list.rss",
        alert_threshold_minutes=360,
    ),
]
