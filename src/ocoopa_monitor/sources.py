from __future__ import annotations

from typing import List

from .models import SourceConfig


DEFAULT_SOURCES: List[SourceConfig] = [
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
    SourceConfig(
        source_name="google_news_regular_rss",
        source_type="news",
        priority="P1",
        lane="regular",
        method="rss",
        url="google-news://regular",
        alert_threshold_minutes=360,
    ),
]
